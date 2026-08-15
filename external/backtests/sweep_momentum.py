"""Momentum Rotation v4 — parameter sweep harness on the Backtrader engine.

Fetches OKX native daily candles once (disk cache), then runs the
MomentumRotationV4 strategy over a grid of parameter overrides and prints a
ranked table (CAGR / total / Sharpe / MaxDD / trades).

Usage:
  python external/backtests/sweep_momentum.py \
      --pairs BTC,ETH,BNB,SOL,XRP,DOGE,ADA,TRX,AVAX,LTC --days 1100 \
      --grid sizing
"""
import argparse
import itertools
import os
import pickle
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import backtrader as bt  # noqa: E402
import bt_okx  # noqa: E402
import backtrader_momentum_rotation as mom  # noqa: E402

CACHE = os.path.join(os.path.dirname(__file__), ".momentum_sweep_cache.pkl")


def load_data(insts, tf, days):
    if os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            d = pickle.load(f)
        if d.get("tf") == tf and d.get("days") == days and d.get("insts") == insts:
            return d["raw"]
    raw = bt_okx.run_sync(bt_okx.load_universe(insts, tf, days))
    raw = bt_okx.align(raw)
    with open(CACHE, "wb") as f:
        pickle.dump({"tf": tf, "days": days, "insts": insts, "raw": raw}, f)
    return raw


def run(raw, overrides, capital=10000.0):
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.addstrategy(mom.MomentumRotationV4, **overrides)
    for inst, df in raw.items():
        cerebro.adddata(bt_okx.as_bt_feed(df, name=inst), name=inst)
    cerebro.broker.setcash(capital * mom.SCALE)
    cerebro.broker.set_checksubmit(False)
    cerebro.broker.setcommission(commission=mom.COMMISSION,
                                 commtype=bt.CommInfoBase.COMM_PERC,
                                 percabs=True, stocklike=True)
    cerebro.broker.set_slippage_perc(perc=mom.SLIPPAGE)
    cerebro.addanalyzer(mom.EquityCurve, _name="eq")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    strat = cerebro.run()[0]

    end = strat.broker.getvalue() / mom.SCALE
    if strat.bankrupt:
        end = 0.0
    curve = [(ts, max(0.0, v / mom.SCALE))
             for ts, v in strat.analyzers.eq.get_analysis()["curve"]]
    start = curve[0][1]
    years = (curve[-1][0] - curve[0][0]).days / 365.25 if len(curve) > 1 else 0.0
    total = (end / start - 1) * 100 if end > 0 else -100.0
    cagr = ((end / start) ** (1 / years) - 1) * 100 if years > 0 and end > 0 else 0.0

    eq = np.array([v for _, v in curve], dtype=float)
    mask = eq[:-1] > 0
    rets = np.zeros_like(eq[:-1])
    np.divide(np.diff(eq), eq[:-1], out=rets, where=mask)
    sharpe = (rets.mean() / rets.std() * np.sqrt(365)) if rets.std() > 0 else 0.0
    peak = np.maximum.accumulate(eq)
    maxdd = ((eq - peak) / peak).min() * 100
    ta = strat.analyzers.trades.get_analysis()
    closed = ta.total.closed if ta.total else 0
    won = ta.won.total if ta.won else 0
    return {"total": total, "cagr": cagr, "sharpe": sharpe, "maxdd": maxdd,
            "closed": closed, "won": won, "end": end, "years": years,
            "bankrupt": strat.bankrupt}


GRIDS = {
    # sizing / exposure: risk, margin cap, leverage, concurrency
    "sizing": {
        "risk_per_trade": [0.10, 0.14, 0.18, 0.25],
        "allocation_pct": [0.5, 0.75, 1.0],
        "max_leverage": [2.0, 3.0],
        "top_k": [2, 3],
    },
    # entry: momentum strength / ADX / RSI / volatility filter
    "entry": {
        "min_roc": [3.0, 4.5, 6.0, 8.0],
        "adx_min": [20.0, 25.0, 29.0, 35.0],
        "vol_mult": [1.2, 1.5, 1.8, 2.2],
        "corr_threshold": [0.7, 0.85],
        "top_k": [3],
    },
    # exit: stop / trailing / partial TP / breakeven
    "exit": {
        "atr_stop_mult": [2.0, 2.7, 3.5, 4.5],
        "trail_atr_mult": [0.2, 1.0, 2.0, 3.0],
        "breakeven_pct": [0.03, 0.05, 0.08],
        "top_k": [3],
    },
    # portfolio: concurrency / holding / shorts / regime
    "portfolio": {
        "top_k": [2, 3, 4, 5],
        "min_hold_days": [5, 11, 15, 20],
        "allow_short": [True, False],
        "corr_threshold": [0.7, 0.9],
    },
}


def parse_overrides(items):
    out = {}
    for s in items:
        k, v = s.split("=", 1)
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        else:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="BTC,ETH,BNB,SOL,XRP,DOGE,ADA,TRX,AVAX,LTC")
    ap.add_argument("--days", type=int, default=1100)
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--grid", default="sizing")
    ap.add_argument("--overrides", nargs="*", default=[])
    ap.add_argument("--min-dd", type=float, default=-60.0)
    ap.add_argument("--limit", type=int, default=25)
    p = ap.parse_args()

    coins = [c.strip() for c in p.pairs.split(",") if c.strip()]
    insts = [mom.SWAP_MAP.get(c, c + "-USDT-SWAP") for c in coins]
    base = parse_overrides(p.overrides)

    raw = load_data(insts, p.tf, p.days)
    first = next(iter(raw.values()))
    print(f"Data: {len(insts)} insts, {len(first)} bars "
          f"({first.index[0].date()} -> {first.index[-1].date()})")

    grid = GRIDS[p.grid]
    keys = list(grid.keys())
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*grid.values())]
    print(f"Grid '{p.grid}': {len(combos)} combos")

    results = []
    for i, combo in enumerate(combos):
        ov = dict(combo)
        ov.update(base)
        m = run(raw, ov)
        m["overrides"] = ov
        results.append(m)
        if (i + 1) % 20 == 0:
            print(f"  ...{i + 1}/{len(combos)} done", flush=True)

    good = [r for r in results if r["maxdd"] >= p.min_dd]
    good.sort(key=lambda r: -r["cagr"])
    print(f"\n{'CAGR':>7} {'Total':>8} {'Sharpe':>6} {'MaxDD':>7} {'Liq':>3} {'Closed':>6}   params")
    print("-" * 108)
    for r in good[: p.limit]:
        ov = r["overrides"]
        param_s = " ".join(f"{k}={v}" for k, v in sorted(ov.items()))
        print(f"{r['cagr']:6.1f}% {r['total']:7.1f}% {r['sharpe']:6.2f} "
              f"{r['maxdd']:6.1f}% {'Y' if r['bankrupt'] else '-':>3} {r['closed']:6d}   {param_s}")
    print(f"\nrows shown = {min(len(good), p.limit)} of {len(good)} "
          f"passing MinDD>={p.min_dd}% (of {len(results)})")


if __name__ == "__main__":
    main()
