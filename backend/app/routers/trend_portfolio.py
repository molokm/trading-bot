"""Read-only HTTP surface for the daily trend portfolio (paper only).

There is no endpoint here that can place, amend or cancel an order.  The only
side effect of ``/api/trend/cycle`` is that the in-process paper book advances
to the newest closed daily bar and its state file is rewritten.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.bots.trend_portfolio.config import TrendPortfolioConfig
from app.bots.trend_portfolio.engine import Bar, TrendPortfolio, backtest
from app.bots.trend_portfolio.runner import fetch_daily_bars, get_runner
from app.services.route_policy import ADMIN, policy

log = logging.getLogger("trend_portfolio")

router = APIRouter(tags=["trend-portfolio"])


class BacktestRequest(BaseModel):
    risk_per_sleeve: float | None = None
    entry_lookback: int | None = None
    exit_lookback: int | None = None
    chandelier_mult: float | None = None
    sizing_halflife_days: float | None = None
    regime_symbol: str | None = None
    regime_ma: int | None = None
    coins: list[str] | None = None
    bars: int = 300


@router.get("/api/trend/status")
@policy(ADMIN)
async def trend_status():
    runner = get_runner()
    if runner is None:
        return {"enabled": False, "reason": "config rejected"}
    return runner.status()


@router.get("/api/trend/positions")
@policy(ADMIN)
async def trend_positions():
    runner = get_runner()
    if runner is None:
        return {"enabled": False, "positions": []}
    return {"enabled": runner.cfg.enabled, "positions": runner.positions()}


@router.get("/api/trend/trades")
@policy(ADMIN)
async def trend_trades(limit: int = 50):
    """Closed paper trades, newest first. Read-only."""
    runner = get_runner()
    if runner is None:
        return {"enabled": False, "trades": []}
    return {"enabled": runner.cfg.enabled, "trades": runner.trades(limit)}


@router.post("/api/trend/cycle")
@policy(ADMIN)
async def trend_cycle():
    """Advance the paper book to the newest closed daily bar."""
    runner = get_runner()
    if runner is None:
        return {"ok": False, "reason": "config rejected"}
    return {"ok": True, **await runner.cycle()}


@router.post("/api/trend/backtest")
@policy(ADMIN)
async def trend_backtest(req: BacktestRequest):
    """Replay the engine over recent daily bars. Read-only, no orders."""
    cfg = TrendPortfolioConfig.from_env()
    if req.risk_per_sleeve is not None:
        cfg.risk_per_sleeve = req.risk_per_sleeve
    if req.entry_lookback is not None:
        cfg.entry_lookback = req.entry_lookback
    if req.exit_lookback is not None:
        cfg.exit_lookback = req.exit_lookback
    if req.chandelier_mult is not None:
        cfg.chandelier_mult = req.chandelier_mult
    if req.sizing_halflife_days is not None:
        cfg.sizing_halflife_days = req.sizing_halflife_days
    if req.regime_symbol is not None:
        cfg.regime_symbol = req.regime_symbol.strip().upper()
    if req.regime_ma is not None:
        cfg.regime_ma = req.regime_ma
    if req.coins:
        cfg.universe = tuple(c.upper() for c in req.coins)
    try:
        cfg.validate()
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    series = {}
    errors = {}
    for coin in cfg.universe:
        try:
            bars = await fetch_daily_bars(f"{coin}-USDT", limit=req.bars)
            if bars:
                series[coin] = bars
        except Exception as exc:
            errors[coin] = type(exc).__name__
    if not series:
        return {"ok": False, "reason": "no market data", "errors": errors}

    res = backtest(series, cfg)
    return {
        "ok": True,
        "coins_used": len(series),
        "errors": errors,
        "stats": res["stats"],
        "book": res["book"],
        "trades": res["trades"],
        "curve": res["curve"][-180:],
    }
