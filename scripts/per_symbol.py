"""Per-symbol detail + date range for one BacktestParams set."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.services.backtest_lab import (  # noqa: E402
    BacktestParams, frame_from_candles, prepare, run_backtest,
)

DIR = "/tmp/candles_1h"
P = BacktestParams(min_adx=20.0, min_roc=0.15, atr_stop_mult=1.15,
                   tp_r=2.0, max_hold_bars=24)

print(f"params: {P}\n")
tot_tr = tot_gw = tot_gl = 0
for s in sorted(os.listdir(DIR)):
    if not s.endswith(".json"):
        continue
    with open(os.path.join(DIR, s)) as f:
        rows = json.load(f)
    ts = sorted(int(r[0]) for r in rows)
    df = prepare(frame_from_candles(rows))
    r = run_backtest(df, P, block_count=16)
    # recover wins/losses sums from block returns is lossy; use equity curve
    eq = r.equity_curve
    tot_tr += r.trades
    print(f"{s[:4]:5} bars={len(rows):5d} "
          f"{ts[0]//1000}..{ts[-1]//1000}  n={r.trades:4d} WR={r.win_rate*100:5.1f}% "
          f"PF={r.profit_factor:5.2f} net={r.net_return_pct:8.2f}% "
          f"DD={r.max_drawdown_pct:6.2f}% sh={r.sharpe:5.2f}")
print(f"\ntotal trades across symbols: {tot_tr}")
