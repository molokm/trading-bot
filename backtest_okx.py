#!/usr/bin/env python3
"""Run Daily Swing Strategy backtest on real OKX data (3y, no look-ahead)."""

import httpx
import pandas as pd
import numpy as np
import time
import sys

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
MAX_CONCURRENT = 3
UNIVERSE = ['BTC-USDT', 'ETH-USDT', 'BNB-USDT', 'SOL-USDT']
COIN_NAMES = {'BTC-USDT': 'BTC', 'ETH-USDT': 'ETH', 'BNB-USDT': 'BNB', 'SOL-USDT': 'SOL'}

# === FETCH DATA FROM OKX ===
def fetch_candles(inst_id: str, bar: str = '1D', total_candles: int = 1100) -> pd.DataFrame:
    """Fetch daily candles from OKX, paginating backwards."""
    all_candles = []
    after = ''
    fetched = 0
    
    while fetched < total_candles:
        params = {'instId': inst_id, 'bar': bar, 'limit': '300'}
        if after:
            params['after'] = after
        
        resp = httpx.get('https://www.okx.com/api/v5/market/candles', params=params, timeout=15)
        data = resp.json()
        
        if data.get('code') != '0' or not data.get('data'):
            break
        
        candles = data['data']
        all_candles.extend(candles)
        fetched += len(candles)
        after = candles[-1][0]  # oldest ts for next page
        
        if len(candles) < 300:
            break
        time.sleep(0.2)
    
    if not all_candles:
        return pd.DataFrame()
    
    df = pd.DataFrame(all_candles, columns=[
        'ts', 'Open', 'High', 'Low', 'Close', 'Volume', 'VolCcy', 'VolCcyQuote', 'Confirm'
    ])
    df['ts'] = pd.to_datetime(df['ts'].astype(int), unit='ms')
    df.set_index('ts', inplace=True)
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)
    df.sort_index(inplace=True)
    return df

# === INDICATORS ===
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + gain/loss)

def atr(df, n=14):
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
    df['ATR'] = atr(df, ATR_PERIOD)
    df['ROC'] = roc(df['Close'], ROC_PERIOD)
    df['Trend_up'] = df['EMA_fast'] > df['EMA_slow']
    df['Donchian_high'] = df['Close'].rolling(DONCHIAN_LEN).max().shift(1)
    return df

