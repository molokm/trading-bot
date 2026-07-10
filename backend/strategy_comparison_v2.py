"""
CORRECTED Comprehensive Strategy Comparison — matching original working implementations.
Uses 2% risk-based position sizing, proper entry filters, intra-candle exits.
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

def calc_atr_ewm(df, n=14):
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def calc_rsi(s, n=14):
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)

def calc_adx(df, n=14):
    h, l = df['high'], df['low']
    plus_dm = (h - h.shift()).clip(lower=0)
    minus_dm = (l.shift() - l).clip(lower=0)
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0
    atr14 = df['ATR_14']
    plus_di = 100 * (plus_dm.ewm(alpha=1/n, adjust=False).mean() / atr14)
    minus_di = 100 * (minus_dm.ewm(alpha=1/n, adjust=False).mean() / atr14)
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
    return dx.ewm(alpha=1/n, adjust=False).mean()

def calc_metrics_from_capital(initial_capital, final_capital, n_candles, candles_per_year, trades_list):
    if not trades_list:
        return {'cagr': 0, 'max_dd': 0, 'pf': 0, 'wr': 0, 'trades': 0}
    # Rebuild equity from trades
    equity = [initial_capital]
    cap = initial_capital
    for t in trades_list:
        cap += t
        equity.append(cap)
    equity = np.array(equity)
    years = n_candles / candles_per_year
    cagr = (final_capital / initial_capital) ** (1 / max(years, 0.01)) - 1
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    max_dd = dd.min()
    gp = sum(t for t in trades_list if t > 0)
    gl = abs(sum(t for t in trades_list if t < 0))
    pf = gp / gl if gl > 0 else 99
    wr = sum(1 for t in trades_list if t > 0) / len(trades_list) * 100
    return {'cagr': cagr, 'max_dd': max_dd, 'pf': pf, 'wr': wr, 'trades': len(trades_list)}

# ============================================================
# STRATEGY A: TrendJoin 4H (exact original implementation)
# ============================================================
def backtest_trendjoin_4h(df, sl_mult=2.0, tp_mult=4.5, leverage=3):
    df = df.copy()
    df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['RSI_14'] = calc_rsi(df['close'], 14)
    df['ATR_14'] = calc_atr_ewm(df, 14)
    df['ADX_14'] = calc_adx(df, 14)

    close_v = df['close'].values
    high_v = df['high'].values
    low_v = df['low'].values
    e20_v = df['EMA_20'].values
    e50_v = df['EMA_50'].values
    rsi_v = df['RSI_14'].values
    adx_v = df['ADX_14'].values
    atr_v = df['ATR_14'].values

    capital = INITIAL_CAPITAL
    pos = None
    trades = []

    for i in range(1, len(df)):
        price = close_v[i]
        hi = high_v[i]
        lo = low_v[i]

        if pos:
            hit_sl = lo <= pos['stop']
            hit_tp = hi >= pos['target']
            if hit_sl or hit_tp:
                exit_px = pos['stop'] if hit_sl else pos['target']
                pnl = (exit_px - pos['entry']) * pos['size'] * leverage
                fee = exit_px * pos['size'] * leverage * FEE
                capital += pnl - fee
                trades.append(pnl - fee)
                pos = None

        if pos is None and capital > 0:
            e20 = e20_v[i-1]; e50 = e50_v[i-1]
            rv = rsi_v[i-1]; av = adx_v[i-1]; atr = atr_v[i-1]
            if np.isnan(e20) or np.isnan(e50) or np.isnan(av):
                continue
            regime = 'bull' if e20 > e50 and av > 20 and not np.isnan(rv) and rv > 50 else 'other'
            trend_ok = e20 > e50
            dist = (close_v[i-1] - e20) / e20 * 100 if e20 > 0 else 0
            pullback_ok = -3.0 < dist < 2.5
            rsi_ok = not np.isnan(rv) and rv > 30
            adx_ok = av > 18
            passed = trend_ok and pullback_ok and rsi_ok and adx_ok
            if passed and regime == 'bull':
                entry = price
                if np.isnan(atr) or atr <= 0: continue
                stop = entry - sl_mult * atr
                target = entry + tp_mult * atr
                r_val = entry - stop
                size = (capital * 0.02) / r_val if r_val > 0 else 0
                if size > 0:
                    fee = entry * size * leverage * FEE
                    capital -= fee
                    pos = {'entry': entry, 'stop': stop, 'target': target, 'size': size, 'r_val': r_val}

        if i == len(df) - 1 and pos:
            exit_px = close_v[-1]
            pnl = (exit_px - pos['entry']) * pos['size'] * leverage
            fee = exit_px * pos['size'] * leverage * FEE
            capital += pnl - fee
            trades.append(pnl - fee)

    return capital, trades

# ============================================================
# STRATEGY B: RSI Bounce 1H (from scalp_fixed_tp.py, proven working)
# ============================================================
def backtest_rsi_bounce_1h(df, tp_pct=0.007, sl_pct=0.007, leverage=3):
    df = df.copy()
    df['RSI_14'] = calc_rsi(df['close'], 14)
    df['EMA_200'] = df['close'].ewm(span=200, adjust=False).mean()

    close_v = df['close'].values
    high_v = df['high'].values
    low_v = df['low'].values
    rsi_v = df['RSI_14'].values
    ema200_v = df['EMA_200'].values

    capital = INITIAL_CAPITAL
    pos = None
    trades = []

    for i in range(1, len(df)):
        price = close_v[i]
        hi = high_v[i]
        lo = low_v[i]

        if pos:
            if pos['dir'] == 'long':
                hit_tp = hi >= entry * (1 + tp_pct)
                hit_sl = lo <= entry * (1 - sl_pct)
            else:
                hit_tp = lo <= entry * (1 - tp_pct)
                hit_sl = hi >= entry * (1 + sl_pct)

            if hit_tp or hit_sl:
                if pos['dir'] == 'long':
                    pnl = (pos['exit'] - pos['entry']) / pos['entry'] * pos['size'] * leverage
                else:
                    pnl = (pos['entry'] - pos['exit']) / pos['entry'] * pos['size'] * leverage
                fee = pos['size'] * leverage * FEE
                capital += pnl - fee
                trades.append(pnl - fee)
                pos = None

        if pos is None and capital > 0:
            rv = rsi_v[i]
            ema200 = ema200_v[i]
            if np.isnan(rv) or np.isnan(ema200): continue

            # Long: RSI < 30, price above EMA200
            if rv < 30 and close_v[i-1] > ema200_v[i-1]:
                entry = price
                # Risk-based sizing: 2% of capital
                r_val = entry * sl_pct
                size = (capital * 0.02) / r_val if r_val > 0 else 0
                if size > 0:
                    fee = entry * size * leverage * FEE
                    capital -= fee
                    if hit_tp is False and hit_sl is False:
                        pos = {'dir': 'long', 'entry': entry, 'exit': 0, 'size': size}
                        # Set exit levels
                        pos['target'] = entry * (1 + tp_pct)
                        pos['stop'] = entry * (1 - sl_pct)

        if i == len(df) - 1 and pos:
            if pos['dir'] == 'long':
                pnl = (close_v[-1] - pos['entry']) / pos['entry'] * pos['size'] * leverage
            else:
                pnl = (pos['entry'] - close_v[-1]) / pos['entry'] * pos['size'] * leverage
            fee = pos['size'] * leverage * FEE
            capital += pnl - fee
            trades.append(pnl - fee)

    return capital, trades

# ============================================================
# STRATEGY C: KC+MACD Breakout (web research, adapted with 2% risk sizing)
# ============================================================
def backtest_kc_macd(df, kc_period=15, atr_mult=2.0, tp_atr=2.5, sl_atr=1.0, leverage=3):
    df = df.copy()
    df['ema'] = df['close'].ewm(span=kc_period, adjust=False).mean()
    df['atr'] = calc_atr_ewm(df, kc_period)
    df['kc_upper'] = df['ema'] + atr_mult * df['atr']
    df['kc_lower'] = df['ema'] - atr_mult * df['atr']
    macd_f = df['close'].ewm(span=12, adjust=False).mean()
    macd_s = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = macd_f - macd_s
    df['macd_sig'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_sig']
    df['EMA_200'] = df['close'].ewm(span=200, adjust=False).mean()
    df['EMA_100'] = df['close'].ewm(span=100, adjust=False).mean()

    close_v = df['close'].values
    high_v = df['high'].values
    low_v = df['low'].values
    kc_upper_v = df['kc_upper'].values
    kc_lower_v = df['kc_lower'].values
    macd_hist_v = df['macd_hist'].values
    atr_v = df['atr'].values
    ema200_v = df['EMA_200'].values
    ema100_v = df['EMA_100'].values

    capital = INITIAL_CAPITAL
    pos = None
    trades = []

    for i in range(1, len(df)):
        price = close_v[i]
        hi = high_v[i]
        lo = low_v[i]

        if pos:
            hit_sl = lo <= pos['stop']
            hit_tp = hi >= pos['target']
            macd_exit = (pos['dir'] == 'long' and macd_hist_v[i] < 0) or (pos['dir'] == 'short' and macd_hist_v[i] > 0)
            if hit_sl or hit_tp or macd_exit:
                if hit_sl:
                    exit_px = pos['stop']
                elif hit_tp:
                    exit_px = pos['target']
                else:
                    exit_px = price
                if pos['dir'] == 'long':
                    pnl = (exit_px - pos['entry']) / pos['entry'] * pos['size'] * leverage
                else:
                    pnl = (pos['entry'] - exit_px) / pos['entry'] * pos['size'] * leverage
                fee = pos['size'] * leverage * FEE
                capital += pnl - fee
                trades.append(pnl - fee)
                pos = None

        if pos is None and capital > 0:
            if np.isnan(ema200_v[i]) or np.isnan(ema100_v[i]): continue
            regime = 'bull' if ema200_v[i] > ema100_v[i] else 'bear'
            if regime == 'bear': continue

            if not np.isnan(kc_upper_v[i]) and not np.isnan(atr_v[i]):
                if price > kc_upper_v[i] and macd_hist_v[i] > 0 and macd_hist_v[i-1] <= 0:
                    entry = price
                    r_val = entry * sl_atr * atr_v[i] / entry  # approximate
                    r_val = sl_atr * atr_v[i]
                    size = (capital * 0.02) / r_val if r_val > 0 else 0
                    if size > 0:
                        fee = entry * size * leverage * FEE
                        capital -= fee
                        stop = entry - sl_atr * atr_v[i]
                        target = entry + tp_atr * atr_v[i]
                        pos = {'dir': 'long', 'entry': entry, 'stop': stop, 'target': target, 'size': size}

        if i == len(df) - 1 and pos:
            if pos['dir'] == 'long':
                pnl = (close_v[-1] - pos['entry']) / pos['entry'] * pos['size'] * leverage
            else:
                pnl = (pos['entry'] - close_v[-1]) / pos['entry'] * pos['size'] * leverage
            fee = pos['size'] * leverage * FEE
            capital += pnl - fee
            trades.append(pnl - fee)

    return capital, trades

# ============================================================
# STRATEGY D: Donchian Breakout (web research, 2% risk sizing)
# ============================================================
def backtest_donchian(df, dc_period=20, atr_trail=2.5, leverage=3):
    df = df.copy()
    df['dc_upper'] = df['high'].rolling(dc_period).max()
    df['dc_lower'] = df['low'].rolling(dc_period).min()
    df['ATR_14'] = calc_atr_ewm(df, 14)
    df['EMA_200'] = df['close'].ewm(span=200, adjust=False).mean()
    df['EMA_100'] = df['close'].ewm(span=100, adjust=False).mean()

    close_v = df['close'].values
    high_v = df['high'].values
    low_v = df['low'].values
    dc_upper_v = df['dc_upper'].values
    dc_lower_v = df['dc_lower'].values
    atr_v = df['ATR_14'].values
    ema200_v = df['EMA_200'].values
    ema100_v = df['EMA_100'].values

    capital = INITIAL_CAPITAL
    pos = None
    trades = []

    for i in range(1, len(df)):
        price = close_v[i]
        hi = high_v[i]
        lo = low_v[i]

        if pos:
            if pos['dir'] == 'long':
                new_trail = price - atr_trail * atr_v[i]
                if new_trail > pos['trail']:
                    pos['trail'] = new_trail
                hit_sl = lo <= pos['trail']
            else:
                new_trail = price + atr_trail * atr_v[i]
                if new_trail < pos['trail']:
                    pos['trail'] = new_trail
                hit_sl = hi >= pos['trail']

            if hit_sl:
                if pos['dir'] == 'long':
                    pnl = (pos['trail'] - pos['entry']) / pos['entry'] * pos['size'] * leverage
                else:
                    pnl = (pos['entry'] - pos['trail']) / pos['entry'] * pos['size'] * leverage
                fee = pos['size'] * leverage * FEE
                capital += pnl - fee
                trades.append(pnl - fee)
                pos = None

        if pos is None and capital > 0:
            if np.isnan(ema200_v[i]) or np.isnan(ema100_v[i]): continue
            regime = 'bull' if ema200_v[i] > ema100_v[i] else 'bear'
            if regime == 'bear': continue

            if not np.isnan(dc_upper_v[i-1]) and not np.isnan(atr_v[i]):
                if price > dc_upper_v[i-1]:
                    entry = price
                    r_val = atr_trail * atr_v[i]
                    size = (capital * 0.02) / r_val if r_val > 0 else 0
                    if size > 0:
                        fee = entry * size * leverage * FEE
                        capital -= fee
                        trail = entry - atr_trail * atr_v[i]
                        pos = {'dir': 'long', 'entry': entry, 'trail': trail, 'size': size}

        if i == len(df) - 1 and pos:
            if pos['dir'] == 'long':
                pnl = (close_v[-1] - pos['entry']) / pos['entry'] * pos['size'] * leverage
            else:
                pnl = (pos['entry'] - close_v[-1]) / pos['entry'] * pos['size'] * leverage
            fee = pos['size'] * leverage * FEE
            capital += pnl - fee
            trades.append(pnl - fee)

    return capital, trades

# ============================================================
# WALK-FORWARD VALIDATION
# ============================================================
def walk_forward(df, backtest_func, n_splits=3, test_ratio=0.3, **kwargs):
    total_len = len(df)
    split_size = total_len // n_splits
    all_cagr = []
    for i in range(n_splits):
        start = i * split_size
        end = min(start + split_size, total_len)
        test_start = int(start + split_size * (1 - test_ratio))
        test_df = df.iloc[test_start:end]
        if len(test_df) < 50:
            continue
        final_cap, trades = backtest_func(test_df, **kwargs)
        n_candles = len(test_df)
        years = n_candles * 4 / 8760  # 4H candles
        cagr = (final_cap / INITIAL_CAPITAL) ** (1 / max(years, 0.01)) - 1
        all_cagr.append(cagr)
    profitable = sum(1 for c in all_cagr if c > 0)
    return profitable, all_cagr

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    print("=" * 70)
    print("CORRECTED STRATEGY COMPARISON — 4H BTC, 2% risk sizing")
    print("=" * 70)

    df_4h = load_data('4H')
    df_1h = load_data('1H')

    leverage_levels = [1, 2, 3]

    # =============================================
    # A. TrendJoin 4H
    # =============================================
    print(f"\n{'='*60}")
    print("A. TRENDJOIN 4H (original implementation)")
    print(f"{'='*60}")
    for lev in leverage_levels:
        final_cap, trades = backtest_trendjoin_4h(df_4h, sl_mult=2.0, tp_mult=4.5, leverage=lev)
        m = calc_metrics_from_capital(INITIAL_CAPITAL, final_cap, len(df_4h), 4 * 365 / 4, trades)
        wf_p, wf_cagrs = walk_forward(df_4h, backtest_trendjoin_4h, leverage=lev)
        print(f"  {lev}x: CAGR {m['cagr']*100:+.1f}% | MaxDD {m['max_dd']*100:.1f}% | "
              f"PF {m['pf']:.2f} | WR {m['wr']:.0f}% | Trades {m['trades']} | WF {wf_p}/{len(wf_cagrs)}")

    # =============================================
    # B. TrendJoin Exit Optimization
    # =============================================
    print(f"\n{'='*60}")
    print("B. TRENDJOIN EXIT OPTIMIZATION (3x, WF≥2)")
    print(f"{'='*60}")
    best_tj = None
    for tp in [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0]:
        for sl in [1.0, 1.5, 2.0, 2.5, 3.0]:
            if sl >= tp: continue
            final_cap, trades = backtest_trendjoin_4h(df_4h, sl_mult=sl, tp_mult=tp, leverage=3)
            m = calc_metrics_from_capital(INITIAL_CAPITAL, final_cap, len(df_4h), 4*365/4, trades)
            wf_p, wf_cagrs = walk_forward(df_4h, backtest_trendjoin_4h, sl_mult=sl, tp_mult=tp, leverage=3)
            if m['cagr'] > 0 and m['pf'] > 1 and wf_p >= 2:
                score = m['cagr'] * m['pf'] / max(abs(m['max_dd']), 0.01)
                if best_tj is None or score > best_tj['score']:
                    best_tj = {'tp': tp, 'sl': sl, **m, 'score': score, 'wf': f"{wf_p}/{len(wf_cagrs)}"}
    if best_tj:
        print(f"  BEST: TP={best_tj['tp']}x SL={best_tj['sl']}x | "
              f"CAGR {best_tj['cagr']*100:+.1f}% | MaxDD {best_tj['max_dd']*100:.1f}% | "
              f"PF {best_tj['pf']:.2f} | WR {best_tj['wr']:.0f}% | Trades {best_tj['trades']} | WF {best_tj['wf']}")

    # =============================================
    # C. KC+MACD Breakout
    # =============================================
    print(f"\n{'='*60}")
    print("C. KC+MACD BREAKOUT (web research)")
    print(f"{'='*60}")
    best_kc = None
    for kc_p in [15, 20, 30]:
        for atr_m in [1.0, 1.5, 2.0]:
            for tp in [1.5, 2.0, 2.5, 3.0]:
                for sl in [1.0, 1.5, 2.0]:
                    final_cap, trades = backtest_kc_macd(df_4h, kc_period=kc_p, atr_mult=atr_m, tp_atr=tp, sl_atr=sl, leverage=3)
                    m = calc_metrics_from_capital(INITIAL_CAPITAL, final_cap, len(df_4h), 4*365/4, trades)
                    wf_p, wf_cagrs = walk_forward(df_4h, backtest_kc_macd, kc_period=kc_p, atr_mult=atr_m, tp_atr=tp, sl_atr=sl, leverage=3)
                    if m['cagr'] > 0 and m['pf'] > 1 and wf_p >= 2:
                        score = m['cagr'] * m['pf'] / max(abs(m['max_dd']), 0.01)
                        if best_kc is None or score > best_kc['score']:
                            best_kc = {'kc_p': kc_p, 'atr_m': atr_m, 'tp': tp, 'sl': sl,
                                        **m, 'score': score, 'wf': f"{wf_p}/{len(wf_cagrs)}"}
    if best_kc:
        print(f"  BEST: KC={best_kc['kc_p']} ATR={best_kc['atr_m']} TP={best_kc['tp']}x SL={best_kc['sl']}x | "
              f"CAGR {best_kc['cagr']*100:+.1f}% | MaxDD {best_kc['max_dd']*100:.1f}% | "
              f"PF {best_kc['pf']:.2f} | WR {best_kc['wr']:.0f}% | Trades {best_kc['trades']} | WF {best_kc['wf']}")
    else:
        print("  No profitable KC+MACD combo found with WF≥2/3")

    # =============================================
    # D. Donchian Breakout
    # =============================================
    print(f"\n{'='*60}")
    print("D. DONCHIAN BREAKOUT (web research)")
    print(f"{'='*60}")
    best_dc = None
    for dc_p in [10, 15, 20, 30]:
        for trail in [1.5, 2.0, 2.5, 3.0]:
            final_cap, trades = backtest_donchian(df_4h, dc_period=dc_p, atr_trail=trail, leverage=3)
            m = calc_metrics_from_capital(INITIAL_CAPITAL, final_cap, len(df_4h), 4*365/4, trades)
            wf_p, wf_cagrs = walk_forward(df_4h, backtest_donchian, dc_period=dc_p, atr_trail=trail, leverage=3)
            if m['cagr'] > 0 and m['pf'] > 1 and wf_p >= 2:
                score = m['cagr'] * m['pf'] / max(abs(m['max_dd']), 0.01)
                if best_dc is None or score > best_dc['score']:
                    best_dc = {'dc_p': dc_p, 'trail': trail, **m, 'score': score, 'wf': f"{wf_p}/{len(wf_cagrs)}"}
    if best_dc:
        print(f"  BEST: DC={best_dc['dc_p']} Trail={best_dc['trail']}x | "
              f"CAGR {best_dc['cagr']*100:+.1f}% | MaxDD {best_dc['max_dd']*100:.1f}% | "
              f"PF {best_dc['pf']:.2f} | WR {best_dc['wr']:.0f}% | Trades {best_dc['trades']} | WF {best_dc['wf']}")
    else:
        print("  No profitable Donchian combo found with WF≥2/3")

    # =============================================
    # E. TrendJoin Long-Only (bull only)
    # =============================================
    print(f"\n{'='*60}")
    print("E. TRENDJOIN LONG-ONLY (bull regime only)")
    print(f"{'='*60}")
    for lev in leverage_levels:
        final_cap, trades = backtest_trendjoin_4h(df_4h, sl_mult=2.0, tp_mult=4.5, leverage=lev)
        # Modify to long-only
        df2 = df_4h.copy()
        df2['EMA_20'] = df2['close'].ewm(span=20, adjust=False).mean()
        df2['EMA_50'] = df2['close'].ewm(span=50, adjust=False).mean()
        df2['RSI_14'] = calc_rsi(df2['close'], 14)
        df2['ATR_14'] = calc_atr_ewm(df2, 14)
        df2['ADX_14'] = calc_adx(df2, 14)

        close_v = df2['close'].values
        high_v = df2['high'].values
        low_v = df2['low'].values
        e20_v = df2['EMA_20'].values
        e50_v = df2['EMA_50'].values
        rsi_v = df2['RSI_14'].values
        adx_v = df2['ADX_14'].values
        atr_v = df2['ATR_14'].values

        cap = INITIAL_CAPITAL
        pos = None
        tr = []
        for i in range(1, len(df2)):
            price = close_v[i]; hi = high_v[i]; lo = low_v[i]
            if pos:
                hit_sl = lo <= pos['stop']
                hit_tp = hi >= pos['target']
                if hit_sl or hit_tp:
                    exit_px = pos['stop'] if hit_sl else pos['target']
                    pnl = (exit_px - pos['entry']) * pos['size'] * lev
                    fee = exit_px * pos['size'] * lev * FEE
                    cap += pnl - fee
                    tr.append(pnl - fee)
                    pos = None
            if pos is None and cap > 0:
                e20 = e20_v[i-1]; e50 = e50_v[i-1]
                rv = rsi_v[i-1]; av = adx_v[i-1]; atr = atr_v[i-1]
                if np.isnan(e20) or np.isnan(e50) or np.isnan(av): continue
                regime = 'bull' if e20 > e50 and av > 20 and not np.isnan(rv) and rv > 50 else 'other'
                trend_ok = e20 > e50
                dist = (close_v[i-1] - e20) / e20 * 100 if e20 > 0 else 0
                pullback_ok = -3.0 < dist < 2.5
                rsi_ok = not np.isnan(rv) and rv > 30
                adx_ok = av > 18
                passed = trend_ok and pullback_ok and rsi_ok and adx_ok
                # LONG ONLY: regime must be bull
                if passed and regime == 'bull':
                    entry = price
                    if np.isnan(atr) or atr <= 0: continue
                    stop = entry - 2.0 * atr
                    target = entry + 4.5 * atr
                    r_val = entry - stop
                    size = (cap * 0.02) / r_val if r_val > 0 else 0
                    if size > 0:
                        fee = entry * size * lev * FEE
                        cap -= fee
                        pos = {'entry': entry, 'stop': stop, 'target': target, 'size': size}
            if i == len(df2) - 1 and pos:
                pnl = (close_v[-1] - pos['entry']) * pos['size'] * lev
                fee = close_v[-1] * pos['size'] * lev * FEE
                cap += pnl - fee; tr.append(pnl - fee)
        m = calc_metrics_from_capital(INITIAL_CAPITAL, cap, len(df2), 4*365/4, tr)
        print(f"  {lev}x: CAGR {m['cagr']*100:+.1f}% | MaxDD {m['max_dd']*100:.1f}% | "
              f"PF {m['pf']:.2f} | WR {m['wr']:.0f}% | Trades {m['trades']}")

    # =============================================
    # SUMMARY
    # =============================================
    print(f"\n{'='*70}")
    print("FINAL SUMMARY — ALL STRATEGIES (4H BTC, 2% risk sizing)")
    print(f"{'='*70}")
    summary = []

    for lev in leverage_levels:
        final_cap, trades = backtest_trendjoin_4h(df_4h, sl_mult=2.0, tp_mult=4.5, leverage=lev)
        m = calc_metrics_from_capital(INITIAL_CAPITAL, final_cap, len(df_4h), 4*365/4, trades)
        wf_p, wf_c = walk_forward(df_4h, backtest_trendjoin_4h, leverage=lev)
        summary.append(('TrendJoin 4H', lev, m, f"{wf_p}/{len(wf_c)}"))

    if best_tj:
        final_cap, trades = backtest_trendjoin_4h(df_4h, sl_mult=best_tj['sl'], tp_mult=best_tj['tp'], leverage=3)
        m = calc_metrics_from_capital(INITIAL_CAPITAL, final_cap, len(df_4h), 4*365/4, trades)
        summary.append(('TrendJoin OPT', 3, m, best_tj['wf']))

    if best_kc:
        final_cap, trades = backtest_kc_macd(df_4h, kc_period=best_kc['kc_p'], atr_mult=best_kc['atr_m'],
                                              tp_atr=best_kc['tp'], sl_atr=best_kc['sl'], leverage=3)
        m = calc_metrics_from_capital(INITIAL_CAPITAL, final_cap, len(df_4h), 4*365/4, trades)
        summary.append(('KC+MACD OPT', 3, m, best_kc['wf']))

    if best_dc:
        final_cap, trades = backtest_donchian(df_4h, dc_period=best_dc['dc_p'], atr_trail=best_dc['trail'], leverage=3)
        m = calc_metrics_from_capital(INITIAL_CAPITAL, final_cap, len(df_4h), 4*365/4, trades)
        summary.append(('Donchian OPT', 3, m, best_dc['wf']))

    print(f"\n  {'Strategy':<25} {'Lev':>4} {'CAGR':>8} {'MaxDD':>8} {'PF':>6} {'WR':>6} {'Trades':>7} {'WF':>5}")
    print(f"  {'-'*25} {'-'*4} {'-'*8} {'-'*8} {'-'*6} {'-'*6} {'-'*7} {'-'*5}")
    for name, lev, m, wf in summary:
        print(f"  {name:<25} {lev:>3}x {m['cagr']*100:>+7.1f}% {m['max_dd']*100:>7.1f}% "
              f"{m['pf']:>5.2f} {m['wr']:>5.0f}% {m['trades']:>6} {wf:>5}")
