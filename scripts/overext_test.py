"""Is the entry too late? Skip entries when the move is already overextended.

Live observation (05–10 Nov): BTC shorts fired after a 7% drop and were stopped
out by the bounce; ETH was signalled at ROC3=-4.55%. A trend entry should join a
move, not chase its last leg. Two point-in-time filters are tested:

  * ROC3 cap  : skip when |ROC_3| > cap (3-bar move already too big)
  * EMA stretch: skip when |Close-EMA20| > k*ATR (price far from its mean)

Swept over both regime halves; a knob is only interesting when the sign is the
same in both halves. Read-only research.
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.services.backtest_lab import (  # noqa: E402
    FEE_ROUND_PCT, SLIPPAGE_PCT, WARMUP_BARS, BacktestParams, _signal,
    frame_from_candles, prepare,
)

DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/candles_1110"
SYMS = sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))


def load(sym, lo=0, hi=None):
    with open(os.path.join(DIR, f"{sym}.json")) as f:
        rows = json.load(f)
    rows = sorted(rows, key=lambda r: int(r[0]))
    rows = rows[lo:hi] if hi else rows[lo:]
    return prepare(frame_from_candles(rows))


def run(df, p, max_roc3=None, max_stretch_atr=None):
    n = len(df)
    trades = []
    i = WARMUP_BARS
    while i < n - 1:
        row = df.iloc[i]
        side = _signal(row, p)
        if side is None:
            i += 1
            continue
        atr = float(row["ATR_14"] or 0)
        entry = float(df.iloc[i + 1]["Open"])
        if atr <= 0 or entry <= 0:
            i += 1
            continue
        if max_roc3 is not None and abs(float(row["ROC_3"] or 0)) > max_roc3:
            i += 1
            continue
        if max_stretch_atr is not None:
            stretch = abs(float(row["Close"]) - float(row["EMA_20"])) / atr
            if stretch > max_stretch_atr:
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
        print(f"{label:46} no trades")
        return
    n = sum(r["n"] for r in ok)
    wr = sum(r["wr"] * r["n"] for r in ok) / n
    pf = float(np.mean([r["pf"] for r in ok]))
    net = sum(r["net"] for r in ok)
    exp = sum(r["exp"] for r in ok)
    print(f"{label:46} n={n:5d} WR={wr*100:5.1f}% PF={pf:5.2f} "
          f"net={net:8.2f}% avgExp={exp:6.2f}%")


def main():
    with open(os.path.join(DIR, "BTC.json")) as f:
        total = len(json.load(f))
    half = total // 2
    slices = {"full": (0, None), "older half": (0, half),
              "newer half": (half, total),
              "last 10d": (max(0, total - 240), None)}
    dfs = {k: {s: load(s, lo, hi) for s in SYMS} for k, (lo, hi) in slices.items()}
    p = BacktestParams(min_adx=24.0, min_roc=0.15, atr_stop_mult=1.15,
                       tp_r=2.0, max_hold_bars=24)

    print("=== A. ROC3 cap (skip entries after an already-large 3-bar move) ===")
    for name in slices:
        print(f"-- {name} --")
        for cap in (None, 3.0, 2.5, 2.0, 1.5):
            lbl = "no cap (current)" if cap is None else f"ROC3 <= {cap:.1f}%"
            row(lbl, [run(df, p, max_roc3=cap) for df in dfs[name].values()])
        print()

    print("=== B. EMA20 stretch cap (skip entries far from the mean) ===")
    for name in slices:
        print(f"-- {name} --")
        for k in (None, 3.0, 2.5, 2.0, 1.5, 1.0):
            lbl = "no cap (current)" if k is None else f"|close-EMA20| <= {k:.1f} ATR"
            row(lbl, [run(df, p, max_stretch_atr=k) for df in dfs[name].values()])
        print()


if __name__ == "__main__":
    main()
