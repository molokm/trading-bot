#!/usr/bin/env python3
"""Honest 3-year backtest — MACD + Donchian breakout + Chandelier exit strategy.

A genuinely different design from the current long-only momentum/impulse:
  INDICATORS : Donchian channel (N-day high breakout) for entry
               + MACD histogram for trend/momentum confirmation
               (replaces EMA/RSI/ADX/ROC combo)
  RISK MGMT  : Inverse-volatility position sizing (each coin = equal risk,
               notional ~ 1 / ATR%) + Chandelier exit (trailing stop =
               highest-high-since-entry minus N*ATR, never lowers)
  TAKE-PROFIT: Time-based exit (max holding period) + optional hard ATR
               backstop stop-loss. Replaces % partial-take-profit.

Same honest engine rules:
  1. Signal on bar T CLOSE (causal indicators only)
  2. Enter at bar T+1 OPEN
  3. Stops checked against day's HIGH/LOW first (pessimistic)
  4. Commission 0.10% + slippage 0.05% per side
  5. Binance daily OHLCV (~3y) as proxy for OKX SWAP prices
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

COINS = ["BTC", "ETH", "BNB", "SOL"]
CT_VAL = {"BTC": 0.01, "ETH": 0.1, "BNB": 0.1, "SOL": 0.1}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "BNB": 0.01, "SOL": 0.01}

COMMISSION = 0.001    # 0.10% taker
SLIPPAGE = 0.0005     # 0.05%
DAYS_BACK = 1100
CACHE_PATH = os.path.join(os.path.dirname(__file__), "honest_3y_cache.json")
FUNDING_CACHE_PATH = os.path.join(os.path.dirname(__file__), "honest_3y_funding_cache.json")
OUT_PATH = os.path.join(os.path.dirname(__file__), "honest_macd_donchian_results.json")

FUNDING_INTERVALS_PER_DAY = 3


@dataclass
class MDConfig:
    name: str
    capital: float = 10000.0
    # Indicators
    donchian_n: int = 20          # breakout above N-day high
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    # Risk management
    inverse_vol_target: float = 0.10   # target portfolio risk fraction per coin
    max_notional_pct: float = 1.00     # max notional as fraction of equity
    chandelier_atr: float = 3.0        # trail = highest-high - N*ATR
    hard_stop_atr: float = 5.0         # backstop stop-loss = entry - N*ATR (0=disabled)
    # Take-profit (partial-take-profit + breakeven stop on remainder)
    tp_pct: float = 0.0                # first TP % (0.05 = +5%): close tp_ratio, move stop to breakeven
    tp_ratio: float = 0.5              # fraction of position closed at first TP (0<r<1)
    tp2_pct: float = 0.0               # second TP % for remainder (0=let time/rotation/chandelier exit)
    be_pct: float = 0.0                # move stop to breakeven for ALL positions once price up be_pct (0.02=+2%)
    max_hold_days: int = 30            # time-based exit (0=no time exit)
    # Universe
    top_k: int = 2                     # max simultaneous positions


# ── Indicators ──

def sma(data, period):
    n = len(data)
    out = [0.0] * n
    if n < period:
        return out
    s = sum(data[:period])
    out[period - 1] = s / period
    for i in range(period, n):
        s += data[i] - data[i - period]
        out[i] = s / period
    return out


def ema(data, period):
    if not data:
        return []
    k = 2 / (period + 1)
    out = [data[0]]
    for v in data[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(closes, fast=12, slow=26, signal=9):
    e_fast = ema(closes, fast)
    e_slow = ema(closes, slow)
    line = [e_fast[i] - e_slow[i] for i in range(len(closes))]
    sig = ema(line, signal)
    hist = [line[i] - sig[i] for i in range(len(closes))]
    return line, sig, hist


def donchian_high(highs, period):
    """N-day highest high EXCLUDING current bar (causal, no look-ahead).
    A close breaks out only when it exceeds the PRIOR N-1 bars' highs,
    since close <= today's high by definition."""
    n = len(highs)
    out = [0.0] * n
    if n < period:
        return out
    for i in range(period, n):
        out[i] = max(highs[i - period:i])
    return out


