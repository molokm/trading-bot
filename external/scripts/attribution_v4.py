#!/usr/bin/env python3
"""Attribution: which single change kills V3 → V4. One change at a time off V3_LIVE."""
import asyncio
from copy import replace

from honest_backtest_3y import (
    V3_LIVE, run_strategy, load_data, load_funding, print_report,
)

VARIANTS = [
    ("v3 + top_k=3", dict(top_k=3)),
    ("v3 + adx_min=25", dict(adx_min=25.0)),
    ("v3 + adx 25 + top_k 3", dict(adx_min=25.0, top_k=3)),
    ("v3 + dual-roc 20/50 + ema 15/70", dict(roc_fast_period=20, roc_slow_period=50, ema_fast=15, ema_slow=70)),
    ("v3 + risk 0.02", dict(risk_per_trade=0.02)),
    ("v3 + atr_stop 2.5", dict(atr_stop_mult=2.5)),
    ("v3 + trail 1.5", dict(trail_atr_mult=1.5)),
    ("v3 + allow_short=False", dict(allow_short=False)),
]


def main():
    data = asyncio.run(load_data(force_refresh=False))
    funding = asyncio.run(load_funding(force_refresh=False))

    base = run_strategy(data, V3_LIVE, funding)
    print_report(base)

    results = [("V3_LIVE (base)", base)]
    for name, over in VARIANTS:
        cfg = replace(V3_LIVE, name=f"Momentum Rotation v3 + {name}")
        for k, v in over.items():
            setattr(cfg, k, v)
        r = run_strategy(data, cfg, funding)
        print_report(r)
        results.append((name, r))

    print("\n" + "=" * 88)
    print("ATTRIBUTION (one change at a time off V3_LIVE)")
    print("=" * 88)
    print(f"  {'Variant':32s} {'Return':>8} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7} {'Trades':>7} {'WR':>6} {'PF':>5}")
    for name, r in results:
        print(f"  {name:32s} {r['total_return_pct']:+7.1f}% {r['cagr_pct']:6.1f}% "
              f"{r['max_drawdown_pct']:6.1f}% {r['sharpe']:6.2f} {r['closed_trades']:7d} "
              f"{r['win_rate']:5.1f}% {r.get('profit_factor', 0):5.2f}")


if __name__ == "__main__":
    main()
