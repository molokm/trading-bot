"""Portfolio comparison: momentum + impulse diversification.

Runs both tuned strategies on the same data, combines their equity curves with
a fixed capital split (50/50 etc.) and reports portfolio CAGR / MaxDD / Sharpe
plus a per-fold breakdown to show diversification benefit.

Note: each strategy sizes off its own allocated capital, so portfolio equity =
w*mom_curve + (1-w)*imp_curve (returns are proportional to allocated capital).

Usage:
  python external/backtests/portfolio_compare.py
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import backtrader as bt  # noqa: E402
import bt_okx  # noqa: E402
import backtrader_momentum_rotation as mom  # noqa: E402
import backtrader_impulse as imp  # noqa: E402
from sweep_momentum import load_data as load_mom  # noqa: E402
from sweep_impulse import load_data as load_imp  # noqa: E402
from walkforward import run_curve, metrics, window  # noqa: E402

COINS = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "AVAX", "LTC"]

MOM_TUNED = dict(top_k=2, allocation_pct=0.5, min_roc=4.5, adx_min=25.0,
                 vol_mult=2.2, corr_threshold=0.85, atr_stop_mult=4.5,
                 trail_atr_mult=3.0, breakeven_pct=0.05, min_hold_days=11,
                 risk_per_trade=0.20)
IMP_TUNED = dict(top_k=3, max_adds=0, cooldown_bars=3, max_hold_bars=30,
                 entry_roc=3.0, vol_mult=1.5, sl_atr_mult=5.0,
                 trail_atr_mult=12.0, tp1_atr=2.0, tp1_frac=0.3,
                 tp2_atr=10.0, tp2_frac=0.3, risk_per_trade=0.10)


def main():
    raw_m = load_mom([mom.SWAP_MAP[c] for c in COINS], "1d", 1100)
    raw_i = load_imp([imp.SWAP_MAP[c] for c in COINS], "1d", 1100)

    curve_m = run_curve(mom, raw_m, MOM_TUNED)
    curve_i = run_curve(imp, raw_i, IMP_TUNED)

    # align by date (both start 2023-05-03)
    dm = {ts: v for ts, v in curve_m}
    di = {ts: v for ts, v in curve_i}
    dates = sorted(set(dm) & set(di))
    m = np.array([dm[d] for d in dates])
    i = np.array([di[d] for d in dates])
    ts = np.array(dates)

    print(f"Data: {ts[0].date()} -> {ts[-1].date()} ({len(ts)} bars)")
    print("\nCapital split: momentum / impulse")
    print(f"{'split':>14} {'CAGR':>8} {'MaxDD':>8} {'Sharpe':>7}   (indiv: mom "
          f"CAGR {metrics([(d, v) for d, v in zip(ts, m)])['cagr']:.1f}% / imp "
          f"{metrics([(d, v) for d, v in zip(ts, i)])['cagr']:.1f}%)")

    rows = []
    for w in (0.0, 0.3, 0.5, 0.7, 1.0):
        eq = w * m + (1 - w) * i
        mm = metrics([(d, v) for d, v in zip(ts, eq)])
        rows.append((w, mm))
        print(f"{f'{w:.0%}/{1-w:.0%}':>14} {mm['cagr']:7.1f}% {mm['maxdd']:7.1f}% "
              f"{mm['sharpe']:7.2f}")

    # per-fold breakdown for the 50/50 portfolio
    t0, t1 = ts[0], ts[-1]
    folds = 4
    edges = [t0 + (t1 - t0) * i / folds for i in range(folds + 1)]
    eq = 0.5 * m + 0.5 * i
    print(f"\nPer-fold 50/50 portfolio (vs components):")
    print(f"{'fold':<17} {'portf':>10} {'momentum':>10} {'impulse':>10}")
    for k in range(folds):
        lo, hi = edges[k], edges[k + 1]
        fold_idx = [(d, v) for d, v in zip(ts, eq) if lo <= d <= hi]
        m_idx = [(d, v) for d, v in zip(ts, m) if lo <= d <= hi]
        i_idx = [(d, v) for d, v in zip(ts, i) if lo <= d <= hi]
        cagr = lambda c: metrics(c)["cagr"]
        fname = "F%d %s..%s" % (k + 1, lo.strftime("%y-%m"), hi.strftime("%y-%m"))
        print(f"{fname:<17} {cagr(fold_idx):9.1f}% {cagr(m_idx):9.1f}% {cagr(i_idx):9.1f}%")


if __name__ == "__main__":
    main()
