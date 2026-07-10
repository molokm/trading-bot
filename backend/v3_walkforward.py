"""
Walk-Forward validation for Daily Swing V3
3 windows: 2023-2024, 2024-2025, 2025-2026
"""
import sys
sys.path.insert(0, '/Users/vladislavmolok/Documents/Торговый БОТ/backend')
from daily_swing_v3 import *
import yfinance as yf

print("=== V3 Walk-Forward Validation ===\n")

data = {}
for sym, ticker in TICKERS.items():
    df = yf.download(ticker, period='3y', interval='1d', auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)
    df.index = pd.to_datetime(df.index)
    data[sym] = enrich(df)

common_dates = data['BTC'].index
for sym in UNIVERSE:
    common_dates = common_dates.intersection(data[sym].index)
for sym in UNIVERSE:
    data[sym] = data[sym].loc[common_dates]

print(f"Total: {len(common_dates)} days ({common_dates[0].date()} to {common_dates[-1].date()})\n")

# 3 walk-forward windows (each ~1 year)
windows = [
    ("2023-06-18", "2024-06-18", "Window 1: Jun 2023 - Jun 2024"),
    ("2024-06-18", "2025-06-18", "Window 2: Jun 2024 - Jun 2025"),
    ("2025-06-18", "2026-06-18", "Window 3: Jun 2025 - Jun 2026"),
]

results = []
for start, end, label in windows:
    start_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)
    sub_data = {}
    for sym in UNIVERSE:
        sub_data[sym] = data[sym].loc[start_dt:end_dt]
    n_days = len(sub_data['BTC'])
    if n_days < 100:
        print(f"  {label}: insufficient data ({n_days} days), skipping")
        continue

    trades, final_eq, max_dd, eq_curve = run_backtest(sub_data, INITIAL_CAPITAL, verbose=False)
    m = calc_metrics(trades, INITIAL_CAPITAL, final_eq, max_dd, eq_curve)
    results.append((label, m))

    print(f"  {label}")
    print(f"    Trades: {m['n_trades']}, CAGR: {m['cagr']:+.1f}%, MaxDD: {m['max_drawdown']:.1f}%, "
          f"PF: {m['profit_factor']:.2f}, WR: {m['win_rate']:.1f}%, Sharpe: {m['sharpe']:.2f}")
    print()

# Full period
trades, final_eq, max_dd, eq_curve = run_backtest(data, INITIAL_CAPITAL, verbose=False)
m = calc_metrics(trades, INITIAL_CAPITAL, final_eq, max_dd, eq_curve)
print(f"  Full Period (3 years)")
print(f"    Trades: {m['n_trades']}, CAGR: {m['cagr']:+.1f}%, MaxDD: {m['max_drawdown']:.1f}%, "
      f"PF: {m['profit_factor']:.2f}, WR: {m['win_rate']:.1f}%, Sharpe: {m['sharpe']:.2f}")

# Consistency check
positive_windows = sum(1 for _, r in results if r['cagr'] > 0)
print(f"\n  WF Consistency: {positive_windows}/{len(results)} windows profitable")
print(f"  {'PASS' if positive_windows >= 2 else 'FAIL'}")
