#!/usr/bin/env python3
"""
Momentum Strategy Backtest — based on smart-lab.ru/blog/826197 concepts

Core idea: Time-series momentum (Moskowitz et al. 2012)
- Assets with positive past returns continue to rise
- Assets with negative past returns continue to fall

Implementation:
- Primary signal: Rate of Change (ROC) across multiple periods
- Trend filter: EMA alignment + ADX for trend strength
- Volatility filter: ATR-based position sizing (risk parity)
- Entry: Long when momentum + trend + strength align
- Exit: Trailing stop + momentum reversal
"""

import httpx
import pandas as pd
import numpy as np
import time
import sys

# === CONFIG ===
INITIAL_CAPITAL = 10_000
RISK_PER_TRADE = 0.02
COMMISSION_PCT = 0.001  # 0.1% per side
MAX_CONCURRENT = 3
UNIVERSE = ['BTC-USDT', 'ETH-USDT', 'BNB-USDT', 'SOL-USDT']
COIN_NAMES = {'BTC-USDT': 'BTC', 'ETH-USDT': 'ETH', 'BNB-USDT': 'BNB', 'SOL-USDT': 'SOL'}

# === MOMENTUM PARAMETERS ===
# Multi-timeframe ROC (what the article calls "time-series momentum")
ROC_FAST = 10       # short-term momentum
ROC_MED = 30        # medium-term momentum
ROC_SLOW = 90       # long-term momentum

# Trend filters
EMA_FAST = 21
EMA_SLOW = 55
ADX_PERIOD = 14
ADX_THRESHOLD = 20  # minimum trend strength

# Volatility
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0
ATR_TARGET_MULT = 4.0  # R:R = 1:2

# Risk management
TRAILING_BREAKEVEN_R = 1.0   # move SL to breakeven at 1R
TRAILING_PARTIAL_R = 2.0     # lock 50% at 2R
TRAILING_ACCEL_R = 3.0       # tighten trail at 3R
MAX_POSITION_PCT = 0.25      # max 25% of equity per position
COOLDOWN_BARS = 3            # bars to wait after a loss

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

def roc(s, n):
    return s.pct_change(n) * 100

def adx(df, n=14):
    """Average Directional Index — trend strength."""
    h, l, c = df['High'], df['Low'], df['Close']
    up = h - h.shift(1)
    down = l.shift(1) - l
    plus_dm = np.where((up > down) & (up > 0), up, 0)
    minus_dm = np.where((down > up) & (down > 0), down, 0)
    
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr_val = tr.ewm(alpha=1/n, adjust=False).mean()
    
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_val
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_val
    
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1/n, adjust=False).mean()
    return adx_val, plus_di, minus_di

def enrich(df):
    df = df.copy()
    # Moving averages
    df['EMA_fast'] = ema(df['Close'], EMA_FAST)
    df['EMA_slow'] = ema(df['Close'], EMA_SLOW)
    
    # Multi-timeframe momentum (core of the strategy)
    df['ROC_fast'] = roc(df['Close'], ROC_FAST)
    df['ROC_med'] = roc(df['Close'], ROC_MED)
    df['ROC_slow'] = roc(df['Close'], ROC_SLOW)
    
    # Momentum score: weighted combination
    df['Mom_score'] = df['ROC_fast'] * 0.5 + df['ROC_med'] * 0.3 + df['ROC_slow'] * 0.2
    
    # Trend strength
    df['ADX'], df['Plus_DI'], df['Minus_DI'] = adx(df, ADX_PERIOD)
    
    # Volatility
    df['ATR'] = atr(df, ATR_PERIOD)
    df['ATR_pct'] = df['ATR'] / df['Close'] * 100  # ATR as % of price
    
    # Volume momentum (confirmation)
    df['Vol_MA'] = df['Volume'].rolling(20).mean()
    df['Vol_ratio'] = df['Volume'] / df['Vol_MA']
    
    # Trend state
    df['Trend_up'] = df['EMA_fast'] > df['EMA_slow']
    df['Strong_trend'] = df['ADX'] > ADX_THRESHOLD
    
    return df

# === DATA FETCH ===
def fetch_candles(inst_id, bar='1D', total=1100):
    all_candles = []
    after = ''
    while len(all_candles) < total:
        params = {'instId': inst_id, 'bar': bar, 'limit': '300'}
        if after:
            params['after'] = after
        resp = httpx.get('https://www.okx.com/api/v5/market/candles', params=params, timeout=15)
        data = resp.json()
        if data.get('code') != '0' or not data.get('data'):
            break
        candles = data['data']
        all_candles.extend(candles)
        after = candles[-1][0]
        if len(candles) < 300:
            break
        time.sleep(0.2)
    
    df = pd.DataFrame(all_candles, columns=[
        'ts', 'Open', 'High', 'Low', 'Close', 'Volume', 'VolCcy', 'VolCcyQuote', 'Confirm'
    ])
    df['ts'] = pd.to_datetime(df['ts'].astype(int), unit='ms')
    df.set_index('ts', inplace=True)
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)
    df.sort_index(inplace=True)
    return df