# === BACKTEST ===
def run_backtest(data_dict, initial_capital=INITIAL_CAPITAL):
    equity_ref = {'value': initial_capital}
    equity_curve = []
    positions = {sym: None for sym in data_dict}
    all_trades = []
    
    # Build common date index
    all_dates = data_dict['BTC-USDT'].index
    
    for date in all_dates:
        for sym, df in data_dict.items():
            if date not in df.index:
                continue
            idx = df.index.get_loc(date)
            row = df.iloc[idx]
            position = positions[sym]
            
            if position is not None:
                r_dist = position['entry_price'] - position['stop']
                if r_dist > 0:
                    new_peak_r = max(position['peak_r'],
                                    (row['High'] - position['entry_price']) / r_dist)
                    position['peak_r'] = new_peak_r
                    if new_peak_r >= 1.0:
                        position['stop'] = max(position['stop'], position['entry_price'])
                    if new_peak_r >= 2.0:
                        lock = position['entry_price'] + new_peak_r * r_dist * 0.5
                        position['stop'] = max(position['stop'], lock)
                
                if row['Low'] <= position['stop']:
                    exit_price = position['stop']
                    entry_val = position['shares'] * position['entry_price']
                    exit_val = position['shares'] * exit_price
                    pnl = exit_val - entry_val - (entry_val + exit_val) * COMMISSION_PCT
                    equity_ref['value'] += pnl
                    all_trades.append({
                        'symbol': COIN_NAMES[sym], 'entry_date': position['entry_date'],
                        'exit_date': date, 'pnl': pnl, 'exit_reason': 'stop',
                        'entry_price': position['entry_price'], 'exit_price': exit_price,
                        'risk_reward': (exit_price - position['entry_price']) / (position['entry_price'] - position['stop']) if position['entry_price'] != position['stop'] else 0
                    })
                    positions[sym] = None
                elif row['High'] >= position['target']:
                    exit_price = position['target']
                    entry_val = position['shares'] * position['entry_price']
                    exit_val = position['shares'] * exit_price
                    pnl = exit_val - entry_val - (entry_val + exit_val) * COMMISSION_PCT
                    equity_ref['value'] += pnl
                    all_trades.append({
                        'symbol': COIN_NAMES[sym], 'entry_date': position['entry_date'],
                        'exit_date': date, 'pnl': pnl, 'exit_reason': 'target',
                        'entry_price': position['entry_price'], 'exit_price': exit_price,
                        'risk_reward': (exit_price - position['entry_price']) / (position['entry_price'] - position['stop']) if position['entry_price'] != position['stop'] else 0
                    })
                    positions[sym] = None
            
            if positions[sym] is None and idx > 0:
                open_count = sum(1 for p in positions.values() if p is not None)
                if open_count >= MAX_CONCURRENT:
                    continue
                prev = df.iloc[idx-1]
                trend_up = prev['Trend_up']
                rsi_oversold = prev['RSI'] < RSI_ENTRY
                momentum_ok = prev['ROC'] > ROC_MIN
                atr_valid = prev['ATR'] > 0 and not np.isnan(prev['ATR'])
                breakout = (prev['Close'] > prev['Donchian_high']) and not np.isnan(prev['Donchian_high'])
                
                signal_mr = trend_up and rsi_oversold and momentum_ok and atr_valid
                signal_bo = trend_up and breakout and atr_valid
                
                if signal_mr or signal_bo:
                    entry_price = row['Open']
                    atr_val = prev['ATR']
                    stop = entry_price - ATR_STOP_MULT * atr_val
                    target = entry_price + ATR_TARGET_MULT * atr_val
                    risk_per_share = entry_price - stop
                    if risk_per_share <= 0:
                        continue
                    
                    current_equity = equity_ref['value']
                    risk_dollars = current_equity * RISK_PER_TRADE
                    shares = risk_dollars / risk_per_share
                    max_shares = (current_equity * 0.5) / entry_price
                    shares = min(shares, max_shares)
                    if shares <= 0:
                        continue
                    
                    positions[sym] = {
                        'entry_date': date, 'entry_price': entry_price,
                        'stop': stop, 'target': target, 'shares': shares,
                        'peak_r': 0,
                    }
        
        equity_curve.append({'date': date, 'equity': equity_ref['value']})
    
    return all_trades, equity_ref['value'], pd.DataFrame(equity_curve).set_index('date')

