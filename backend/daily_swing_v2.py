"""
Daily Swing Strategy V2 — Improved
Fixes V1 issues:
1. Separate pullback + breakout entry modes (no contradictory signals)
2. ADX regime filter (trending market only)
3. Volume confirmation
4. Drawdown circuit breaker
5. Wider stops, lower risk per trade
"""

import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

# === CONFIG ===
INITIAL_CAPITAL = 10_000
RISK_PER_TRADE = 0.015          # 1.5% (was 2%)
ATR_STOP_MULT = 2.0             # 2.0x ATR (was 1.8x, wider to survive noise)
ATR_TARGET_MULT = 4.5           # 4.5x ATR (R:R = 1:2.25)
RSI_PERIOD = 14
RSI_PULLBACK = 40               # Pullback: RSI < 40 (was 45)
ROC_PERIOD = 5
ROC_MIN = -3.0                  # Less negative threshold
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
DONCHIAN_LEN = 30               # 30-day (was 20, fewer false breakouts)
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

# V2 NEW FILTERS
ADX_PERIOD = 14
ADX_MIN = 20                     # Minimum ADX for trend
VOLUME_MULT = 1.5               # Volume must be > 1.5x 20-day avg
DD_CIRCUIT_BREAKER = 0.15       # Stop trading if DD > 15%
DD_COOLDOWN_DAYS = 10           # Wait 10 days after circuit breaker
ATR_PCT_MIN = 0.015             # Min ATR as % of price (avoid dead markets)
COOLDOWN_DAYS = 3               # Min days between trades on same symbol


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
    """Average Directional Index — trend strength"""
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
    df['ROC'] = roc(df['Close'], ROC_PERIOD)
    df['Trend_up'] = df['EMA_fast'] > df['EMA_slow']
    df['Donchian_high'] = df['Close'].rolling(DONCHIAN_LEN).max().shift(1)
    df['Donchian_low'] = df['Close'].rolling(DONCHIAN_LEN).min().shift(1)

    # V2 indicators
    adx_val, plus_di, minus_di = adx(df, ADX_PERIOD)
    df['ADX'] = adx_val
    df['Plus_DI'] = plus_di
    df['Minus_DI'] = minus_di
    df['Vol_sma'] = df['Volume'].rolling(20).mean()
    df['Vol_ratio'] = df['Volume'] / df['Vol_sma'].replace(0, np.nan)
    df['ATR_pct'] = df['ATR'] / df['Close']

    # Pullback proximity to EMA20
    df['Dist_EMA20'] = (df['Close'] - df['EMA_fast']) / df['EMA_fast']

    return df


