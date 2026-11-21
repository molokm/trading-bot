"""Is the breadth gate's "core = BTC,ETH" the right restriction?

The live config falls back to BTC/ETH only when <breadth_min_frac of coins
trend. If BTC/ETH are the *worst* per-unit edge, that fallback trades the
weakest names exactly when the regime is thin. Checked per regime half so a
single 60-day window cannot decide it.
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
CORE = ("BTC", "ETH")
P = BacktestParams(min_adx=20.0, min_roc=0.15, atr_stop_mult=1.15,
                   tp_r=2.0, max_hold_bars=24)


def load(sym, lo=0, hi=None):
    with open(os.path.join(DIR, f"{sym}.json")) as f:
        rows = json.load(f)
    rows = sorted(rows, key=lambda r: int(r[0]))
    rows = rows[lo:hi] if hi else rows[lo:]
    return prepare(frame_from_candles(rows))


def agg(syms, lo, hi):
    n = wr_n = net = 0
    pfs, dds = [], []
    for s in syms:
        df = load(s, lo, hi)
        if len(df) < 200:
            continue
        r = run_backtest(df, P, block_count=8)
        n += r.trades
        wr_n += r.win_rate * r.trades
        net += r.net_return_pct
        if r.trades and np.isfinite(r.profit_factor):
            pfs.append(r.profit_factor)
        dds.append(r.max_drawdown_pct)
    if not n:
        return None
    return dict(n=n, wr=wr_n / n, pf=float(np.mean(pfs)), net=net,
                dd=float(np.mean(dds)))


def main():
    with open(os.path.join(DIR, "BTC.json")) as f:
        total = len(json.load(f))
    syms = sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))
    rest = [s for s in syms if s not in CORE]
    half = total // 2
    slices = {"older half": (0, half), "newer half": (half, total),
              "full": (0, None)}

    print("breadth fallback core (BTC,ETH) vs the rest\n")
    print(f"{'slice':<12} {'group':<10} {'n':>5} {'WR':>6} {'PF':>6} "
          f"{'net%':>9} {'avgDD%':>8}")
    for name, (lo, hi) in slices.items():
        for gname, group in (("BTC,ETH", CORE), ("rest", rest),
                             ("all", syms)):
            r = agg(group, lo, hi)
            if not r:
                continue
            print(f"{name:<12} {gname:<10} {r['n']:5d} {r['wr']*100:5.1f}% "
                  f"{r['pf']:6.2f} {r['net']:9.2f} {r['dd']:8.2f}")
        print()

    # Does BTC itself need a HIGHER or LOWER ADX bar than the alts?
    print("BTC vs alts: PF at adx=20 / 28 / 32 (full window)")
    for label, group in (("BTC", ["BTC"]), ("ETH", ["ETH"]), ("rest", rest)):
        for adx in (20.0, 28.0, 32.0):
            p = BacktestParams(min_adx=adx, min_roc=0.15, atr_stop_mult=1.15,
                               tp_r=2.0, max_hold_bars=24)
            n = wr_n = 0
            pfs = []
            for s in group:
                df = load(s)
                r = run_backtest(df, p, block_count=8)
                n += r.trades
                wr_n += r.win_rate * r.trades
                if r.trades and np.isfinite(r.profit_factor):
                    pfs.append(r.profit_factor)
            if n:
                print(f"  {label:<5} adx={adx:4.0f} n={n:4d} "
                      f"WR={wr_n/n*100:5.1f}% PF={np.mean(pfs):5.2f}")


if __name__ == "__main__":
    main()
