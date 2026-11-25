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
import json
import logging
import time
from typing import Dict, List, Optional

import httpx

from app.bots.base import load_state, save_state
from app.bots.trend_portfolio.config import TrendPortfolioConfig
from app.bots.trend_portfolio.engine import Bar, TrendPortfolio, feed_chronological

log = logging.getLogger("trend_portfolio")

OKX_BASE = "https://www.okx.com"
BINANCE_BASE = "https://api.binance.com"

# Durable-state schema.  v1 was written while the OKX fetch silently served
# only 26/45 coins (rate limit); v2 starts a fresh replay so the forward
# record is based on the full universe.  Bump on any state format change.
_STATE_SCHEMA = 2


def _drop_forming_bar(bars: List[Bar]) -> List[Bar]:
    """Sort ascending and drop the candle that is still forming today."""
    bars.sort(key=lambda b: b.ts)
    if bars:
        bars.pop()
    return bars


async def fetch_okx_daily_bars(inst_id: str, limit: int = 300, timeout: float = 20.0) -> List[Bar]:
    """Public daily candles, ascending, with the still-forming bar dropped."""
    url = f"{OKX_BASE}/api/v5/market/candles"
    params = {"instId": inst_id, "bar": "1D", "limit": str(min(max(limit, 1), 300))}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params=params, headers={"User-Agent": "copix-trend-paper"})
        resp.raise_for_status()
        payload = resp.json()
    if str(payload.get("code", "0")) != "0":
        raise RuntimeError(f"okx {payload.get('code')}: {payload.get('msg')}")
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
    return _drop_forming_bar(bars)


async def fetch_binance_daily_bars(coin: str, limit: int = 300, timeout: float = 20.0) -> List[Bar]:
    """Public UTC-bucketed daily klines — the venue the research was measured on."""
    url = f"{BINANCE_BASE}/api/v3/klines"
    params = {
        "symbol": f"{coin.upper()}USDT",
        "interval": "1d",
        "limit": min(max(limit, 1), 1000),
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params=params, headers={"User-Agent": "copix-trend-paper"})
        resp.raise_for_status()
        rows = resp.json()
    if not isinstance(rows, list):
        raise RuntimeError(f"binance unexpected payload: {type(rows).__name__}")
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
    return _drop_forming_bar(bars)


async def fetch_daily_bars(
    inst_id: str, limit: int = 300, timeout: float = 20.0, source: str = "okx"
) -> List[Bar]:
    """Daily bars from the configured venue (``inst_id`` is e.g. ``BTC-USDT``)."""
    if source == "binance":
        return await fetch_binance_daily_bars(inst_id.split("-")[0], limit=limit, timeout=timeout)
    return await fetch_okx_daily_bars(inst_id, limit=limit, timeout=timeout)


