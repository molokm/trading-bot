"""
Daily Swing Strategy — Full Backtest
CAGR +32.87%, Sharpe 0.88, Max DD -24.45% (claimed)
BTC/ETH/BNB/SOL on D1, 3 years (2023-2026)
Commissions: 0.1% per side
"""

import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

# === CONFIG ===
INITIAL_CAPITAL = 10_000
RISK_PER_TRADE = 0.02
ATR_STOP_MULT = 1.8
ATR_TARGET_MULT = 4.0
RSI_PERIOD = 14
RSI_ENTRY = 45
ROC_PERIOD = 5
ROC_MIN = -5.0
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
DONCHIAN_LEN = 20
COMMISSION_PCT = 0.001
SLIPPAGE_PCT = 0.0005
MAX_CONCURRENT = 3
UNIVERSE = ['BTC', 'ETH', 'BNB', 'SOL']
TICKERS = {
    'BTC': 'BTC-USD',
    'ETH': 'ETH-USD',
    'BNB': 'BNB-USD',
    'SOL': 'SOL-USD',
}


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)

def calc_atr(df, n=14):
    h, l, c = df['High'], df['Low'], df['Close']
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def roc(s, n=5):
    return s.pct_change(n) * 100


def enrich(df):
    df = df.copy()
    df['EMA_fast'] = ema(df['Close'], EMA_FAST)
    df['EMA_slow'] = ema(df['Close'], EMA_SLOW)
    df['RSI'] = rsi(df['Close'], RSI_PERIOD)
    df['ATR'] = calc_atr(df, ATR_PERIOD)
    df['ROC'] = roc(df['Close'], ROC_PERIOD)
    df['Trend_up'] = df['EMA_fast'] > df['EMA_slow']
    df['Donchian_high'] = df['Close'].rolling(DONCHIAN_LEN).max().shift(1)
    return df


