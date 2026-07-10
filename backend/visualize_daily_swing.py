"""
Daily Swing Strategy — Full Visual Backtest
Charts: equity curve, drawdown, per-symbol candles with signals, monthly heatmap
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
    enrich, run_backtest, calc_metrics, INITIAL_CAPITAL,
    UNIVERSE, TICKERS, COMMISSION_PCT, SLIPPAGE_PCT,
    RISK_PER_TRADE, ATR_STOP_MULT, ATR_TARGET_MULT,
    RSI_ENTRY, ROC_MIN, EMA_FAST, EMA_SLOW, DONCHIAN_LEN
)

import yfinance as yf

OUTPUT_DIR = '/Users/vladislavmolok/Documents/Торговый БОТ/backend'

print("Downloading D1 candles...")
data = {}
raw_data = {}
for sym, ticker in TICKERS.items():
    df = yf.download(ticker, period='3y', interval='1d', auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)
    df.index = pd.to_datetime(df.index)
    raw_data[sym] = df.copy()
    data[sym] = enrich(df)
    print(f"  {sym}: {len(df)} candles")

# Align
common_dates = data['BTC'].index
for sym in UNIVERSE:
    common_dates = common_dates.intersection(data[sym].index)
for sym in UNIVERSE:
    data[sym] = data[sym].loc[common_dates]
    raw_data[sym] = raw_data[sym].loc[common_dates]

print("Running backtest...")
trades, final_equity, max_dd, equity_curve = run_backtest(data, INITIAL_CAPITAL)
metrics = calc_metrics(trades, INITIAL_CAPITAL, final_equity, max_dd, equity_curve)
eq_df = pd.DataFrame(equity_curve).set_index('date')

# ═══════════════════════════════════════════════
# CHART 1: Equity Curve + Drawdown
# ═══════════════════════════════════════════════
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10), height_ratios=[3, 1],
                                 sharex=True, gridspec_kw={'hspace': 0.05})

# Equity
ax1.plot(eq_df.index, eq_df['equity'], color='#2196F3', linewidth=1.5, label='Equity')
ax1.axhline(INITIAL_CAPITAL, color='gray', linestyle='--', alpha=0.5, linewidth=0.8)
ax1.fill_between(eq_df.index, INITIAL_CAPITAL, eq_df['equity'],
                  where=eq_df['equity'] >= INITIAL_CAPITAL,
                  color='#4CAF50', alpha=0.1)
ax1.fill_between(eq_df.index, INITIAL_CAPITAL, eq_df['equity'],
                  where=eq_df['equity'] < INITIAL_CAPITAL,
                  color='#F44336', alpha=0.1)
ax1.set_ylabel('Equity ($)', fontsize=12)
ax1.set_title(f'Daily Swing Strategy — CAGR {metrics["cagr"]:+.1f}% | '
              f'Sharpe {metrics["sharpe"]:.2f} | MaxDD {metrics["max_drawdown"]:.1f}% | '
              f'PF {metrics["profit_factor"]:.2f}',
              fontsize=14, fontweight='bold')
ax1.legend(loc='upper left', fontsize=10)
ax1.grid(True, alpha=0.3)
ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'${x:,.0f}'))

# Drawdown
running_max = eq_df['equity'].cummax()
drawdown = (eq_df['equity'] - running_max) / running_max * 100
ax2.fill_between(eq_df.index, 0, drawdown, color='#F44336', alpha=0.4)
ax2.plot(eq_df.index, drawdown, color='#D32F2F', linewidth=0.8)
ax2.set_ylabel('Drawdown (%)', fontsize=12)
ax2.set_xlabel('Date', fontsize=12)
ax2.grid(True, alpha=0.3)
ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
plt.xticks(rotation=45)

plt.tight_layout()
plt.savefig(f'{OUTPUT_DIR}/chart_equity_drawdown.png', dpi=150, bbox_inches='tight')
plt.close()
print("Saved: chart_equity_drawdown.png")

# ═══════════════════════════════════════════════
# CHART 2: Per-Symbol Candle + Signals
# ═══════════════════════════════════════════════
fig, axes = plt.subplots(4, 1, figsize=(16, 20), sharex=False)
colors_map = {'BTC': '#F7931A', 'ETH': '#627EEA', 'BNB': '#F3BA2F', 'SOL': '#9945FF'}

for i, sym in enumerate(UNIVERSE):
    ax = axes[i]
    df = raw_data[sym].copy()
    df_enriched = data[sym]

    # Candlestick-like: just close line + high/low range
    ax.fill_between(df.index, df['Low'], df['High'], alpha=0.15, color=colors_map[sym])
    ax.plot(df.index, df['Close'], color=colors_map[sym], linewidth=1, label=f'{sym} Close')

    # EMAs
    ax.plot(df_enriched.index, df_enriched['EMA_fast'], color='#FF9800',
            linewidth=0.8, alpha=0.7, label=f'EMA {EMA_FAST}')
    ax.plot(df_enriched.index, df_enriched['EMA_slow'], color='#9C27B0',
            linewidth=0.8, alpha=0.7, label=f'EMA {EMA_SLOW}')

    # Entry/exit markers
    sym_trades = [t for t in trades if t['symbol'] == sym]
    for t in sym_trades:
        entry_date = t['entry_date']
        exit_date = t['exit_date']
        entry_price = t['entry_price']
        exit_price = t['exit_price']
        pnl = t['pnl']

        marker_color = '#4CAF50' if pnl > 0 else '#F44336'
        marker = '^' if pnl > 0 else 'v'

        # Entry
        if entry_date in df.index:
            ax.scatter(entry_date, entry_price, marker='^', color='#2196F3',
                      s=60, zorder=5, edgecolors='black', linewidth=0.5)
        # Exit
        if exit_date in df.index:
            ax.scatter(exit_date, exit_price, marker='v', color=marker_color,
                      s=60, zorder=5, edgecolors='black', linewidth=0.5)
        # Line between
        if entry_date in df.index and exit_date in df.index:
            ax.plot([entry_date, exit_date], [entry_price, exit_price],
                   color=marker_color, linewidth=0.6, alpha=0.5)

    n_trades = len(sym_trades)
    n_wins = len([t for t in sym_trades if t['pnl'] > 0])
    pnl_total = sum(t['pnl'] for t in sym_trades)
    ax.set_title(f'{sym}  |  {n_trades} trades, WR {n_wins/n_trades*100:.0f}%, '
                 f'PnL ${pnl_total:+,.0f}', fontsize=12, fontweight='bold')
    ax.set_ylabel('Price ($)', fontsize=10)
    ax.legend(loc='upper left', fontsize=8)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, fontsize=8)

plt.tight_layout()
plt.savefig(f'{OUTPUT_DIR}/chart_signals.png', dpi=150, bbox_inches='tight')
plt.close()
print("Saved: chart_signals.png")

# ═══════════════════════════════════════════════
# CHART 3: Monthly Returns Heatmap
# ═══════════════════════════════════════════════
monthly_ret = eq_df['equity'].resample('ME').last().pct_change().dropna() * 100
monthly_df = pd.DataFrame({
    'year': monthly_ret.index.year,
    'month': monthly_ret.index.month,
    'return': monthly_ret.values
})
pivot = monthly_df.pivot_table(index='year', columns='month', values='return')
pivot.columns = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][:len(pivot.columns)]

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
ax.set_title('Monthly Returns Heatmap', fontsize=13, fontweight='bold')
plt.tight_layout()
plt.savefig(f'{OUTPUT_DIR}/chart_monthly_heatmap.png', dpi=150, bbox_inches='tight')
plt.close()
print("Saved: chart_monthly_heatmap.png")

# ═══════════════════════════════════════════════
# CHART 4: Trade Distribution
# ═══════════════════════════════════════════════
fig, axes = plt.subplots(1, 3, figsize=(16, 5))

# PnL distribution
pnl_values = [t['pnl'] for t in trades]
colors = ['#4CAF50' if p > 0 else '#F44336' for p in pnl_values]
axes[0].bar(range(len(pnl_values)), sorted(pnl_values), color=sorted(colors, key=lambda x: x), width=1)
axes[0].axhline(0, color='gray', linewidth=0.8)
axes[0].set_xlabel('Trade #')
axes[0].set_ylabel('PnL ($)')
axes[0].set_title('Trade PnL Distribution')
axes[0].grid(True, alpha=0.3)

# Cumulative PnL
cum_pnl = np.cumsum(pnl_values)
axes[1].plot(cum_pnl, color='#2196F3', linewidth=1.5)
axes[1].axhline(0, color='gray', linewidth=0.8, linestyle='--')
axes[1].set_xlabel('Trade #')
axes[1].set_ylabel('Cumulative PnL ($)')
axes[1].set_title('Cumulative PnL')
axes[1].grid(True, alpha=0.3)

# Win/Loss by symbol
symbols = list(metrics['symbol_stats'].keys())
win_rates = [metrics['symbol_stats'][s]['win_rate'] for s in symbols]
bar_colors = [colors_map[s] for s in symbols]
axes[2].bar(symbols, win_rates, color=bar_colors, edgecolor='black', linewidth=0.5)
axes[2].axhline(50, color='gray', linewidth=0.8, linestyle='--')
axes[2].set_ylabel('Win Rate (%)')
axes[2].set_title('Win Rate by Symbol')
axes[2].set_ylim(0, 100)
axes[2].grid(True, alpha=0.3)

plt.tight_layout()
plt.savefig(f'{OUTPUT_DIR}/chart_trade_analysis.png', dpi=150, bbox_inches='tight')
plt.close()
print("Saved: chart_trade_analysis.png")

# ═══════════════════════════════════════════════
# CHART 5: R-R distribution (entry -> stop vs entry -> target)
# ═══════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(10, 5))
r_multiples = []
for t in trades:
    risk = t['entry_price'] - (t['entry_price'] * (1 - ATR_STOP_MULT * 0.01))  # approximate
    if t['pnl'] > 0:
        # Won
        r_multiples.append(t['pnl'] / (t['shares'] * ATR_STOP_MULT * 0.01 * t['entry_price']))
    else:
        r_multiples.append(t['pnl'] / (t['shares'] * ATR_STOP_MULT * 0.01 * t['entry_price']))

# Simpler: just PnL distribution
positive = [t['pnl'] for t in trades if t['pnl'] > 0]
negative = [t['pnl'] for t in trades if t['pnl'] <= 0]
ax.hist(positive, bins=30, color='#4CAF50', alpha=0.7, label=f'Wins ({len(positive)})', edgecolor='black')
ax.hist(negative, bins=30, color='#F44336', alpha=0.7, label=f'Losses ({len(negative)})', edgecolor='black')
ax.axvline(np.mean(positive), color='#4CAF50', linestyle='--', linewidth=2, label=f'Avg Win: ${np.mean(positive):,.0f}')
ax.axvline(np.mean(negative), color='#F44336', linestyle='--', linewidth=2, label=f'Avg Loss: ${np.mean(negative):,.0f}')
ax.set_xlabel('PnL ($)')
ax.set_ylabel('Count')
ax.set_title('Win/Loss PnL Distribution')
ax.legend()
ax.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig(f'{OUTPUT_DIR}/chart_pnl_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print("Saved: chart_pnl_distribution.png")

print("\n=== All charts generated ===")
