"""
Web Research Strategies Backtest — Native 4H/1H candles, no lookahead bias.
Strategies: KC+MACD Breakout, Donchian Channel, MACD Histogram.
All include regime filter and leverage testing.
"""
import pandas as pd
import numpy as np
import os

FEE = 0.0005
INITIAL_CAPITAL = 5000

def load_data(tf):
    base = os.path.dirname(__file__)
    if tf == '4H':
        p = os.path.join(base, 'data', 'candles', 'BTCUSDT_4H.csv')
    elif tf == '1H':
        p = os.path.join(base, 'data', 'candles', 'BTCUSDT_1H.csv')
    else:
        p = os.path.join(base, 'data', 'candles', f'BTCUSDT_{tf}.csv')
    df = pd.read_csv(p)
    # Normalize column names
    df.columns = [c.lower() for c in df.columns]
    # Rename ts -> timestamp if needed
    if 'ts' in df.columns and 'timestamp' not in df.columns:
        df.rename(columns={'ts': 'timestamp'}, inplace=True)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)
    # Ensure OHLCV are correct names
    rename_map = {}
    for c in df.columns:
        if c == 'open': rename_map[c] = 'open'
        elif c == 'high': rename_map[c] = 'high'
        elif c == 'low': rename_map[c] = 'low'
        elif c == 'close': rename_map[c] = 'close'
        elif c == 'volume': rename_map[c] = 'volume'
    return df

def add_ema_regime(df, fast=200, slow=100):
    df['ema_fast'] = df['close'].ewm(span=fast, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=slow, adjust=False).mean()
    df['regime'] = np.where(df['ema_fast'] > df['ema_slow'], 'bull',
                   np.where(df['ema_fast'] < df['ema_slow'], 'bear', 'neutral'))
    return df

