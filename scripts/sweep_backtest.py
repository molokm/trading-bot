"""Entry/exit knob sweep on the deterministic skeleton (backtest_lab).

Read-only research: prints a table, changes nothing.  Compare settings by
RELATIVE metrics (PF, DD, win-rate, Sharpe, PBO) — absolute net returns of a
skeleton are NOT comparable to live, which also runs the LLM layer.
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.services.backtest_lab import (  # noqa: E402
    BacktestParams, frame_from_candles, prepare, run_backtest, summarise_grid,
)

DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/candles_1h"
SYMS = sys.argv[2].split(",") if len(sys.argv) > 2 else [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "BCH", "OKB",
    "LINK", "AVAX", "ADA", "LTC", "DOT", "UNI", "NEAR",
]


def load(syms):
    dfs = {}
    for s in syms:
        p = os.path.join(DIR, f"{s}.json")
        if not os.path.exists(p):
            continue
        with open(p) as f:
            rows = json.load(f)
        if len(rows) < 200:
            continue
        dfs[s] = prepare(frame_from_candles(rows))
    return dfs


def show(label, results_by_sym):
    """Aggregate a dict {sym: BacktestResult} into one row."""
    tr = sum(r.trades for r in results_by_sym.values())
    if not tr:
        print(f"{label:52}  no trades")
        return None
    gw = sum(sum(t for t in r.block_returns if t > 0) for r in results_by_sym.values())
    # recompute PF from per-symbol aggregate of wins/losses is not stored;
    # approximate with net / (sum of |negative block returns|) is wrong, so we
    # weight by trades instead: use the mean PF across symbols that traded.
    pfs = [r.profit_factor for r in results_by_sym.values()
           if r.trades and np.isfinite(r.profit_factor)]
    pf = float(np.mean(pfs)) if pfs else float("nan")
    wr = sum(r.win_rate * r.trades for r in results_by_sym.values()) / tr
    dd = max(r.max_drawdown_pct for r in results_by_sym.values())
    sh = float(np.mean([r.sharpe for r in results_by_sym.values() if r.trades]))
    net = sum(r.net_return_pct for r in results_by_sym.values())
    print(f"{label:52}  n={tr:4d}  WR={wr*100:5.1f}%  PF={pf:5.2f}  "
          f"net={net:7.2f}%  maxDD={dd:6.2f}%  sharpe={sh:5.2f}")
    return dict(trades=tr, wr=wr, pf=pf, net=net, dd=dd, sharpe=sh)


def main():
    dfs = load(SYMS)
    print(f"symbols: {len(dfs)}  bars/symbol: {len(next(iter(dfs.values())))}\n")

    base = dict(min_adx=20.0, min_roc=0.15, atr_stop_mult=1.15, tp_r=2.0,
                max_hold_bars=24)

    print("=== A. current production settings ===")
    res = {}
    for s, df in dfs.items():
        res[s] = run_backtest(df, BacktestParams(**base), block_count=16)
    show("CURRENT adx20 roc0.15 stop1.15 tp2.0 hold24", res)

    print("\n=== B. entry quality: does a higher bar help? ===")
    for adx in (20, 24, 28, 32):
        for roc in (0.15, 0.30, 0.50):
            r = {}
            for s, df in dfs.items():
                r[s] = run_backtest(df, BacktestParams(
                    min_adx=float(adx), min_roc=roc,
                    atr_stop_mult=base["atr_stop_mult"], tp_r=base["tp_r"],
                    max_hold_bars=base["max_hold_bars"]), block_count=16)
            show(f"adx>={adx:<3} roc>={roc}", r)

    print("\n=== C. stop geometry (loss partial ~ tighter effective stop) ===")
    for stop in (0.7, 0.875, 1.0, 1.15, 1.4, 1.6):
        r = {}
        for s, df in dfs.items():
            r[s] = run_backtest(df, BacktestParams(
                min_adx=20.0, min_roc=0.15, atr_stop_mult=stop,
                tp_r=base["tp_r"], max_hold_bars=base["max_hold_bars"]),
                block_count=16)
        show(f"stop={stop}x ATR (tp fixed 2R of that stop)", r)

    print("\n=== D. take geometry (RR) ===")
    for tp in (1.5, 2.0, 2.5, 3.0):
        r = {}
        for s, df in dfs.items():
            r[s] = run_backtest(df, BacktestParams(
                min_adx=20.0, min_roc=0.15, atr_stop_mult=1.15, tp_r=tp,
                max_hold_bars=24), block_count=16)
        show(f"tp={tp}R", r)

    print("\n=== E. hold horizon ===")
    for h in (12, 24, 48, 96):
        r = {}
        for s, df in dfs.items():
            r[s] = run_backtest(df, BacktestParams(
                min_adx=20.0, min_roc=0.15, atr_stop_mult=1.15, tp_r=2.0,
                max_hold_bars=h), block_count=16)
        show(f"max_hold={h} bars", r)

    print("\n=== F. coin subsets (is breadth really adding?) ===")
    subsets = {
        "majors BTC,ETH": ["BTC", "ETH"],
        "+SOL,XRP": ["BTC", "ETH", "SOL", "XRP"],
        "current 14": list(dfs.keys()),
        "no DOGE,BCH,OKB,ADA": [s for s in dfs
                                if s not in ("DOGE", "BCH", "OKB", "ADA")],
        "top-vol SOL,XRP,DOGE,LINK,NEAR,ADA,AVAX": [
            s for s in dfs if s in ("SOL", "XRP", "DOGE", "LINK", "NEAR",
                                    "ADA", "AVAX")],
    }
    for name, syms in subsets.items():
        sub = {s: dfs[s] for s in syms if s in dfs}
        if not sub:
            continue
        r = {}
        for s, df in sub.items():
            r[s] = run_backtest(df, BacktestParams(**base), block_count=16)
        show(f"subset {name}", r)


if __name__ == "__main__":
    main()