def run_backtest(data_dict, initial_capital=INITIAL_CAPITAL, verbose=False):
    equity = initial_capital
    peak_equity = initial_capital
    max_drawdown = 0
    positions = {sym: None for sym in data_dict}
    last_trade_idx = {sym: -COOLDOWN_DAYS - 1 for sym in data_dict}  # per-symbol cooldown
    all_trades = []
    equity_curve = []
    circuit_breaker_until = -1  # index when circuit breaker resets
    circuit_breaker_triggered = False

    dates = data_dict['BTC'].index

    for day_idx, date in enumerate(dates):
        # Circuit breaker check
        if circuit_breaker_until > day_idx:
            # Track equity even when not trading
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
                    last_trade_idx[sym] = day_idx
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
                    last_trade_idx[sym] = day_idx

            # --- Check entry ---
            if positions[sym] is None and idx > 0 and day_idx > circuit_breaker_until:
                # Cooldown check
                if (day_idx - last_trade_idx[sym]) < COOLDOWN_DAYS:
                    continue
                open_count = sum(1 for p in positions.values() if p is not None)
                if open_count >= MAX_CONCURRENT:
                    continue

                prev = df.iloc[idx - 1]
                prev2 = df.iloc[idx - 2] if idx >= 2 else prev

                # === V2 FILTERS ===
                # 1. Trend must be up
                trend_up = prev['Trend_up']

                # 2. ADX must show trending market
                adx_ok = prev['ADX'] > ADX_MIN and not np.isnan(prev['ADX'])

                # 3. ATR must be meaningful (not dead market)
                atr_pct_ok = prev['ATR_pct'] > ATR_PCT_MIN and not np.isnan(prev['ATR_pct'])

                # 4. Volume confirmation
                vol_ok = prev['Vol_ratio'] > VOLUME_MULT and not np.isnan(prev['Vol_ratio'])

                # 5. Momentum not collapsing
                momentum_ok = prev['ROC'] > ROC_MIN

                if not (trend_up and adx_ok and atr_pct_ok and momentum_ok):
                    continue

                signal = False
                signal_type = None

                # === MODE 1: PULLBACK ENTRY ===
                # Price pulled back near EMA20 in uptrend
                pullback = (-0.03 < prev['Dist_EMA20'] < 0.02)  # Within -3% to +2% of EMA20
                rsi_pullback = prev['RSI'] < RSI_PULLBACK
                rsi_not_dead = prev['RSI'] > 25  # Not deeply oversold (panic)

                if pullback and rsi_pullback and rsi_not_dead and vol_ok:
                    signal = True
                    signal_type = 'pullback'

                # === MODE 2: BREAKOUT ENTRY ===
                # Price breaks 30-day high with volume
                if not signal:
                    breakout = (not np.isnan(prev['Donchian_high'])) and (prev['Close'] > prev['Donchian_high'])
                    # Extra: DI+ > DI- (bullish momentum)
                    di_ok = prev['Plus_DI'] > prev['Minus_DI']
                    if breakout and vol_ok and di_ok:
                        signal = True
                        signal_type = 'breakout'

                if signal:
                    entry_price = row['Open'] * (1 + SLIPPAGE_PCT)
                    atr_val = prev['ATR']
                    stop = entry_price - ATR_STOP_MULT * atr_val
                    target = entry_price + ATR_TARGET_MULT * atr_val
                    risk_per_share = entry_price - stop
                    if risk_per_share <= 0:
                        continue

                    risk_dollars = equity * RISK_PER_TRADE
                    shares = risk_dollars / risk_per_share
                    max_shares = (equity * 0.35) / entry_price  # 35% max (was 50%)
                    shares = min(shares, max_shares)
                    if shares <= 0 or entry_price * shares > equity * 0.35:
                        continue

                    positions[sym] = {
                        'entry_date': date, 'entry_price': entry_price,
                        'stop': stop, 'target': target, 'shares': shares,
                        'peak_r': 0, 'signal_type': signal_type,
                    }

        # Track equity
        total_equity = equity
        for sym, pos in positions.items():
            if pos is not None and date in data_dict[sym].index:
                cur_price = data_dict[sym].loc[date, 'Close']
                total_equity += pos['shares'] * (cur_price - pos['entry_price'])

        equity_curve.append({'date': date, 'equity': total_equity})
        peak_equity = max(peak_equity, total_equity)
        dd = (peak_equity - total_equity) / peak_equity
        max_drawdown = max(max_drawdown, dd)

        # Circuit breaker
        if dd >= DD_CIRCUIT_BREAKER and not circuit_breaker_triggered:
            circuit_breaker_until = day_idx + DD_COOLDOWN_DAYS
            circuit_breaker_triggered = True
            if verbose:
                print(f"  [CIRCUIT BREAKER] DD={dd*100:.1f}% at {date.date()}, resuming after {dates[circuit_breaker_until].date()}")
        elif dd < DD_CIRCUIT_BREAKER * 0.5:
            circuit_breaker_triggered = False

    # Close remaining positions
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
        r = t['exit_reason']
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

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
        'signal_types': signal_types,
        'symbol_stats': symbol_stats,
        'final_equity': final_equity,
    }


if __name__ == '__main__':
    import yfinance as yf

    print("=== Daily Swing Strategy V2 — Backtest ===\n")
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

    print(f"\nCommon dates: {len(common_dates)} days")
    print(f"\nRunning V2 backtest with ${INITIAL_CAPITAL:,} initial capital...\n")

    trades, final_equity, max_dd, equity_curve = run_backtest(data, INITIAL_CAPITAL, verbose=True)
    metrics = calc_metrics(trades, INITIAL_CAPITAL, final_equity, max_dd, equity_curve)

    print("\n" + "=" * 60)
    print("V2 RESULTS")
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
    print("Signal Types:")
    for st, count in metrics['signal_types'].items():
        print(f"  {st:20s}: {count}")
    print()
    print("Per-Symbol:")
    for sym, stats in metrics['symbol_stats'].items():
        print(f"  {sym:4s}: {stats['trades']:3d} trades, "
              f"WR {stats['win_rate']:.1f}%, "
              f"PnL ${stats['total_pnl']:+,.2f}")
    print("=" * 60)