def atr(highs, lows, closes, period=14):
    n = len(closes)
    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    out = [0.0] * n
    if n < period + 1:
        return out
    val = sum(trs[1:period + 1]) / period
    out[period] = val
    for i in range(period + 1, n):
        val = (val * (period - 1) + trs[i]) / period
        out[i] = val
    return out


def build_frame(candles, cfg):
    closes = [c["C"] for c in candles]
    highs = [c["H"] for c in candles]
    lows = [c["L"] for c in candles]
    _, _, hist = macd(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    return {
        "candles": candles,
        "closes": closes,
        "highs": highs,
        "lows": lows,
        "macd_hist": hist,
        "donchian": donchian_high(highs, cfg.donchian_n),
        "atr": atr(highs, lows, closes, cfg.atr_period),
    }


def calc_inverse_vol_size(equity, coin, price, atr_val, cfg):
    """Equal-risk sizing: notional such that each coin contributes
    cfg.inverse_vol_target of equity in daily risk (1 ATR move)."""
    if atr_val <= 0 or price <= 0:
        return 0.0
    notional = equity * cfg.inverse_vol_target / (atr_val / price)
    max_notional = equity * cfg.max_notional_pct
    notional = min(notional, max_notional)
    sz = notional / price
    lot = LOT_SZ[coin]
    sz = math.floor(sz / lot + 1e-12) * lot
    return max(sz, 0.0)


def _unrealized(positions, mtm):
    u = 0.0
    for coin, pos in positions.items():
        cur = mtm[coin]
        u += pos["size"] * (cur - pos["entry"])
    return u


def run_strategy(daily_data, cfg, funding=None, window=None):
    coin_data = {}
    for coin in COINS:
        bars = daily_data.get(coin, [])
        if len(bars) < 250:
            raise RuntimeError(f"{coin}: not enough bars ({len(bars)})")
        coin_data[coin] = build_frame(bars, cfg)

    common = None
    for coin, cd in coin_data.items():
        dates = {c["date"] for c in cd["candles"]}
        common = dates if common is None else (common & dates)
    all_dates = sorted(common)
    date_idx = {coin: {c["date"]: i for i, c in enumerate(cd["candles"])}
                for coin, cd in coin_data.items()}

    equity = cfg.capital
    positions = {}
    trades = []
    equity_curve = []
    filters_hit = defaultdict(int)
    total_funding = 0.0

    warmup = max(cfg.donchian_n, cfg.macd_slow + cfg.macd_signal + 10, 60)
    if window is not None:
        w_start, w_end = window
        idxs = [j for j, d in enumerate(all_dates) if w_start <= d <= w_end]
        if len(idxs) < 30:
            raise RuntimeError(f"window {w_start}→{w_end}: too few common dates ({len(idxs)})")
        i_lo, i_hi = idxs[0], idxs[-1]
    else:
        i_lo, i_hi = warmup, len(all_dates) - 1
    if i_lo < warmup:
        i_lo = warmup

    for i in range(i_lo, i_hi + 1):
        date = all_dates[i]

        mtm = {}
        for coin in COINS:
            ci = date_idx[coin][date]
            mtm[coin] = coin_data[coin]["candles"][ci]["C"]

        # Funding accrual for open positions
        if funding:
            for coin in list(positions.keys()):
                pos = positions[coin]
                ci = date_idx[coin][date]
                bar = coin_data[coin]["candles"][ci]
                rate_day = funding.get(coin, {}).get(date, 0.0)
                if rate_day != 0.0:
                    notional = pos["size"] * bar["C"]
                    fpnl = -notional * rate_day
                    equity += fpnl
                    total_funding += fpnl
                    pos["funding"] = pos.get("funding", 0.0) + fpnl

        # ── 1. Manage open positions on TODAY's bar (H/L first) ──
        for coin in list(positions.keys()):
            pos = positions[coin]
            ci = date_idx[coin][date]
            bar = coin_data[coin]["candles"][ci]
            a = coin_data[coin]["atr"][ci]
            exit_raw = None
            reason = None

            # Chandelier: update trailing stop first, then check against low
            if bar["H"] > pos["peak"]:
                pos["peak"] = bar["H"]
            trail = a * cfg.chandelier_atr
            new_stop = pos["peak"] - trail
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop

            # pessimistic: stop vs LOW before peak used above (peak updated already,
            # but stop comparison below uses the UPDATED stop which is conservative)
            if bar["L"] <= pos["stop"]:
                exit_raw = pos["stop"]
                reason = "chandelier_stop"

            # hard backstop
            if cfg.hard_stop_atr > 0 and not reason and bar["L"] <= pos["hard_stop"]:
                exit_raw = pos["hard_stop"]
                reason = "hard_stop"

            # global breakeven trigger: move stop to breakeven for ALL positions
            # once price has risen be_pct from entry (locks in ~zero with commission)
            if (not reason and cfg.be_pct > 0 and not pos.get("be_active")
                    and bar["H"] >= pos["entry"] * (1 + cfg.be_pct)):
                breakeven = pos["entry"] / (1 - COMMISSION - SLIPPAGE)
                if breakeven > pos["stop"]:
                    pos["stop"] = breakeven
                pos["be_active"] = True

            # ── Partial take-profit: close tp_ratio at first TP, move stop to breakeven ──
            partial_done = False
            if (not reason and cfg.tp_pct > 0 and not pos.get("partial_taken")
                    and bar["H"] >= pos["entry"] * (1 + cfg.tp_pct)):
                tp_fill = pos["entry"] * (1 + cfg.tp_pct) * (1 - SLIPPAGE)
                close_sz = pos["size"] * cfg.tp_ratio
                pnl = close_sz * (tp_fill - pos["entry"]) - close_sz * tp_fill * COMMISSION
                equity += pnl
                trades.append({
                    "date": date, "coin": coin, "pnl": round(pnl, 2),
                    "reason": "partial_tp", "entry": pos["entry"], "exit": round(tp_fill, 4),
                    "size": close_sz, "closed": False,
                })
                pos["size"] -= close_sz
                # move remainder stop to breakeven (net-zero after round-trip costs)
                breakeven = pos["entry"] / (1 - COMMISSION - SLIPPAGE)
                if breakeven > pos["stop"]:
                    pos["stop"] = breakeven
                pos["partial_taken"] = True
                partial_done = True
                if pos["size"] <= 0:
                    reason = "partial_tp_full"
                    exit_raw = pos["entry"] * (1 + cfg.tp_pct)

            # second take-profit for remainder (full exit)
            if (not reason and cfg.tp2_pct > 0 and pos.get("partial_taken")
                    and bar["H"] >= pos["entry"] * (1 + cfg.tp2_pct)):
                exit_raw = pos["entry"] * (1 + cfg.tp2_pct)
                reason = "take_profit2"

            # time-based TP
            if not reason and cfg.max_hold_days > 0 and (i - pos["entry_i"]) >= cfg.max_hold_days:
                exit_raw = bar["C"]
                reason = "time_exit"

            if reason:
                fill = exit_raw * (1 - SLIPPAGE)
                pnl = pos["size"] * (fill - pos["entry"]) - pos["size"] * fill * COMMISSION
                equity += pnl
                trades.append({
                    "date": date, "coin": coin, "pnl": round(pnl, 2),
                    "funding_pnl": round(pos.get("funding", 0.0), 2),
                    "reason": reason, "entry": pos["entry"], "exit": round(fill, 4),
                    "size": pos["size"], "closed": True, "hold_days": i - pos["entry_i"],
                })
                del positions[coin]

        # ── 2. Entry decisions using YESTERDAY signal ──
        ranked = []
        for coin, cd in coin_data.items():
            si = date_idx[coin][all_dates[i - 1]]  # signal bar = yesterday close
            atr_v = cd["atr"][si]
            if atr_v <= 0:
                continue
            close = cd["closes"][si]
            dc = cd["donchian"][si]
            hist = cd["macd_hist"][si]
            if dc <= 0:
                continue
            breakout = close > dc
            macd_pos = hist > 0
            ranked.append({
                "coin": coin, "breakout": breakout, "macd_pos": macd_pos,
                "atr": atr_v, "strength": (close / dc - 1) * 100,
            })

        ranked.sort(key=lambda x: x["strength"], reverse=True)

        # Build target set: coins with breakout + positive MACD, top_k max
        targets = []
        for row in ranked:
            if len(targets) >= cfg.top_k:
                break
            if row["breakout"] and row["macd_pos"]:
                targets.append(row["coin"])
        target_set = set(targets)

        # Close positions not in target set (rotation exit) at TODAY OPEN
        for coin in list(positions.keys()):
            if coin in target_set:
                continue
            pos = positions[coin]
            ci = date_idx[coin][date]
            exit_raw = coin_data[coin]["candles"][ci]["O"]
            # if breakeven triggered (partial TP taken or be_pct reached),
            # remainder is floored at breakeven (never lose)
            if (pos.get("partial_taken") or pos.get("be_active")) and cfg.tp_pct > 0:
                breakeven = pos["entry"] / (1 - COMMISSION - SLIPPAGE)
                if exit_raw < breakeven:
                    exit_raw = breakeven
            fill = exit_raw * (1 - SLIPPAGE)
            pnl = pos["size"] * (fill - pos["entry"]) - pos["size"] * fill * COMMISSION
            equity += pnl
            trades.append({
                "date": date, "coin": coin, "pnl": round(pnl, 2),
                "funding_pnl": round(pos.get("funding", 0.0), 2),
                "reason": "rotation_exit", "entry": pos["entry"], "exit": round(fill, 4),
                "size": pos["size"], "closed": True, "hold_days": i - pos["entry_i"],
            })
            del positions[coin]

        # Open new positions at TODAY OPEN
        for coin in targets:
            if coin in positions:
                continue
            ci = date_idx[coin][date]
            entry_raw = coin_data[coin]["candles"][ci]["O"]
            si = date_idx[coin][all_dates[i - 1]]
            atr_v = coin_data[coin]["atr"][si]
            fill = entry_raw * (1 + SLIPPAGE)
            sz = calc_inverse_vol_size(equity, coin, fill, atr_v, cfg)
            if sz <= 0:
                continue
            fee = sz * fill * COMMISSION
            equity -= fee
            hard_stop = fill - atr_v * cfg.hard_stop_atr if cfg.hard_stop_atr > 0 else 0.0
            positions[coin] = {
                "size": sz, "entry": fill,
                "peak": fill,
                "stop": fill - atr_v * cfg.chandelier_atr,
                "hard_stop": hard_stop,
                "entry_i": i,
                "partial_taken": False,
                "be_active": False,
            }
            trades.append({
                "date": date, "coin": coin, "pnl": round(-fee, 2),
                "reason": "open", "entry": round(fill, 4), "exit": None,
                "size": sz, "closed": False,
            })

        unreal = _unrealized(positions, mtm)
        equity_curve.append({"date": date, "equity": equity + unreal})

    # Force-close at last close
    if positions and all_dates:
        date = all_dates[i_hi]
        for coin in list(positions.keys()):
            pos = positions[coin]
            ci = date_idx[coin][date]
            exit_raw = coin_data[coin]["candles"][ci]["C"]
            fill = exit_raw * (1 - SLIPPAGE)
            pnl = pos["size"] * (fill - pos["entry"]) - pos["size"] * fill * COMMISSION
            equity += pnl
            trades.append({
                "date": date, "coin": coin, "pnl": round(pnl, 2),
                "funding_pnl": round(pos.get("funding", 0.0), 2),
                "reason": "backtest_end", "entry": pos["entry"], "exit": round(fill, 4),
                "size": pos["size"], "closed": True,
            })
        if equity_curve:
            equity_curve[-1]["equity"] = equity

    return summarize(cfg, equity_curve, trades, filters_hit, equity, total_funding)


def summarize(cfg, equity_curve, trades, filters_hit, final_equity, total_funding):
    capital = cfg.capital
    closed = [t for t in trades if t.get("closed")]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    reason_counts = {}
    for t in closed:
        r = t.get("reason", "unknown")
        reason_counts[r] = reason_counts.get(r, 0) + 1
    partial_tp_count = sum(1 for t in trades if t.get("reason") == "partial_tp")
    return {
        "strategy": cfg.name,
        "config": asdict(cfg),
        "period": f"{equity_curve[0]['date'] if equity_curve else ''} → {equity_curve[-1]['date'] if equity_curve else ''}",
        "years": round(max(len(equity_curve) / 365.25, 1e-9), 2),
        "capital": capital,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity / capital - 1) * 100, 1),
        "cagr_pct": round(((final_equity / capital) ** (1 / max(len(equity_curve) / 365.25, 1e-9)) - 1) * 100, 1),
        "max_drawdown_pct": round(_max_dd(equity_curve)["dd"], 1),
        "max_drawdown_date": _max_dd(equity_curve)["date"],
        "sharpe": round(_sharpe(equity_curve), 2),
        "closed_trades": len(closed),
        "partial_tp_count": partial_tp_count,
        "exit_reasons": reason_counts,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(
            (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)))
            if losses and sum(t["pnl"] for t in losses) != 0 else 0.0, 2
        ),
        "total_funding_pnl": round(total_funding, 2),
        "funding_pct_of_capital": round(total_funding / capital * 100, 2) if capital else 0.0,
        "filters_hit": dict(filters_hit),
        "yearly": _yearly(equity_curve, capital),
        "equity_curve": equity_curve[::7],
        "recent_trades": closed[-15:],
    }


