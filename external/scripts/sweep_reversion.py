#!/usr/bin/env python3
"""Quick sweep over mean-reversion 1H configs to check if ANY has edge."""
import asyncio
from copy import replace
import honest_backtest_reversion_1h as m

data = asyncio.run(m.load_data(force_refresh=False))
funding = asyncio.run(m.load_funding(force_refresh=False))

base = dict(
    top_k=2, rsi_period=14, bb_period=20, bb_std=2.0,
    min_hold_bars=2, max_hold_bars=24, max_leverage=1.0,
    risk_per_trade=0.01, tp_atr_mult=2.0, sl_atr_mult=1.5,
    vol_mult=1.5, corr_threshold=0.7, max_margin_pct=0.5,
    allow_short=True, tp_bb_mid=False,
)

VARIANTS = [
    ("base", dict()),
    ("long-only", dict(allow_short=False)),
    ("trend200 long-only", dict(ema_trend=200, allow_short=False)),
    ("trend200 both", dict(ema_trend=200)),
    ("rsi 25/75", dict(rsi_oversold=25.0, rsi_overbought=75.0)),
    ("rsi 20/80 + trend", dict(rsi_oversold=20.0, rsi_overbought=80.0, ema_trend=200)),
    ("sl 1.0 wide", dict(sl_atr_mult=1.0, tp_atr_mult=2.0)),
    ("tp 4 sl 2", dict(tp_atr_mult=4.0, sl_atr_mult=2.0)),
    ("tp_bb_mid sl 2", dict(tp_bb_mid=True, sl_atr_mult=2.0, tp_atr_mult=0.0)),
    ("tp_bb_mid trend", dict(tp_bb_mid=True, ema_trend=200, sl_atr_mult=2.0)),
    ("top_k 1", dict(top_k=1)),
    ("max_hold 12", dict(max_hold_bars=12)),
    ("max_hold 48", dict(max_hold_bars=48)),
    ("vol_mult 2.0", dict(vol_mult=2.0)),
    ("risk 0.02", dict(risk_per_trade=0.02)),
]

print(f"  {'Variant':24s} {'Return':>8} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7} {'Trades':>7} {'WR':>6} {'PF':>5}")
for name, over in VARIANTS:
    cfg = replace(m.REV_BASE)
    cfg.name = f"MR1H {name}"
    for k, v in over.items():
        setattr(cfg, k, v)
    r = m.run_strategy(data, cfg, funding)
    print(f"  {name:24s} {r['total_return_pct']:+7.1f}% {r['cagr_pct']:6.1f}% "
          f"{r['max_drawdown_pct']:6.1f}% {r['sharpe']:6.2f} {r['closed_trades']:7d} "
          f"{r['win_rate']:5.1f}% {r.get('profit_factor', 0):5.2f}")
