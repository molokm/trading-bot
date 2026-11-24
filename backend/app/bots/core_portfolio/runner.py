"""Paper runner for the core portfolio (two independent books).

SAFETY CONTRACT (do not weaken)
-------------------------------
* The runner is **read-only** against the exchange: only public candle
  endpoints are called.  There is no import of any order-placing method and no
  credential is read.
* ``paper_only`` stays True (validated in the config) and ``enabled`` gates the
  background loop; a disabled runner never fetches.
* Every failure is swallowed into the returned status dict.

Data contract
-------------
* 6h bars must be UTC-bucketed (Binance / Binance Vision); OKX buckets 6H on
  UTC+8.  A book that has been fed from one venue is never fed from another:
  on a forced venue change the affected block is restarted and the status says
  so, because mixing buckets silently shifts every timestamp by 2 hours.
* Bars are replayed in strict timestamp order (``feed_chronological``), so the
  BTC gate is evaluated at each bar's own time, not at today's regime.
* The daily and 6h blocks are separate accounts seeded with
  ``capital * weight``; nothing rebalances between them, matching the blend
  that was validated.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import httpx

from app.bots.base import load_state, save_state
from app.bots.core_portfolio.config import CorePortfolioConfig
from app.bots.core_portfolio.engine6h import (
    Trend6hBook,
    feed_chronological as feed_6h_chronological,
    gate_map,
)
from app.bots.trend_portfolio.engine import (
    Bar,
    TrendPortfolio,
    feed_chronological as feed_daily_chronological,
)

log = logging.getLogger("core_portfolio")

OKX_BASE = "https://www.okx.com"
BINANCE_BASE = "https://api.binance.com"
BINANCE_VISION_BASE = "https://data-api.binance.vision"

VENUES: Tuple[str, ...] = ("binance", "binance_vision", "okx")
BAR_MS = {"6h": 6 * 3_600_000, "1d": 24 * 3_600_000}
OKX_BAR = {"6h": "6H", "1d": "1D"}
MAX_PER_REQUEST = {"okx": 300, "binance": 1000, "binance_vision": 1000}


# ── venue plumbing ───────────────────────────────────────────────────────
def _venue_order(primary: str, preferred: Optional[str], allow_okx: bool = True) -> List[str]:
    """Try the venue the book is already on, then the configured one, then the rest."""
    order: List[str] = []
    for v in (preferred, primary, *VENUES):
        if not v or v in order:
            continue
        if v == "okx" and not allow_okx:
            continue
        order.append(v)
    return order


def _bucket(venue: str) -> str:
    """Candle bucketing — bars from different buckets must never be mixed."""
    return "utc" if venue in ("binance", "binance_vision") else "utc+8"


def _drop_forming(bars: List[Bar], interval: str, now_ms: int) -> List[Bar]:
    bars.sort(key=lambda b: b.ts)
    span = BAR_MS[interval]
    while bars and bars[-1].ts + span > now_ms:
        bars.pop()
    return bars


def _rows_to_bars(rows: Sequence[Sequence]) -> List[Bar]:
    out: List[Bar] = []
    for r in rows:
        try:
            out.append(Bar(ts=int(r[0]), open=float(r[1]), high=float(r[2]),
                           low=float(r[3]), close=float(r[4])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


async def _fetch_okx(inst_id: str, interval: str, limit: int, timeout: float) -> List[Bar]:
    """OKX public candles, newest-first pages, paginated via ``after``."""
    url = f"{OKX_BASE}/api/v5/market/candles"
    need = max(int(limit), 1)
    rows: List[Sequence] = []
    after: Optional[int] = None
    headers = {"User-Agent": "copix-core-paper"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        while len(rows) < need:
            params = {
                "instId": inst_id,
                "bar": OKX_BAR[interval],
                "limit": str(min(MAX_PER_REQUEST["okx"], need - len(rows))),
            }
            if after is not None:
                params["after"] = str(after)
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
            if str(payload.get("code", "0")) != "0":
                raise RuntimeError(f"okx {payload.get('code')}: {payload.get('msg')}")
            page = payload.get("data") or []
            if not page:
                break
            rows.extend(page)
            oldest = min(int(r[0]) for r in page if r and r[0])
            if len(page) < 2 or oldest <= 0:
                break
            after = oldest
            await asyncio.sleep(0.12)   # OKX /market/candles rate limit
    bars = _rows_to_bars(rows)
    # Pagination may overlap on the boundary; keep one bar per ts.
    dedup = {b.ts: b for b in bars}
    return [dedup[k] for k in sorted(dedup)]


async def _fetch_binance(base: str, coin: str, interval: str, limit: int,
                         timeout: float) -> List[Bar]:
    url = f"{base}/api/v3/klines"
    params = {
        "symbol": f"{coin.upper()}USDT",
        "interval": interval,
        "limit": str(min(max(int(limit), 1), MAX_PER_REQUEST["binance"])),
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            url, params=params, headers={"User-Agent": "copix-core-paper"}
        )
        resp.raise_for_status()
        rows = resp.json()
    if not isinstance(rows, list):
        raise RuntimeError(f"binance unexpected payload: {type(rows).__name__}")
    return _rows_to_bars(rows)


async def fetch_bars(coin: str, interval: str, limit: int, venue: str,
                     timeout: float = 20.0) -> List[Bar]:
    """Closed candles for ``coin`` (e.g. ``BTC``) on one venue, ascending."""
    if venue == "okx":
        bars = await _fetch_okx(f"{coin.upper()}-USDT", interval, limit, timeout)
    elif venue == "binance_vision":
        bars = await _fetch_binance(
            BINANCE_VISION_BASE, coin, interval, limit, timeout
        )
    elif venue == "binance":
        bars = await _fetch_binance(BINANCE_BASE, coin, interval, limit, timeout)
    else:
        raise ValueError(f"unknown venue {venue!r}")
    return _drop_forming(bars, interval, int(time.time() * 1000))


# ── curve helpers ────────────────────────────────────────────────────────
def curve_stats(points: Sequence[dict], start: float, bars_per_year: int = 365) -> dict:
    """CAGR / vol / maxDD / Sharpe from daily equity points."""
    eq = [max(float(p.get("equity") or 0.0), 1e-9) for p in points]
    if len(eq) < 2 or start <= 0:
        return {"cagr_pct": 0.0, "vol_pct": 0.0, "max_dd_pct": 0.0,
                "sharpe": 0.0, "days": len(eq), "final_equity": round(eq[-1], 2) if eq else 0.0}
    years = len(eq) / float(bars_per_year)
    cagr = ((eq[-1] / start) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    peak = eq[0]
    max_dd = 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = max(max_dd, 1 - v / peak)
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    sd = math.sqrt(var)
    return {
        "cagr_pct": round(cagr, 2),
        "vol_pct": round(sd * math.sqrt(bars_per_year) * 100, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "sharpe": round(mean / sd * math.sqrt(bars_per_year), 2) if sd > 0 else 0.0,
        "days": len(eq),
        "final_equity": round(eq[-1], 2),
    }


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).date().isoformat()


# ── runner ───────────────────────────────────────────────────────────────
class CorePaperRunner:
    """Steps both paper books over newly closed bars, once a cycle."""

    def __init__(self, cfg: Optional[CorePortfolioConfig] = None):
        self.cfg = cfg or CorePortfolioConfig.from_env()
        self.cfg.validate()
        self.daily_cfg = self.cfg.daily_book_config()
        self.h6 = Trend6hBook(self.cfg)
        self.daily = TrendPortfolio(self.daily_cfg)
        self.h6_venue: str = ""
        self.daily_venue: str = ""
        self.h6_bucket: str = ""
        self.daily_bucket: str = ""
        self.equity_history: List[dict] = []
        self.last_cycle_ts: float = 0.0
        self.last_error: str = ""
        self.cycles: int = 0
        self.bars_consumed: int = 0
        self.fresh_bars: int = 0
        self.sources_used: Dict[str, int] = {}
        self.data_note: str = ""
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._restore(load_state(self.cfg.state_name, {}) or {})

    # ── state ────────────────────────────────────────────────────────────
    def _restore(self, raw: dict) -> None:
        self.h6_venue = str(raw.get("h6_venue") or "")
        self.daily_venue = str(raw.get("daily_venue") or "")
        self.h6_bucket = str(raw.get("h6_bucket") or (_bucket(self.h6_venue) if self.h6_venue else ""))
        self.daily_bucket = str(
            raw.get("daily_bucket") or (_bucket(self.daily_venue) if self.daily_venue else "")
        )
        self.bars_consumed = int(raw.get("bars_consumed") or 0)
        self.cycles = int(raw.get("cycles") or 0)
        self.sources_used = dict(raw.get("sources_used") or {})
        self.equity_history = list(raw.get("equity_history") or [])[-self.cfg.max_equity_points:]
        if isinstance(raw.get("h6"), dict):
            try:
                self.h6 = Trend6hBook.from_state(self.cfg, raw["h6"])
            except Exception as exc:
                log.warning("core h6 state rejected, restarting block: %s", exc)
        if isinstance(raw.get("daily"), dict):
            try:
                self.daily = TrendPortfolio.from_state(self.daily_cfg, raw["daily"])
            except Exception as exc:
                log.warning("core daily state rejected, restarting block: %s", exc)

    def _state(self) -> dict:
        return {
            "h6_venue": self.h6_venue,
            "daily_venue": self.daily_venue,
            "h6_bucket": self.h6_bucket,
            "daily_bucket": self.daily_bucket,
            "bars_consumed": self.bars_consumed,
            "cycles": self.cycles,
            "sources_used": dict(self.sources_used),
            "equity_history": self.equity_history[-self.cfg.max_equity_points:],
            "h6": self.h6.to_state(),
            "daily": self.daily.to_state(),
        }

    # ── lifecycle ────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
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
            except Exception as exc:   # never kill the app
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("core_portfolio cycle failed: %s", self.last_error)
            await asyncio.sleep(max(300, self.cfg.poll_interval_sec))

    # ── one cycle ────────────────────────────────────────────────────────
    async def cycle(self) -> dict:
        self.cycles += 1
        self.last_cycle_ts = time.time()
        errors: Dict[str, str] = {}
        notes: List[str] = []
        self.fresh_bars = 0

        # 1) 6h block — the gate asset's bars are fetched first and decide the
        #    venue for the whole block, so all timestamps share one bucketing.
        venue6, series6, gate_bars, errors6 = await self._gather(
            "6h",
            per_coin_limit=self.cfg.h6_candle_limit,
            gate_limit=self.cfg.gate_bars,
            preferred=self.h6_venue,
            # The 6h block was validated on UTC bars.  OKX buckets 6H on
            # UTC+8, so an automatic fallback would silently trade a
            # different strategy; only use OKX when the operator asked for it.
            allow_okx=(self.cfg.source == "okx"),
        )
        errors.update(errors6)
        if venue6:
            bucket6 = _bucket(venue6)
            if self.h6_bucket and self.h6_bucket != bucket6:
                notes.append(
                    f"6h bucketing {self.h6_bucket}→{bucket6}: block restarted"
                )
                self.h6 = Trend6hBook(self.cfg)
            self.h6_bucket = bucket6
            self.h6_venue = venue6
            self.sources_used[venue6] = self.sources_used.get(venue6, 0) + 1
            if venue6 != self.cfg.source:
                notes.append(f"{self.cfg.source} unreachable — 6h served by {venue6}")
            gates = gate_map(gate_bars, self.cfg.regime_ma_days)
            fresh: Dict[str, List[Bar]] = {}
            for coin, bars in series6.items():
                sleeve = self.h6.sleeves.get(coin)
                if sleeve is None:
                    continue
                new = [b for b in bars if b.ts > sleeve.last_ts]
                if new:
                    fresh[coin] = new
            fed = feed_6h_chronological(self.h6, fresh, gates)
            self.fresh_bars += fed
            self.bars_consumed += fed
            if not gate_bars or len(gate_bars) < self.cfg.regime_ma_days * 4:
                notes.append("6h gate has too little BTC history — treated as ON")

        # 2) daily block — its own venue (Binance UTC preferred for the same
        #    reason), its own gate from the book's BTC sleeve.
        venue1, series1, _, errors1 = await self._gather(
            "1d",
            per_coin_limit=self.cfg.daily_candle_limit,
            gate_limit=0,
            preferred=self.daily_venue,
            allow_okx=True,
        )
        errors.update(errors1)
        if venue1:
            bucket1 = _bucket(venue1)
            if self.daily_bucket and self.daily_bucket != bucket1:
                notes.append(
                    f"daily bucketing {self.daily_bucket}→{bucket1}: block restarted"
                )
                self.daily = TrendPortfolio(self.daily_cfg)
            self.daily_bucket = bucket1
            self.daily_venue = venue1
            self.sources_used[venue1] = self.sources_used.get(venue1, 0) + 1
            same_daily = venue1 == self.cfg.daily_source or (
                venue1 in ("binance", "binance_vision") and self.cfg.daily_source == "binance"
            )
            if not same_daily:
                notes.append(f"{self.cfg.daily_source} unreachable — daily served by {venue1}")
            fresh_d: Dict[str, List[Bar]] = {}
            for coin, bars in series1.items():
                sleeve = self.daily.sleeves.get(coin)
                if sleeve is None:
                    continue
                new = [b for b in bars if b.ts > sleeve.last_ts]
                if new:
                    fresh_d[coin] = new
            fed = feed_daily_chronological(self.daily, fresh_d)
            self.fresh_bars += fed
            self.bars_consumed += fed

        self.data_note = "; ".join(notes)
        if not venue6 and not venue1:
            self.last_error = "no market data from any venue"
        elif not venue6:
            self.last_error = "6h block: no data this cycle"
        elif not venue1:
            self.last_error = "daily block: no data this cycle"
        else:
            self.last_error = ""
        self._record_equity()
        save_state(self.cfg.state_name, self._state())
        status = self.status()
        status["fresh_bars"] = self.fresh_bars
        status["errors"] = errors
        return status

    async def _gather(
        self,
        interval: str,
        per_coin_limit: int,
        gate_limit: int,
        preferred: str,
        allow_okx: bool = True,
    ) -> Tuple[str, Dict[str, List[Bar]], List[Bar], Dict[str, str]]:
        """Pick one venue for the whole block and fetch its candles."""
        errors: Dict[str, str] = {}
        gate_coin = self.cfg.regime_symbol or ""
        need = min(5, max(len(self.cfg.universe), 1))
        for venue in _venue_order(self.cfg.source, preferred, allow_okx):
            venue_errors: Dict[str, str] = {}
            gate_bars: List[Bar] = []
            if gate_coin and gate_limit:
                try:
                    gate_bars = await fetch_bars(gate_coin, interval, gate_limit, venue)
                except Exception as exc:
                    errors[gate_coin] = f"{venue} {type(exc).__name__}"
                    continue
                if not gate_bars:
                    errors[gate_coin] = f"{venue} empty"
                    continue
            series: Dict[str, List[Bar]] = {}
            fails_in_row = 0
            for coin in self.cfg.universe:
                if coin == gate_coin and gate_bars:
                    series[coin] = gate_bars
                    continue
                try:
                    bars = await fetch_bars(coin, interval, per_coin_limit, venue)
                except Exception as exc:
                    venue_errors[coin] = f"{venue} {type(exc).__name__}"
                    fails_in_row += 1
                    # A blocked host (Binance answers 451 from some cloud
                    # regions) fails the first coins; stop probing it for the
                    # rest of the cycle instead of sending 45 doomed requests.
                    if fails_in_row >= 3 and not series:
                        errors["_venues"] = (
                            f"{venue}: {type(exc).__name__} on the first coins"
                        )
                        break
                    continue
                fails_in_row = 0
                if bars:
                    series[coin] = bars
                if venue == "okx":
                    await asyncio.sleep(0.12)
                else:
                    await asyncio.sleep(0)
            if len(series) >= need:
                return venue, series, gate_bars, venue_errors
            errors["_venues"] = f"{venue}: only {len(series)}/{len(self.cfg.universe)} coins"
        return "", {}, [], errors

    def _record_equity(self) -> None:
        ts = max(self.h6.last_ts or 0, self.daily.last_ts or 0) or int(time.time() * 1000)
        point = {
            "ts": ts,
            "day": _day(ts),
            "equity": round(self.total_equity, 2),
            "daily": round(self.daily.mark_equity(), 2),
            "h6": round(self.h6.equity, 2),
        }
        if self.equity_history and self.equity_history[-1].get("day") == point["day"]:
            self.equity_history[-1] = point
        else:
            self.equity_history.append(point)
        if len(self.equity_history) > self.cfg.max_equity_points:
            self.equity_history = self.equity_history[-self.cfg.max_equity_points:]

    # ── reporting ────────────────────────────────────────────────────────
    @property
    def total_equity(self) -> float:
        return self.daily.mark_equity() + self.h6.equity

    def _blocks(self) -> dict:
        d = self.daily.stats()
        h = self.h6.stats()
        return {"daily": d, "h6": h}

    def _combined(self) -> dict:
        blocks = self._blocks()
        d, h = blocks["daily"], blocks["h6"]
        trades = d["trades"] + h["trades"]
        wins = (d["win_rate_pct"] * d["trades"] + h["win_rate_pct"] * h["trades"]) / 100.0
        eq = self.total_equity
        notional = d["open_notional"] + h["open_notional"]
        start = float(self.cfg.capital)
        return {
            "equity": round(eq, 2),
            "start_equity": round(start, 2),
            "return_pct": round(100 * (eq / start - 1), 2) if start else 0.0,
            "trades": trades,
            "win_rate_pct": round(100 * wins / trades, 1) if trades else 0.0,
            "open_positions": d["open_positions"] + h["open_positions"],
            "open_notional": round(notional, 2),
            "gross_leverage": round(notional / eq, 3) if eq > 0 else 0.0,
            "fees_paid": round(d["fees_paid"] + h["fees_paid"], 2),
            "realized_pnl": round(d["realized_pnl"] + h["realized_pnl"], 2),
            "daily_weight": round(self.cfg.daily_weight, 3),
            "h6_weight": round(1.0 - self.cfg.daily_weight, 3),
            "curve": curve_stats(self.equity_history, start),
        }

    def status(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "paper_only": self.cfg.paper_only,
            "running": self._running,
            "cycles": self.cycles,
            "last_cycle_ts": self.last_cycle_ts,
            "last_error": self.last_error,
            "bars_consumed": self.bars_consumed,
            "source": self.cfg.source,
            "h6_venue": self.h6_venue,
            "daily_venue": self.daily_venue,
            "h6_bucket": self.h6_bucket,
            "daily_bucket": self.daily_bucket,
            "sources_used": dict(self.sources_used),
            "data_note": self.data_note,
            "universe_size": len(self.cfg.universe),
            "regime_symbol": self.cfg.regime_symbol,
            "regime_ma_days": self.cfg.regime_ma_days,
            "h6": {
                "entry": self.cfg.h6_entry,
                "exit": self.cfg.h6_exit,
                "atr_period": self.cfg.h6_atr_period,
                "chandelier": self.cfg.h6_chandelier,
                "risk_per_sleeve": self.cfg.h6_risk_per_sleeve,
                "capital": round(self.cfg.h6_capital, 2),
            },
            "daily": {
                "entry": self.cfg.daily_entry,
                "exit": self.cfg.daily_exit,
                "chandelier": self.cfg.daily_chandelier,
                "risk_per_sleeve": self.cfg.daily_risk_per_sleeve,
                "capital": round(self.cfg.daily_capital, 2),
            },
            "combined": self._combined(),
            "blocks": self._blocks(),
            "equity_curve": self.equity_history[-400:],
        }

    def positions(self) -> List[dict]:
        out = []
        for p in self.daily.positions():
            out.append({**p, "block": "daily"})
        for p in self.h6.positions():
            out.append({**p, "block": "6h"})
        out.sort(key=lambda p: p.get("notional", 0), reverse=True)
        return out

    def trades(self, limit: int = 50) -> List[dict]:
        """Closed paper trades from both blocks, newest first."""
        n = max(1, min(int(limit or 50), 500))
        rows = [{**t, "block": "daily"} for t in self.daily.closed_trades]
        rows += [{**t, "block": "6h"} for t in self.h6.closed_trades]
        rows.sort(key=lambda t: t.get("exit_ts") or 0, reverse=True)
        return rows[:n]


# ── backtest (same engines, historical bars) ─────────────────────────────
def backtest_core(
    series_daily: Dict[str, Sequence[Bar]],
    series_6h: Dict[str, Sequence[Bar]],
    cfg: CorePortfolioConfig,
) -> dict:
    """Replay both books on historical bars and blend them as two accounts."""
    daily = TrendPortfolio(cfg.daily_book_config())
    h6 = Trend6hBook(cfg)

    daily_by_ts: Dict[int, Dict[str, Bar]] = {}
    for coin, bars in series_daily.items():
        for b in bars:
            daily_by_ts.setdefault(b.ts, {})[coin] = b
    curve_daily: Dict[str, float] = {}
    for ts in sorted(daily_by_ts):
        for coin, bar in daily_by_ts[ts].items():
            daily.on_bar(coin, bar)
        curve_daily[_day(ts)] = daily.mark_equity()

    gate_bars = series_6h.get(cfg.regime_symbol) or []
    gates = gate_map(gate_bars, cfg.regime_ma_days)
    six_by_ts: Dict[int, Dict[str, Bar]] = {}
    for coin, bars in series_6h.items():
        for b in bars:
            six_by_ts.setdefault(b.ts, {})[coin] = b
    curve_6h: Dict[str, float] = {}
    for ts in sorted(six_by_ts):
        for coin, bar in six_by_ts[ts].items():
            h6.on_bar(coin, bar, gates.get(ts, True))
        curve_6h[_day(ts)] = h6.equity

    common = sorted(set(curve_daily) & set(curve_6h))
    if not common:
        return {"ok": False, "reason": "no common days"}
    combined = [
        {"day": d, "equity": curve_daily[d] + curve_6h[d]}
        for d in common
    ]
    return {
        "ok": True,
        "days": len(common),
        "window": {"from": common[0], "to": common[-1]},
        "stats": curve_stats(combined, cfg.capital),
        "blocks": {
            "daily": curve_stats(
                [{"equity": curve_daily[d]} for d in common], cfg.daily_capital
            ),
            "h6": curve_stats(
                [{"equity": curve_6h[d]} for d in common], cfg.h6_capital
            ),
        },
        "trades": {
            "daily": sum(s.trades for s in daily.sleeves.values()),
            "h6": sum(s.trades for s in h6.sleeves.values()),
        },
        "curve": combined[-500:],
    }


# ── singleton (same pattern as the daily trend runner) ───────────────────
_runner: Optional[CorePaperRunner] = None


def get_runner() -> Optional[CorePaperRunner]:
    """Lazily build the singleton runner (rejected config → None)."""
    global _runner
    if _runner is None:
        try:
            cfg = CorePortfolioConfig.from_env()
        except Exception as exc:
            log.warning("core_portfolio config rejected: %s", exc)
            return None
        _runner = CorePaperRunner(cfg)
    return _runner


async def stop_runner() -> None:
    if _runner is not None:
        await _runner.stop()
