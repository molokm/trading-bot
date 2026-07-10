"""
Daily Swing Strategy V3 — Minimal
V1 params + ADX filter only. No trailing stop.
"""

import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

INITIAL_CAPITAL = 10_000
RISK_PER_TRADE = 0.015             # 1.5% (was 2%, to reduce MaxDD)
ATR_STOP_MULT = 1.8
ATR_TARGET_MULT = 4.5              # R:R = 1:2.5
RSI_PERIOD = 14
RSI_PULLBACK = 45
ROC_MIN = -3.0
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
DONCHIAN_LEN = 20
COMMISSION_PCT = 0.001
SLIPPAGE_PCT = 0.0005
MAX_CONCURRENT = 3
UNIVERSE = ['BTC', 'ETH', 'BNB', 'SOL']
TICKERS = {
    'BTC': 'BTC-USD', 'ETH': 'ETH-USD', 'BNB': 'BNB-USD', 'SOL': 'SOL-USD',
}
ADX_MIN = 18
COOLDOWN_DAYS = 3
DD_CIRCUIT_BREAKER = 0.15         # 15% (was 20%)
DD_COOLDOWN_DAYS = 10


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

def adx(df, n=14):
    h, l, c = df['High'], df['Low'], df['Close']
    up_move = h - h.shift(1)
    down_move = l.shift(1) - l
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr_val = tr.ewm(alpha=1/n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_val
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_val
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1/n, adjust=False).mean()
    return adx_val, plus_di, minus_di


def enrich(df):
    df = df.copy()
    df['EMA_fast'] = ema(df['Close'], EMA_FAST)
    df['EMA_slow'] = ema(df['Close'], EMA_SLOW)
    df['RSI'] = rsi(df['Close'], RSI_PERIOD)
    df['ATR'] = calc_atr(df, ATR_PERIOD)
    df['ROC'] = roc(df['Close'], 5)
    df['Trend_up'] = df['EMA_fast'] > df['EMA_slow']
    df['Donchian_high'] = df['Close'].rolling(DONCHIAN_LEN).max().shift(1)
    adx_val, plus_di, minus_di = adx(df, 14)
    df['ADX'] = adx_val
    df['Plus_DI'] = plus_di
    df['Minus_DI'] = minus_di
    df['ATR_pct'] = df['ATR'] / df['Close']
    df['Dist_EMA'] = (df['Close'] - df['EMA_fast']) / df['EMA_fast']
    return df


