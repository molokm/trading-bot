#!/usr/bin/env python3
"""Sensitivity of the winning tribo config to exchange fee assumptions.

Current backtest assumes taker 0.10% + 0.05% slippage per side.
Real OKX Lv1 fees: maker 0.02%, taker 0.05%. Sweep both to see how
CAGR / DD / final equity change with the actual fee schedule.
"""
import asyncio, sys, os
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

# (label, fee per side, slippage per side)
SCENARIOS = [
    ("backtest default (taker 0.10% + slip 0.05%)", 0.0010, 0.0005),
    ("real taker-only (0.05% + slip 0.05%)",       0.0005, 0.0005),
    ("real 50/50 maker/taker (0.035% + slip 0.05%)", 0.00035, 0.0005),
    ("real maker-only (0.02% + slip 0.05%)",       0.0002, 0.0005),
    ("real maker-only, no slippage (0.02%)",       0.0002, 0.0000),
    ("zero fee (upper bound)",                     0.0000, 0.0000),
]


async def main():
    data = await hb.load_data()
    print(f"{'scenario':55s} {'ret%':>8s} {'CAGR%':>7s} {'DD%':>6s} {'final':>10s}")
    print("-" * 90)
    for label, fee, slip in SCENARIOS:
        ob.COMMISSION = fee
        ob.SLIPPAGE = slip
        r = ob.run(data, ob.Cfg(**BEST))
        print(f"{label:55s} {r['return_pct']:8.1f} {r['cagr_pct']:7.1f} {r['max_dd_pct']:6.1f} {r['final']:10.2f}")


if __name__ == "__main__":
    asyncio.run(main())