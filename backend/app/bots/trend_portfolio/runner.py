"""Paper runner for the daily trend portfolio.

SAFETY CONTRACT (do not weaken)
-------------------------------
* The runner is **read-only** against the exchange: it only calls public
  market-data endpoints.  There is no import of any order-placing method and no
  client credential is read.
* ``cfg.paper_only`` must be True (validated by the engine) and
  ``TrendPortfolioConfig.enabled`` must be True before a single cycle runs.
* Every failure is swallowed into the returned status dict — the runner must
  never take the API process down.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

import httpx

from app.bots.base import load_state, save_state
from app.bots.trend_portfolio.config import TrendPortfolioConfig
from app.bots.trend_portfolio.engine import Bar, TrendPortfolio, feed_chronological

log = logging.getLogger("trend_portfolio")

OKX_BASE = "https://www.okx.com"


async def fetch_daily_bars(inst_id: str, limit: int = 300, timeout: float = 20.0) -> List[Bar]:
    """Public daily candles, ascending, with the still-forming bar dropped."""
    url = f"{OKX_BASE}/api/v5/market/candles"
    params = {"instId": inst_id, "bar": "1D", "limit": str(min(max(limit, 1), 300))}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params=params, headers={"User-Agent": "copix-trend-paper"})
        resp.raise_for_status()
        payload = resp.json()
    rows = payload.get("data") or []
    bars: List[Bar] = []
    for r in rows:
        try:
            bars.append(
                Bar(
                    ts=int(r[0]),
                    open=float(r[1]),
                    high=float(r[2]),
                    low=float(r[3]),
                    close=float(r[4]),
                )
            )
        except (TypeError, ValueError, IndexError):
            continue
    bars.sort(key=lambda b: b.ts)
    if bars:
        bars.pop()  # last candle is the bar still forming
    return bars


class TrendPaperRunner:
    """Steps the portfolio once per cycle over newly closed daily bars."""

    def __init__(self, cfg: Optional[TrendPortfolioConfig] = None):
        self.cfg = cfg or TrendPortfolioConfig.from_env()
        state = load_state(self.cfg.state_name, {}) or {}
        self.book = TrendPortfolio.from_state(self.cfg, state)
        self.last_cycle_ts: float = 0.0
        self.last_error: str = ""
        self.cycles: int = 0
        self.bars_consumed: int = 0
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ── lifecycle ────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        """Start the background loop. Returns False when disabled."""
        if not self.cfg.enabled:
            return False
        if self._running:
            return True
        self._running = True
        self._task = asyncio.create_task(self._loop())
        return True

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never kill the app
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("trend_portfolio cycle failed: %s", self.last_error)
            await asyncio.sleep(max(60, self.cfg.poll_interval_sec))

    # ── one cycle ────────────────────────────────────────────────────────
    async def cycle(self) -> dict:
        """Fetch fresh bars for the universe and step the book."""
        self.cycles += 1
        self.last_cycle_ts = time.time()
        errors: Dict[str, str] = {}
        series: Dict[str, List[Bar]] = {}
        for coin in self.cfg.universe:
            inst = f"{coin}-USDT"
            try:
                bars = await fetch_daily_bars(inst, limit=self.cfg.candle_limit)
            except Exception as exc:
                errors[coin] = f"{type(exc).__name__}"
                continue
            sleeve = self.book.sleeves.get(coin)
            if sleeve is None:
                continue
            fresh = [b for b in bars if b.ts > sleeve.last_ts]
            if fresh:
                series[coin] = fresh
            await asyncio.sleep(0)  # cooperative
        # Replay in strict timestamp order: the regime gate reads the regime
        # asset's latest close, so feeding one coin at a time would judge every
        # historical alt entry against *today's* regime state (see
        # engine.feed_chronological).
        fresh = feed_chronological(self.book, series)
        self.bars_consumed += fresh
        save_state(self.cfg.state_name, self.book.to_state())
        status = self.status()
        status["fresh_events"] = fresh
        status["errors"] = errors
        return status

    # ── reporting ────────────────────────────────────────────────────────
    def status(self) -> dict:
        st = self.book.stats()
        return {
            "enabled": self.cfg.enabled,
            "paper_only": self.cfg.paper_only,
            "running": self._running,
            "cycles": self.cycles,
            "last_cycle_ts": self.last_cycle_ts,
            "last_error": self.last_error,
            "bars_consumed": self.bars_consumed,
            "risk_per_sleeve": self.cfg.risk_per_sleeve,
            "risk_per_trade_pct": round(100 * self.cfg.risk_per_trade_pct, 3),
            "entry_lookback": self.cfg.entry_lookback,
            "exit_lookback": self.cfg.exit_lookback,
            "chandelier_mult": self.cfg.chandelier_mult,
            "sizing_halflife_days": self.cfg.sizing_halflife_days,
            "regime_symbol": self.cfg.regime_symbol,
            "regime_ma": self.cfg.regime_ma,
            "universe_size": len(self.cfg.universe),
            **st,
        }

    def positions(self) -> List[dict]:
        return self.book.positions()

    def trades(self, limit: int = 50) -> List[dict]:
        """Closed paper trades, newest first."""
        n = max(1, min(int(limit or 50), 500))
        return list(reversed(self.book.closed_trades[-n:]))


_runner: Optional[TrendPaperRunner] = None


def get_runner() -> Optional[TrendPaperRunner]:
    """Lazily build the singleton runner (disabled config → not created)."""
    global _runner
    if _runner is None:
        try:
            cfg = TrendPortfolioConfig.from_env()
        except Exception as exc:
            log.warning("trend_portfolio config rejected: %s", exc)
            return None
        _runner = TrendPaperRunner(cfg)
    return _runner


async def stop_runner() -> None:
    """Stop the singleton loop if it was ever created (shutdown hook)."""
    if _runner is not None:
        await _runner.stop()
