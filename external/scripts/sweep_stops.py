"""Sweep stop params (atr_stop_mult, trail_atr_mult) for live v3 config at risk 5%.

Reuses the honest backtest engine from honest_backtest_3y.py so every run has the
same rules: signal@T close -> entry@T+1 open, stops vs H/L, fee+slip, no look-ahead.
"""

import asyncio
import json
import sys

from honest_backtest_3y import StratConfig, run_strategy, load_data, buy_and_hold_btc

BASE = dict(
    adx_min=22.0, min_hold_days=3, risk_per_trade=0.05,
    max_leverage=2.0, breakeven_pct=0.02,
    partial_tp_pct=0.10, partial_tp_ratio=0.5,
    max_margin_pct=2.0,
)

STOP_MULTS = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5]
TRAIL_MULTS = [0.1, 0.15, 0.2, 0.3]

OUT = "sweep_stops_results.json"


async def main():
    force = "--refresh" in sys.argv
    data = await load_data(force_refresh=force)
    results = []
    for stop_mult in STOP_MULTS:
        for trail_mult in TRAIL_MULTS:
            cfg = StratConfig(
                name=f"stop={stop_mult}_trail={trail_mult}",
                atr_stop_mult=stop_mult,
                trail_atr_mult=trail_mult,
                **BASE,
            )
            r = run_strategy(data, cfg)
            results.append({
                "atr_stop_mult": stop_mult,
                "trail_atr_mult": trail_mult,
                "total_return_pct": r["total_return_pct"],
                "cagr_pct": r["cagr_pct"],
                "max_drawdown_pct": r["max_drawdown_pct"],
                "sharpe": r["sharpe"],
                "closed_trades": r["closed_trades"],
                "win_rate": r["win_rate"],
                "profit_factor": r["profit_factor"],
                "exit_reasons": r["exit_reasons"],
                "gross_profit": r["gross_profit"],
                "gross_loss": r["gross_loss"],
                "final_equity": r["final_equity"],
            })
            print(f"stop={stop_mult:.1f} trail={trail_mult:.2f} | "
                  f"ret={r['total_return_pct']:+7.1f}% CAGR={r['cagr_pct']:6.1f}% "
                  f"DD={r['max_drawdown_pct']:5.1f}% Sharpe={r['sharpe']:4.2f} "
                  f"trades={r['closed_trades']} WR={r['win_rate']:.1f}% PF={r['profit_factor']:.2f}",
                  flush=True)
    with open(OUT, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