class TrendPaperRunner:
    """Steps the portfolio once per cycle over newly closed daily bars.

    State durability: the book is persisted to the database when a ``db``
    handle is attached (``configure(db)`` from startup), with the JSON file as
    a best-effort fallback.  Render's container filesystem is ephemeral, so a
    file-only book replays its whole candle window after every deploy and can
    never build a forward track record — the DB copy is what makes the
    ``live_since`` baseline meaningful.
    """

    def __init__(self, cfg: Optional[TrendPortfolioConfig] = None, db=None):
        self.cfg = cfg or TrendPortfolioConfig.from_env()
        self.db = db
        self._db_restored = False
        self._state_key = f"{self.cfg.state_name}_state"
        state = load_state(self.cfg.state_name, {}) or {}
        self.book = TrendPortfolio.from_state(self.cfg, state)
        # Forward track record starts when the *first persisted* snapshot is
        # written (i.e. after the initial replay).  Replayed PnL is already in
        # the baseline, so ``forward_return_pct`` only measures what happened
        # after the book was first saved to durable storage.
        self.live_since = int(state.get("live_since") or 0)
        self.live_start_equity = float(state.get("live_start_equity") or 0.0)
        self.state_store = "file"
        self.last_cycle_ts: float = 0.0
        self.last_error: str = ""
        self.cycles: int = 0
        self.bars_consumed: int = 0
        self.sources_used: Dict[str, int] = {}
        self.data_note: str = ""
        self.last_failed: int = 0
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ── durable state ────────────────────────────────────────────────────
    async def _restore_from_db(self) -> None:
        """Load the durable snapshot once, before the first cycle.

        The DB copy wins over the container file: the file is recreated empty
        after every deploy, and mixing the two would silently reset the book.
        """
        if self._db_restored or self.db is None:
            return
        try:
            raw = await self.db.get_setting(self._state_key)
        except Exception as exc:
            # Transient DB error: retry on the next cycle instead of silently
            # starting a second, file-only book.
            log.warning("trend_portfolio db restore failed: %s", exc)
            return
        self._db_restored = True
        if not raw:
            self.state_store = self._store_label()
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or not data.get("sleeves"):
                return
            if data.get("schema") != _STATE_SCHEMA:
                log.info("trend_portfolio state schema %r -> %s: fresh replay",
                         data.get("schema"), _STATE_SCHEMA)
                self.state_store = self._store_label()
                return
            book = TrendPortfolio.from_state(self.cfg, data)
            # Crash guard: if the previous save failed, the container file can
            # be one bar ahead of the DB.  Keep the newer book and let the
            # normal save write it back.
            if book.last_ts < self.book.last_ts:
                log.info("trend_portfolio db state behind file (db=%s file=%s)",
                         book.last_ts, self.book.last_ts)
                return
            self.book = book
            self.live_since = int(data.get("live_since") or 0)
            self.live_start_equity = float(data.get("live_start_equity") or 0.0)
            self.state_store = self._store_label()
            log.info("trend_portfolio state restored from db (last_ts=%s)",
                     self.book.last_ts)
        except Exception as exc:
            log.warning("trend_portfolio db state rejected: %s", exc)

    def _store_label(self) -> str:
        if self.db is None:
            return "file"
        return "postgres" if getattr(self.db, "_pg_mode", False) else "sqlite"

    async def _save_state(self) -> None:
        data = self.book.to_state()
        data["schema"] = _STATE_SCHEMA
        data["live_since"] = self.live_since
        data["live_start_equity"] = self.live_start_equity
        data["saved_at"] = int(time.time() * 1000)
        save_state(self.cfg.state_name, data)   # best-effort file copy
        if self.db is None:
            self.state_store = "file"
            return
        try:
            await self.db.set_setting(self._state_key, json.dumps(data))
            self.state_store = self._store_label()
        except Exception as exc:
            self.state_store = "file"
            log.warning("trend_portfolio db save failed: %s", exc)

    async def _fetch_retry(self, inst: str, source: str, attempts: int = 2):
        """Fetch one coin with one retry and OKX pacing.

        OKX rate-limits ``/market/candles`` (and its responses from cloud IPs
        are sometimes slow), which silently shrank the universe to the first
        coins of the list.  Pacing plus a single retry keeps the book on the
        full 45-coin universe.
        """
        last: Optional[Exception] = None
        for _ in range(max(1, attempts)):
            try:
                bars = await fetch_daily_bars(
                    inst, limit=self.cfg.candle_limit, source=source
                )
            except Exception as exc:
                last = exc
                await asyncio.sleep(0.3)
                continue
            if source == "okx":
                await asyncio.sleep(0.12)
            return bars
        raise last or RuntimeError(f"{source}: no data")

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
        await self._restore_from_db()
        self.cycles += 1
        self.last_cycle_ts = time.time()
        errors: Dict[str, str] = {}
        series: Dict[str, List[Bar]] = {}
        primary_down = False
        primary_failures = 0
        missing = 0
        for coin in self.cfg.universe:
            inst = f"{coin}-USDT"
            sleeve = self.book.sleeves.get(coin)
            if sleeve is None:
                continue
            primary = self.cfg.source
            bars = None
            if not primary_down:
                try:
                    bars = await self._fetch_retry(inst, primary)
                    self.sources_used[primary] = self.sources_used.get(primary, 0) + 1
                except Exception as exc:
                    primary_failures += 1
                    # Binance returns 451 from some cloud regions.  After a few
                    # failures stop probing it for the rest of the cycle instead
                    # of doubling the request count on every coin.
                    if primary_failures >= 3:
                        primary_down = True
                    errors[coin] = f"{primary} failed ({type(exc).__name__})"
            if bars is None:
                # Binance blocks some cloud regions; the book must keep running
                # on OKX rather than silently losing days.
                fallback = "okx" if primary != "okx" else "binance"
                try:
                    bars = await self._fetch_retry(inst, fallback)
                    self.sources_used[fallback] = self.sources_used.get(fallback, 0) + 1
                except Exception as exc2:
                    errors[coin] = f"{errors.get(coin, primary)}; {fallback} {type(exc2).__name__}"
                    missing += 1
                    continue
            fresh = [b for b in bars if b.ts > sleeve.last_ts]
            if fresh and self.live_since and sleeve.last_ts == 0:
                # A coin seen for the first time *after* the forward baseline
                # must not dump its whole history into the forward record:
                # seed it with the newest bar and let indicators warm up.
                fresh = fresh[-1:]
            if fresh:
                series[coin] = fresh
        if primary_down:
            self.data_note = (
                f"{self.cfg.source} unreachable from this host — bars served by the "
                f"fallback venue for this cycle"
            )
        else:
            self.data_note = "; ".join(f"{c}: {e}" for c, e in list(errors.items())[:5])
        # Only coins with no bars at all count as failed: a coin served by the
        # fallback venue keeps the book complete.
        self.last_failed = missing
        # Replay in strict timestamp order: the regime gate reads the regime
        # asset's latest close, so feeding one coin at a time would judge every
        # historical alt entry against *today's* regime state (see
        # engine.feed_chronological).
        fresh = feed_chronological(self.book, series)
        self.bars_consumed += fresh
        # Start the forward baseline after the very first replay, so replayed
        # PnL is excluded from ``forward_return_pct``.
        if not self.live_since:
            self.live_since = int(time.time() * 1000)
            self.live_start_equity = self.book.mark_equity()
        await self._save_state()
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
            "source": self.cfg.source,
            "sources_used": dict(self.sources_used),
            "data_note": self.data_note,
            "last_failed": self.last_failed,
            "state_store": self.state_store,
            "live_since": self.live_since,
            "live_start_equity": round(self.live_start_equity, 2),
            "forward_days": round(
                (time.time() * 1000 - self.live_since) / 86_400_000, 1
            ) if self.live_since else 0.0,
            "forward_return_pct": round(
                (self.book.mark_equity() / self.live_start_equity - 1) * 100, 2
            ) if self.live_start_equity else 0.0,
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
_db = None


def configure(db) -> None:
    """Attach the app database so the book's state survives restarts.

    Called from startup right after ``db.init()``.  A runner that was built
    earlier (file state only) is re-pointed at the DB and re-reads it on the
    next cycle.
    """
    global _db
    _db = db
    if _runner is not None:
        _runner.db = db
        _runner._db_restored = False


def get_runner() -> Optional[TrendPaperRunner]:
    """Lazily build the singleton runner (disabled config → not created)."""
    global _runner
    if _runner is None:
        try:
            cfg = TrendPortfolioConfig.from_env()
        except Exception as exc:
            log.warning("trend_portfolio config rejected: %s", exc)
            return None
        _runner = TrendPaperRunner(cfg, db=_db)
    return _runner


async def stop_runner() -> None:
    """Stop the singleton loop if it was ever created (shutdown hook)."""
    if _runner is not None:
        await _runner.stop()