def _max_dd(equity_curve):
    peak = 0.0
    max_dd = 0.0
    date = ""
    for pt in equity_curve:
        peak = max(peak, pt["equity"])
        dd = (peak - pt["equity"]) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            date = pt["date"]
    return {"dd": max_dd, "date": date}


def _yearly(equity_curve, capital):
    by_year = {}
    for pt in equity_curve:
        y = pt["date"][:4]
        by_year[y] = pt["equity"]
    yearly = []
    prev = capital
    for y in sorted(by_year):
        eq = by_year[y]
        yearly.append({"year": y, "equity": round(eq, 2), "return_pct": round((eq / prev - 1) * 100, 1)})
        prev = eq
    return yearly


def _sharpe(equity_curve):
    rets = []
    for j in range(1, len(equity_curve)):
        prev = equity_curve[j - 1]["equity"]
        if prev > 0:
            rets.append(equity_curve[j]["equity"] / prev - 1)
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = math.sqrt(var)
    return (mean * 365) / (std * math.sqrt(365)) if std > 0 else 0.0


def print_report(r):
    print("\n" + "=" * 72)
    print(f"  {r['strategy']}")
    print("=" * 72)
    print(f"  Period:          {r['period']}  ({r['years']}y)")
    print(f"  Final equity:    ${r['final_equity']:,.2f}   (start ${r['capital']:,.0f})")
    print(f"  Total return:    {r['total_return_pct']:+.1f}%")
    print(f"  CAGR:            {r['cagr_pct']:.1f}%")
    print(f"  Max drawdown:    {r['max_drawdown_pct']:.1f}%  ({r.get('max_drawdown_date','')})")
    print(f"  Sharpe:          {r['sharpe']:.2f}")
    if "closed_trades" in r:
        print(f"  Trades:          {r['closed_trades']}  |  WR {r['win_rate']}%  |  "
              f"PF {r.get('profit_factor', 0)}  |  avgW ${r.get('avg_win', 0)} / avgL ${r.get('avg_loss', 0)}")
    if r.get("yearly"):
        print("  Yearly:")
        for y in r["yearly"]:
            print(f"    {y['year']}: {y['return_pct']:+6.1f}%   eq=${y['equity']:,.0f}")
    if r.get("exit_reasons"):
        print("  Exits:")
        for k, v in sorted(r["exit_reasons"].items(), key=lambda x: -x[1]):
            print(f"    {k:18s} {v}")
    if "total_funding_pnl" in r:
        print(f"  Funding PnL:    ${r['total_funding_pnl']:,.2f}  "
              f"({r['funding_pct_of_capital']:+.2f}% of capital)")