def calc_metrics(trades, equity_curve, initial_capital, years):
    if not trades:
        return {}
    
    final = equity_curve['equity'].iloc[-1]
    total_return = (final / initial_capital - 1) * 100
    cagr = ((final / initial_capital) ** (1 / years) - 1) * 100
    
    # Daily returns for Sharpe
    daily_returns = equity_curve['equity'].pct_change().dropna()
    sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(365)) if daily_returns.std() > 0 else 0
    
    # Max drawdown
    peak = equity_curve['equity'].cummax()
    dd = (equity_curve['equity'] - peak) / peak
    max_dd = dd.min() * 100
    
    # Trade stats
    pnls = [t['pnl'] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    win_rate = len(winners) / len(pnls) * 100 if pnls else 0
    avg_win = np.mean(winners) if winners else 0
    avg_loss = abs(np.mean(losers)) if losers else 1
    profit_factor = sum(winners) / abs(sum(losers)) if losers else float('inf')
    
    # By symbol
    by_sym = {}
    for sym in ['BTC', 'ETH', 'BNB', 'SOL']:
        sym_trades = [t for t in trades if t['symbol'] == sym]
        sym_pnls = [t['pnl'] for t in sym_trades]
        sym_wins = [p for p in sym_pnls if p > 0]
        if sym_trades:
            by_sym[sym] = {
                'trades': len(sym_trades),
                'win_rate': len(sym_wins) / len(sym_trades) * 100,
                'total_pnl': sum(sym_pnls),
                'avg_pnl': np.mean(sym_pnls),
            }
    
    # By exit reason
    stops = [t for t in trades if t['exit_reason'] == 'stop']
    targets = [t for t in trades if t['exit_reason'] == 'target']
    
    return {
        'initial': initial_capital,
        'final': final,
        'total_return': total_return,
        'cagr': cagr,
        'sharpe': sharpe,
        'max_dd': max_dd,
        'total_trades': len(trades),
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor,
        'stops': len(stops),
        'targets': len(targets),
        'by_sym': by_sym,
    }

# === MAIN ===
if __name__ == '__main__':
    print("Fetching 3y daily data from OKX...\n")
    
    data = {}
    for sym in UNIVERSE:
        print(f"  {sym}...", end=' ', flush=True)
        df = fetch_candles(sym, '1D', 1100)
        if df.empty:
            print("FAILED!")
            sys.exit(1)
        print(f"{len(df)} candles ({df.index[0].date()} → {df.index[-1].date()})")
        data[sym] = enrich(df)
        time.sleep(0.3)
    
    # Find common date range
    start = max(df.index[0] for df in data.values())
    end = min(df.index[-1] for df in data.values())
    years = (end - start).days / 365.25
    print(f"\nCommon range: {start.date()} → {end.date()} ({years:.1f}y)")
    
    # Trim to common range
    for sym in data:
        data[sym] = data[sym].loc[start:end]
    
    print(f"\nRunning backtest...")
    trades, final_equity, equity_curve = run_backtest(data)
    m = calc_metrics(trades, equity_curve, INITIAL_CAPITAL, years)
    
    print(f"\n{'='*60}")
    print(f"  DAILY SWING STRATEGY — BACKTEST RESULTS")
    print(f"{'='*60}")
    print(f"  Period:     {start.date()} → {end.date()} ({years:.1f} years)")
    print(f"  Capital:    ${INITIAL_CAPITAL:,}")
    print(f"  Final:      ${m['final']:,.2f}")
    print(f"{'='*60}")
    print(f"  CAGR:       {m['cagr']:+.2f}%")
    print(f"  Total:      {m['total_return']:+.2f}%")
    print(f"  Sharpe:     {m['sharpe']:.2f}")
    print(f"  Max DD:     {m['max_dd']:.2f}%")
    print(f"{'='*60}")
    print(f"  Trades:     {m['total_trades']}")
    print(f"  Win Rate:   {m['win_rate']:.1f}%")
    print(f"  Avg Win:    ${m['avg_win']:.2f}")
    print(f"  Avg Loss:   ${m['avg_loss']:.2f}")
    print(f"  Profit F:   {m['profit_factor']:.2f}")
    print(f"  Targets:    {m['targets']}  |  Stops: {m['stops']}")
    print(f"{'='*60}")
    print(f"  BY COIN:")
    for sym, s in m['by_sym'].items():
        print(f"    {sym:4s}  trades={s['trades']:3d}  win={s['win_rate']:.0f}%  pnl=${s['total_pnl']:+,.2f}")
    print(f"{'='*60}")
    
    # Monthly returns
    monthly = equity_curve['equity'].resample('ME').last().pct_change().dropna()
    print(f"\n  MONTHLY RETURNS (last 12):")
    for dt, ret in monthly.tail(12).items():
        print(f"    {dt.strftime('%Y-%m')}  {ret*100:+6.2f}%")
    print()