def run_backtest(data_dict, initial_capital=INITIAL_CAPITAL, verbose=False):
    equity = initial_capital
    peak_equity = initial_capital
    max_drawdown = 0
    positions = {sym: None for sym in data_dict}
    all_trades = []
    equity_curve = []

    dates = data_dict['BTC'].index

    for date in dates:
        for sym, df in data_dict.items():
            if date not in df.index:
                continue
            idx = df.index.get_loc(date)
            row = df.iloc[idx]
            position = positions[sym]

            # --- Manage open position ---
            if position is not None:
                r_dist = position['entry_price'] - position['stop']
                if r_dist > 0:
                    new_peak_r = max(
                        position['peak_r'],
                        (row['High'] - position['entry_price']) / r_dist
                    )
                    position['peak_r'] = new_peak_r
                    if new_peak_r >= 1.0:
                        position['stop'] = max(position['stop'], position['entry_price'])
                    if new_peak_r >= 2.0:
                        lock = position['entry_price'] + new_peak_r * r_dist * 0.5
                        position['stop'] = max(position['stop'], lock)

                # Check stop
                if row['Low'] <= position['stop']:
                    exit_price = position['stop'] * (1 - SLIPPAGE_PCT)
                    entry_val = position['shares'] * position['entry_price']
                    exit_val = position['shares'] * exit_price
                    commission = (entry_val + exit_val) * COMMISSION_PCT
                    pnl = exit_val - entry_val - commission
                    equity += pnl
                    all_trades.append({
                        'symbol': sym, 'entry_date': position['entry_date'],
                        'exit_date': date, 'pnl': pnl, 'exit_reason': 'stop',
                        'entry_price': position['entry_price'], 'exit_price': exit_price,
                        'shares': position['shares'],
                    })
                    positions[sym] = None
                # Check target
                elif row['High'] >= position['target']:
                    exit_price = position['target'] * (1 - SLIPPAGE_PCT)
                    entry_val = position['shares'] * position['entry_price']
                    exit_val = position['shares'] * exit_price
                    commission = (entry_val + exit_val) * COMMISSION_PCT
                    pnl = exit_val - entry_val - commission
                    equity += pnl
                    all_trades.append({
                        'symbol': sym, 'entry_date': position['entry_date'],
                        'exit_date': date, 'pnl': pnl, 'exit_reason': 'target',
                        'entry_price': position['entry_price'], 'exit_price': exit_price,
                        'shares': position['shares'],
                    })
                    positions[sym] = None

            # --- Check entry ---
            if positions[sym] is None and idx > 0:
                open_count = sum(1 for p in positions.values() if p is not None)
                if open_count >= MAX_CONCURRENT:
                    continue
                prev = df.iloc[idx - 1]
                trend_up = prev['Trend_up']
                rsi_oversold = prev['RSI'] < RSI_ENTRY
                momentum_ok = prev['ROC'] > ROC_MIN
                atr_valid = prev['ATR'] > 0 and not np.isnan(prev['ATR'])
                breakout = (not np.isnan(prev['Donchian_high'])) and (prev['Close'] > prev['Donchian_high'])

                signal_mr = trend_up and rsi_oversold and momentum_ok and atr_valid
                signal_bo = trend_up and breakout and atr_valid

                if signal_mr or signal_bo:
                    entry_price = row['Open'] * (1 + SLIPPAGE_PCT)
                    atr_val = prev['ATR']
                    stop = entry_price - ATR_STOP_MULT * atr_val
                    target = entry_price + ATR_TARGET_MULT * atr_val
                    risk_per_share = entry_price - stop
                    if risk_per_share <= 0:
                        continue

                    risk_dollars = equity * RISK_PER_TRADE
                    shares = risk_dollars / risk_per_share
                    max_shares = (equity * 0.5) / entry_price
                    shares = min(shares, max_shares)
                    if shares <= 0 or entry_price * shares > equity * 0.5:
                        continue

                    positions[sym] = {
                        'entry_date': date, 'entry_price': entry_price,
                        'stop': stop, 'target': target, 'shares': shares,
                        'peak_r': 0,
                    }

        # Track equity curve
        total_equity = equity
        for sym, pos in positions.items():
            if pos is not None:
                cur_price = data_dict[sym].loc[date, 'Close'] if date in data_dict[sym].index else pos['entry_price']
                total_equity += pos['shares'] * (cur_price - pos['entry_price'])

        equity_curve.append({'date': date, 'equity': total_equity})
        peak_equity = max(peak_equity, total_equity)
        dd = (peak_equity - total_equity) / peak_equity
        max_drawdown = max(max_drawdown, dd)

    # Close remaining positions at last price
    for sym, pos in positions.items():
        if pos is not None:
            last_price = data_dict[sym]['Close'].iloc[-1]
            exit_price = last_price * (1 - SLIPPAGE_PCT)
            entry_val = pos['shares'] * pos['entry_price']
            exit_val = pos['shares'] * exit_price
            commission = (entry_val + exit_val) * COMMISSION_PCT
            pnl = exit_val - entry_val - commission
            equity += pnl
            all_trades.append({
                'symbol': sym, 'entry_date': pos['entry_date'],
                'exit_date': dates[-1], 'pnl': pnl, 'exit_reason': 'forced_close',
                'entry_price': pos['entry_price'], 'exit_price': exit_price,
                'shares': pos['shares'],
            })

    return all_trades, equity, max_drawdown, equity_curve


