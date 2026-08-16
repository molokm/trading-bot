"""MACD+Donchian Validation — parameter sweep harness on the Backtrader engine.

Fetches OKX native daily candles once (disk cache), then runs the MacdDonchian
strategy over a grid of parameter overrides and prints a ranked table
(total return / CAGR / Sharpe / MaxDD / trades).

Usage:
  python external/backtests/sweep_validation.py --days 1100 --grid sizing
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
import backtrader_macd_donchian as val  # noqa: E402

CACHE = os.path.join(os.path.dirname(__file__), ".validation_sweep_cache.pkl")
COINS = ["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"]


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
    cerebro.addstrategy(val.MacdDonchian, **overrides)
    for inst, df in raw.items():
        cerebro.adddata(bt_okx.as_bt_feed(df, name=inst), name=inst)
    cerebro.broker.setcash(capital * val.SCALE)
    cerebro.broker.set_checksubmit(False)
    cerebro.broker.setcommission(commission=val.COMMISSION,
                                 commtype=bt.CommInfoBase.COMM_PERC,
                                 percabs=True, stocklike=True)
    cerebro.broker.set_slippage_perc(perc=val.SLIPPAGE)
    cerebro.addanalyzer(val.EquityCurve, _name="eq")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    strat = cerebro.run()[0]

    curve = [(ts, v / val.SCALE) for ts, v in strat.analyzers.eq.get_analysis()["curve"]]
    eq = np.array([v for _, v in curve], dtype=float)
    start, end = eq[0], eq[-1]
    years = (curve[-1][0] - curve[0][0]).days / 365.25
    total = (end / start - 1) * 100
    cagr = ((end / start) ** (1 / years) - 1) * 100 if years > 0 and end > 0 else 0.0
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
            "closed": closed, "won": won, "end": end, "years": years}


# focused grids for sequential passes ---------------------------------------

GRIDS = {
    # sizing / concurrency
    "sizing": {
        "top_k": [2, 3, 4, 5],
        "risk_per_trade": [0.10, 0.14, 0.20, 0.25],
        "allocation_pct": [0.15, 0.3, 0.5],
        "max_leverage": [1.0, 2.0],
    },
    # entry: donchian window + MACD speed + confirmation
    "entry": {
        "donchian_n": [10, 15, 20, 25],
        "macd_fast": [8, 12],
        "macd_slow": [21, 26],
        "top_k": [4],
    },
    # exit: chandelier / partial TP / second TP / breakeven
    "exit": {
        "chandelier_atr": [3.0, 4.0, 5.0, 6.0],
        "tp_pct": [0.06, 0.08, 0.10, 0.12],
        "tp_ratio": [0.2, 0.3, 0.5],
        "tp2_pct": [0.08, 0.10, 0.15],
        "be_pct": [0.01, 0.015, 0.02],
        "max_hold_days": [3, 5, 7],
        "top_k": [4],
    },
    # exit block 1: chandelier + hold (small grid)
    "chandelier": {
        "chandelier_atr": [3.0, 3.5, 4.0, 4.5, 5.0, 6.0],
        "max_hold_days": [3, 5, 7],
        "top_k": [4],
    },
    # exit block 2: partial TP + breakeven (small grid)
    "tp": {
        "tp_pct": [0.06, 0.08, 0.10, 0.12],
        "tp_ratio": [0.2, 0.3, 0.5],
        "tp2_pct": [0.08, 0.10, 0.15],
        "be_pct": [0.01, 0.015, 0.02],
        "top_k": [4],
    },
    # refine: combos around a promising config
    "refine": {
        "top_k": [3, 4],
        "donchian_n": [15, 20],
        "chandelier_atr": [3.5, 4.0, 5.0],
        "tp_ratio": [0.3, 0.5],
        "risk_per_trade": [0.14, 0.20],
        "allocation_pct": [0.3, 0.5],
        "max_leverage": [1.0, 2.0],
    },
}


def parse_overrides(items):
    out = {}
    for s in items:
        k, v = s.split("=", 1)
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        elif v.lstrip("-").isdigit():
            out[k] = int(v)
        else:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default=",".join(COINS))
    ap.add_argument("--days", type=int, default=1100)
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--grid", default="sizing")
    ap.add_argument("--overrides", nargs="*", default=[])
    ap.add_argument("--min-dd", type=float, default=-60.0, help="max acceptable drawdown")
    ap.add_argument("--limit", type=int, default=25, help="rows to print")
    p = ap.parse_args()

    coins = [c.strip() for c in p.pairs.split(",") if c.strip()]
    insts = [val.SWAP_MAP.get(c, c + "-USDT-SWAP") for c in coins]
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
        ov.update(base)  # explicit --overrides take precedence over grid
        m = run(raw, ov)
        m["overrides"] = ov
        results.append(m)
        if (i + 1) % 20 == 0:
            print(f"  ...{i + 1}/{len(combos)} done", flush=True)

    good = [r for r in results if r["maxdd"] >= p.min_dd]
    good.sort(key=lambda r: -r["cagr"])
    print(f"\n{'CAGR':>7} {'Total':>8} {'Sharpe':>6} {'MaxDD':>7} {'Closed':>6}   params")
    print("-" * 100)
    for r in good[: p.limit]:
        ov = r["overrides"]
        param_s = " ".join(f"{k}={v}" for k, v in sorted(ov.items()))
        print(f"{r['cagr']:6.1f}% {r['total']:7.1f}% {r['sharpe']:6.2f} "
              f"{r['maxdd']:6.1f}% {r['closed']:6d}   {param_s}")
    print(f"\nrows shown = {min(len(good), p.limit)} of {len(good)} "
          f"passing MinDD>={p.min_dd}% (of {len(results)})")


if __name__ == "__main__":
    main()