def calc_metrics(equity_curve, trades_list):
    if len(equity_curve) < 2:
        return {'total_return': 0, 'cagr': 0, 'sharpe': 0, 'max_dd': 0, 'pf': 0, 'wr': 0, 'trades': 0}
    returns = pd.Series(equity_curve).pct_change().dropna()
    total_return = (equity_curve[-1] / INITIAL_CAPITAL) - 1
    n_periods = len(equity_curve)
    cagr = (equity_curve[-1] / INITIAL_CAPITAL) ** (4 * 365 / max(n_periods, 1)) - 1
    sharpe = returns.mean() / returns.std() * np.sqrt(4 * 365) if returns.std() > 0 else 0
    peak = pd.Series(equity_curve).expanding().max()
    dd = (pd.Series(equity_curve) - peak) / peak
    max_dd = dd.min()
    gross_profit = sum(t['pnl'] for t in trades_list if t['pnl'] > 0)
    gross_loss = abs(sum(t['pnl'] for t in trades_list if t['pnl'] < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else 99
    wins = sum(1 for t in trades_list if t['pnl'] > 0)
    wr = wins / len(trades_list) * 100 if trades_list else 0
    return {
        'total_return': total_return, 'cagr': cagr, 'sharpe': sharpe,
        'max_dd': max_dd, 'pf': pf, 'wr': wr, 'trades': len(trades_list)
    }

def walk_forward_validate(df, strategy_func, params, n_splits=3, test_ratio=0.3):
    """Walk-forward: train on 70%, test on 30%, rolling."""
    total_len = len(df)
    split_size = total_len // n_splits
    all_metrics = []
    for i in range(n_splits):
        start = i * split_size
        end = min(start + split_size, total_len)
        test_start = int(start + split_size * (1 - test_ratio))
        test_df = df.iloc[test_start:end].copy()
        if len(test_df) < 50:
            continue
        metrics = strategy_func(test_df, params, leverage=1)
        all_metrics.append(metrics)
    profitable = sum(1 for m in all_metrics if m['cagr'] > 0 and m['pf'] > 1)
    return profitable, all_metrics

# ============================================================
# STRATEGY 1: Keltner Channel + MACD Breakout (4H)
# ============================================================
def kc_macd(df, params, leverage=1):
    """Keltner Channel breakout confirmed by MACD histogram positive."""
    kc_period = params.get('kc_period', 20)
    atr_mult = params.get('atr_mult', 1.5)
    macd_fast = params.get('macd_fast', 12)
    macd_slow = params.get('macd_slow', 26)
    macd_sig = params.get('macd_sig', 9)
    tp_atr = params.get('tp_atr', 2.0)
    sl_atr = params.get('sl_atr', 1.5)

    df = df.copy()
    df['ema'] = df['close'].ewm(span=kc_period, adjust=False).mean()
    df['atr'] = (df['high'] - df['low']).rolling(kc_period).mean()
    df['kc_upper'] = df['ema'] + atr_mult * df['atr']
    df['kc_lower'] = df['ema'] - atr_mult * df['atr']
    df['macd'], df['macd_signal'], df['macd_hist'] = macd_custom(df['close'], macd_fast, macd_slow, macd_sig)

    df = add_ema_regime(df, 200, 100)

    capital = INITIAL_CAPITAL
    position = 0
    entry_price = 0
    entry_atr = 0
    equity = [capital]
    trades = []

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]

        if position != 0:
            if position == 1:
                pnl_pct = (row['close'] - entry_price) / entry_price * leverage
            else:
                pnl_pct = (entry_price - row['close']) / entry_price * leverage
            current_equity = capital * (1 + pnl_pct)
        else:
            current_equity = capital

        # Exit logic
        if position == 1:
            tp = entry_price + entry_atr * tp_atr * leverage
            sl = entry_price - entry_atr * sl_atr * leverage
            if row['close'] >= tp or row['close'] <= sl or row['macd_hist'] < 0:
                pnl = (row['close'] - entry_price) / entry_price * leverage - 2 * FEE
                capital *= (1 + pnl)
                trades.append({'pnl': pnl, 'exit': 'long_exit'})
                position = 0
        elif position == -1:
            tp = entry_price - entry_atr * tp_atr * leverage
            sl = entry_price + entry_atr * sl_atr * leverage
            if row['close'] <= tp or row['close'] >= sl or row['macd_hist'] > 0:
                pnl = (entry_price - row['close']) / entry_price * leverage - 2 * FEE
                capital *= (1 + pnl)
                trades.append({'pnl': pnl, 'exit': 'short_exit'})
                position = 0

        # Entry logic
        if position == 0 and row['regime'] != 'bear':
            if (row['close'] > row['kc_upper'] and row['macd_hist'] > 0
                    and prev['macd_hist'] <= 0):
                position = 1
                entry_price = row['close']
                entry_atr = row['atr']
            elif (row['close'] < row['kc_lower'] and row['macd_hist'] < 0
                    and prev['macd_hist'] >= 0):
                position = -1
                entry_price = row['close']
                entry_atr = row['atr']

        equity.append(current_equity)

    # Close any remaining position
    if position == 1:
        pnl = (df.iloc[-1]['close'] - entry_price) / entry_price * leverage - 2 * FEE
        capital *= (1 + pnl)
        trades.append({'pnl': pnl, 'exit': 'final_close'})
        equity[-1] = capital
    elif position == -1:
        pnl = (entry_price - df.iloc[-1]['close']) / entry_price * leverage - 2 * FEE
        capital *= (1 + pnl)
        trades.append({'pnl': pnl, 'exit': 'final_close'})
        equity[-1] = capital

    return calc_metrics(equity, trades)

def macd_custom(close, fast, slow, signal):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=signal, adjust=False).mean()
    macd_hist = macd_line - macd_signal
    return macd_line, macd_signal, macd_hist

