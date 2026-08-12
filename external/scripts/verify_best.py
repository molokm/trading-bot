#!/usr/bin/env python3
"""Walk-forward sanity check: evaluate the winning tribo config on disjoint
segments of the OKX daily data to detect overfitting to a single regime."""
import asyncio, sys, os, json
from dataclasses import fields
sys.path.insert(0, os.path.dirname(__file__))
import honest_backtest_3y as hb
import opt_bot as ob

BEST = dict(
    roc_period=14, ema_fast=20, ema_slow=50, adx_period=14, adx_min=25.0,
    sma_long=200, min_roc=3.0, top_k=2, allocation_pct=1.0,
    portfolio_trailing=0.0, min_hold_days=20, max_leverage=2.0, risk_pct=0.10,
    atr_stop_mult=3.0, trail_atr_mult=0.2, breakeven_pct=0.02,
    partial_tp_pct=0.05, partial_tp_ratio=0.5, allow_short=True, capital=10000.0,
)


def slice_data(data, from_idx, to_idx):
    out = {}
    for coin, bars in data.items():
        out[coin] = bars[from_idx:to_idx]
    return out


async def main():
    data = await hb.load_data()
    # align on common dates and get base ranges per coin
    base = data["BTC"]
    n = len(base)
    print(f"BTC bars: {n} ({base[0]['date']} -> {base[-1]['date']})")

    full = ob.run(data, ob.Cfg(**BEST))
    print(f"\nFULL WINDOW: CAGR {full['cagr_pct']}% ret {full['return_pct']}% DD {full['max_dd_pct']}%")

    # two halves
    mid = n // 2
    print("\n=== FIRST HALF ===")
    d1 = slice_data(data, 0, mid)
    r1 = ob.run(d1, ob.Cfg(**BEST))
    print(f"  {r1['years']:.2f}y  CAGR {r1['cagr_pct']}%  ret {r1['return_pct']}%  DD {r1['max_dd_pct']}%")

    print("=== SECOND HALF ===")
    d2 = slice_data(data, mid, n)
    r2 = ob.run(d2, ob.Cfg(**BEST))
    print(f"  {r2['years']:.2f}y  CAGR {r2['cagr_pct']}%  ret {r2['return_pct']}%  DD {r2['max_dd_pct']}%")

    # three windows (~10 months each)
    print("\n=== THREE SEGMENTS ===")
    third = n // 3
    for k in range(3):
        a = k * third
        b = n if k == 2 else (k + 1) * third
        if b - a < 220:
            continue
        dd = slice_data(data, a, b)
        rr = ob.run(dd, ob.Cfg(**BEST))
        print(f"  seg{k+1} ({dd['BTC'][0]['date']} -> {dd['BTC'][-1]['date']}): "
              f"{rr['years']:.2f}y  CAGR {rr['cagr_pct']}%  ret {rr['return_pct']}%  DD {rr['max_dd_pct']}%")

    # BTC buy-and-hold benchmark for reference
    print("\n=== BTC BUY&HOLD (full window) ===")
    bnh = hb.buy_and_hold_btc(data)
    print(f"  CAGR {bnh['cagr_pct']}%  ret {bnh['total_return_pct']}%  DD {bnh['max_drawdown_pct']}%  Sharpe {bnh['sharpe']}")


if __name__ == "__main__":
    asyncio.run(main())