def buy_and_hold_btc(daily_data, capital=10000.0):
    bars = daily_data["BTC"]
    start = 210
    if len(bars) <= start + 10:
        start = 50
    entry = bars[start]["C"] * (1 + SLIPPAGE)
    fee0 = capital * COMMISSION
    units = (capital - fee0) / entry
    curve = []
    for b in bars[start:]:
        eq = units * b["C"]
        curve.append({"date": b["date"], "equity": eq})
    exit_px = bars[-1]["C"] * (1 - SLIPPAGE)
    final = units * exit_px * (1 - COMMISSION)
    years = max(len(curve) / 365.25, 1e-9)
    peak = 0.0
    max_dd = 0.0
    for pt in curve:
        peak = max(peak, pt["equity"])
        dd = (peak - pt["equity"]) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    rets = []
    for j in range(1, len(curve)):
        if curve[j - 1]["equity"] > 0:
            rets.append(curve[j]["equity"] / curve[j - 1]["equity"] - 1)
    mean = sum(rets) / len(rets) if rets else 0
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) if rets else 0
    sharpe = (mean * 365) / (std * math.sqrt(365)) if std > 0 else 0
    return {
        "strategy": "BTC Buy & Hold",
        "period": f"{curve[0]['date']} → {curve[-1]['date']}",
        "years": round(years, 2),
        "capital": capital,
        "final_equity": round(final, 2),
        "total_return_pct": round((final / capital - 1) * 100, 1),
        "cagr_pct": round(((final / capital) ** (1 / years) - 1) * 100, 1),
        "max_drawdown_pct": round(max_dd, 1),
        "sharpe": round(sharpe, 2),
    }


