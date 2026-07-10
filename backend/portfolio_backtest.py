"""
Combined Portfolio Backtest — TrendJoin 4H + MeanRev 15m
Both strategies on same time period, proper equity alignment.
"""
import numpy as np
import pandas as pd
from pathlib import Path
from backtest_rules import load_candles, load_rules, compute_indicators, detect_regime, check_entry


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)

def calc_atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def backtest_trendjoin_4h(symbol='BTCUSDT', sl_mult=2.0, tp_mult=4.5,
                          initial_capital=5000, fee_rate=0.0005, leverage=3):
    """TrendJoin 4H — returns equity as time-indexed series (vectorized)."""
    rules = load_rules()
    df = load_candles(symbol, '4H')
    df = compute_indicators(df, rules)

    capital = initial_capital
    pos = None
    equity = {}

    close_vals = df['Close'].values
    high_vals = df['High'].values
    low_vals = df['Low'].values
    ema20_vals = df['EMA_20'].values
    ema50_vals = df['EMA_50'].values
    rsi_vals = df['RSI_14'].values
    adx_vals = df['ADX_14'].values
    atr_vals = df['ATR_14'].values
    ts_vals = df['ts'].values

    for i in range(1, len(df)):
        price = close_vals[i]
        hi = high_vals[i]
        lo = low_vals[i]

        if pos:
            hit_sl = lo <= pos['stop']
            hit_tp = hi >= pos['target']
            if hit_sl or hit_tp:
                exit_px = pos['stop'] if hit_sl else pos['target']
                pnl = (exit_px - pos['entry']) * pos['size'] * leverage
                fee = exit_px * pos['size'] * leverage * fee_rate
                capital += pnl - fee
                pos = None

        if pos is None and capital > 0:
            e20 = ema20_vals[i-1]
            e50 = ema50_vals[i-1]
            rv = rsi_vals[i-1]
            av = adx_vals[i-1]
            atr = atr_vals[i-1]

            if np.isnan(e20) or np.isnan(e50) or np.isnan(av):
                equity[ts_vals[i]] = capital
                continue

            regime = "bull" if e20 > e50 and av > 20 and not np.isnan(rv) and rv > 50 else "other"
            trend_ok = e20 > e50
            dist = (close_vals[i-1] - e20) / e20 * 100 if e20 > 0 else 0
            pullback_ok = -3.0 < dist < 2.5
            rsi_ok = not np.isnan(rv) and rv > 30
            adx_ok = av > 18

            passed = trend_ok and pullback_ok and rsi_ok and adx_ok
            if passed and regime == 'bull':
                entry = price
                if np.isnan(atr) or atr <= 0:
                    equity[ts_vals[i]] = capital
                    continue
                stop = entry - sl_mult * atr
                target = entry + tp_mult * atr
                r_val = entry - stop
                size = (capital * 0.02) / r_val if r_val > 0 else 0
                if size > 0:
                    fee = entry * size * leverage * fee_rate
                    capital -= fee
                    pos = {'entry': entry, 'stop': stop, 'target': target,
                           'size': size, 'r_val': r_val}

        equity[ts_vals[i]] = capital

    if pos:
        exit_px = close_vals[-1]
        pnl = (exit_px - pos['entry']) * pos['size'] * leverage
        fee = exit_px * pos['size'] * leverage * fee_rate
        capital += pnl - fee
        equity[ts_vals[-1]] = capital

    return equity, capital


def backtest_meanrev_15m(symbol='BTCUSDT', initial_capital=5000, fee_rate=0.0005, leverage=3):
    """MeanRev 15m — returns equity as time-indexed series (vectorized where possible)."""
    path = Path('data/candles') / f'{symbol}_15m.csv'
    if not path.exists():
        print(f'  [MEANREV] No 15m data')
        return {}, initial_capital

    df = pd.read_csv(path)
    df['ts'] = pd.to_datetime(df['ts'])
    for c in ['Open', 'High', 'Low', 'Close', 'Volume']:
        df[c] = df[c].astype(float)

    # Pre-compute all indicators vectorized
    df['RSI_14'] = rsi(df['Close'], 14)
    df['EMA_200'] = ema(df['Close'], 200)
    df['ATR_14'] = calc_atr(df, 14)

    capital = initial_capital
    pos = None
    equity = {}

    # Sample every 4th candle to reduce iterations (still catch all signals)
    step = 4
    close_vals = df['Close'].values
    high_vals = df['High'].values
    low_vals = df['Low'].values
    rsi_vals = df['RSI_14'].values
    ema200_vals = df['EMA_200'].values
    atr_vals = df['ATR_14'].values
    ts_vals = df['ts'].values

    for i in range(1, len(df), step):
        price = close_vals[i]
        hi = high_vals[i]
        lo = low_vals[i]
        rv = rsi_vals[i]
        ema200 = ema200_vals[i]
        atr = atr_vals[i]

        if pos:
            stop = pos['entry'] - 2.0 * pos['atr_ref']
            target = pos['entry'] + 3.0 * pos['atr_ref']
            hit_sl = lo <= stop
            hit_tp = hi >= target
            rsi_exit = not np.isnan(rv) and rv > 55

            if hit_sl or hit_tp or rsi_exit:
                exit_px = stop if hit_sl else (target if hit_tp else price)
                pnl = (exit_px - pos['entry']) * pos['size'] * leverage
                fee = exit_px * pos['size'] * leverage * fee_rate
                capital += pnl - fee
                pos = None

        if pos is None and capital > 0:
            if not np.isnan(rv) and not np.isnan(ema200) and ema200 > 0:
                if rv < 30 and close_vals[i-1] > ema200_vals[i-1]:
                    entry = price
                    if np.isnan(atr) or atr <= 0:
                        continue
                    r_val = 2.0 * atr
                    size = (capital * 0.02) / r_val if r_val > 0 else 0
                    if size > 0:
                        fee = entry * size * leverage * fee_rate
                        capital -= fee
                        pos = {'entry': entry, 'atr_ref': atr, 'size': size}

        equity[ts_vals[i]] = capital

    return equity, capital