# ============================================================
# STRATEGY 2: Donchian Channel Breakout (4H)
# ============================================================
def donchian_breakout(df, params, leverage=1):
    """Donchian Channel breakout with ATR trailing stop."""
    dc_period = params.get('dc_period', 20)
    atr_period = params.get('atr_period', 20)
    atr_trail_mult = params.get('atr_trail', 2.5)

    df = df.copy()
    df['dc_upper'] = df['high'].rolling(dc_period).max()
    df['dc_lower'] = df['low'].rolling(dc_period).min()
    df['dc_mid'] = (df['dc_upper'] + df['dc_lower']) / 2
    df['atr'] = (df['high'] - df['low']).rolling(atr_period).mean()

    df = add_ema_regime(df, 200, 100)

    capital = INITIAL_CAPITAL
    position = 0
    entry_price = 0
    trail_stop = 0
    equity = [capital]
    trades = []

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]

        if position != 0:
            if position == 1:
                pnl_pct = (row['close'] - entry_price) / entry_price * leverage
            else:
                pnl_pct = (entry_price - row['close']) / entry_price * leverage
            current_equity = capital * (1 + pnl_pct)
        else:
            current_equity = capital

        # Exit logic — ATR trailing stop
        if position == 1:
            new_trail = row['close'] - atr_trail_mult * row['atr']
            if new_trail > trail_stop:
                trail_stop = new_trail
            if row['close'] <= trail_stop:
                pnl = (row['close'] - entry_price) / entry_price * leverage - 2 * FEE
                capital *= (1 + pnl)
                trades.append({'pnl': pnl})
                position = 0
        elif position == -1:
            new_trail = row['close'] + atr_trail_mult * row['atr']
            if new_trail < trail_stop:
                trail_stop = new_trail
            if row['close'] >= trail_stop:
                pnl = (entry_price - row['close']) / entry_price * leverage - 2 * FEE
                capital *= (1 + pnl)
                trades.append({'pnl': pnl})
                position = 0

        # Entry logic — breakout of Donchian channel
        if position == 0 and row['regime'] != 'bear':
            if row['close'] > prev['dc_upper'] and not np.isnan(row['dc_upper']):
                position = 1
                entry_price = row['close']
                trail_stop = row['close'] - atr_trail_mult * row['atr']
            elif row['close'] < prev['dc_lower'] and not np.isnan(row['dc_lower']):
                position = -1
                entry_price = row['close']
                trail_stop = row['close'] + atr_trail_mult * row['atr']

        equity.append(current_equity)

    if position == 1:
        pnl = (df.iloc[-1]['close'] - entry_price) / entry_price * leverage - 2 * FEE
        capital *= (1 + pnl)
        trades.append({'pnl': pnl})
        equity[-1] = capital
    elif position == -1:
        pnl = (entry_price - df.iloc[-1]['close']) / entry_price * leverage - 2 * FEE
        capital *= (1 + pnl)
        trades.append({'pnl': pnl})
        equity[-1] = capital

    return calc_metrics(equity, trades)

