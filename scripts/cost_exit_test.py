"""Two candidate improvements, each checked on BOTH regime halves.

1) COST GATE: production vetoes an entry when ATR1H% < round_trip × 4 = 0.56%.
   Round trip is modelled as 0.14% (taker both legs) but the live book pays
   ~0.065% (post-only maker entry). Sweep the threshold down and see whether
   the extra trades it unlocks were actually worth taking.

2) EXITS: is max_hold 18-24h / tp ~2R cutting winners? Sweep tp and hold on
   the same skeleton (adx=24, stop=1.15) as production.

Read-only: prints tables, changes nothing.  Compare RELATIVE metrics only —
absolute skeleton net is not comparable to live (live also runs the LLM layer).
"""
import json
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.services.backtest_lab import (  # noqa: E402
    FEE_ROUND_PCT, SLIPPAGE_PCT, WARMUP_BARS, BacktestParams, _signal,
    frame_from_candles, prepare,
)

DIR = "/tmp/candles_1h"
SYMS = sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))


def load(sym, lo=0, hi=None):
    with open(os.path.join(DIR, f"{sym}.json")) as f:
        rows = json.load(f)
    rows = sorted(rows, key=lambda r: int(r[0]))
    rows = rows[lo:hi] if hi else rows[lo:]
    return prepare(frame_from_candles(rows))


def run(df, p, min_atr_pct=None):
    """run_backtest + optional cost-gate filter (ATR1H% >= min_atr_pct)."""
    n = len(df)
    trades = []
    i = WARMUP_BARS
    while i < n - 1:
        side = _signal(df.iloc[i], p)
        if side is None:
            i += 1
            continue
        atr = float(df.iloc[i]["ATR_14"] or 0)
        close = float(df.iloc[i]["Close"] or 0)
        entry = float(df.iloc[i + 1]["Open"])
        if atr <= 0 or entry <= 0:
            i += 1
            continue
        if min_atr_pct is not None and close > 0 and (atr / close * 100.0) < min_atr_pct:
            i += 1
            continue
        stop_dist = atr * p.atr_stop_mult
        if side == "long":
            entry_px = entry * (1 + SLIPPAGE_PCT)
            stop, take = entry_px - stop_dist, entry_px + stop_dist * p.tp_r
        else:
            entry_px = entry * (1 - SLIPPAGE_PCT)
            stop, take = entry_px + stop_dist, entry_px - stop_dist * p.tp_r
        exit_px = None
        end = min(n - 1, i + p.max_hold_bars)
        for j in range(i + 1, end + 1):
            hi, lo = float(df.iloc[j]["High"]), float(df.iloc[j]["Low"])
            if side == "long":
                if lo <= stop:
                    exit_px = stop
                    break
                if hi >= take:
                    exit_px = take
                    break
            else:
                if hi >= stop:
                    exit_px = stop
                    break
                if lo <= take:
                    exit_px = take
                    break
        if exit_px is None:
            exit_px = float(df.iloc[end]["Close"])
        if exit_px <= 0:
            i += 1
            continue
        if side == "long":
            ret = (exit_px * (1 - SLIPPAGE_PCT) - entry_px) / entry_px
        else:
            ret = (entry_px - exit_px * (1 + SLIPPAGE_PCT)) / entry_px
        trades.append(ret - FEE_ROUND_PCT)
        i = max(j + 1, i + 1)
    return stats(trades)


def stats(trades):
    if not trades:
        return None
    w = [t for t in trades if t > 0]
    l = [-t for t in trades if t < 0]
    pf = (sum(w) / sum(l)) if sum(l) > 0 else float("inf")
    return dict(n=len(trades), wr=len(w) / len(trades), pf=pf,
                net=sum(trades) * 100.0, exp=float(np.mean(trades)) * 100.0)


def row(label, results):
    ok = [r for r in results if r]
    if not ok:
        print(f"{label:44} no trades")
        return
    n = sum(r["n"] for r in ok)
    wr = sum(r["wr"] * r["n"] for r in ok) / n
    pf = float(np.mean([r["pf"] for r in ok]))
    net = sum(r["net"] for r in ok)
    exp = sum(r["exp"] for r in ok)
    print(f"{label:44} n={n:5d} WR={wr*100:5.1f}% PF={pf:5.2f} "
          f"net={net:8.2f}% avgExp={exp:6.2f}%")


def main():
    with open(os.path.join(DIR, "BTC.json")) as f:
        total = len(json.load(f))
    half = total // 2
    slices = {"full": (0, None), "older half": (0, half), "newer half": (half, total)}
    dfs = {name: {s: load(s, lo, hi) for s in SYMS} for name, (lo, hi) in slices.items()}

    base = dict(min_adx=24.0, min_roc=0.15, atr_stop_mult=1.15)

    print("=== 1. COST GATE: max(need%) threshold, tp2.0 hold24 ===")
    print("(production = 0.56% on a 0.14% model; measured live round trip "
          "0.065% => ~0.26%)")
    thr_list = [0.56, 0.40, 0.28, 0.26, 0.20, 0.14, 0.0]
    for name, part in slices.items():
        print(f"-- {name} --")
        for thr in thr_list:
            p = BacktestParams(**base, tp_r=2.0, max_hold_bars=24)
            row(f"ATR1H% >= {thr:.2f}",
                [run(df, p, min_atr_pct=thr or None) for df in dfs[name].values()])
        print()

    print("=== 2. EXITS: tp x hold (no cost gate) ===")
    for name, part in slices.items():
        print(f"-- {name} --")
        for tp in (2.0, 3.0):
            for h in (24, 48):
                p = BacktestParams(**base, tp_r=tp, max_hold_bars=h)
                row(f"tp={tp}R hold={h}",
                    [run(df, p) for df in dfs[name].values()])
        print()


if __name__ == "__main__":
    main()