def run_backtest(data_dict, initial_capital=INITIAL_CAPITAL, verbose=False):
    equity = initial_capital
    peak_equity = initial_capital
    max_drawdown = 0
    positions = {sym: None for sym in data_dict}
    last_trade_idx = {sym: -COOLDOWN_DAYS - 1 for sym in data_dict}
    all_trades = []
    equity_curve = []
    circuit_breaker_until = -1
    circuit_breaker_triggered = False

    dates = data_dict['BTC'].index

    for day_idx, date in enumerate(dates):
        if circuit_breaker_until > day_idx:
            total_equity = equity
            for sym, pos in positions.items():
                if pos is not None and date in data_dict[sym].index:
                    cur_price = data_dict[sym].loc[date, 'Close']
                    total_equity += pos['shares'] * (cur_price - pos['entry_price'])
            equity_curve.append({'date': date, 'equity': total_equity})
            continue

        for sym, df in data_dict.items():
            if date not in df.index:
                continue
            idx = df.index.get_loc(date)
            row = df.iloc[idx]
            position = positions[sym]

            if position is not None:
                # Check stop
                if row['Low'] <= position['stop']:
                    exit_price = max(position['stop'] * (1 - SLIPPAGE_PCT), row['Low'])
                    entry_val = position['shares'] * position['entry_price']
                    exit_val = position['shares'] * exit_price
                    commission = (entry_val + exit_val) * COMMISSION_PCT
                    pnl = exit_val - entry_val - commission
                    equity += pnl
                    all_trades.append({
                        'symbol': sym, 'entry_date': position['entry_date'],
                        'exit_date': date, 'pnl': pnl, 'exit_reason': 'stop',
                        'entry_price': position['entry_price'], 'exit_price': exit_price,
                        'shares': position['shares'], 'signal_type': position.get('signal_type', '?'),
                    })
                    positions[sym] = None
                    last_trade_idx[sym] = day_idx
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
                        'shares': position['shares'], 'signal_type': position.get('signal_type', '?'),
                    })
                    positions[sym] = None
                    last_trade_idx[sym] = day_idx

            if positions[sym] is None and idx > 0 and day_idx > circuit_breaker_until:
                if (day_idx - last_trade_idx[sym]) < COOLDOWN_DAYS:
                    continue
                open_count = sum(1 for p in positions.values() if p is not None)
                if open_count >= MAX_CONCURRENT:
                    continue

                prev = df.iloc[idx - 1]

                trend_up = prev['Trend_up']
                adx_ok = prev['ADX'] > ADX_MIN and not np.isnan(prev['ADX'])
                atr_pct_ok = prev['ATR_pct'] > 0.012 and not np.isnan(prev['ATR_pct'])
                momentum_ok = prev['ROC'] > ROC_MIN

                if not (trend_up and adx_ok and atr_pct_ok and momentum_ok):
                    continue

                signal = False
                signal_type = None

                # PULLBACK
                pullback = (-0.04 < prev['Dist_EMA'] < 0.025)
                rsi_pullback = prev['RSI'] < RSI_PULLBACK
                rsi_not_dead = prev['RSI'] > 28
                if pullback and rsi_pullback and rsi_not_dead:
                    signal = True
                    signal_type = 'pullback'

                # BREAKOUT
                if not signal:
                    breakout = (not np.isnan(prev['Donchian_high'])) and (prev['Close'] > prev['Donchian_high'])
                    di_ok = prev['Plus_DI'] > prev['Minus_DI']
                    if breakout and di_ok:
                        signal = True
                        signal_type = 'breakout'

                # MOMENTUM
                if not signal:
                    if prev['RSI'] > 55 and prev['ROC'] > 2.0 and trend_up:
                        signal = True
                        signal_type = 'momentum'

                if signal:
                    entry_price = row['Open'] * (1 + SLIPPAGE_PCT)
                    atr_val = prev['ATR']
                    stop = entry_price - ATR_STOP_MULT * atr_val
                    target = entry_price + ATR_TARGET_MULT * atr_val
                    risk_per_share = entry_price - stop
                    if risk_per_share <= 0:
                        continue

                    # Dynamic sizing: reduce in drawdown
                    current_dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
                    risk_mult = 1.0
                    if current_dd > 0.10:
                        risk_mult = 0.6
                    elif current_dd > 0.05:
                        risk_mult = 0.8

                    risk_dollars = equity * RISK_PER_TRADE * risk_mult
                    shares = risk_dollars / risk_per_share
                    max_shares = (equity * 0.35) / entry_price
                    shares = min(shares, max_shares)
                    if shares <= 0:
                        continue

                    positions[sym] = {
                        'entry_date': date, 'entry_price': entry_price,
                        'stop': stop, 'target': target, 'shares': shares,
                        'signal_type': signal_type,
                    }

        total_equity = equity
        for sym, pos in positions.items():
            if pos is not None and date in data_dict[sym].index:
                cur_price = data_dict[sym].loc[date, 'Close']
                total_equity += pos['shares'] * (cur_price - pos['entry_price'])

        equity_curve.append({'date': date, 'equity': total_equity})
        peak_equity = max(peak_equity, total_equity)
        dd = (peak_equity - total_equity) / peak_equity
        max_drawdown = max(max_drawdown, dd)

        if dd >= DD_CIRCUIT_BREAKER and not circuit_breaker_triggered:
            circuit_breaker_until = day_idx + DD_COOLDOWN_DAYS
            circuit_breaker_triggered = True
            if verbose:
                print(f"  [CIRCUIT BREAKER] DD={dd*100:.1f}% at {date.date()}")
        elif dd < DD_CIRCUIT_BREAKER * 0.5:
            circuit_breaker_triggered = False

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
                'shares': pos['shares'], 'signal_type': pos.get('signal_type', '?'),
            })

    return all_trades, equity, max_drawdown, equity_curve