def calc_metrics(trades, initial_capital, final_equity, max_dd, equity_curve):
    # Trade stats
    n_trades = len(trades)
    winners = [t for t in trades if t['pnl'] > 0]
    losers = [t for t in trades if t['pnl'] <= 0]
    win_rate = len(winners) / n_trades * 100 if n_trades else 0
    avg_win = np.mean([t['pnl'] for t in winners]) if winners else 0
    avg_loss = np.mean([t['pnl'] for t in losers]) if losers else 0
    profit_factor = abs(sum(t['pnl'] for t in winners) / sum(t['pnl'] for t in losers)) if losers and sum(t['pnl'] for t in losers) != 0 else float('inf')

    # Returns
    total_return_pct = (final_equity / initial_capital - 1) * 100
    years = len(equity_curve) / 365.25
    cagr = ((final_equity / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0

    # Sharpe (daily returns)
    eq_df = pd.DataFrame(equity_curve).set_index('date')
    daily_ret = eq_df['equity'].pct_change().dropna()
    sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(365)) if daily_ret.std() > 0 else 0

    # Sortino
    neg_ret = daily_ret[daily_ret < 0]
    sortino = (daily_ret.mean() / neg_ret.std() * np.sqrt(365)) if len(neg_ret) > 0 and neg_ret.std() > 0 else 0

    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        r = t['exit_reason']
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # Per-symbol breakdown
    symbol_stats = {}
    for sym in ['BTC', 'ETH', 'BNB', 'SOL']:
        sym_trades = [t for t in trades if t['symbol'] == sym]
        sym_pnl = sum(t['pnl'] for t in sym_trades)
        sym_winners = len([t for t in sym_trades if t['pnl'] > 0])
        symbol_stats[sym] = {
            'trades': len(sym_trades),
            'win_rate': sym_winners / len(sym_trades) * 100 if sym_trades else 0,
            'total_pnl': sym_pnl,
        }

    return {
        'n_trades': n_trades,
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor,
        'total_return_pct': total_return_pct,
        'cagr': cagr,
        'sharpe': sharpe,
        'sortino': sortino,
        'max_drawdown': max_dd * 100,
        'exit_reasons': exit_reasons,
        'symbol_stats': symbol_stats,
        'final_equity': final_equity,
    }


if __name__ == '__main__':
    import yfinance as yf

    print("=== Daily Swing Strategy — Backtest ===\n")
    print("Downloading D1 candles (3 years)...")

    data = {}
    for sym, ticker in TICKERS.items():
        df = yf.download(ticker, period='3y', interval='1d', auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)
        df.index = pd.to_datetime(df.index)
        print(f"  {sym}: {len(df)} candles ({df.index[0].date()} to {df.index[-1].date()})")
        data[sym] = enrich(df)

    # Align all to common dates
    common_dates = data['BTC'].index
    for sym in UNIVERSE:
        common_dates = common_dates.intersection(data[sym].index)
    for sym in UNIVERSE:
        data[sym] = data[sym].loc[common_dates]

    print(f"\nCommon dates: {len(common_dates)} days ({common_dates[0].date()} to {common_dates[-1].date()})")
    print(f"\nRunning backtest with ${INITIAL_CAPITAL:,} initial capital...\n")

    trades, final_equity, max_dd, equity_curve = run_backtest(data, INITIAL_CAPITAL)
    metrics = calc_metrics(trades, INITIAL_CAPITAL, final_equity, max_dd, equity_curve)

    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"  Initial Capital:   ${INITIAL_CAPITAL:>12,}")
    print(f"  Final Equity:      ${metrics['final_equity']:>12,.2f}")
    print(f"  Total Return:      {metrics['total_return_pct']:>+11.2f}%")
    print(f"  CAGR:              {metrics['cagr']:>+11.2f}%")
    print(f"  Sharpe Ratio:      {metrics['sharpe']:>11.2f}")
    print(f"  Sortino Ratio:     {metrics['sortino']:>11.2f}")
    print(f"  Max Drawdown:      {metrics['max_drawdown']:>11.2f}%")
    print(f"  Profit Factor:     {metrics['profit_factor']:>11.2f}")
    print(f"  Total Trades:      {metrics['n_trades']:>11d}")
    print(f"  Win Rate:          {metrics['win_rate']:>11.1f}%")
    print(f"  Avg Win:           ${metrics['avg_win']:>12,.2f}")
    print(f"  Avg Loss:          ${metrics['avg_loss']:>12,.2f}")
    print()
    print("Exit Reasons:")
    for reason, count in metrics['exit_reasons'].items():
        print(f"  {reason:20s}: {count}")
    print()
    print("Per-Symbol:")
    for sym, stats in metrics['symbol_stats'].items():
        print(f"  {sym:4s}: {stats['trades']:3d} trades, "
              f"WR {stats['win_rate']:.1f}%, "
              f"PnL ${stats['total_pnl']:+,.2f}")
    print("=" * 60)

    # Top 5 / Bottom 5 trades
    if trades:
        sorted_trades = sorted(trades, key=lambda t: t['pnl'], reverse=True)
        print("\nTop 5 winning trades:")
        for t in sorted_trades[:5]:
            print(f"  {t['symbol']:4s} {str(t['entry_date'].date()):>12s} -> {str(t['exit_date'].date()):>12s}  "
                  f"PnL ${t['pnl']:+,.2f} ({t['exit_reason']})")
        print("\nBottom 5 losing trades:")
        for t in sorted_trades[-5:]:
            print(f"  {t['symbol']:4s} {str(t['entry_date'].date()):>12s} -> {str(t['exit_date'].date()):>12s}  "
                  f"PnL ${t['pnl']:+,.2f} ({t['exit_reason']})")

    # Monthly returns
    eq_df = pd.DataFrame(equity_curve).set_index('date')
    monthly = eq_df['equity'].resample('ME').last().pct_change().dropna()
    print(f"\nMonthly returns: {len(monthly)} months")
    print(f"  Positive: {sum(1 for r in monthly if r > 0)} / {len(monthly)}")
    print(f"  Best:  {monthly.max()*100:+.2f}%")
    print(f"  Worst: {monthly.min()*100:+.2f}%")
    print(f"  Avg:   {monthly.mean()*100:+.2f}%")
