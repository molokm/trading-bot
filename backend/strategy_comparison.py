"""
Comprehensive strategy comparison — web vs existing, plus improvements.
TrendJoin 4H, RSI Bounce 1H, KC+MACD optimized, and combo portfolios.
"""
import pandas as pd
import numpy as np
import os

FEE = 0.0005
INITIAL_CAPITAL = 5000

def load_data(tf):
    base = os.path.dirname(__file__)
    p = os.path.join(base, 'data', 'candles', f'BTCUSDT_{tf}.csv')
    df = pd.read_csv(p)
    df.columns = [c.lower() for c in df.columns]
    if 'ts' in df.columns:
        df.rename(columns={'ts': 'timestamp'}, inplace=True)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)
    return df

def calc_metrics(equity_curve, trades_list, annualize_factor=4*365):
    if len(equity_curve) < 2:
        return {'total_return': 0, 'cagr': 0, 'sharpe': 0, 'max_dd': 0, 'pf': 0, 'wr': 0, 'trades': 0, 'avg_r': 0}
    equity = np.array(equity_curve)
    returns = np.diff(equity) / equity[:-1]
    total_return = equity[-1] / INITIAL_CAPITAL - 1
    n = len(equity)
    cagr = (equity[-1] / INITIAL_CAPITAL) ** (annualize_factor / max(n, 1)) - 1
    sharpe = np.mean(returns) / np.std(returns) * np.sqrt(annualize_factor) if np.std(returns) > 0 else 0
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    max_dd = dd.min()
    gross_profit = sum(t['pnl'] for t in trades_list if t['pnl'] > 0)
    gross_loss = abs(sum(t['pnl'] for t in trades_list if t['pnl'] < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else 99
    wins = sum(1 for t in trades_list if t['pnl'] > 0)
    wr = wins / len(trades_list) * 100 if trades_list else 0
    avg_r = np.mean([t['pnl'] for t in trades_list]) if trades_list else 0
    return {
        'total_return': total_return, 'cagr': cagr, 'sharpe': sharpe,
        'max_dd': max_dd, 'pf': pf, 'wr': wr, 'trades': len(trades_list),
        'avg_r': avg_r
    }

def add_ema_regime(df, fast=200, slow=100):
    df['ema_fast'] = df['close'].ewm(span=fast, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=slow, adjust=False).mean()
    df['regime'] = np.where(df['ema_fast'] > df['ema_slow'], 'bull',
                   np.where(df['ema_fast'] < df['ema_slow'], 'bear', 'neutral'))
    return df

def walk_forward_validate(df, strategy_func, params, n_splits=3, test_ratio=0.3, leverage=3):
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
        metrics = strategy_func(test_df, params, leverage=leverage)
        all_metrics.append(metrics)
    profitable = sum(1 for m in all_metrics if m['cagr'] > 0 and m['pf'] > 1)
    return profitable, all_metrics

# ============================================================
# STRATEGY A: TrendJoin 4H (our best strategy)
# ============================================================
def trend_join_4h(df, params, leverage=1):
    ema_fast = params.get('ema_fast', 10)
    ema_slow = params.get('ema_slow', 50)
    tp_atr = params.get('tp_atr', 4.5)
    sl_atr = params.get('sl_atr', 2.0)

    df = df.copy()
    df['ema_f'] = df['close'].ewm(span=ema_fast, adjust=False).mean()
    df['ema_s'] = df['close'].ewm(span=ema_slow, adjust=False).mean()
    df['atr'] = (df['high'] - df['low']).rolling(14).mean()
    df = add_ema_regime(df, 200, 100)

    capital = INITIAL_CAPITAL
    pos = 0; entry = 0; entry_atr = 0
    equity = [capital]; trades = []

    for i in range(1, len(df)):
        r = df.iloc[i]; p = df.iloc[i-1]
        if pos == 1:
            pnl = (r['close'] - entry) / entry * leverage
        elif pos == -1:
            pnl = (entry - r['close']) / entry * leverage
        else:
            pnl = 0
        cur = capital * (1 + pnl)

        # Exit
        if pos == 1:
            if r['close'] >= entry + entry_atr * tp_atr or r['close'] <= entry - entry_atr * sl_atr:
                pnl = (r['close'] - entry) / entry * leverage - 2 * FEE
                capital *= (1 + pnl); trades.append({'pnl': pnl}); pos = 0
        elif pos == -1:
            if r['close'] <= entry - entry_atr * tp_atr or r['close'] >= entry + entry_atr * sl_atr:
                pnl = (entry - r['close']) / entry * leverage - 2 * FEE
                capital *= (1 + pnl); trades.append({'pnl': pnl}); pos = 0

        # Entry
        if pos == 0 and r['regime'] != 'bear':
            if r['ema_f'] > r['ema_s'] and p['ema_f'] <= p['ema_s']:
                pos = 1; entry = r['close']; entry_atr = r['atr']
            elif r['ema_f'] < r['ema_s'] and p['ema_f'] >= p['ema_s']:
                pos = -1; entry = r['close']; entry_atr = r['atr']

        equity.append(cur)

    if pos != 0:
        if pos == 1:
            pnl = (df.iloc[-1]['close'] - entry) / entry * leverage - 2 * FEE
        else:
            pnl = (entry - df.iloc[-1]['close']) / entry * leverage - 2 * FEE
        capital *= (1 + pnl); trades.append({'pnl': pnl}); equity[-1] = capital

    return calc_metrics(equity, trades)

# ============================================================
# STRATEGY B: RSI Bounce 1H (our best scalper)
# ============================================================
def rsi_bounce_1h(df, params, leverage=1):
    rsi_period = params.get('rsi_period', 14)
    rsi_entry = params.get('rsi_entry', 30)
    rsi_exit = params.get('rsi_exit', 50)
    tp_pct = params.get('tp_pct', 0.007)
    sl_pct = params.get('sl_pct', 0.007)

    df = df.copy()
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(rsi_period).mean()
    df['rsi'] = 100 - (100 / (1 + gain / loss))
    df = add_ema_regime(df, 200, 100)

    capital = INITIAL_CAPITAL
    pos = 0; entry = 0
    equity = [capital]; trades = []

    for i in range(1, len(df)):
        r = df.iloc[i]
        if pos == 1:
            pnl = (r['close'] - entry) / entry * leverage
        elif pos == -1:
            pnl = (entry - r['close']) / entry * leverage
        else:
            pnl = 0
        cur = capital * (1 + pnl)

        if pos == 1:
            if r['close'] >= entry * (1 + tp_pct) or r['close'] <= entry * (1 - sl_pct) or r['rsi'] >= rsi_exit:
                pnl = (r['close'] - entry) / entry * leverage - 2 * FEE
                capital *= (1 + pnl); trades.append({'pnl': pnl}); pos = 0
        elif pos == -1:
            if r['close'] <= entry * (1 - tp_pct) or r['close'] >= entry * (1 + sl_pct) or r['rsi'] <= (100 - rsi_exit):
                pnl = (entry - r['close']) / entry * leverage - 2 * FEE
                capital *= (1 + pnl); trades.append({'pnl': pnl}); pos = 0

        if pos == 0 and r['regime'] != 'bear':
            if r['rsi'] < rsi_entry:
                pos = 1; entry = r['close']
            elif r['rsi'] > (100 - rsi_entry):
                pos = -1; entry = r['close']

        equity.append(cur)

    if pos != 0:
        if pos == 1:
            pnl = (df.iloc[-1]['close'] - entry) / entry * leverage - 2 * FEE
        else:
            pnl = (entry - df.iloc[-1]['close']) / entry * leverage - 2 * FEE
        capital *= (1 + pnl); trades.append({'pnl': pnl}); equity[-1] = capital

    return calc_metrics(equity, trades, annualize_factor=252*24)  # 1H candles

# ============================================================
# STRATEGY C: KC+MACD Optimized (web research)
# ============================================================
def kc_macd_opt(df, params, leverage=1):
    kc_period = params.get('kc_period', 15)
    atr_mult = params.get('atr_mult', 2.0)
    tp_atr = params.get('tp_atr', 2.5)
    sl_atr = params.get('sl_atr', 1.0)

    df = df.copy()
    df['ema'] = df['close'].ewm(span=kc_period, adjust=False).mean()
    df['atr'] = (df['high'] - df['low']).rolling(kc_period).mean()
    df['kc_upper'] = df['ema'] + atr_mult * df['atr']
    df['kc_lower'] = df['ema'] - atr_mult * df['atr']
    macd_f = df['close'].ewm(span=12, adjust=False).mean()
    macd_s = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = macd_f - macd_s
    df['macd_sig'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_sig']
    df = add_ema_regime(df, 200, 100)

    capital = INITIAL_CAPITAL
    pos = 0; entry = 0; entry_atr = 0
    equity = [capital]; trades = []

    for i in range(1, len(df)):
        r = df.iloc[i]; p = df.iloc[i-1]
        if pos == 1:
            pnl = (r['close'] - entry) / entry * leverage
        elif pos == -1:
            pnl = (entry - r['close']) / entry * leverage
        else:
            pnl = 0
        cur = capital * (1 + pnl)

        if pos == 1:
            tp = entry + entry_atr * tp_atr * leverage
            sl = entry - entry_atr * sl_atr * leverage
            if r['close'] >= tp or r['close'] <= sl or r['macd_hist'] < 0:
                pnl = (r['close'] - entry) / entry * leverage - 2 * FEE
                capital *= (1 + pnl); trades.append({'pnl': pnl}); pos = 0
        elif pos == -1:
            tp = entry - entry_atr * tp_atr * leverage
            sl = entry + entry_atr * sl_atr * leverage
            if r['close'] <= tp or r['close'] >= sl or r['macd_hist'] > 0:
                pnl = (entry - r['close']) / entry * leverage - 2 * FEE
                capital *= (1 + pnl); trades.append({'pnl': pnl}); pos = 0

        if pos == 0 and r['regime'] != 'bear':
            if r['close'] > r['kc_upper'] and r['macd_hist'] > 0 and p['macd_hist'] <= 0:
                pos = 1; entry = r['close']; entry_atr = r['atr']
            elif r['close'] < r['kc_lower'] and r['macd_hist'] < 0 and p['macd_hist'] >= 0:
                pos = -1; entry = r['close']; entry_atr = r['atr']

        equity.append(cur)

    if pos != 0:
        if pos == 1:
            pnl = (df.iloc[-1]['close'] - entry) / entry * leverage - 2 * FEE
        else:
            pnl = (entry - df.iloc[-1]['close']) / entry * leverage - 2 * FEE
        capital *= (1 + pnl); trades.append({'pnl': pnl}); equity[-1] = capital

    return calc_metrics(equity, trades)

# ============================================================
# PORTFOLIO COMBINER
# ============================================================
def run_portfolio(df_4h, df_1h, strategies_list):
    """Run multiple strategies, split capital equally, combine equity."""
    capital_per = INITIAL_CAPITAL / len(strategies_list)
    all_trades = []
    equity_curves = []
    results = []

    for name, func, params, tf, lev in strategies_list:
        df = df_4h.copy() if tf == '4H' else df_1h.copy()
        # Scale initial capital
        orig = INITIAL_CAPITAL
        # We need to run with capital_per as initial
        # Hack: run normally then scale equity
        metrics = func(df, params, leverage=lev)
        # Scale equity curve by capital_per / INITIAL_CAPITAL
        results.append({'name': name, **metrics})
        all_trades.extend([{'pnl': t['pnl'], 'name': name} for t in [{'pnl': metrics['avg_r']}] * metrics['trades']])

    # Simple combined metrics
    total_cagr = sum(r['cagr'] for r in results)
    avg_pf = np.mean([r['pf'] for r in results if r['pf'] < 50])
    total_trades = sum(r['trades'] for r in results)
    return results, total_cagr, avg_pf, total_trades

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    print("=" * 70)
    print("COMPREHENSIVE STRATEGY COMPARISON")
    print("=" * 70)

    df_4h = load_data('4H')
    df_4h = add_ema_regime(df_4h, 200, 100)
    df_1h = load_data('1H')
    df_1h = add_ema_regime(df_1h, 200, 100)

    # =============================================
    # Section 1: Individual strategy comparison
    # =============================================
    print(f"\n{'='*70}")
    print("SECTION 1: Individual Strategy Comparison (3x leverage)")
    print(f"{'='*70}")

    configs = [
        ("TrendJoin 4H (EMA 10/50, TP 4.5, SL 2.0)", trend_join_4h,
         {'ema_fast': 10, 'ema_slow': 50, 'tp_atr': 4.5, 'sl_atr': 2.0}, '4H', 3),
        ("TrendJoin 4H (EMA 15/50, TP 4.5, SL 2.0)", trend_join_4h,
         {'ema_fast': 15, 'ema_slow': 50, 'tp_atr': 4.5, 'sl_atr': 2.0}, '4H', 3),
        ("TrendJoin 4H (EMA 20/50, TP 3.0, SL 1.5)", trend_join_4h,
         {'ema_fast': 20, 'ema_slow': 50, 'tp_atr': 3.0, 'sl_atr': 1.5}, '4H', 3),
        ("RSI Bounce 1H (TP 0.7%, SL 0.7%)", rsi_bounce_1h,
         {'rsi_period': 14, 'rsi_entry': 30, 'rsi_exit': 50, 'tp_pct': 0.007, 'sl_pct': 0.007}, '1H', 3),
        ("RSI Bounce 1H (TP 1.0%, SL 1.0%)", rsi_bounce_1h,
         {'rsi_period': 14, 'rsi_entry': 30, 'rsi_exit': 50, 'tp_pct': 0.01, 'sl_pct': 0.01}, '1H', 3),
        ("KC+MACD Optimized (KC 15, TP 2.5, SL 1.0)", kc_macd_opt,
         {'kc_period': 15, 'atr_mult': 2.0, 'tp_atr': 2.5, 'sl_atr': 1.0}, '4H', 3),
    ]

    all_results = []
    for name, func, params, tf, lev in configs:
        df = df_4h.copy() if tf == '4H' else df_1h.copy()
        m = func(df, params, leverage=lev)
        wf_p, wf_d = walk_forward_validate(df, func, params, 3, 0.3, leverage=lev)
        all_results.append({
            'name': name, 'tf': tf, 'lev': lev, **m,
            'wf': f"{wf_p}/{len(wf_d)}"
        })

    print(f"\n  {'Strategy':<45} {'TF':>3} {'CAGR':>8} {'MaxDD':>8} {'PF':>6} {'WR':>6} {'Trades':>7} {'WF':>5}")
    print(f"  {'-'*45} {'-'*3} {'-'*8} {'-'*8} {'-'*6} {'-'*6} {'-'*7} {'-'*5}")
    for r in all_results:
        print(f"  {r['name']:<45} {r['tf']:>3} {r['cagr']*100:>+7.1f}% {r['max_dd']*100:>7.1f}% "
              f"{r['pf']:>5.2f} {r['wr']:>5.0f}% {r['trades']:>6} {r['wf']:>5}")

    # =============================================
    # Section 2: Portfolio combinations
    # =============================================
    print(f"\n{'='*70}")
    print("SECTION 2: Portfolio Combinations")
    print(f"{'='*70}")

    # Portfolio A: TrendJoin 4H 3x + RSI Bounce 1H 3x (our best)
    tj_params = {'ema_fast': 10, 'ema_slow': 50, 'tp_atr': 4.5, 'sl_atr': 2.0}
    rsi_params = {'rsi_period': 14, 'rsi_entry': 30, 'rsi_exit': 50, 'tp_pct': 0.007, 'sl_pct': 0.007}
    kc_params = {'kc_period': 15, 'atr_mult': 2.0, 'tp_atr': 2.5, 'sl_atr': 1.0}

    tj_m = trend_join_4h(df_4h, tj_params, leverage=3)
    rsi_m = rsi_bounce_1h(df_1h, rsi_params, leverage=3)
    kc_m = kc_macd_opt(df_4h, kc_params, leverage=3)

    print(f"\n  Individual results (3x):")
    print(f"    TrendJoin 4H: CAGR {tj_m['cagr']*100:+.1f}%, MaxDD {tj_m['max_dd']*100:.1f}%, PF {tj_m['pf']:.2f}, Trades {tj_m['trades']}")
    print(f"    RSI Bounce 1H: CAGR {rsi_m['cagr']*100:+.1f}%, MaxDD {rsi_m['max_dd']*100:.1f}%, PF {rsi_m['pf']:.2f}, Trades {rsi_m['trades']}")
    print(f"    KC+MACD 4H: CAGR {kc_m['cagr']*100:+.1f}%, MaxDD {kc_m['max_dd']*100:.1f}%, PF {kc_m['pf']:.2f}, Trades {kc_m['trades']}")

    # Combined CAGR (weighted by capital allocation)
    # Portfolio 1: 50% TJ + 50% RSI
    p1_cagr = (tj_m['cagr'] + rsi_m['cagr']) / 2
    p1_pf = (tj_m['pf'] + rsi_m['pf']) / 2
    p1_trades = tj_m['trades'] + rsi_m['trades']

    # Portfolio 2: 40% TJ + 30% RSI + 30% KC
    p2_cagr = tj_m['cagr'] * 0.4 + rsi_m['cagr'] * 0.3 + kc_m['cagr'] * 0.3
    p2_pf = (tj_m['pf'] * 0.4 + rsi_m['pf'] * 0.3 + kc_m['pf'] * 0.3)
    p2_trades = tj_m['trades'] + rsi_m['trades'] + kc_m['trades']

    print(f"\n  Portfolio 1: 50% TrendJoin 4H + 50% RSI Bounce 1H (3x each)")
    print(f"    Estimated CAGR: {p1_cagr*100:+.1f}% | Avg PF: {p1_pf:.2f} | Total Trades: {p1_trades}")

    print(f"\n  Portfolio 2: 40% TrendJoin + 30% RSI + 30% KC+MACD (3x each)")
    print(f"    Estimated CAGR: {p2_cagr*100:+.1f}% | Avg PF: {p2_pf:.2f} | Total Trades: {p2_trades}")

    # =============================================
    # Section 3: TrendJoin long-only comparison
    # =============================================
    print(f"\n{'='*70}")
    print("SECTION 3: TrendJoin Long-Only (bull regime only)")
    print(f"{'='*70}")

    def trend_join_long_only(df, params, leverage=1):
        ema_fast = params.get('ema_fast', 10)
        ema_slow = params.get('ema_slow', 50)
        tp_atr = params.get('tp_atr', 4.5)
        sl_atr = params.get('sl_atr', 2.0)

        df = df.copy()
        df['ema_f'] = df['close'].ewm(span=ema_fast, adjust=False).mean()
        df['ema_s'] = df['close'].ewm(span=ema_slow, adjust=False).mean()
        df['atr'] = (df['high'] - df['low']).rolling(14).mean()
        df = add_ema_regime(df, 200, 100)

        capital = INITIAL_CAPITAL
        pos = 0; entry = 0; entry_atr = 0
        equity = [capital]; trades = []

        for i in range(1, len(df)):
            r = df.iloc[i]; p = df.iloc[i-1]
            pnl = 0
            if pos == 1:
                pnl = (r['close'] - entry) / entry * leverage
            cur = capital * (1 + pnl)

            if pos == 1:
                if r['close'] >= entry + entry_atr * tp_atr or r['close'] <= entry - entry_atr * sl_atr:
                    pnl = (r['close'] - entry) / entry * leverage - 2 * FEE
                    capital *= (1 + pnl); trades.append({'pnl': pnl}); pos = 0

            # Only long entries, only in bull regime
            if pos == 0 and r['regime'] == 'bull':
                if r['ema_f'] > r['ema_s'] and p['ema_f'] <= p['ema_s']:
                    pos = 1; entry = r['close']; entry_atr = r['atr']

            equity.append(cur)

        if pos == 1:
            pnl = (df.iloc[-1]['close'] - entry) / entry * leverage - 2 * FEE
            capital *= (1 + pnl); trades.append({'pnl': pnl}); equity[-1] = capital

        return calc_metrics(equity, trades)

    for lev in [1, 2, 3, 5]:
        m = trend_join_long_only(df_4h, {'ema_fast': 10, 'ema_slow': 50, 'tp_atr': 4.5, 'sl_atr': 2.0}, leverage=lev)
        wf_p, wf_d = walk_forward_validate(df_4h, trend_join_long_only,
            {'ema_fast': 10, 'ema_slow': 50, 'tp_atr': 4.5, 'sl_atr': 2.0}, 3, 0.3, leverage=lev)
        print(f"  Long-only {lev}x: CAGR {m['cagr']*100:+.1f}% | MaxDD {m['max_dd']*100:.1f}% | "
              f"PF {m['pf']:.2f} | WR {m['wr']:.0f}% | Trades {m['trades']} | WF {wf_p}/{len(wf_d)}")

    # =============================================
    # Section 4: TrendJoin with tighter exits
    # =============================================
    print(f"\n{'='*70}")
    print("SECTION 4: TrendJoin Exit Optimization (4H, 3x)")
    print(f"{'='*70}")

    best_exit = None
    for tp in [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0]:
        for sl in [1.0, 1.5, 2.0, 2.5, 3.0]:
            if sl >= tp:
                continue
            p = {'ema_fast': 10, 'ema_slow': 50, 'tp_atr': tp, 'sl_atr': sl}
            m = trend_join_4h(df_4h, p, leverage=3)
            wf_p, wf_d = walk_forward_validate(df_4h, trend_join_4h, p, 3, 0.3, leverage=3)
            if m['cagr'] > 0 and m['pf'] > 1 and wf_p >= 2:
                score = m['cagr'] * m['pf'] / max(abs(m['max_dd']), 0.01)
                if best_exit is None or score > best_exit['score']:
                    best_exit = {'tp': tp, 'sl': sl, **m, 'score': score, 'wf': f"{wf_p}/{len(wf_d)}"}

    if best_exit:
        print(f"\n  BEST EXIT (3x, WF≥2/3):")
        print(f"    TP={best_exit['tp']}x ATR, SL={best_exit['sl']}x ATR")
        print(f"    CAGR {best_exit['cagr']*100:+.1f}% | MaxDD {best_exit['max_dd']*100:.1f}% | "
              f"PF {best_exit['pf']:.2f} | WR {best_exit['wr']:.0f}% | Trades {best_exit['trades']} | WF {best_exit['wf']}")
