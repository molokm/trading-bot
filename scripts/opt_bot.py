#!/usr/bin/env python3
"""Honest rotation backtest + grid-search optimizer for the new "tribo" bot.

Same honest rules as honest_backtest_3y.py but parameterised & grid-swept:
  signal @ bar T close (causal) -> entry @ T+1 open
  stops / partials checked against day HIGH/LOW first (pessimistic)
  risk-of-equity size, max leverage, cap margin by allocation% of equity
  optional portfolio-trailing global halt
  commission 0.10% + slippage 0.05% per side
  Reuses indicators / data / sizing helpers from honest_backtest_3y.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict

sys.path.insert(0, os.path.dirname(__file__))
import honest_backtest_3y as hb

COINS = hb.COINS
CT_VAL = hb.CT_VAL
LOT_SZ = hb.LOT_SZ
COMMISSION = hb.COMMISSION
SLIPPAGE = hb.SLIPPAGE


@dataclass
class Cfg:
    name: str = "tribo"
    # market
    symbols: list = field(default_factory=lambda: ["BTC", "ETH", "BNB", "SOL"])
    timeframe: str = "1d"
    # indicators
    roc_period: int = 30
    ema_fast: int = 20
    ema_slow: int = 50
    adx_period: int = 14
    adx_min: float = 30.0
    sma_long: int = 200
    min_roc: float = 5.0          # min |roc| to even rank a coin
    # risk
    top_k: int = 2
    allocation_pct: float = 0.70  # max total margin = eq*allocation
    portfolio_trailing: float = 0.20  # global halt if eq < peak*(1-x); 0=off
    min_hold_days: int = 7
    max_leverage: float = 3.0
    risk_pct: float = 0.02
    atr_stop_mult: float = 1.5
    trail_atr_mult: float = 0.5
    breakeven_pct: float = 0.03
    partial_tp_pct: float = 0.05
    partial_tp_ratio: float = 0.5
    rsi_long_max: float = 75.0
    rsi_short_min: float = 25.0
    vol_mult: float = 1.5
    corr_threshold: float = 0.7
    allow_short: bool = True
    capital: float = 10000.0


def run(data: dict, c: Cfg) -> dict:
    coins = c.symbols
    cd = {}
    for coin in coins:
        bars = data.get(coin, [])
        if len(bars) < 260:
            raise RuntimeError(f"{coin}: only {len(bars)} bars")
        closes = [b["C"] for b in bars]
        highs = [b["H"] for b in bars]
        lows = [b["L"] for b in bars]
        cd[coin] = {
            "bars": bars,
            "roc": hb.roc_series(closes, c.roc_period),
            "ema_f": hb.ema_series(closes, c.ema_fast),
            "ema_s": hb.ema_series(closes, c.ema_slow),
            "atr": hb.atr_series(highs, lows, closes, 14),
            "adx": hb.adx_series(highs, lows, closes, c.adx_period),
            "rsi": hb.rsi_series(closes, 14),
            "sma": hb.sma_series(closes, c.sma_long),
        }

    common = None
    for cc in cd.values():
        dates = {b["date"] for b in cc["bars"]}
        common = dates if common is None else (common & dates)
    all_dates = sorted(common)
    date_idx = {}
    for coin, cc in cd.items():
        date_idx[coin] = {b["date"]: i for i, b in enumerate(cc["bars"])}

    equity = c.capital
    peak_equity = c.capital
    flat_equity = equity * (1 - c.portfolio_trailing * 0.6) if c.portfolio_trailing > 0 else 0.0
    positions = {}
    trades = []
    eq = []
    filters = defaultdict(int)
    start_i = max(c.ema_slow + 20, c.sma_long + 10)

    for i in range(start_i, len(all_dates)):
        date = all_dates[i]
        prev = all_dates[i - 1]

        mtm = {}
        for coin in coins:
            ci = date_idx[coin][date]
            mtm[coin] = cd[coin]["bars"][ci]["C"]

        # manage open positions on TODAY (H/L first)
        for coin in list(positions.keys()):
            pos = positions[coin]
            ci = date_idx[coin][date]
            bar = cd[coin]["bars"][ci]
            ct = CT_VAL[coin]
            trail = pos["atr"] * c.trail_atr_mult if pos["atr"] > 0 else pos["entry"] * 0.02
            hit = False
            exit_raw = None
            if pos["side"] == "long":
                if bar["L"] <= pos["stop"]:
                    hit, exit_raw = True, pos["stop"]
                if not hit:
                    if bar["H"] > pos["peak"]:
                        pos["peak"] = bar["H"]
                        if pos["peak"] - trail > pos["stop"]:
                            pos["stop"] = pos["peak"] - trail
                    if not pos["be"] and bar["C"] >= pos["entry"] * (1 + c.breakeven_pct):
                        pos["stop"] = max(pos["stop"], pos["entry"] * 0.999); pos["be"] = True
            else:
                if bar["H"] >= pos["stop"]:
                    hit, exit_raw = True, pos["stop"]
                if not hit:
                    if bar["L"] < pos["peak"]:
                        pos["peak"] = bar["L"]
                        if pos["peak"] + trail < pos["stop"]:
                            pos["stop"] = pos["peak"] + trail
                    if not pos["be"] and bar["C"] <= pos["entry"] * (1 - c.breakeven_pct):
                        pos["stop"] = min(pos["stop"], pos["entry"] * 1.001); pos["be"] = True

            if not hit and not pos["pt"] and c.partial_tp_pct > 0:
                tp = pos["entry"] * (1 + c.partial_tp_pct) if pos["side"] == "long" else pos["entry"] * (1 - c.partial_tp_pct)
                trig = bar["H"] >= tp if pos["side"] == "long" else bar["L"] <= tp
                if trig:
                    cl = math.floor(pos["size"] * c.partial_tp_ratio / LOT_SZ[coin] + 1e-12) * LOT_SZ[coin]
                    if 0 < cl < pos["size"]:
                        fill = tp * (1 - SLIPPAGE) if pos["side"] == "long" else tp * (1 + SLIPPAGE)
                        pnl = (cl * ct * (fill - pos["entry"])) if pos["side"] == "long" else (cl * ct * (pos["entry"] - fill))
                        pnl -= cl * ct * fill * COMMISSION
                        equity += pnl; pos["size"] -= cl; pos["pt"] = True
                        filters["partial_tp"] += 1

            if hit:
                fill = exit_raw * (1 - SLIPPAGE) if pos["side"] == "long" else exit_raw * (1 + SLIPPAGE)
                pnl = (pos["size"] * ct * (fill - pos["entry"])) if pos["side"] == "long" else (pos["size"] * ct * (pos["entry"] - fill))
                pnl -= pos["size"] * ct * fill * COMMISSION
                equity += pnl
                del positions[coin]

        unreal = _unreal(positions, mtm)
        now_eq = equity + unreal
        peak_equity = max(peak_equity, now_eq)

        # global portfolio-trailing halt
        if c.portfolio_trailing > 0 and peak_equity > 0 and now_eq < peak_equity * (1 - c.portfolio_trailing):
            for coin in list(positions.keys()):
                pos = positions[coin]
                ci = date_idx[coin][date]
                er = cd[coin]["bars"][ci]["C"]
                ct = CT_VAL[coin]
                pnl = (pos["size"] * ct * (er - pos["entry"])) if pos["side"] == "long" else (pos["size"] * ct * (pos["entry"] - er))
                pnl -= pos["size"] * ct * er * COMMISSION
                equity += pnl
            positions = {}
            flat_equity = peak_equity * (1 - c.portfolio_trailing * 0.6)
            eq.append({"date": date, "equity": equity})
            continue

        # rotation cadence
        if c.min_hold_days > 0 and positions and i - last_entry(positions) < c.min_hold_days:
            eq.append({"date": date, "equity": equity + _unreal(positions, mtm)})
            continue

        # while recovering from a halt, stay flat
        if c.portfolio_trailing > 0 and equity < flat_equity:
            eq.append({"date": date, "equity": equity + _unreal(positions, mtm)})
            continue

        # BTC 200SMA regime
        bsym = "BTC" if "BTC" in cd else coins[0]
        bs = date_idx[bsym][prev]
        btc_above = True
        if cd[bsym]["sma"][bs] > 0:
            btc_above = cd[bsym]["bars"][bs]["C"] > cd[bsym]["sma"][bs]

        ranked = []
        for coin in coins:
            cc = cd[coin]
            si = date_idx[coin][prev]
            atr = cc["atr"][si]
            if atr <= 0:
                continue
            atr_slice = [cc["atr"][j] for j in range(max(0, si - 29), si + 1) if cc["atr"][j] > 0]
            avg = sum(atr_slice) / len(atr_slice) if atr_slice else 0.0
            if avg > 0 and atr > avg * c.vol_mult:
                filters["volatility"] += 1; continue
            ema_trend = cc["ema_f"][si] > cc["ema_s"][si]
            rsi = cc["rsi"][si]
            roc = cc["roc"][si]
            adx = cc["adx"][si]
            if rsi > c.rsi_long_max and ema_trend:
                filters["rsi_over"] += 1; continue
            if rsi < c.rsi_short_min and not ema_trend:
                filters["rsi_under"] += 1; continue
            if not btc_above and ema_trend and roc > 0:
                filters["bear_long"] += 1; continue
            if abs(roc) < c.min_roc:
                filters["min_roc"] += 1; continue
            trend_val = ((cc["ema_f"][si] - cc["ema_s"][si]) / cc["ema_s"][si] * 100) if cc["ema_s"][si] > 0 else 0.0
            score = roc * 0.5 + trend_val * 0.3 + (adx / 50) * 0.2
            rets = [cc["bars"][j]["C"] / cc["bars"][j - 1]["C"] - 1 for j in range(max(1, si - 29), si + 1) if cc["bars"][j - 1]["C"] > 0]
            ranked.append({"coin": coin, "score": score, "roc": roc, "ema_trend": ema_trend, "adx": adx, "atr": atr, "rets": rets})

        ranked.sort(key=lambda x: x["score"], reverse=True)
        targets = []
        for row in ranked:
            if len(targets) >= c.top_k:
                break
            if row["roc"] > c.min_roc and row["ema_trend"] and row["adx"] >= c.adx_min:
                side = "long"
            elif c.allow_short and row["roc"] < -c.min_roc and not row["ema_trend"] and row["adx"] >= c.adx_min:
                side = "short"
            else:
                continue
            ok = True
            for held in ([positions[cc]["rets"] for cc in positions] + [t["rets"] for t in targets]):
                if held and abs(hb.correlation(row["rets"], held)) > c.corr_threshold:
                    ok = False; filters["correlation"] += 1; break
            if not ok:
                continue
            targets.append({"symbol": row["coin"], "side": side, "atr": row["atr"], "rets": row["rets"]})

        tset = {(t["symbol"], t["side"]) for t in targets}

        # rotate-out at today open
        for coin in list(positions.keys()):
            pos = positions[coin]
            if (coin, pos["side"]) in tset:
                continue
            ci = date_idx[coin][date]
            fill = cd[coin]["bars"][ci]["O"]
            ct = CT_VAL[coin]
            pnl = (pos["size"] * ct * (fill - pos["entry"])) if pos["side"] == "long" else (pos["size"] * ct * (pos["entry"] - fill))
            pnl -= pos["size"] * ct * fill * COMMISSION
            equity += pnl
            del positions[coin]

        # open new
        used_margin = sum(pos["size"] * CT_VAL[pos["symbol"]] * mtm[pos["symbol"]] for pos in positions.values())
        for t in targets:
            coin, side = t["symbol"], t["side"]
            if coin in positions:
                continue
            ci = date_idx[coin][date]
            entry_raw = cd[coin]["bars"][ci]["O"]
            atr = t["atr"]
            lev = hb.dynamic_leverage(atr, entry_raw, c.max_leverage)
            stop_dist = atr * c.atr_stop_mult
            fill = entry_raw * (1 + SLIPPAGE) if side == "long" else entry_raw * (1 - SLIPPAGE)
            stop = fill - stop_dist if side == "long" else fill + stop_dist
            sz = hb.calc_size(equity, coin, fill, stop_dist, lev, c.risk_pct, c.allocation_pct)
            notional = sz * CT_VAL[coin] * fill
            if used_margin + notional > equity * c.allocation_pct:
                filters["allocation"] += 1
                continue
            fee = sz * CT_VAL[coin] * fill * COMMISSION
            equity -= fee
            used_margin += notional
            positions[coin] = {"side": side, "size": sz, "entry": fill, "stop": stop, "peak": fill, "atr": atr, "be": False, "pt": False, "entry_i": i, "symbol": coin, "rets": t["rets"]}

        eq.append({"date": date, "equity": equity + _unreal(positions, mtm)})

    # close leftovers at last close
    if positions and all_dates:
        date = all_dates[-1]
        for coin in list(positions.keys()):
            pos = positions[coin]
            ci = date_idx[coin][date]
            fill = cd[coin]["bars"][ci]["C"]
            ct = CT_VAL[coin]
            pnl = (pos["size"] * ct * (fill - pos["entry"])) if pos["side"] == "long" else (pos["size"] * ct * (pos["entry"] - fill))
            pnl -= pos["size"] * ct * fill * COMMISSION
            equity += pnl
        if eq:
            eq[-1]["equity"] = equity

    return _summ(c, eq, equity)


def last_entry(pos):
    return max((p["entry_i"] for p in pos.values()), default=-10**9)


def _unreal(pos, mtm):
    u = 0.0
    for c, p in pos.items():
        ct = CT_VAL[c]
        cur = mtm.get(c)
        if cur is None:
            continue
        u += p["size"] * ct * (cur - p["entry"]) if p["side"] == "long" else p["size"] * ct * (p["entry"] - cur)
    return u


def _summ(c, eq, final):
    cap = c.capital
    years = max(len(eq) / 365.25, 1e-9)
    tr = (final / cap - 1) * 100
    cagr = (final / cap) ** (1 / years) - 1 if final > 0 else -1
    peak = cap; mdd = 0.0
    for pt in eq:
        peak = max(peak, pt["equity"])
        dd = (peak - pt["equity"]) / peak * 100 if peak > 0 else 0
        mdd = max(mdd, dd)
    return {
        "strategy": c.name, "config": asdict(c),
        "years": round(years, 2), "final": round(final, 2),
        "return_pct": round(tr, 1), "cagr_pct": round(cagr * 100, 1),
        "max_dd_pct": round(mdd, 1),
    }


def _worker(data, cfg):
    try:
        return run(data, cfg)
    except Exception:
        return None


def print_r(r):
    print(f"  {r['strategy']:12s} ret={r['return_pct']:+7.1f}%  CAGR={r['cagr_pct']:+6.1f}%  "
          f"DD={r['max_dd_pct']:5.1f}%  cfg={json.dumps({k: v for k, v in r['config'].items() if k not in ('symbols', 'capital', 'timeframe')})}")


def main():
    force = "--refresh" in sys.argv
    if "--runone" in sys.argv:
        import json as _json
        c = Cfg()
        d = asyncio.run(hb.load_data(force_refresh=force))
        r = run(d, c)
        print_r(r)
        return

    # grid search
    data = asyncio.run(hb.load_data(force_refresh=force))
    base = dict(
        roc_period=30, ema_fast=20, ema_slow=50, adx_period=14, adx_min=30.0,
        sma_long=200, min_roc=5.0, top_k=2, allocation_pct=0.70,
        portfolio_trailing=0.20, min_hold_days=7, max_leverage=3.0, risk_pct=0.02,
        atr_stop_mult=1.5, trail_atr_mult=0.5, breakeven_pct=0.03,
        partial_tp_pct=0.05, partial_tp_ratio=0.5, allow_short=True,
    )
    # sweep key knobs around the user-provided config
    knobs = {
        "min_hold_days": [10, 15, 20, 30],
        "adx_min": [0.0, 25.0],
        "min_roc": [3.0, 5.0, 8.0],
        "roc_period": [14, 30],
        "top_k": [1, 2],
        "risk_pct": [0.08, 0.10, 0.12, 0.15],
        "allocation_pct": [1.0],
        "portfolio_trailing": [0.0],
        "allow_short": [True],
        "max_leverage": [2.0],
        "trail_atr_mult": [0.2, 0.3, 0.5],
        "atr_stop_mult": [2.0, 2.5, 3.0],
        "partial_tp_pct": [0.05],
        "breakeven_pct": [0.02, 0.05],
        "ema_fast": [20],
        "ema_slow": [50],
    }
    sweep_keys = list(knobs.keys())
    combos = list(itertools.product(*knobs.values()))
    total = len(combos)
    print(f"grid total: {total}", flush=True)
    workers = min(8, os.cpu_count() or 4)
    row0 = combos[0]

    def to_cfg(combo):
        kv = dict(zip(sweep_keys, combo))
        return Cfg(
            roc_period=kv["roc_period"], adx_min=kv["adx_min"], min_roc=kv["min_roc"],
            min_hold_days=kv["min_hold_days"], top_k=kv["top_k"], risk_pct=kv["risk_pct"],
            allocation_pct=kv["allocation_pct"], portfolio_trailing=kv["portfolio_trailing"],
            allow_short=kv["allow_short"], max_leverage=kv["max_leverage"],
            trail_atr_mult=kv["trail_atr_mult"], atr_stop_mult=kv["atr_stop_mult"],
            partial_tp_pct=kv["partial_tp_pct"], breakeven_pct=kv["breakeven_pct"],
            ema_fast=kv["ema_fast"], ema_slow=kv["ema_slow"], partial_tp_ratio=0.5,
        )

    import multiprocessing as mp
    from functools import partial
    ctx = mp.get_context("spawn")
    start = time.time()
    with ctx.Pool(workers) as pool:
        results = list(pool.imap_unordered(
            partial(_worker, data),
            [to_cfg(c) for c in combos], chunksize=4))
    results = [r for r in results if r is not None]
    elapsed = time.time() - start
    print(f"\nSwept {len(results)} configs in {elapsed:.1f}s")
    # filter to DD <= 40, sort by CAGR
    ok = [r for r in results if r["max_dd_pct"] <= 40.0]
    ok.sort(key=lambda r: r["cagr_pct"], reverse=True)
    print(f"Configs with DD<=40%: {len(ok)}\n")
    print("TOP 20 by CAGR (DD<=40%):")
    for r in ok[:20]:
        print_r(r)
    # also top by raw CAGR regardless of DD
    results.sort(key=lambda r: r["cagr_pct"], reverse=True)
    print("\nTOP raw CAGR (any DD):")
    for r in results[:10]:
        print_r(r)


if __name__ == "__main__":
    main()