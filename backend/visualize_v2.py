"""
V2 Visualization — compare V1 vs V2
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import warnings
warnings.filterwarnings('ignore')

from test_daily_swing import (
    enrich as enrich_v1, run_backtest as run_v1, INITIAL_CAPITAL,
    UNIVERSE, TICKERS
)
from daily_swing_v2 import (
    enrich as enrich_v2, run_backtest as run_v2
)

import yfinance as yf

OUTPUT_DIR = '/Users/vladislavmolok/Documents/Торговый БОТ/backend'

print("Downloading D1 candles...")
raw_data = {}
data_v1 = {}
data_v2 = {}
for sym, ticker in TICKERS.items():
    df = yf.download(ticker, period='3y', interval='1d', auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)
    df.index = pd.to_datetime(df.index)
    raw_data[sym] = df.copy()
    data_v1[sym] = enrich_v1(df.copy())
    data_v2[sym] = enrich_v2(df.copy())
    print(f"  {sym}: {len(df)} candles")

common_dates = data_v1['BTC'].index
for sym in UNIVERSE:
    common_dates = common_dates.intersection(data_v1[sym].index).intersection(data_v2[sym].index)
for sym in UNIVERSE:
    data_v1[sym] = data_v1[sym].loc[common_dates]
    data_v2[sym] = data_v2[sym].loc[common_dates]
    raw_data[sym] = raw_data[sym].loc[common_dates]

print("Running V1 backtest...")
trades_v1, eq_v1, dd_v1, ec_v1 = run_v1(data_v1, INITIAL_CAPITAL)
print("Running V2 backtest...")
trades_v2, eq_v2, dd_v2, ec_v2 = run_v2(data_v2, INITIAL_CAPITAL, verbose=True)

eq_df1 = pd.DataFrame(ec_v1).set_index('date')
eq_df2 = pd.DataFrame(ec_v2).set_index('date')

# ═══════════════════════════════════════════════
# CHART 1: V1 vs V2 Equity Curve
# ═══════════════════════════════════════════════
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10), height_ratios=[3, 1],
                                 sharex=True, gridspec_kw={'hspace': 0.05})

ax1.plot(eq_df1.index, eq_df1['equity'], color='#999999', linewidth=1.2,
         label=f'V1: CAGR {((eq_v1/INITIAL_CAPITAL)**(365.25/len(ec_v1))-1)*100:+.1f}%, '
               f'Sharpe {eq_df1["equity"].pct_change().mean()/eq_df1["equity"].pct_change().std()*np.sqrt(365):.2f}, '
               f'MaxDD {dd_v1*100:.1f}%',
         linestyle='--', alpha=0.7)
ax1.plot(eq_df2.index, eq_df2['equity'], color='#2196F3', linewidth=2,
         label=f'V2: CAGR {((eq_v2/INITIAL_CAPITAL)**(365.25/len(ec_v2))-1)*100:+.1f}%, '
               f'Sharpe {eq_df2["equity"].pct_change().mean()/eq_df2["equity"].pct_change().std()*np.sqrt(365):.2f}, '
               f'MaxDD {dd_v2*100:.1f}%')
ax1.axhline(INITIAL_CAPITAL, color='gray', linestyle='--', alpha=0.5, linewidth=0.8)
ax1.set_ylabel('Equity ($)', fontsize=12)
ax1.set_title('V1 (gray dashed) vs V2 (blue) — Equity Curve Comparison', fontsize=14, fontweight='bold')
ax1.legend(loc='upper left', fontsize=10)
ax1.grid(True, alpha=0.3)
ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'${x:,.0f}'))

# Drawdown
for eq_df, color, label in [(eq_df1, '#999999', 'V1'), (eq_df2, '#2196F3', 'V2')]:
    running_max = eq_df['equity'].cummax()
    dd = (eq_df['equity'] - running_max) / running_max * 100
    ax2.plot(eq_df.index, dd, color=color, linewidth=0.8, label=label, alpha=0.7)
ax2.set_ylabel('Drawdown (%)', fontsize=12)
ax2.set_xlabel('Date', fontsize=12)
ax2.grid(True, alpha=0.3)
ax2.legend(loc='lower left', fontsize=10)
ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
plt.xticks(rotation=45)

plt.tight_layout()
plt.savefig(f'{OUTPUT_DIR}/chart_v1_vs_v2.png', dpi=150, bbox_inches='tight')
plt.close()
print("Saved: chart_v1_vs_v2.png")

# ═══════════════════════════════════════════════
# CHART 2: V2 Signals on candles
# ═══════════════════════════════════════════════
colors_map = {'BTC': '#F7931A', 'ETH': '#627EEA', 'BNB': '#F3BA2F', 'SOL': '#9945FF'}
fig, axes = plt.subplots(4, 1, figsize=(16, 20), sharex=False)

for i, sym in enumerate(UNIVERSE):
    ax = axes[i]
    df = raw_data[sym].copy()

    ax.fill_between(df.index, df['Low'], df['High'], alpha=0.15, color=colors_map[sym])
    ax.plot(df.index, df['Close'], color=colors_map[sym], linewidth=1, label=f'{sym} Close')

    df_v2 = data_v2[sym]
    ax.plot(df_v2.index, df_v2['EMA_fast'], color='#FF9800', linewidth=0.8, alpha=0.7, label=f'EMA 20')
    ax.plot(df_v2.index, df_v2['EMA_slow'], color='#9C27B0', linewidth=0.8, alpha=0.7, label=f'EMA 50')

    sym_trades = [t for t in trades_v2 if t['symbol'] == sym]
    pullbacks = [t for t in sym_trades if t.get('signal_type') == 'pullback']
    breakouts = [t for t in sym_trades if t.get('signal_type') == 'breakout']

    for t in pullbacks:
        marker_color = '#4CAF50' if t['pnl'] > 0 else '#F44336'
        if t['entry_date'] in df.index:
            ax.scatter(t['entry_date'], t['entry_price'], marker='^', color='#2196F3',
                      s=80, zorder=5, edgecolors='black', linewidth=0.5)
        if t['exit_date'] in df.index:
            ax.scatter(t['exit_date'], t['exit_price'], marker='v', color=marker_color,
                      s=80, zorder=5, edgecolors='black', linewidth=0.5)

    for t in breakouts:
        marker_color = '#4CAF50' if t['pnl'] > 0 else '#F44336'
        if t['entry_date'] in df.index:
            ax.scatter(t['entry_date'], t['entry_price'], marker='D', color='#FF9800',
                      s=80, zorder=5, edgecolors='black', linewidth=0.5)
        if t['exit_date'] in df.index:
            ax.scatter(t['exit_date'], t['exit_price'], marker='v', color=marker_color,
                      s=80, zorder=5, edgecolors='black', linewidth=0.5)

    n_trades = len(sym_trades)
    n_wins = len([t for t in sym_trades if t['pnl'] > 0])
    pnl_total = sum(t['pnl'] for t in sym_trades)
    ax.set_title(f'{sym}  |  {n_trades} trades, WR {n_wins/n_trades*100:.0f}%, '
                 f'PnL ${pnl_total:+,.0f}  |  ▲=pullback ◆=breakout',
                 fontsize=12, fontweight='bold')
    ax.set_ylabel('Price ($)', fontsize=10)
    ax.legend(loc='upper left', fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, fontsize=8)

plt.tight_layout()
plt.savefig(f'{OUTPUT_DIR}/chart_v2_signals.png', dpi=150, bbox_inches='tight')
plt.close()
print("Saved: chart_v2_signals.png")

# ═══════════════════════════════════════════════
# CHART 3: V2 Monthly heatmap
# ═══════════════════════════════════════════════
monthly_ret = eq_df2['equity'].resample('ME').last().pct_change().dropna() * 100
monthly_df = pd.DataFrame({
    'year': monthly_ret.index.year,
    'month': monthly_ret.index.month,
    'return': monthly_ret.values
})
pivot = monthly_df.pivot_table(index='year', columns='month', values='return')
month_names = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
pivot.columns = [month_names[m-1] for m in pivot.columns]

fig, ax = plt.subplots(figsize=(14, 4))
im = ax.imshow(pivot.values, cmap='RdYlGn', aspect='auto', vmin=-15, vmax=15)
ax.set_xticks(range(len(pivot.columns)))
ax.set_xticklabels(pivot.columns, fontsize=10)
ax.set_yticks(range(len(pivot.index)))
ax.set_yticklabels(pivot.index, fontsize=10)

for i in range(len(pivot.index)):
    for j in range(len(pivot.columns)):
        val = pivot.values[i, j]
        if not np.isnan(val):
            text_color = 'white' if abs(val) > 8 else 'black'
            ax.text(j, i, f'{val:+.1f}%', ha='center', va='center',
                   fontsize=9, color=text_color, fontweight='bold')

plt.colorbar(im, label='Monthly Return %', shrink=0.8)
ax.set_title('V2 Monthly Returns Heatmap', fontsize=13, fontweight='bold')
plt.tight_layout()
plt.savefig(f'{OUTPUT_DIR}/chart_v2_heatmap.png', dpi=150, bbox_inches='tight')
plt.close()
print("Saved: chart_v2_heatmap.png")

print("\n=== All V2 charts generated ===")