# === BACKTEST ===
def run_backtest(data_dict, initial_capital=INITIAL_CAPITAL):
    equity_ref = {'value': initial_capital}
    equity_curve = []
    positions = {sym: None for sym in data_dict}
    cooldown = {sym: 0 for sym in data_dict}
    all_trades = []
    
    all_dates = data_dict['BTC-USDT'].index
    
    for date in all_dates:
        for sym, df in data_dict.items():
            if date not in df.index:
                continue
            idx = df.index.get_loc(date)
            if idx < 1:
                continue
            row = df.iloc[idx]
            position = positions[sym]
            
            # === MANAGE OPEN POSITION ===
            if position is not None:
                r_dist = position['entry_price'] - position['stop']
                if r_dist > 0:
                    new_peak_r = max(position['peak_r'],
                                    (row['High'] - position['entry_price']) / r_dist)
                    position['peak_r'] = new_peak_r
                    
                    # Breakeven
                    if new_peak_r >= TRAILING_BREAKEVEN_R:
                        position['stop'] = max(position['stop'], position['entry_price'])
                    
                    # Lock 50% profit
                    if new_peak_r >= TRAILING_PARTIAL_R:
                        lock = position['entry_price'] + new_peak_r * r_dist * 0.5
                        position['stop'] = max(position['stop'], lock)
                    
                    # Tighten trail at 3R
                    if new_peak_r >= TRAILING_ACCEL_R:
                        tight = row['Close'] - position['atr'] * 1.5
                        position['stop'] = max(position['stop'], tight)
                
                # Check stop loss
                if row['Low'] <= position['stop']:
                    exit_price = position['stop']
                    entry_val = position['shares'] * position['entry_price']
                    exit_val = position['shares'] * exit_price
                    pnl = exit_val - entry_val - (entry_val + exit_val) * COMMISSION_PCT
                    equity_ref['value'] += pnl
                    
                    # Set cooldown on loss
                    if pnl < 0:
                        cooldown[sym] = COOLDOWN_BARS
                    
                    all_trades.append({
                        'symbol': COIN_NAMES[sym], 'entry_date': position['entry_date'],
                        'exit_date': date, 'pnl': pnl, 'exit_reason': 'stop',
                        'entry_price': position['entry_price'], 'exit_price': exit_price,
                        'r_multiple': (exit_price - position['entry_price']) / (position['entry_price'] - position['stop']) if position['entry_price'] != position['stop'] else 0,
                    })
                    positions[sym] = None
                
                # Check target
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
                        'r_multiple': (exit_price - position['entry_price']) / (position['entry_price'] - position['stop']) if position['entry_price'] != position['stop'] else 0,
                    })
                    positions[sym] = None
                
                # Momentum reversal exit
                elif position['side'] == 'long' and row['Mom_score'] < -5:
                    # Momentum flipped bearish — exit early
                    exit_price = row['Close']
                    entry_val = position['shares'] * position['entry_price']
                    exit_val = position['shares'] * exit_price
                    pnl = exit_val - entry_val - (entry_val + exit_val) * COMMISSION_PCT
                    equity_ref['value'] += pnl
                    all_trades.append({
                        'symbol': COIN_NAMES[sym], 'entry_date': position['entry_date'],
                        'exit_date': date, 'pnl': pnl, 'exit_reason': 'momentum_reversal',
                        'entry_price': position['entry_price'], 'exit_price': exit_price,
                        'r_multiple': (exit_price - position['entry_price']) / (position['entry_price'] - position['stop']) if position['entry_price'] != position['stop'] else 0,
                    })
                    positions[sym] = None
            
            # === ENTRY SIGNALS ===
            if positions[sym] is None and idx > 0:
                open_count = sum(1 for p in positions.values() if p is not None)
                if open_count >= MAX_CONCURRENT:
                    continue
                
                # Cooldown after loss
                if cooldown[sym] > 0:
                    cooldown[sym] -= 1
                    continue
                
                prev = df.iloc[idx-1]
                
                # === MOMENTUM ENTRY (core strategy) ===
                # 1. Multi-timeframe momentum: fast + medium both positive
                momentum_up = prev['ROC_fast'] > 0 and prev['ROC_med'] > 0
                mom_score_positive = prev['Mom_score'] > 2  # weighted score positive
                
                # 2. Trend alignment: EMA fast > slow
                trend_up = prev['Trend_up']
                
                # 3. Strong trend: ADX > threshold
                strong = prev['Strong_trend']
                
                # 4. DI confirmation: +DI > -DI
                di_bullish = prev['Plus_DI'] > prev['Minus_DI']
                
                # 5. Volatility not too high (avoid choppy markets)
                vol_ok = prev['ATR_pct'] < 6.0  # ATR < 6% of price
                
                # 6. Volume confirmation
                vol_expanding = prev['Vol_ratio'] > 0.8
                
                if (momentum_up and mom_score_positive and trend_up and 
                    strong and di_bullish and vol_ok and vol_expanding):
                    
                    entry_price = row['Open']
                    atr_val = prev['ATR']
                    if atr_val <= 0 or np.isnan(atr_val):
                        continue
                    
                    stop = entry_price - ATR_STOP_MULT * atr_val
                    target = entry_price + ATR_TARGET_MULT * atr_val
                    risk_per_share = entry_price - stop
                    if risk_per_share <= 0:
                        continue
                    
                    # Volatility-adjusted position sizing
                    current_equity = equity_ref['value']
                    risk_dollars = current_equity * RISK_PER_TRADE
                    shares = risk_dollars / risk_per_share
                    
                    # Cap at max position size
                    max_shares = (current_equity * MAX_POSITION_PCT) / entry_price
                    shares = min(shares, max_shares)
                    if shares <= 0:
                        continue
                    
                    positions[sym] = {
                        'entry_date': date, 'entry_price': entry_price,
                        'stop': stop, 'target': target, 'shares': shares,
                        'peak_r': 0, 'atr': atr_val, 'side': 'long',
                    }
        
        equity_curve.append({'date': date, 'equity': equity_ref['value']})
    
    return all_trades, equity_ref['value'], pd.DataFrame(equity_curve).set_index('date')

