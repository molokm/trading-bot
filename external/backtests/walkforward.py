"""Walk-forward / out-of-sample check for the tuned strategies.

Split the 2023-05..2026-08 window into In-Sample (IS, ~60%) and Out-of-Sample
(OOS, ~40%). For a chosen strategy:
  1. sweep a focused lever grid on IS only -> best IS config (by CAGR, MaxDD cap)
  2. evaluate the IS-best config on the OOS window
  3. evaluate the full-window-tuned config on OOS
  4. evaluate the OLD default config on OOS

If the IS-best config also does well OOS (and the full-tuned config does not
collapse OOS), the strategy generalizes rather than overfits.

Usage:
  python external/backtests/walkforward.py --strat momentum
  python external/backtests/walkforward.py --strat impulse
"""
import argparse
import itertools
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import backtrader as bt  # noqa: E402
import bt_okx  # noqa: E402
import backtrader_momentum_rotation as mom  # noqa: E402
import backtrader_impulse as imp  # noqa: E402
from sweep_momentum import load_data as load_mom  # noqa: E402
from sweep_impulse import load_data as load_imp  # noqa: E402

MODULES = {"momentum": mom, "impulse": imp}
LOADERS = {"momentum": load_mom, "impulse": load_imp}
COINS = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "AVAX", "LTC"]

# focused levers per strategy (full-window tuning already found the good ones)
GRIDS = {
    "momentum": {
        "risk_per_trade": [0.10, 0.15, 0.20],
        "corr_threshold": [0.7, 0.85],
        "trail_atr_mult": [0.2, 3.0],
        "vol_mult": [1.8, 2.2],
    },
    "impulse": {
        "risk_per_trade": [0.10, 0.15],
        "top_k": [3, 4],
        "trail_atr_mult": [8.0, 12.0],
        "tp2_atr": [6.0, 10.0],
    },
}

# full-window tuned configs (as baked into defaults)
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

# old defaults (pre-tuning) — for relative comparison on OOS
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


def run_curve(mod, raw, overrides):
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.addstrategy(mod.Impulse1D if mod is imp else mod.MomentumRotationV4,
                        **overrides)
    for inst, df in raw.items():
        cerebro.adddata(bt_okx.as_bt_feed(df, name=inst), name=inst)
    cerebro.broker.setcash(10000 * mod.SCALE)
    cerebro.broker.set_checksubmit(False)
    cerebro.broker.setcommission(commission=mod.COMMISSION,
                                 commtype=bt.CommInfoBase.COMM_PERC,
                                 percabs=True, stocklike=True)
    cerebro.broker.set_slippage_perc(perc=mod.SLIPPAGE)
    cerebro.addanalyzer(mod.EquityCurve, _name="eq")
    strat = cerebro.run()[0]
    scale = mod.SCALE
    if hasattr(strat, "bankrupt"):
        curve = [(ts, max(0.0, v / scale)) for ts, v in strat.analyzers.eq.get_analysis()["curve"]]
    else:
        curve = [(ts, v / scale) for ts, v in strat.analyzers.eq.get_analysis()["curve"]]
    return curve


def metrics(curve):
    if len(curve) < 2:
        return {"cagr": 0.0, "maxdd": 0.0, "sharpe": 0.0, "total": 0.0}
    eq = np.array([v for _, v in curve], dtype=float)
    start, end = eq[0], eq[-1]
    years = (curve[-1][0] - curve[0][0]).days / 365.25
    if start <= 0:
        return {"cagr": 0.0, "maxdd": (eq.min() - 1) * 100, "sharpe": 0.0, "total": -100.0}
    total = (end / start - 1) * 100
    cagr = ((end / start) ** (1 / years) - 1) * 100 if years > 0 and end > 0 else 0.0
    mask = eq[:-1] > 0
    rets = np.zeros_like(eq[:-1])
    np.divide(np.diff(eq), eq[:-1], out=rets, where=mask)
    sharpe = (rets.mean() / rets.std() * np.sqrt(365)) if rets.std() > 0 else 0.0
    peak = np.maximum.accumulate(eq)
    maxdd = ((eq - peak) / peak).min() * 100
    return {"cagr": cagr, "maxdd": maxdd, "sharpe": sharpe, "total": total}


def window(curve, start, end):
    return [(ts, v) for ts, v in curve if start <= ts <= end]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strat", choices=["momentum", "impulse"], default="momentum")
    ap.add_argument("--days", type=int, default=1100)
    ap.add_argument("--is-frac", type=float, default=0.6)
    ap.add_argument("--min-dd", type=float, default=-55.0)
    p = ap.parse_args()

    mod = MODULES[p.strat]
    raw = LOADERS[p.strat]([mod.SWAP_MAP[c] for c in COINS], "1d", p.days)
    first = next(iter(raw.values()))
    all_ts = [df.index[0] for df in raw.values()]
    t0 = min(all_ts)
    t1 = max(df.index[-1] for df in raw.values())
    import pandas as pd
    mid = t0 + (t1 - t0) * p.is_frac
    print(f"Full: {t0.date()} -> {t1.date()} | IS: {t0.date()} -> {mid.date()} | "
          f"OOS: {mid.date()} -> {t1.date()}")

    base = dict(TUNED[p.strat])
    grid = GRIDS[p.strat]
    keys = list(grid.keys())
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*grid.values())]
    print(f"IS sweep: {len(combos)} combos on levers {keys}")

    is_results = []
    for combo in combos:
        ov = dict(base)
        ov.update(combo)
        curve = run_curve(mod, raw, ov)
        m = metrics(window(curve, t0, mid))
        m["overrides"] = ov
        is_results.append(m)
    good = [r for r in is_results if r["maxdd"] >= p.min_dd]
    good.sort(key=lambda r: -r["cagr"])
    best_is = good[0]["overrides"]
    print(f"\nBest IS config (CAGR {good[0]['cagr']:.1f}%, MaxDD {good[0]['maxdd']:.1f}%):")
    print("  " + " ".join(f"{k}={v}" for k, v in sorted(best_is.items())))

    print(f"\n{'config':<28} {'IS CAGR':>9} {'OOS CAGR':>9} {'OOS MaxDD':>10} {'OOS Sharpe':>10}")
    print("-" * 72)
    configs = [
        ("best_IS", best_is),
        ("tuned_full", dict(TUNED[p.strat])),
        ("old_default", dict(OLD[p.strat])),
    ]
    for label, ov in configs:
        curve = run_curve(mod, raw, ov)
        m_is = metrics(window(curve, t0, mid))
        m_oos = metrics(window(curve, mid, t1))
        print(f"{label:<28} {m_is['cagr']:8.1f}% {m_oos['cagr']:8.1f}% "
              f"{m_oos['maxdd']:9.1f}% {m_oos['sharpe']:10.2f}")

    print("\nVerdict: IS config that also holds up OOS => strategy generalizes;")
    print("if tuned_full collapses OOS while best_IS survives => full tuning was overfit.")


if __name__ == "__main__":
    main()
