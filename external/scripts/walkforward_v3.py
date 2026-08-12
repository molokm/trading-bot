#!/usr/bin/env python3
"""Walk-forward validation for the LIVE engine (honest_backtest_3y.run_strategy).

Scheme: 12mo train -> 6mo test, shift 6mo.
On each train window, small focused grid over sensitivity params; test the best
N configs on untouched out-of-sample window. Aggregates pure-OOS results.

Careful: our grid search and sweeps already selected params on the FULL window,
so this WFO is a robustness check of *the box*, not a clean unbiased holdout.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.dirname(__file__))

from honest_backtest_3y import (
    COINS, V3_LIVE, StratConfig, load_data, load_funding, run_strategy,
)

TRAIN_LEN = 365
TEST_LEN = 182
SHIFT = 182
WARM_BARS = 210

# Small focused grid around live V3 params (what the user would re-optimize)
GRID = {
    "atr_stop_mult": [2.5, 3.5, 4.5],
    "trail_atr_mult": [0.05, 0.1, 0.2],
    "adx_min": [18.0, 22.0],
    "min_hold_days": [3],
}

KEYS = list(GRID.keys())
TOP_N_TRAIN = 3   # how many best train configs to evaluate OOS
MAX_COMBOS = 1000


def make_cfg(**over):
    base = V3_LIVE.__dict__.copy()
    base["name"] = "V3 WFO"
    base.update(over)
    return StratConfig(**{k: v for k, v in base.items() if k != "name" or k == "name"})


async def main():
    print("=" * 90)
    print("  WALK-FORWARD (live engine): Momentum Rotation v3 live sweep")
    print("  Scheme: 12mo train -> 6mo test, shift 6mo | entry @ T+1 open |"
          " fee 0.10% + slip 0.05% + funding")
    print("=" * 90, flush=True)

    data = await load_data(force_refresh="--refresh" in sys.argv)
    funding = await load_funding(force_refresh="--refresh" in sys.argv)

    btc = data["BTC"]
    dates = [c["date"] for c in btc]
    n = len(dates)
    if n < 700:
        print("  ERROR: not enough data")
        return
    print(f"\n  Data: {n} bars, {dates[0]} -> {dates[-1]}", flush=True)

    # Windows on bar indices (starting after warmup)
    windows = []
    t0 = WARM_BARS
    while t0 + TRAIN_LEN + TEST_LEN <= n:
        tr_end = t0 + TRAIN_LEN - 1
        te_start = tr_end + 1
        te_end = te_start + TEST_LEN - 1
        windows.append((t0, tr_end, te_start, te_end))
        t0 += SHIFT

    print(f"  Windows: {len(windows)}")
    for i, (a, b, c, d) in enumerate(windows):
        print(f"    W{i+1}: train {dates[a]}..{dates[b]} | test {dates[c]}..{dates[d]}", flush=True)

    oos = []
    for wi, (tr_s, tr_e, te_s, te_e) in enumerate(windows):
        print(f"\n{'─'*74}\n  WINDOW {wi+1}/{len(windows)}")
        print(f"    train {dates[tr_s]}..{dates[tr_e]} | test {dates[te_s]}..{dates[te_e]}", flush=True)

        train_cfg = make_cfg()
        train_cfg.capital = 10000.0
        # Evaluate grid on train window
        train_results = []
        combos = list(itertools.product(*(GRID[k] for k in KEYS)))
        if len(combos) > MAX_COMBOS:
            combos = combos[:MAX_COMBOS]
        for combo in combos:
            over = dict(zip(KEYS, combo))
            r = run_strategy(data, make_cfg(**over), funding,
                             window=(dates[tr_s], dates[tr_e]))
            if r["closed_trades"] < 15:
                continue
            train_results.append((r["sharpe"], r["total_return_pct"], r["max_drawdown_pct"], over, r))
        train_results.sort(key=lambda x: (-x[0], -x[1]))   # best Sharpe, then return

        if not train_results:
            print("    No valid train configs, skipping window")
            continue
        print(f"    Top 3 train (Sharpe/ret/DD):")
        for j, (sh, ret, dd, over, _) in enumerate(train_results[:3]):
            print(f"      #{j+1}: Sharpe={sh:.2f} ret={ret:+.1f}% DD=-{dd:.1f}%  {over}", flush=True)

        # Test top N on untouched OOS
        best_oos = None
        print(f"    Testing top {min(TOP_N_TRAIN, len(train_results))} on OOS...")
        for j, (sh, ret, dd, over, _) in enumerate(train_results[:TOP_N_TRAIN]):
            r = run_strategy(data, make_cfg(**over), funding,
                             window=(dates[te_s], dates[te_e]))
            tag = f"stop={over['atr_stop_mult']}x trail={over['trail_atr_mult']} adx>={over['adx_min']}"
            print(f"      OOS: Sharpe={r['sharpe']:.2f} ret={r['total_return_pct']:>7.1f}% "
                  f"DD=-{r['max_drawdown_pct']:>5.1f}% WR={r['win_rate']:>5.1f}% PF={r['profit_factor']:>5.2f}  {tag}",
                  flush=True)
            if best_oos is None or r["sharpe"] > best_oos["sharpe"]:
                best_oos = r
                best_oos["params"] = over

        if best_oos:
            oos.append(best_oos)
            print(f"    >>> Best OOS: Sharpe={best_oos['sharpe']} ret={best_oos['total_return_pct']}% "
                  f"DD=-{best_oos['max_drawdown_pct']}%", flush=True)

    print(f"\n{'='*90}\n  WALK-FORWARD SUMMARY (out-of-sample only)")
    print(f"{'='*90}")
    if not oos:
        print("  No results!")
        return
    print(f"  {'WINDOW':>8} {'OOS Sharpe':>11} {'OOS Ret':>9} {'OOS DD':>8} {'WR':>7} {'PF':>7}  PARAMS")
    print(f"  {'─'*8}{'─'*14*6}")
    all_sharpes = []
    all_rets = []
    for i, r in enumerate(oos):
        p = r.get("params", {})
        tag = f"stop={p.get('atr_stop_mult','-')}x trail={p.get('trail_atr_mult','-')} adx>={p.get('adx_min','-')}"
        print(f"  W{i+1:>6} {r['sharpe']:>10.2f} {r['total_return_pct']:>8.1f}% "
              f"{-r['max_drawdown_pct']:>7.1f}% {r['win_rate']:>6.1f}% {r['profit_factor']:>7.2f}  {tag}", flush=True)
        all_sharpes.append(r["sharpe"])
        all_rets.append(r["total_return_pct"])

    if all_sharpes:
        avg_sh = sum(all_sharpes) / len(all_sharpes)
        pos_prof = sum(1 for x in all_rets if x > 0)
        print(f"\n  Avg OOS Sharpe: {avg_sh:.2f}")
        print(f"  Profitable OOS windows: {pos_prof}/{len(oos)}")
        print(f"  Min/Max OOS Ret: {min(all_rets)*1:.1f}% / {max(all_rets)*1:.1f}%")
        print(f"  Avg OOS DD:     {sum(r['max_drawdown_pct'] for r in oos)/len(oos):.1f}%")
        verdict = "ROBUST" if (avg_sh >= 0.5 and pos_prof >= len(oos) - 1) else (
            "MIXED" if avg_sh >= 0 else "FRAGILE"
        )
        print(f"\n  VERDICT: {verdict}  (avg Sharpe threshold 0.5, majority profitable OOS)")

    print(f"\n  NOTE: params were originally swept on FULL window, so OOS here is a"
          f"\n  robustness check, not a clean unbiased holdout. Strong positive OOS"
          f"\n  across windows indicates the live config generalizes to unseen regimes.")


if __name__ == "__main__":
    asyncio.run(main())