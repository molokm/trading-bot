"""Multi-fold walk-forward (4 folds) for the tuned strategies + regime filter.

Splits the 2023-05..2026-08 window into 4 sequential folds. For each fold we
report CAGR / MaxDD / Sharpe for a set of named configs, computed on that fold's
sub-window of the equity curve. This shows per-period consistency and lets us
judge the Impulse regime filter across different market regimes.

Usage:
  python external/backtests/walkforward_folds.py --strat momentum
  python external/backtests/walkforward_folds.py --strat impulse
"""
import argparse
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

MODULES = {"momentum": mom, "impulse": imp}
LOADERS = {"momentum": load_mom, "impulse": load_imp}
COINS = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "AVAX", "LTC"]

TUNED = {
    "momentum": dict(top_k=2, allocation_pct=0.5, min_roc=4.5, adx_min=25.0,
                     vol_mult=2.2, corr_threshold=0.85, atr_stop_mult=4.5,
                     trail_atr_mult=3.0, breakeven_pct=0.05, min_hold_days=11,
                     risk_per_trade=0.20),
    "impulse": dict(top_k=3, max_adds=0, cooldown_bars=3, max_hold_bars=30,
                    entry_roc=3.0, vol_mult=1.5, sl_atr_mult=5.0,
                    trail_atr_mult=12.0, tp1_atr=2.0, tp1_frac=0.3,
                    tp2_atr=10.0, tp2_frac=0.3, risk_per_trade=0.10),
}
OLD = {
    "momentum": dict(top_k=2, risk_per_trade=0.14, allocation_pct=1.0,
                     adx_min=29.0, vol_mult=1.8, corr_threshold=0.7,
                     atr_stop_mult=2.7, trail_atr_mult=0.2, min_hold_days=11,
                     min_roc=4.5, breakeven_pct=0.05),
    "impulse": dict(top_k=4, risk_per_trade=0.10, max_adds=2, cooldown_bars=5,
                    max_hold_bars=30, entry_roc=4.0, vol_mult=1.5,
                    sl_atr_mult=5.0, trail_atr_mult=8.0, tp1_atr=2.0,
                    tp1_frac=0.3, tp2_atr=6.0, tp2_frac=0.3),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strat", choices=["momentum", "impulse"], default="impulse")
    ap.add_argument("--days", type=int, default=1100)
    ap.add_argument("--folds", type=int, default=4)
    p = ap.parse_args()

    mod = MODULES[p.strat]
    raw = LOADERS[p.strat]([mod.SWAP_MAP[c] for c in COINS], "1d", p.days)
    all_ts = [df.index[0] for df in raw.values()]
    t0 = min(all_ts)
    t1 = max(df.index[-1] for df in raw.values())

    edges = [t0 + (t1 - t0) * i / p.folds for i in range(p.folds + 1)]
    names = [f"F{i + 1} {edges[i].strftime('%y-%m')}..{edges[i + 1].strftime('%y-%m')}"
             for i in range(p.folds)]

    configs = {"tuned": dict(TUNED[p.strat]), "old_default": dict(OLD[p.strat])}
    if p.strat == "impulse":
        for m in (1, 2, 3):
            configs[f"tuned+reg{m}"] = dict(TUNED[p.strat], regime_mode=m)

    # cache equity curves per config (each curve is the full-window run)
    curves = {name: run_curve(mod, raw, ov) for name, ov in configs.items()}

    # fold metrics
    fold_metrics = {name: [] for name in configs}
    for i in range(p.folds):
        lo, hi = edges[i], edges[i + 1]
        for name, curve in curves.items():
            m = metrics(window(curve, lo, hi))
            fold_metrics[name].append(m)

    print(f"\n{mod.SWAP_MAP['BTC']} — {t0.date()} -> {t1.date()} ({p.folds} folds)")
    print(f"\nPer-fold CAGR %")
    print(f"{'config':<18}" + "".join(f"{names[i]:>17}" for i in range(p.folds)) + f"{'mean':>8}")
    for name in configs:
        vals = [fold_metrics[name][i]["cagr"] for i in range(p.folds)]
        print(f"{name:<18}" + "".join(f"{v:16.1f}%" for v in vals) + f"{np.mean(vals):7.1f}%")

    print(f"\nPer-fold MaxDD %")
    print(f"{'config':<18}" + "".join(f"{names[i]:>17}" for i in range(p.folds)) + f"{'worst':>8}")
    for name in configs:
        vals = [fold_metrics[name][i]["maxdd"] for i in range(p.folds)]
        print(f"{name:<18}" + "".join(f"{v:16.1f}%" for v in vals) + f"{min(vals):7.1f}%")

    print(f"\nPer-fold Sharpe")
    print(f"{'config':<18}" + "".join(f"{names[i]:>17}" for i in range(p.folds)) + f"{'mean':>8}")
    for name in configs:
        vals = [fold_metrics[name][i]["sharpe"] for i in range(p.folds)]
        print(f"{name:<18}" + "".join(f"{v:16.2f}" for v in vals) + f"{np.mean(vals):7.2f}")

    print("\nNotes: folds are mark-to-market sub-windows of the full equity curve.")
    print("A config is robust if its per-fold CAGR is consistently positive and")
    print("no fold produces a deep MaxDD / negative fold CAGR.")


if __name__ == "__main__":
    main()