def calc_metrics(trades, initial_capital, final_equity, max_dd, equity_curve):
    n_trades = len(trades)
    winners = [t for t in trades if t['pnl'] > 0]
    losers = [t for t in trades if t['pnl'] <= 0]
    win_rate = len(winners) / n_trades * 100 if n_trades else 0
    avg_win = np.mean([t['pnl'] for t in winners]) if winners else 0
    avg_loss = np.mean([t['pnl'] for t in losers]) if losers else 0
    profit_factor = abs(sum(t['pnl'] for t in winners) / sum(t['pnl'] for t in losers)) if losers and sum(t['pnl'] for t in losers) != 0 else float('inf')
    total_return_pct = (final_equity / initial_capital - 1) * 100
    years = len(equity_curve) / 365.25
    cagr = ((final_equity / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0
    eq_df = pd.DataFrame(equity_curve).set_index('date')
    daily_ret = eq_df['equity'].pct_change().dropna()
    sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(365)) if daily_ret.std() > 0 else 0
    neg_ret = daily_ret[daily_ret < 0]
    sortino = (daily_ret.mean() / neg_ret.std() * np.sqrt(365)) if len(neg_ret) > 0 and neg_ret.std() > 0 else 0
    exit_reasons = {}
    for t in trades:
        exit_reasons[t['exit_reason']] = exit_reasons.get(t['exit_reason'], 0) + 1
    signal_types = {}
    for t in trades:
        st = t.get('signal_type', '?')
        signal_types[st] = signal_types.get(st, 0) + 1
    symbol_stats = {}
    for sym in UNIVERSE:
        sym_trades = [t for t in trades if t['symbol'] == sym]
        sym_pnl = sum(t['pnl'] for t in sym_trades)
        sym_winners = len([t for t in sym_trades if t['pnl'] > 0])
        symbol_stats[sym] = {
            'trades': len(sym_trades),
            'win_rate': sym_winners / len(sym_trades) * 100 if sym_trades else 0,
            'total_pnl': sym_pnl,
        }
    monthly = {}
    for t in trades:
        month = t['exit_date'].strftime('%Y-%m')
        monthly[month] = monthly.get(month, 0) + t['pnl']
    return {
        'n_trades': n_trades, 'win_rate': win_rate, 'avg_win': avg_win,
        'avg_loss': avg_loss, 'profit_factor': profit_factor,
        'total_return_pct': total_return_pct, 'cagr': cagr, 'sharpe': sharpe,
        'sortino': sortino, 'max_drawdown': max_dd * 100,
        'exit_reasons': exit_reasons, 'signal_types': signal_types,
        'symbol_stats': symbol_stats, 'final_equity': final_equity,
        'monthly': monthly,
    }


if __name__ == '__main__':
    import yfinance as yf
    print("=== Daily Swing Strategy V3 — Minimal ===\n")
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
    common_dates = data['BTC'].index
    for sym in UNIVERSE:
        common_dates = common_dates.intersection(data[sym].index)
    for sym in UNIVERSE:
        data[sym] = data[sym].loc[common_dates]
    print(f"\nCommon dates: {len(common_dates)} days\n")
    trades, final_equity, max_dd, equity_curve = run_backtest(data, INITIAL_CAPITAL, verbose=True)
    metrics = calc_metrics(trades, INITIAL_CAPITAL, final_equity, max_dd, equity_curve)
    print("\n" + "=" * 60)
    print("V3 RESULTS")
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
    print("Exit Reasons:", metrics['exit_reasons'])
    print("Signal Types:", metrics['signal_types'])
    print("\nPer-Symbol:")
    for sym, st in metrics['symbol_stats'].items():
        print(f"  {sym:4s}: {st['trades']:3d} trades, WR {st['win_rate']:.1f}%, PnL ${st['total_pnl']:+,.2f}")
    print()
    print("Monthly Returns:")
    for month, pnl in sorted(metrics['monthly'].items()):
        bar = '+' * max(1, int(abs(pnl) / 50)) if pnl > 0 else '-' * max(1, int(abs(pnl) / 50))
        print(f"  {month}: ${pnl:>+8,.2f}  {bar}")
    print("=" * 60)
