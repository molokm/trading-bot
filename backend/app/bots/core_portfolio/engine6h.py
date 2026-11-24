"""6h trend block: Donchian breakout + ATR chandelier, long-only.

Causality contract
------------------
* A signal is evaluated on a bar's **close** and filled at the **next** bar's
  open; there is no same-bar execution.
* The Donchian windows and the chandelier stop exclude the bar being judged.
* The BTC regime gate is a per-bar flag computed from BTC closes up to and
  including that bar, and it is looked up by *timestamp* (never by list index,
  which broke late-listed coins in the research script).
* Every sleeve compounds its own equity from 1/N of the block capital, exactly
  like the backtest (equal weight, no cross-sleeve rebalancing).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from app.bots.core_portfolio.config import CorePortfolioConfig
from app.bots.trend_portfolio.engine import Bar


@dataclass
class Sleeve6:
    coin: str
    highs: List[float] = field(default_factory=list)
    lows: List[float] = field(default_factory=list)
    closes: List[float] = field(default_factory=list)
    trs: List[float] = field(default_factory=list)
    prev_close: Optional[float] = None
    atr: float = 0.0
    last_ts: int = 0
    pos: int = 0                       # +1 long, 0 flat
    qty: float = 0.0
    entry: float = 0.0
    entry_ts: int = 0
    entry_cost: float = 0.0
    stop: float = 0.0
    ref: float = 0.0                   # last price the sleeve was marked at
    pending: Optional[int] = None      # target direction at the next open
    pending_atr: float = 0.0
    pending_stop: float = 0.0          # stop from the signal bar's close
    pending_reason: str = ""
    equity: float = 0.0
    trades: int = 0
    wins: int = 0
    realized: float = 0.0
    fees: float = 0.0

    # ── windows (exclude the bar being judged) ───────────────────────────
    def donchian_high(self, lookback: int) -> Optional[float]:
        if len(self.highs) < lookback:
            return None
        return max(self.highs[-lookback:])

    def donchian_low(self, lookback: int) -> Optional[float]:
        if len(self.lows) < lookback:
            return None
        return min(self.lows[-lookback:])


class Trend6hBook:
    """Long-only 6h Donchian book over the core universe."""

    def __init__(self, cfg: CorePortfolioConfig):
        cfg.validate()
        self.cfg = cfg
        size = cfg.h6_capital / max(len(cfg.universe), 1)
        self.sleeves: Dict[str, Sleeve6] = {
            c: Sleeve6(coin=c, equity=size) for c in cfg.universe
        }
        self.start_equity = float(cfg.h6_capital)
        self.last_ts = 0
        self.event_count = 0
        self.closed_trades: List[dict] = []

    # ── valuation ────────────────────────────────────────────────────────
    @property
    def equity(self) -> float:
        return sum(s.equity for s in self.sleeves.values())

    def open_notional(self) -> float:
        return sum(abs(s.qty * s.ref) for s in self.sleeves.values() if s.pos)

    # ── bar processing ───────────────────────────────────────────────────
    def on_bar(self, coin: str, bar: Bar, gate_on: bool = True) -> List[dict]:
        s = self.sleeves.get(coin)
        if s is None or bar.ts <= s.last_ts:
            return []
        events: List[dict] = []

        # 1) fill whatever was decided on the previous close, at this open
        if s.pending is not None:
            events.extend(self._fill(s, bar, gate_on))

        # 2) mark to market on this close
        if s.pos != 0:
            s.equity += s.qty * (bar.close - s.ref)
        s.ref = bar.close

        # 3) indicators on this close (Wilder ATR, same as the screening)
        if s.prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - s.prev_close),
                abs(bar.low - s.prev_close),
            )
        s.trs.append(tr)
        n = self.cfg.h6_atr_period
        if len(s.trs) == n:
            s.atr = sum(s.trs[-n:]) / n
        elif len(s.trs) > n:
            s.atr = (s.atr * (n - 1) + tr) / n

        # 4) signal on this close, using history *before* this bar
        if s.pos == 0:
            hi = s.donchian_high(self.cfg.h6_entry) if gate_on else None
            if hi is not None and bar.close > hi and s.atr > 0:
                s.pending = 1
                s.pending_atr = s.atr
                # Research convention: the initial chandelier is anchored to
                # the signal bar's close, not to the fill bar's open.
                s.pending_stop = bar.close - self.cfg.h6_chandelier * s.atr
                s.pending_reason = "breakout"
        else:
            lo = s.donchian_low(self.cfg.h6_exit)
            if bar.low <= s.stop:
                s.pending = 0
                s.pending_reason = "stop"
            elif lo is not None and bar.close < lo:
                s.pending = 0
                s.pending_reason = "signal"
            else:
                s.stop = max(s.stop, bar.close - self.cfg.h6_chandelier * s.atr)

        # 5) store history
        s.highs.append(bar.high)
        s.lows.append(bar.low)
        s.closes.append(bar.close)
        s.prev_close = bar.close
        s.last_ts = bar.ts
        if bar.ts > self.last_ts:
            self.last_ts = bar.ts
        return events

    def _fill(self, s: Sleeve6, bar: Bar, gate_on: bool) -> List[dict]:
        target, px = s.pending, bar.open
        events: List[dict] = []
        cost_pct = self.cfg.cost_per_side

        if s.pos != 0:
            qty, entry, entry_cost = s.qty, s.entry, s.entry_cost
            reason = s.pending_reason or "exit"
            s.equity += qty * (px - s.ref)
            cost = abs(qty * px) * cost_pct
            s.equity -= cost
            s.fees += cost
            gross = qty * (px - entry)
            net = gross - entry_cost - cost
            s.trades += 1
            if net > 0:
                s.wins += 1
            s.realized += net
            self.closed_trades.append(
                {
                    "coin": s.coin,
                    "side": "long",
                    "entry": round(entry, 8),
                    "exit": round(px, 8),
                    "qty": qty,
                    "entry_ts": s.entry_ts,
                    "exit_ts": bar.ts,
                    "pnl": round(net, 4),
                    "pnl_pct": round(100 * (px / entry - 1), 2) if entry else 0.0,
                    "reason": reason,
                    "bar": "6H",
                }
            )
            if len(self.closed_trades) > 1_000:
                self.closed_trades = self.closed_trades[-1_000:]
            s.pos = 0
            s.qty = 0.0
            s.ref = px
            events.append(
                {
                    "kind": "close",
                    "coin": s.coin,
                    "ts": bar.ts,
                    "price": px,
                    "qty": qty,
                    "pnl": round(net, 4),
                    "reason": reason,
                }
            )

        if target == 1 and gate_on and s.equity > 0 and px > 0:
            atr_pct = s.pending_atr / px
            if atr_pct > 0:
                lev = min(
                    self.cfg.h6_max_sleeve_leverage,
                    self.cfg.h6_risk_per_sleeve / (self.cfg.h6_chandelier * atr_pct),
                )
            else:
                lev = 0.0
            if lev > 0:
                notional = lev * s.equity
                qty = notional / px
                cost = abs(notional) * cost_pct
                s.equity -= cost
                s.fees += cost
                s.pos = 1
                s.qty = qty
                s.entry = px
                s.entry_ts = bar.ts
                s.entry_cost = cost
                s.stop = (
                    s.pending_stop
                    if s.pending_stop > 0
                    else px - self.cfg.h6_chandelier * s.pending_atr
                )
                s.ref = px
                events.append(
                    {
                        "kind": "enter",
                        "coin": s.coin,
                        "ts": bar.ts,
                        "price": px,
                        "qty": qty,
                        "notional": notional,
                    }
                )

        s.pending = None
        s.pending_reason = ""
        if events:
            self.event_count += len(events)
        return events

    # ── reporting ────────────────────────────────────────────────────────
    def positions(self) -> List[dict]:
        out = []
        for s in self.sleeves.values():
            if not s.pos:
                continue
            mark = s.ref or s.entry
            out.append(
                {
                    "coin": s.coin,
                    "side": 1,
                    "entry": round(s.entry, 8),
                    "qty": s.qty,
                    "stop": round(s.stop, 8),
                    "entry_ts": s.entry_ts,
                    "notional": round(s.qty * mark, 4),
                    "mark": round(mark, 8),
                    "unrealized": round((mark - s.entry) * s.qty, 4),
                    "unrealized_pct": round(100 * (mark / s.entry - 1), 2) if s.entry else 0.0,
                }
            )
        return out

    def stats(self) -> dict:
        eq = self.equity
        trades = sum(s.trades for s in self.sleeves.values())
        wins = sum(s.wins for s in self.sleeves.values())
        notional = self.open_notional()
        return {
            "equity": round(eq, 2),
            "start_equity": round(self.start_equity, 2),
            "return_pct": round(100 * (eq / self.start_equity - 1), 2) if self.start_equity else 0.0,
            "trades": trades,
            "win_rate_pct": round(100 * wins / trades, 1) if trades else 0.0,
            "open_positions": len(self.positions()),
            "open_notional": round(notional, 2),
            "gross_leverage": round(notional / eq, 3) if eq > 0 else 0.0,
            "fees_paid": round(sum(s.fees for s in self.sleeves.values()), 2),
            "realized_pnl": round(sum(s.realized for s in self.sleeves.values()), 2),
            "last_bar_ts": self.last_ts,
        }

    # ── state ────────────────────────────────────────────────────────────
    def to_state(self) -> dict:
        keep = self.cfg.h6_state_bars
        return {
            "start_equity": self.start_equity,
            "last_ts": self.last_ts,
            "closed_trades": self.closed_trades[-500:],
            "sleeves": {
                c: {
                    "highs": s.highs[-keep:],
                    "lows": s.lows[-keep:],
                    "closes": s.closes[-keep:],
                    "trs": s.trs[-keep:],
                    "prev_close": s.prev_close,
                    "atr": s.atr,
                    "last_ts": s.last_ts,
                    "pos": s.pos,
                    "qty": s.qty,
                    "entry": s.entry,
                    "entry_ts": s.entry_ts,
                    "entry_cost": s.entry_cost,
                    "stop": s.stop,
                    "ref": s.ref,
                    "pending": s.pending,
                    "pending_atr": s.pending_atr,
                    "pending_stop": s.pending_stop,
                    "pending_reason": s.pending_reason,
                    "equity": s.equity,
                    "trades": s.trades,
                    "wins": s.wins,
                    "realized": s.realized,
                    "fees": s.fees,
                }
                for c, s in self.sleeves.items()
            },
        }

    @classmethod
    def from_state(cls, cfg: CorePortfolioConfig, state: dict) -> "Trend6hBook":
        book = cls(cfg)
        if not state:
            return book
        book.start_equity = float(state.get("start_equity", cfg.h6_capital))
        book.last_ts = int(state.get("last_ts", 0) or 0)
        book.closed_trades = list(state.get("closed_trades") or [])[-500:]
        keep = cfg.h6_state_bars
        for coin, raw in (state.get("sleeves") or {}).items():
            s = book.sleeves.get(coin)
            if s is None:
                continue
            s.highs = [float(x) for x in raw.get("highs") or []][-keep:]
            s.lows = [float(x) for x in raw.get("lows") or []][-keep:]
            s.closes = [float(x) for x in raw.get("closes") or []][-keep:]
            s.trs = [float(x) for x in raw.get("trs") or []][-keep:]
            s.prev_close = raw.get("prev_close")
            s.atr = float(raw.get("atr", 0.0) or 0.0)
            s.last_ts = int(raw.get("last_ts", 0) or 0)
            s.pos = int(raw.get("pos", 0) or 0)
            s.qty = float(raw.get("qty", 0.0) or 0.0)
            s.entry = float(raw.get("entry", 0.0) or 0.0)
            s.entry_ts = int(raw.get("entry_ts", 0) or 0)
            s.entry_cost = float(raw.get("entry_cost", 0.0) or 0.0)
            s.stop = float(raw.get("stop", 0.0) or 0.0)
            s.ref = float(raw.get("ref", 0.0) or 0.0)
            s.pending = raw.get("pending")
            s.pending_atr = float(raw.get("pending_atr", 0.0) or 0.0)
            s.pending_stop = float(raw.get("pending_stop", 0.0) or 0.0)
            s.pending_reason = str(raw.get("pending_reason") or "")
            s.equity = float(raw.get("equity", s.equity) or s.equity)
            s.trades = int(raw.get("trades", 0) or 0)
            s.wins = int(raw.get("wins", 0) or 0)
            s.realized = float(raw.get("realized", 0.0) or 0.0)
            s.fees = float(raw.get("fees", 0.0) or 0.0)
        return book


def gate_map(btc_bars: Sequence[Bar], ma_days: int) -> Dict[int, bool]:
    """ts → "BTC close above its N-day average" (causal, per bar)."""
    n = ma_days * 4
    closes = [b.close for b in btc_bars]
    out: Dict[int, bool] = {}
    run = 0.0
    for i, bar in enumerate(btc_bars):
        run += closes[i]
        if i >= n:
            run -= closes[i - n]
        out[bar.ts] = True if i < n else closes[i] > run / n
    return out


def feed_chronological(
    book: Trend6hBook,
    series: Dict[str, Sequence[Bar]],
    gates: Optional[Dict[int, bool]] = None,
) -> int:
    """Feed fresh bars in strict timestamp order; returns the number consumed."""
    gates = gates or {}
    flat = [(b.ts, coin, b) for coin, bars in series.items() for b in bars]
    flat.sort(key=lambda x: (x[0], x[1]))
    fed = 0
    for ts, coin, bar in flat:
        book.on_bar(coin, bar, gates.get(ts, True))
        fed += 1
    return fed