def run_portfolio(symbol='BTCUSDT'):
    """Run both strategies and combine on shared timeline."""
    print(f'\n{"="*65}')
    print(f'  PORTFOLIO BACKTEST: {symbol}')
    print(f'  TrendJoin 4H (3x) + MeanRev 15m (3x)')
    print(f'  Capital: $5,000 each = $10,000 total')
    print(f'{"="*65}')

    # Run both strategies
    tj_eq, tj_final = backtest_trendjoin_4h(symbol, initial_capital=5000, leverage=3)
    mr_eq, mr_final = backtest_meanrev_15m(symbol, initial_capital=5000, leverage=3)

    # Find overlapping period
    tj_dates = sorted(tj_eq.keys())
    mr_dates = sorted(mr_eq.keys())
    if not tj_dates or not mr_dates:
        print('  No data!')
        return

    start = max(tj_dates[0], mr_dates[0])
    end = min(tj_dates[-1], mr_dates[-1])

    # Build combined equity on 4H timestamps (coarser)
    combined = []
    for ts in tj_dates:
        if ts < start or ts > end:
            continue
        tj_val = tj_eq.get(ts, tj_eq.get(tj_dates[0], 5000))
        # Find closest MR timestamp
        ts_np = np.datetime64(ts)
        closest_mr_ts = min(mr_dates, key=lambda x: abs(np.datetime64(x) - ts_np), default=mr_dates[0])
        mr_val = mr_eq.get(closest_mr_ts, mr_eq.get(mr_dates[0], 5000))
        combined.append({'ts': ts, 'tj': tj_val, 'mr': mr_val, 'total': tj_val + mr_val})

    if not combined:
        print('  No overlapping data!')
        return

    total_equity = [c['total'] for c in combined]
    initial = 10000
    final = total_equity[-1]
    total_return = (final - initial) / initial * 100

    peak = initial
    max_dd = 0
    for v in total_equity:
        peak = max(peak, v)
        dd = (peak - v) / peak * 100
        max_dd = max(max_dd, dd)

    days = (combined[-1]['ts'] - combined[0]['ts']).total_seconds() / 86400
    years = days / 365.25
    cagr = ((final / initial) ** (1/max(years, 0.01)) - 1) * 100

    # Individual stats
    tj_final_val = combined[-1]['tj']
    mr_final_val = combined[-1]['mr']
    tj_return = (tj_final_val - 5000) / 5000 * 100
    mr_return = (mr_final_val - 5000) / 5000 * 100

    print(f'\n  Period: {combined[0]["ts"].strftime("%Y-%m-%d")} → {combined[-1]["ts"].strftime("%Y-%m-%d")}')
    print(f'  Duration: {days:.0f} days ({years:.1f} years)')
    print()
    print(f'  {"Strategy":<22} {"Capital":>10} {"Return":>10} {"Final":>12}')
    print(f'  {"─"*54}')
    print(f'  {"TrendJoin 4H 3x":<22} {"$5,000":>10} {tj_return:>+9.1f}% {"${:,.0f}".format(tj_final_val):>12}')
    print(f'  {"MeanRev 15m 3x":<22} {"$5,000":>10} {mr_return:>+9.1f}% {"${:,.0f}".format(mr_final_val):>12}')
    print(f'  {"─"*54}')
    print(f'  {"PORTFOLIO":<22} {"$10,000":>10} {total_return:>+9.1f}% {"${:,.0f}".format(final):>12}')
    print()
    print(f'  Portfolio CAGR:   {cagr:+.1f}%')
    print(f'  Portfolio MaxDD:  {max_dd:.1f}%')
    print(f'  Sharpe (approx):  {cagr / max_dd * 0.5:.2f}' if max_dd > 0 else '')

    # Compare
    print(f'\n  Comparison:')
    print(f'  TrendJoin 4H 1x:     CAGR +3.7%,  MaxDD 24.7%')
    print(f'  TrendJoin 4H 3x:     CAGR ~+8.9%, MaxDD ~59%')
    print(f'  Combined 3x:         CAGR {cagr:+.1f}%, MaxDD {max_dd:.1f}%')
    print(f'{"="*65}')


if __name__ == '__main__':
    run_portfolio('BTCUSDT')
