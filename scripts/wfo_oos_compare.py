#!/usr/bin/env python3
"""Honest OOS comparison: base (adx22, roc3) vs proposed (adx19, roc2).
Same windows as walkforward_v3: 12mo train skipped, test on 6mo OOS windows.
Both configs evaluated on untouched OOS windows only (no param selection).
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from honest_backtest_3y import (V3_LIVE, StratConfig, load_data, load_funding, run_strategy)
from walkforward_v3 import (TRAIN_LEN, TEST_LEN, SHIFT, WARM_BARS)


def make_cfg(**over):
    base = V3_LIVE.__dict__.copy()
    base.update(over)
    base["name"] = f"OOS {over.get('adx_min','?')}/{over.get('min_roc','?')}"
    return StratConfig(**base)


async def main():
    data = await load_data(force_refresh="--refresh" in sys.argv)
    funding = await load_funding(force_refresh="--refresh" in sys.argv)

    dates = [c["date"] for c in data["BTC"]]
    n = len(dates)
    windows = []
    t0 = WARM_BARS
    while t0 + TRAIN_LEN + TEST_LEN <= n:
        tr_end = t0 + TRAIN_LEN - 1
        te_start = tr_end + 1
        te_end = te_start + TEST_LEN - 1
        windows.append((te_start, te_end))
        t0 += SHIFT

    CFGS = {
        "BASE  adx22/roc3": dict(adx_min=22.0, min_roc=3.0),
        "PROP  adx19/roc2": dict(adx_min=19.0, min_roc=2.0),
        "MID   adx19/roc3": dict(adx_min=19.0, min_roc=3.0),
        "MID2  adx22/roc2": dict(adx_min=22.0, min_roc=2.0),
    }

    print(f"\n  OOS windows: {len(windows)}")
    for i, (a, b) in enumerate(windows):
        print(f"    W{i+1}: test {dates[a]}..{dates[b]}")

    print(f"\n  {'Config':20s} {'Win':>4s} {'OOS Sharpe':>11s} {'OOS Ret':>9s} {'OOS DD':>7s} {'Trades':>7s}")
    agg = {k: [] for k in CFGS}
    for name, over in CFGS.items():
        for wi, (a, b) in enumerate(windows):
            cfg = make_cfg(**over)
            r = run_strategy(data, cfg, funding, window=(dates[a], dates[b]))
            agg[name].append(r)
            print(f"  {name:20s} W{wi+1:>3} {r['sharpe']:>10.2f} {r['total_return_pct']:>8.1f}% "
                  f"{-r['max_drawdown_pct']:>6.1f}% {r['closed_trades']:>7d}")

    print(f"\n  {'Config':20s} {'AvgOOS Sharpe':>14s} {'Profitable':>11s} {'AvgOOS Ret':>10s}")
    for name, rs in agg.items():
        avg_sh = sum(r["sharpe"] for r in rs) / len(rs)
        prof = sum(1 for r in rs if r["total_return_pct"] > 0)
        avg_ret = sum(r["total_return_pct"] for r in rs) / len(rs)
        print(f"  {name:20s} {avg_sh:>13.2f} {prof:>3d}/{len(rs)} {avg_ret:>9.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
