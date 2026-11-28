"""Read-only HTTP surface for the core portfolio (paper only).

No endpoint here can place, amend or cancel an order.  The only side effect of
``/api/core/cycle`` is that the in-process paper books advance to the newest
closed bars and their state file is rewritten.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from fastapi import APIRouter
from pydantic import BaseModel

from app.bots.core_portfolio.config import CorePortfolioConfig
from app.bots.core_portfolio.runner import (
    backtest_core,
    fetch_bars,
    get_runner,
)
from app.bots.trend_portfolio.engine import Bar
from app.services.route_policy import ADMIN, policy

log = logging.getLogger("core_portfolio")

router = APIRouter(tags=["core-portfolio"])


class CoreBacktestRequest(BaseModel):
    bars_6h: int = 600
    bars_daily: int = 300
    daily_weight: float | None = None
    coins: List[str] | None = None


@router.get("/api/core/status")
@policy(ADMIN)
async def core_status():
    runner = get_runner()
    if runner is None:
        return {"enabled": False, "reason": "config rejected"}
    return runner.status()


@router.get("/api/core/positions")
@policy(ADMIN)
async def core_positions():
    runner = get_runner()
    if runner is None:
        return {"enabled": False, "positions": []}
    return {"enabled": runner.cfg.enabled, "positions": runner.positions()}


@router.get("/api/core/trades")
@policy(ADMIN)
async def core_trades(limit: int = 50):
    """Closed paper trades from both blocks, newest first. Read-only."""
    runner = get_runner()
    if runner is None:
        return {"enabled": False, "trades": []}
    return {"enabled": runner.cfg.enabled, "trades": runner.trades(limit)}


@router.post("/api/core/cycle")
@policy(ADMIN)
async def core_cycle():
    """Advance both paper books to the newest closed bars."""
    runner = get_runner()
    if runner is None:
        return {"ok": False, "reason": "config rejected"}
    return {"ok": True, **await runner.cycle()}


async def _fetch_series(
    cfg: CorePortfolioConfig, interval: str, limit: int, coins: List[str]
) -> tuple[Dict[str, List[Bar]], Dict[str, str], str]:
    """Fetch a block's series from one venue, falling back in place."""
    errors: Dict[str, str] = {}
    order = [cfg.source] + [v for v in ("binance_vision", "binance", "okx") if v != cfg.source]
    for venue in order:
        series: Dict[str, List[Bar]] = {}
        errs: Dict[str, str] = {}
        for coin in coins:
            try:
                bars = await fetch_bars(coin, interval, limit, venue)
            except Exception as exc:
                errs[coin] = f"{venue} {type(exc).__name__}"
                continue
            if bars:
                series[coin] = bars
        if series:
            return series, errs, venue
        errors = errs
    return {}, errors, ""


@router.post("/api/core/backtest")
@policy(ADMIN)
async def core_backtest(req: CoreBacktestRequest):
    """Replay both books over recent bars and blend them. No orders."""
    cfg = CorePortfolioConfig.from_env()
    if req.daily_weight is not None:
        cfg.daily_weight = req.daily_weight
    if req.coins:
        cfg.universe = tuple(c.strip().upper() for c in req.coins if c.strip())
    try:
        cfg.validate()
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    bars6 = min(max(int(req.bars_6h or 600), 120), 1_000)
    bars1 = min(max(int(req.bars_daily or 300), 210), 1_000)
    series6, errors6, venue6 = await _fetch_series(cfg, "6h", bars6, list(cfg.universe))
    series1, errors1, venue1 = await _fetch_series(cfg, "1d", bars1, list(cfg.universe))
    if not series6 or not series1:
        return {"ok": False, "reason": "no market data",
                "errors": {**errors6, **errors1}}
    if cfg.regime_symbol not in series6:
        return {"ok": False, "reason": f"missing {cfg.regime_symbol} 6h bars for the gate"}

    res = backtest_core(series1, series6, cfg)
    res.update({
        "coins_6h": len(series6),
        "coins_daily": len(series1),
        "venue_6h": venue6,
        "venue_daily": venue1,
        "errors": {**errors6, **errors1},
    })
    return res
