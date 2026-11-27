"""Pure engine for the long-only daily trend portfolio.

No I/O, no exchange client, no order placement — this module only turns bars
into signals, fills and an equity curve.  The same code drives both the HTTP
backtest endpoint and the paper runner, so a validated backtest and the live
paper track record cannot drift apart.

Rules (daily bars, closed bar only, fills on the NEXT bar's open):

  entry   close > max(high[-entry_lookback:])          → long next open
  exit    low  <= chandelier stop                       → flat at stop
          close < min(low[-exit_lookback:])             → flat next open
  trail   stop = max(stop, peak_close − chandelier_mult × ATR)
  size    risk = risk_per_sleeve × (account_equity / n_coins)
          notional = risk / (chandelier_mult × ATR)

Everything below is deterministic: the same bars always produce the same
fills, so a backtest can be replayed to the cent.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from app.bots.trend_portfolio.config import TrendPortfolioConfig


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class Position:
    coin: str
    entry: float
    qty: float
    stop: float
    entry_ts: int
    risk_usd: float
    notional: float
    peak_close: float
    side: int = 1                      # +1 long, -1 short

    def as_dict(self) -> dict:
        return {
            "coin": self.coin,
            "side": self.side,
            "entry": round(self.entry, 8),
            "qty": self.qty,
            "stop": round(self.stop, 8),
            "entry_ts": self.entry_ts,
            "risk_usd": round(self.risk_usd, 4),
            "notional": round(self.notional, 4),
            "peak_close": round(self.peak_close, 8),
        }


@dataclass
class Sleeve:
    """Per-coin history + open position."""

    coin: str
    highs: List[float] = field(default_factory=list)
    lows: List[float] = field(default_factory=list)
    closes: List[float] = field(default_factory=list)
    trs: List[float] = field(default_factory=list)
    prev_close: Optional[float] = None
    last_ts: int = 0                   # newest bar already consumed
    position: Optional[Position] = None
    pending: Optional[str] = None      # "enter" | "exit" | None
    trades: int = 0
    wins: int = 0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0

    # ── indicators ───────────────────────────────────────────────────────
    def atr(self, period: int) -> Optional[float]:
        if len(self.trs) < period:
            return None
        window = self.trs[-period:]
        if any(x is None for x in window):
            return None
        return sum(window) / period

    def donchian_high(self, lookback: int, exclude_current: bool = True) -> Optional[float]:
        src = self.highs[:-1] if exclude_current else self.highs
        if len(src) < lookback:
            return None
        window = src[-lookback:]
        if any(math.isnan(x) for x in window):
            return None
        return max(window)

    def donchian_low(self, lookback: int, exclude_current: bool = True) -> Optional[float]:
        src = self.lows[:-1] if exclude_current else self.lows
        if len(src) < lookback:
            return None
        window = src[-lookback:]
        if any(math.isnan(x) for x in window):
            return None
        return min(window)


class TrendPortfolio:
    """Long-only Donchian/Chandelier book over a fixed universe."""

    def __init__(self, cfg: TrendPortfolioConfig):
        cfg.validate()
        self.cfg = cfg
        self.sleeves: Dict[str, Sleeve] = {c: Sleeve(coin=c) for c in cfg.universe}
        self.capital = float(cfg.capital)
        self.start_equity = float(cfg.capital)
        self.realized = 0.0          # closed-trade P&L (research-comparable)
        self.fees = 0.0
        self.funding = 0.0
        self.last_ts = 0
        # sizing base: equity smoothed with ``sizing_halflife_days``
        self.smooth_equity = float(cfg.capital)
        self.smooth_ts = 0
        self.events: List[dict] = []
        # Closed-trade history (bounded, persisted) for the dashboard.
        self.closed_trades: List[dict] = []
        # Drawdown brake: high-water mark of mark equity and the multiplier
        # currently applied to NEW entries (1.0 = no brake).
        self.hwm = float(cfg.capital)
        self.brake = 1.0

    # ── sizing ───────────────────────────────────────────────────────────
    @property
    def realized_equity(self) -> float:
        return self.capital + self.realized - self.fees - self.funding

    def _accounts(self) -> int:
        return max(len(self.sleeves), 1)

    def _risk_usd(self) -> float:
        """Risk per trade, in USD, from the *smoothed* account equity.

        The drawdown brake multiplies this, so a book that is deep below its
        high-water mark opens smaller (never larger) positions.
        """
        return (
            self.cfg.risk_per_sleeve
            * (self.smooth_equity / self._accounts())
            * self.brake
        )

    def _roll_smooth(self, ts: int) -> None:
        """Advance the smoothing base to ``ts`` (day granularity, no drift)."""
        if self.smooth_ts and ts > self.smooth_ts:
            days = (ts - self.smooth_ts) / 86_400_000.0
            if days > 0:
                alpha = 1 - math.exp(math.log(0.5) * days / self.cfg.sizing_halflife_days)
                self.smooth_equity += alpha * (self.realized_equity - self.smooth_equity)
        self.smooth_ts = ts

    def mark_equity(self) -> float:
        """Equity including open-position P&L at last close."""
        eq = self.realized_equity
        for s in self.sleeves.values():
            p = s.position
            if p and s.closes:
                eq += (s.closes[-1] - p.entry) * p.qty * p.side
        return eq

    def open_notional(self) -> float:
        return sum(p.position.notional for p in self.sleeves.values() if p.position)

    def end_of_day(self, ts: int) -> None:
        """Update the high-water mark and the drawdown brake for ``ts``.

        Must be called once per timestamp *after* every coin's bar for that day
        has been fed: the mark is only complete then, and using it on the next
        day's entries keeps the brake strictly causal.
        """
        if self.cfg.brake_start_pct <= 0:
            return
        eq = self.mark_equity()
        if eq > self.hwm:
            self.hwm = eq
        dd = 100.0 * (1 - eq / self.hwm) if self.hwm > 0 else 0.0
        recover = self.cfg.brake_recover_pct or self.cfg.brake_start_pct / 2
        if dd >= self.cfg.brake_start_pct:
            self.brake = self.cfg.brake_mult
        elif dd < recover:
            self.brake = 1.0

    # ── bar feed ─────────────────────────────────────────────────────────
    def on_bar(self, coin: str, bar: Bar) -> List[dict]:
        """Feed one closed daily bar. Returns events produced by this bar."""
        sleeve = self.sleeves.get(coin)
        if sleeve is None:
            return []
        if bar.ts <= sleeve.last_ts:
            return []                      # idempotent: never replay a bar
        out: List[dict] = []

        tr = bar.high - bar.low
        if sleeve.prev_close is not None:
            tr = max(tr, abs(bar.high - sleeve.prev_close), abs(bar.low - sleeve.prev_close))
        sleeve.highs.append(bar.high)
        sleeve.lows.append(bar.low)
        sleeve.closes.append(bar.close)
        sleeve.trs.append(tr)
        sleeve.prev_close = bar.close
        sleeve.last_ts = bar.ts
        if bar.ts > self.last_ts:
            self._roll_smooth(bar.ts)
            self.last_ts = bar.ts

        # 1) fill a pending order at this bar's open
        if sleeve.pending in ("enter", "enter_short") and sleeve.position is None:
            out += self._fill_entry(sleeve, bar)
        elif sleeve.pending == "exit" and sleeve.position is not None:
            side = sleeve.position.side
            out += self._fill_exit(
                sleeve, bar, bar.open * (1 - side * self.cfg.slippage_pct), "signal"
            )
        sleeve.pending = None

        # 2) manage an open position
        if sleeve.position is not None:
            out += self._manage(sleeve, bar)

        # 3) look for a fresh entry
        if sleeve.position is None and sleeve.pending is None:
            out += self._check_entry(sleeve, bar)
        return out

    # ── internals ────────────────────────────────────────────────────────
    def _fill_entry(self, sleeve: Sleeve, bar: Bar) -> List[dict]:
        side = -1 if sleeve.pending == "enter_short" else 1
        atr = sleeve.atr(self.cfg.atr_period)
        if not atr or atr <= 0 or bar.open <= 0:
            return []
        entry = bar.open * (1 + side * self.cfg.slippage_pct)
        stop_dist = self.cfg.chandelier_mult * atr
        risk_usd = self._risk_usd()
        equity = self.smooth_equity
        notional = min(
            risk_usd * entry / stop_dist,
            self.cfg.max_sleeve_leverage * (equity / self._accounts()) * self.brake,
        )
        if notional <= 0:
            return []
        qty = notional / entry
        fee = notional * self.cfg.fee_pct
        self.fees += fee
        sleeve.position = Position(
            coin=sleeve.coin,
            entry=entry,
            qty=qty,
            stop=entry - side * stop_dist,
            entry_ts=bar.ts,
            risk_usd=stop_dist * qty,
            notional=notional,
            peak_close=entry,
            side=side,
        )
        ev = {
            "kind": "open",
            "coin": sleeve.coin,
            "side": side,
            "ts": bar.ts,
            "price": round(entry, 8),
            "qty": qty,
            "notional": round(notional, 4),
            "stop": round(sleeve.position.stop, 8),
            "risk_usd": round(sleeve.position.risk_usd, 4),
        }
        self.events.append(ev)
        return [ev]

    def _fill_exit(self, sleeve: Sleeve, bar: Bar, price: float, reason: str) -> List[dict]:
        p = sleeve.position
        if p is None:
            return []
        fee = abs(price * p.qty) * self.cfg.fee_pct
        pnl = (price - p.entry) * p.qty * p.side - fee
        self.fees += fee
        self.realized += pnl
        sleeve.realized_pnl += pnl
        sleeve.trades += 1
        if pnl > 0:
            sleeve.wins += 1
        sleeve.position = None
        ev = {
            "kind": "close",
            "coin": sleeve.coin,
            "side": p.side,
            "ts": bar.ts,
            "price": round(price, 8),
            "qty": p.qty,
            "pnl": round(pnl, 4),
            "r_multiple": round(pnl / p.risk_usd, 3) if p.risk_usd else None,
            "reason": reason,
        }
        self.events.append(ev)
        self.closed_trades.append({
            "coin": sleeve.coin,
            "side": p.side,
            "entry": round(p.entry, 8),
            "exit": round(price, 8),
            "entry_ts": p.entry_ts,
            "exit_ts": bar.ts,
            "qty": p.qty,
            "notional": round(p.notional, 4),
            "pnl": round(pnl, 4),
            "pnl_pct": round((price / p.entry - 1) * 100 * p.side, 3) if p.entry else 0.0,
            "r_multiple": round(pnl / p.risk_usd, 3) if p.risk_usd else None,
            "reason": reason,
        })
        if len(self.closed_trades) > 500:
            self.closed_trades = self.closed_trades[-500:]
        return [ev]

    def _manage(self, sleeve: Sleeve, bar: Bar) -> List[dict]:
        p = sleeve.position
        out: List[dict] = []
        self.funding += self.cfg.funding_pct_day * p.notional

        atr = sleeve.atr(self.cfg.atr_period)
        if atr and atr > 0:
            if p.side > 0:
                p.peak_close = max(p.peak_close, bar.close)
                p.stop = max(p.stop, p.peak_close - self.cfg.chandelier_mult * atr)
            else:
                p.peak_close = min(p.peak_close, bar.close)
                p.stop = min(p.stop, p.peak_close + self.cfg.chandelier_mult * atr)

        if p.side > 0 and bar.low <= p.stop:
            return self._fill_exit(sleeve, bar, p.stop * (1 - self.cfg.slippage_pct), "stop")
        if p.side < 0 and bar.high >= p.stop:
            return self._fill_exit(sleeve, bar, p.stop * (1 + self.cfg.slippage_pct), "stop")

        if p.side > 0:
            low_n = sleeve.donchian_low(self.cfg.exit_lookback, exclude_current=True)
            if low_n is not None and bar.close < low_n:
                sleeve.pending = "exit"
                out.append({"kind": "exit_pending", "coin": sleeve.coin, "ts": bar.ts})
        else:
            high_n = sleeve.donchian_high(self.cfg.exit_lookback, exclude_current=True)
            if high_n is not None and bar.close > high_n:
                sleeve.pending = "exit"
                out.append({"kind": "exit_pending", "coin": sleeve.coin, "ts": bar.ts})
        return out

    def _regime_ok(self, side: int) -> bool:
        """Market-regime gate (no-op when ``regime_symbol`` is empty)."""
        sym = self.cfg.regime_symbol
        if not sym:
            return True
        s = self.sleeves.get(sym)
        if s is None or len(s.closes) < self.cfg.regime_ma:
            return False
        ma = sum(s.closes[-self.cfg.regime_ma:]) / self.cfg.regime_ma
        last = s.closes[-1]
        return last > ma if side > 0 else last < ma

    def _check_entry(self, sleeve: Sleeve, bar: Bar) -> List[dict]:
        high_n = sleeve.donchian_high(self.cfg.entry_lookback, exclude_current=True)
        if high_n is not None and bar.close > high_n and self._regime_ok(1):
            sleeve.pending = "enter"
            return [{"kind": "enter_pending", "coin": sleeve.coin, "ts": bar.ts}]
        if self.cfg.allow_short and self._regime_ok(-1):
            low_n = sleeve.donchian_low(self.cfg.entry_lookback, exclude_current=True)
            if low_n is not None and bar.close < low_n:
                sleeve.pending = "enter_short"
                return [{"kind": "enter_pending", "coin": sleeve.coin, "ts": bar.ts}]
        return []

    # ── reporting ────────────────────────────────────────────────────────
    def positions(self) -> List[dict]:
        out = []
        for s in self.sleeves.values():
            if not s.position:
                continue
            p = s.position
            mark = s.closes[-1] if s.closes else p.entry
            out.append(
                {
                    **p.as_dict(),
                    "mark": round(mark, 8),
                    "unrealized": round((mark - p.entry) * p.qty, 4),
                    "unrealized_pct": round((mark / p.entry - 1) * 100, 2) if p.entry else 0.0,
                }
            )
        return out

    def stats(self) -> dict:
        trades = sum(s.trades for s in self.sleeves.values())
        wins = sum(s.wins for s in self.sleeves.values())
        eq = self.realized_equity
        return {
            "equity": round(eq, 2),
            "mark_equity": round(self.mark_equity(), 2),
            "sizing_equity": round(self.smooth_equity, 2),
            "start_equity": round(self.start_equity, 2),
            "risk_per_trade_usd": round(self._risk_usd(), 2),
            "risk_per_trade_pct": round(100 * self._risk_usd() / eq, 3) if eq > 0 else 0.0,
            "return_pct": round((eq / self.start_equity - 1) * 100, 2) if self.start_equity else 0.0,
            "realized_pnl": round(self.realized, 2),
            "fees_paid": round(self.fees, 2),
            "funding_paid": round(self.funding, 2),
            "trades": trades,
            "win_rate_pct": round(100 * wins / trades, 1) if trades else 0.0,
            "open_positions": len(self.positions()),
            "open_notional": round(self.open_notional(), 2),
            "gross_leverage": round(self.open_notional() / eq, 3) if eq > 0 else 0.0,
            "brake": round(self.brake, 3),
            "hwm": round(self.hwm, 2),
            "last_bar_ts": self.last_ts,
        }

    # ── state (for the paper runner) ─────────────────────────────────────
    def to_state(self) -> dict:
        return {
            "capital": self.capital,
            "start_equity": self.start_equity,
            "realized": self.realized,
            "fees": self.fees,
            "funding": self.funding,
            "last_ts": self.last_ts,
            "smooth_equity": self.smooth_equity,
            "smooth_ts": self.smooth_ts,
            "hwm": self.hwm,
            "brake": self.brake,
            "closed_trades": self.closed_trades[-500:],
            "sleeves": {
                c: {
                    "highs": s.highs[-self.cfg.state_bars:],
                    "lows": s.lows[-self.cfg.state_bars:],
                    "closes": s.closes[-self.cfg.state_bars:],
                    "trs": s.trs[-self.cfg.state_bars:],
                    "prev_close": s.prev_close,
                    "last_ts": s.last_ts,
                    "pending": s.pending,
                    "trades": s.trades,
                    "wins": s.wins,
                    "realized_pnl": s.realized_pnl,
                    "position": s.position.as_dict() if s.position else None,
                }
                for c, s in self.sleeves.items()
            },
        }

    @classmethod
    def from_state(cls, cfg: TrendPortfolioConfig, state: dict) -> "TrendPortfolio":
        book = cls(cfg)
        if not state:
            return book
        book.capital = float(state.get("capital", cfg.capital))
        book.start_equity = float(state.get("start_equity", cfg.capital))
        book.realized = float(state.get("realized", 0.0))
        book.fees = float(state.get("fees", 0.0))
        book.funding = float(state.get("funding", 0.0))
        book.last_ts = int(state.get("last_ts", 0) or 0)
        book.smooth_equity = float(state.get("smooth_equity", cfg.capital))
        book.smooth_ts = int(state.get("smooth_ts", 0) or 0)
        book.hwm = float(state.get("hwm", cfg.capital) or cfg.capital)
        book.brake = float(state.get("brake", 1.0) or 1.0)
        book.closed_trades = list(state.get("closed_trades") or [])[-500:]
        for coin, raw in (state.get("sleeves") or {}).items():
            s = book.sleeves.get(coin)
            if s is None:
                continue
            s.highs = [float(x) for x in raw.get("highs") or []][-cfg.state_bars:]
            s.lows = [float(x) for x in raw.get("lows") or []][-cfg.state_bars:]
            s.closes = [float(x) for x in raw.get("closes") or []][-cfg.state_bars:]
            s.trs = [float(x) for x in raw.get("trs") or []][-cfg.state_bars:]
            s.prev_close = raw.get("prev_close")
            s.last_ts = int(raw.get("last_ts", 0) or 0)
            s.pending = raw.get("pending")
            s.trades = int(raw.get("trades", 0))
            s.wins = int(raw.get("wins", 0))
            s.realized_pnl = float(raw.get("realized_pnl", 0.0))
            pos = raw.get("position")
            if pos:
                s.position = Position(
                    coin=coin,
                    entry=float(pos["entry"]),
                    qty=float(pos["qty"]),
                    stop=float(pos["stop"]),
                    entry_ts=int(pos.get("entry_ts", 0)),
                    risk_usd=float(pos.get("risk_usd", 0.0)),
                    notional=float(pos.get("notional", 0.0)),
                    peak_close=float(pos.get("peak_close", pos["entry"])),
                    side=int(pos.get("side", 1)),
                )
        return book


def feed_chronological(book: "TrendPortfolio", series: Dict[str, Sequence["Bar"]]) -> int:
    """Feed ``series`` (coin → ascending bars) in strict timestamp order.

    This is the ONLY correct way to replay a book that has a cross-coin
    dependency: the regime gate reads the regime asset's *latest* close, so
    feeding one coin's whole history at a time evaluates every historical
    alt entry against the regime asset's final bar (e.g. today's BTC), not
    the regime state of that day.  ``backtest`` and the paper runner must
    both use this helper so a live book and its validation agree.
    """
    by_ts: Dict[int, Dict[str, Bar]] = {}
    for coin, bars in series.items():
        for b in bars:
            by_ts.setdefault(b.ts, {})[coin] = b
    fed = 0
    for ts in sorted(by_ts):
        for coin, bar in by_ts[ts].items():
            book.on_bar(coin, bar)
            fed += 1
        book.end_of_day(ts)
    return fed


def backtest(
    series: Dict[str, Sequence[Bar]],
    cfg: TrendPortfolioConfig,
) -> dict:
    """Run the same engine over historical bars and return curve + stats.

    ``series`` maps coin → ascending daily bars.
    """
    book = TrendPortfolio(cfg)
    stamps = sorted({b.ts for bars in series.values() for b in bars})
    curve: List[dict] = []
    by_ts: Dict[int, Dict[str, Bar]] = {}
    for coin, bars in series.items():
        for b in bars:
            by_ts.setdefault(b.ts, {})[coin] = b

    for ts in stamps:
        for coin, bar in by_ts.get(ts, {}).items():
            book.on_bar(coin, bar)
        book.end_of_day(ts)
        curve.append({"ts": ts, "equity": book.realized_equity, "mark": book.mark_equity()})

    return {
        "stats": _curve_stats(curve, book.start_equity, cfg.bars_per_year),
        "book": book.stats(),
        "curve": curve,
        "trades": sum(s.trades for s in book.sleeves.values()),
    }


def _curve_stats(curve: List[dict], start: float, bars_per_year: int = 365) -> dict:
    """Drawdown/Sharpe/CAGR on **mark-to-market** equity.

    The old version measured the realized-only curve, which only moves when a
    trade closes: the open-position peak of 2024-12-08 ($280k mark vs $159k
    realized) was invisible, so the same book reported a 24% max drawdown
    instead of the 44% an account actually lived through.  The realized number
    is still returned as ``max_dd_realized_pct`` for comparison.
    """
    if not curve:
        return {"cagr_pct": 0.0, "max_dd_pct": 0.0, "sharpe": 0.0, "days": 0}
    eq = [max(p.get("mark", p["equity"]), 1e-9) for p in curve]
    realized = [max(p["equity"], 1e-9) for p in curve]
    days = len(eq)
    bpy = bars_per_year or 365
    years = days / float(bpy) if days else 0
    final = eq[-1] / start
    cagr = (final ** (1 / years) - 1) * 100 if years > 0 and final > 0 else -100.0

    def _dd(series: List[float]) -> float:
        peak = series[0]
        worst = 0.0
        for v in series:
            peak = max(peak, v)
            worst = max(worst, 1 - v / peak)
        return worst

    max_dd = _dd(eq)
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    sharpe = 0.0
    if rets:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        sd = math.sqrt(var)
        sharpe = (mean / sd) * math.sqrt(bpy) if sd > 0 else 0.0
    return {
        "cagr_pct": round(cagr, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "max_dd_realized_pct": round(_dd(realized) * 100, 2),
        "sharpe": round(sharpe, 2),
        "days": days,
        "final_equity": round(eq[-1], 2),
    }