def calc_metrics(trades, equity_curve, initial_capital, years):
    if not trades:
        return {'error': 'no trades'}
    
    final = equity_curve['equity'].iloc[-1]
    total_return = (final / initial_capital - 1) * 100
    cagr = ((final / initial_capital) ** (1 / years) - 1) * 100
    
    daily_returns = equity_curve['equity'].pct_change().dropna()
    sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(365)) if daily_returns.std() > 0 else 0
    
    peak = equity_curve['equity'].cummax()
    dd = (equity_curve['equity'] - peak) / peak
    max_dd = dd.min() * 100
    
    pnls = [t['pnl'] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    win_rate = len(winners) / len(pnls) * 100 if pnls else 0
    avg_win = np.mean(winners) if winners else 0
    avg_loss = abs(np.mean(losers)) if losers else 1
    profit_factor = sum(winners) / abs(sum(losers)) if losers else float('inf')
    
    r_multiples = [t['r_multiple'] for t in trades]
    avg_r = np.mean(r_multiples) if r_multiples else 0
    
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
    
    by_reason = {}
    for reason in ['stop', 'target', 'momentum_reversal']:
        r_trades = [t for t in trades if t['exit_reason'] == reason]
        if r_trades:
            r_pnls = [t['pnl'] for t in r_trades]
            by_reason[reason] = {
                'count': len(r_trades),
                'total_pnl': sum(r_pnls),
                'avg_pnl': np.mean(r_pnls),
            }
    
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
        'avg_r': avg_r,
        'by_sym': by_sym,
        'by_reason': by_reason,
    }

# === MAIN ===
if __name__ == '__main__':
    print("=" * 60)
    print("  MOMENTUM STRATEGY — TIME-SERIES MOMENTUM (OKX 3Y DATA)")
    print("=" * 60)
    print("\nFetching 3y daily data from OKX...\n")
    
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
    
    start = max(df.index[0] for df in data.values())
    end = min(df.index[-1] for df in data.values())
    years = (end - start).days / 365.25
    print(f"\nCommon range: {start.date()} → {end.date()} ({years:.1f}y)")
    
    for sym in data:
        data[sym] = data[sym].loc[start:end]
    
    print(f"\nRunning backtest...")
    trades, final_equity, equity_curve = run_backtest(data)
    m = calc_metrics(trades, equity_curve, INITIAL_CAPITAL, years)
    
    if 'error' in m:
        print(f"\nERROR: {m['error']}")
        sys.exit(1)
    
    print(f"\n{'='*60}")
    print(f"  MOMENTUM STRATEGY — RESULTS (REAL OKX DATA)")
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
    print(f"  Avg R:      {m['avg_r']:.2f}R")
    print(f"{'='*60}")
    print(f"  BY COIN:")
    for sym, s in m['by_sym'].items():
        print(f"    {sym:4s}  trades={s['trades']:3d}  win={s['win_rate']:.0f}%  pnl=${s['total_pnl']:+,.2f}")
    print(f"{'='*60}")
    print(f"  BY EXIT REASON:")
    for reason, s in m['by_reason'].items():
        print(f"    {reason:20s}  count={s['count']:3d}  pnl=${s['total_pnl']:+,.2f}  avg=${s['avg_pnl']:+.2f}")
    print(f"{'='*60}")
    
    monthly = equity_curve['equity'].resample('ME').last().pct_change().dropna()
    print(f"\n  MONTHLY RETURNS (last 12):")
    for dt, ret in monthly.tail(12).items():
        print(f"    {dt.strftime('%Y-%m')}  {ret*100:+6.2f}%")
    print()
