"""Does "a higher ADX bar helps" survive a regime split?

Cuts every symbol's 1H series in half and re-runs the skeleton on each half
independently. A knob is only worth changing when the sign of its effect is
the same in both halves AND across most symbols — otherwise it is fitting one
regime.
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.services.backtest_lab import (  # noqa: E402
    BacktestParams, frame_from_candles, prepare, run_backtest,
)

DIR = "/tmp/candles_1h"
ADX = (20.0, 24.0, 28.0, 32.0)


def load(sym: str, lo: int = 0, hi: int | None = None):
    with open(os.path.join(DIR, f"{sym}.json")) as f:
        rows = json.load(f)
    rows = sorted(rows, key=lambda r: int(r[0]))
    rows = rows[lo:hi] if hi else rows[lo:]
    return prepare(frame_from_candles(rows))


def aggregate(syms, adx, lo, hi):
    """Sum trades / win-weighted WR / total net across symbols for one slice."""
    n = wr_n = net = 0
    pfs, dds, shs = [], [], []
    for s in syms:
        df = load(s, lo, hi)
        if len(df) < 200:
            continue
        r = run_backtest(df, BacktestParams(min_adx=adx, min_roc=0.15,
                                            atr_stop_mult=1.15, tp_r=2.0,
                                            max_hold_bars=24), block_count=8)
        n += r.trades
        wr_n += r.win_rate * r.trades
        net += r.net_return_pct
        if r.trades and np.isfinite(r.profit_factor):
            pfs.append(r.profit_factor)
        dds.append(r.max_drawdown_pct)
        shs.append(r.sharpe)
    if not n:
        return None
    return dict(n=n, wr=wr_n / n, pf=float(np.mean(pfs)), net=net,
                dd=float(np.mean(dds)), sh=float(np.mean(shs)))


def main():
    with open(os.path.join(DIR, "BTC.json")) as f:
        total = len(json.load(f))
    syms = sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))
    half = total // 2
    slices = {"first half (older)": (0, half), "second half (newer)": (half, total),
              "full": (0, None)}

    for name, (lo, hi) in slices.items():
        print(f"\n=== {name}  bars {lo}..{hi or total} ===")
        print(f"{'min_adx':>8} {'n':>5} {'WR':>6} {'PF':>6} {'net%':>9} "
              f"{'avgDD%':>8} {'sharpe':>7}")
        for adx in ADX:
            r = aggregate(syms, adx, lo, hi)
            if not r:
                continue
            print(f"{adx:8.0f} {r['n']:5d} {r['wr']*100:5.1f}% {r['pf']:6.2f} "
                  f"{r['net']:9.2f} {r['dd']:8.2f} {r['sh']:7.2f}")

    # per-symbol sign check on the full window: is PF monotone in ADX?
    print("\n=== per-symbol: PF at adx=20 vs adx=32 (full window) ===")
    better = same = worse = 0
    for s in syms:
        df = load(s)
        a = run_backtest(df, BacktestParams(min_adx=20.0, min_roc=0.15,
                                            atr_stop_mult=1.15, tp_r=2.0,
                                            max_hold_bars=24), block_count=8)
        b = run_backtest(df, BacktestParams(min_adx=32.0, min_roc=0.15,
                                            atr_stop_mult=1.15, tp_r=2.0,
                                            max_hold_bars=24), block_count=8)
        if not a.trades or not b.trades:
            continue
        if b.profit_factor > a.profit_factor + 0.02:
            better += 1
        elif b.profit_factor < a.profit_factor - 0.02:
            worse += 1
        else:
            same += 1
        print(f"  {s[:4]:5} PF20={a.profit_factor:5.2f}(n={a.trades:3d})  "
              f"PF32={b.profit_factor:5.2f}(n={b.trades:3d})  "
              f"{'BETTER' if b.profit_factor > a.profit_factor + 0.02 else ('WORSE' if b.profit_factor < a.profit_factor - 0.02 else 'flat')}")
    print(f"\n  adx32 better on {better}, flat {same}, worse on {worse}")


if __name__ == "__main__":
    main()