def load_data():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cached = json.load(f)
        if all(c in cached.get("data", {}) for c in COINS):
            if all(len(cached["data"][c]) > 200 for c in COINS):
                print(f"  Using cache ({cached.get('fetched_at',0)})", flush=True)
                return cached["data"]
    raise RuntimeError("no cache — run honest_backtest_3y.py first to build data")


def load_funding():
    if os.path.exists(FUNDING_CACHE_PATH):
        with open(FUNDING_CACHE_PATH) as f:
            cached = json.load(f)
        if all(c in cached.get("data", {}) for c in COINS):
            return cached["data"]
    return None


def main():
    data = load_data()
    funding = load_funding()

    cfgs = [
        MDConfig(name="MACD+Donchian v1 (base)"),
        MDConfig(name="MACD+Donchian v2 (tighter chandelier, time exit 20d)",
                 chandelier_atr=2.0, max_hold_days=20, top_k=3),
        MDConfig(name="MACD+Donchian v3 (wide chandelier, no hard stop)",
                 chandelier_atr=4.0, hard_stop_atr=0.0, max_hold_days=45),
    ]

    results = []
    for cfg in cfgs:
        print(f"\n[run] {cfg.name} ...", flush=True)
        r = run_strategy(data, cfg, funding)
        print_report(r)
        results.append(r)

    bnh = buy_and_hold_btc(data)
    print_report(bnh)

    print("\n" + "=" * 72)
    print("COMPARISON (vs existing honest results for reference)")
    print("=" * 72)
    print(f"  {'Strategy':30s} {'Return':>8} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7} {'Trades':>7}")
    for r in results:
        print(f"  {r['strategy']:30s} {r['total_return_pct']:+7.1f}% {r['cagr_pct']:6.1f}% "
              f"{r['max_drawdown_pct']:6.1f}% {r['sharpe']:6.2f} {r.get('closed_trades', 0):7d}")
    print(f"  {'BTC Buy & Hold':30s} {bnh['total_return_pct']:+7.1f}% {bnh['cagr_pct']:6.1f}% "
          f"{bnh['max_drawdown_pct']:6.1f}% {bnh['sharpe']:6.2f}")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rules": {
            "signal": "bar T close (causal)",
            "entry": "bar T+1 open",
            "stops": "pessimistic H/L first",
            "costs": f"commission {COMMISSION*100:.2f}% + slippage {SLIPPAGE*100:.2f}% per side",
            "data": "Binance daily OHLCV proxy for OKX SWAP (BTC/ETH/BNB/SOL)",
            "look_ahead": False,
        },
        "strategies": [
            {k: v for k, v in r.items() if k not in ("equity_curve", "recent_trades", "config")}
            for r in results
        ],
        "full": results,
        "btc_buy_hold": bnh,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved → {OUT_PATH}")


if __name__ == "__main__":
    main()