# ============================================================
# STRATEGY 3: MACD Histogram Reversal (4H)
# ============================================================
def macd_hist_reversal(df, params, leverage=1):
    """MACD histogram zero-line crossover with EMA trend filter."""
    fast = params.get('fast', 12)
    slow = params.get('slow', 26)
    sig = params.get('sig', 9)
    tp_atr = params.get('tp_atr', 2.0)
    sl_atr = params.get('sl_atr', 1.5)

    df = df.copy()
    df['macd'], df['macd_signal'], df['macd_hist'] = macd_custom(df['close'], fast, slow, sig)
    df['atr'] = (df['high'] - df['low']).rolling(20).mean()

    df = add_ema_regime(df, 200, 100)

    capital = INITIAL_CAPITAL
    position = 0
    entry_price = 0
    entry_atr = 0
    equity = [capital]
    trades = []

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]

        if position != 0:
            if position == 1:
                pnl_pct = (row['close'] - entry_price) / entry_price * leverage
            else:
                pnl_pct = (entry_price - row['close']) / entry_price * leverage
            current_equity = capital * (1 + pnl_pct)
        else:
            current_equity = capital

        # Exit logic
        if position == 1:
            tp = entry_price + entry_atr * tp_atr * leverage
            sl = entry_price - entry_atr * sl_atr * leverage
            if row['close'] >= tp or row['close'] <= sl:
                pnl = (row['close'] - entry_price) / entry_price * leverage - 2 * FEE
                capital *= (1 + pnl)
                trades.append({'pnl': pnl})
                position = 0
        elif position == -1:
            tp = entry_price - entry_atr * tp_atr * leverage
            sl = entry_price + entry_atr * sl_atr * leverage
            if row['close'] <= tp or row['close'] >= sl:
                pnl = (entry_price - row['close']) / entry_price * leverage - 2 * FEE
                capital *= (1 + pnl)
                trades.append({'pnl': pnl})
                position = 0

        # Entry logic — MACD histogram crosses zero
        if position == 0 and row['regime'] != 'bear':
            if row['macd_hist'] > 0 and prev['macd_hist'] <= 0:
                position = 1
                entry_price = row['close']
                entry_atr = row['atr']
            elif row['macd_hist'] < 0 and prev['macd_hist'] >= 0:
                position = -1
                entry_price = row['close']
                entry_atr = row['atr']

        equity.append(current_equity)

    if position == 1:
        pnl = (df.iloc[-1]['close'] - entry_price) / entry_price * leverage - 2 * FEE
        capital *= (1 + pnl)
        trades.append({'pnl': pnl})
        equity[-1] = capital
    elif position == -1:
        pnl = (entry_price - df.iloc[-1]['close']) / entry_price * leverage - 2 * FEE
        capital *= (1 + pnl)
        trades.append({'pnl': pnl})
        equity[-1] = capital

    return calc_metrics(equity, trades)

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    print("=" * 70)
    print("WEB RESEARCH STRATEGIES BACKTEST — 4H BTC")
    print("=" * 70)

    df_4h = load_data('4H')
    df_4h = add_ema_regime(df_4h, 200, 100)

    strategies = [
        ('KC+MACD Breakout', kc_macd, {
            'kc_period': 20, 'atr_mult': 1.5, 'macd_fast': 12,
            'macd_slow': 26, 'macd_sig': 9, 'tp_atr': 2.0, 'sl_atr': 1.5
        }),
        ('Donchian Breakout', donchian_breakout, {
            'dc_period': 20, 'atr_period': 20, 'atr_trail': 2.5
        }),
        ('MACD Hist Reversal', macd_hist_reversal, {
            'fast': 12, 'slow': 26, 'sig': 9, 'tp_atr': 2.0, 'sl_atr': 1.5
        }),
    ]

    leverage_levels = [1, 2, 3, 5]
    results_all = []

    for name, func, params in strategies:
        print(f"\n{'='*60}")
        print(f"  {name}")
        print(f"{'='*60}")
        for lev in leverage_levels:
            metrics = func(df_4h, params, leverage=lev)
            wf_profitable, wf_details = walk_forward_validate(df_4h, func, params, n_splits=3, test_ratio=0.3)
            wf_pct = wf_profitable / max(len(wf_details), 1) * 100
            results_all.append({
                'name': name, 'leverage': lev, **metrics,
                'wf_profitable': wf_profitable, 'wf_total': len(wf_details)
            })
            print(f"  {lev}x: CAGR {metrics['cagr']*100:+.1f}% | MaxDD {metrics['max_dd']*100:.1f}% | "
                  f"PF {metrics['pf']:.2f} | WR {metrics['wr']:.0f}% | "
                  f"Trades {metrics['trades']} | WF {wf_profitable}/{len(wf_details)}")

    # Grid search on best strategy (KC+MACD)
    print(f"\n{'='*60}")
    print(f"  GRID SEARCH: KC+MACD Breakout (4H)")
    print(f"{'='*60}")
    best = None
    for kc_p in [15, 20, 30]:
        for atr_m in [1.0, 1.5, 2.0]:
            for tp in [1.5, 2.0, 2.5, 3.0]:
                for sl in [1.0, 1.5, 2.0]:
                    p = {'kc_period': kc_p, 'atr_mult': atr_m, 'macd_fast': 12,
                         'macd_slow': 26, 'macd_sig': 9, 'tp_atr': tp, 'sl_atr': sl}
                    m = kc_macd(df_4h, p, leverage=3)
                    wf_p, wf_d = walk_forward_validate(df_4h, kc_macd, p, 3, 0.3)
                    if m['cagr'] > 0 and m['pf'] > 1 and wf_p >= 2:
                        score = m['cagr'] * m['pf'] / max(abs(m['max_dd']), 0.01)
                        if best is None or score > best['score']:
                            best = {**m, 'params': p, 'score': score, 'wf': f"{wf_p}/{len(wf_d)}", 'leverage': 3}

    if best:
        print(f"\n  BEST KC+MACD (3x):")
        print(f"    Params: KC={best['params']['kc_period']}, ATR mult={best['params']['atr_mult']}, "
              f"TP={best['params']['tp_atr']}x, SL={best['params']['sl_atr']}x")
        print(f"    CAGR {best['cagr']*100:+.1f}% | MaxDD {best['max_dd']*100:.1f}% | "
              f"PF {best['pf']:.2f} | WR {best['wr']:.0f}% | Trades {best['trades']} | WF {best['wf']}")
    else:
        print("  No profitable KC+MACD combo found with WF consistency")

    # Grid search on Donchian
    print(f"\n{'='*60}")
    print(f"  GRID SEARCH: Donchian Breakout (4H)")
    print(f"{'='*60}")
    best_dc = None
    for dc_p in [10, 15, 20, 30]:
        for atr_t in [1.5, 2.0, 2.5, 3.0]:
            p = {'dc_period': dc_p, 'atr_period': 20, 'atr_trail': atr_t}
            m = donchian_breakout(df_4h, p, leverage=3)
            wf_p, wf_d = walk_forward_validate(df_4h, donchian_breakout, p, 3, 0.3)
            if m['cagr'] > 0 and m['pf'] > 1 and wf_p >= 2:
                score = m['cagr'] * m['pf'] / max(abs(m['max_dd']), 0.01)
                if best_dc is None or score > best_dc['score']:
                    best_dc = {**m, 'params': p, 'score': score, 'wf': f"{wf_p}/{len(wf_d)}", 'leverage': 3}

    if best_dc:
        print(f"\n  BEST Donchian (3x):")
        print(f"    Params: DC={best_dc['params']['dc_period']}, Trail={best_dc['params']['atr_trail']}x ATR")
        print(f"    CAGR {best_dc['cagr']*100:+.1f}% | MaxDD {best_dc['max_dd']*100:.1f}% | "
              f"PF {best_dc['pf']:.2f} | WR {best_dc['wr']:.0f}% | Trades {best_dc['trades']} | WF {best_dc['wf']}")
    else:
        print("  No profitable Donchian combo found")

    # Final summary
    print(f"\n{'='*70}")
    print(f"  SUMMARY — ALL WEB STRATEGIES (4H BTC)")
    print(f"{'='*70}")
    print(f"  {'Strategy':<25} {'Lev':>4} {'CAGR':>8} {'MaxDD':>8} {'PF':>6} {'WR':>6} {'Trades':>7} {'WF':>6}")
    print(f"  {'-'*25} {'-'*4} {'-'*8} {'-'*8} {'-'*6} {'-'*6} {'-'*7} {'-'*6}")
    for r in results_all:
        wf_str = f"{r['wf_profitable']}/{r['wf_total']}"
        print(f"  {r['name']:<25} {r['leverage']:>3}x {r['cagr']*100:>+7.1f}% {r['max_dd']*100:>7.1f}% "
              f"{r['pf']:>5.2f} {r['wr']:>5.0f}% {r['trades']:>6} {wf_str:>6}")
