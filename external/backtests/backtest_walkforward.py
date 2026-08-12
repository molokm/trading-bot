#!/usr/bin/env python3
"""Walk-forward backtest: no future data leakage. Each day only sees candles up to that day."""
import httpx, pandas as pd, numpy as np, time, pickle, os

IC = 10000
SYMS = ['BTC-USDT', 'ETH-USDT', 'BNB-USDT', 'SOL-USDT']
CACHE = '/tmp/okx_cache.pkl'

def fetch_all():
    if os.path.exists(CACHE):
        with open(CACHE, 'rb') as f: return pickle.load(f)
    raw = {}
    for sym in SYMS:
        ac, a = [], []
        for attempt in range(5):
            try:
                while len(ac) < 1100:
                    p = {'instId': sym, 'bar': '1D', 'limit': '300'}
                    if a: p['after'] = a
                    r = httpx.get('https://www.okx.com/api/v5/market/candles', params=p, timeout=30).json()
                    if r.get('code') != '0' or not r.get('data'): break
                    ac.extend(r['data']); a = r['data'][-1][0]
                    if len(r['data']) < 300: break
                    time.sleep(0.3)
                break
            except: time.sleep(2)
        df = pd.DataFrame(ac, columns=['ts','O','H','L','C','V','x1','x2','x3'])
        df['ts'] = pd.to_datetime(df['ts'].astype(int), unit='ms')
        df.set_index('ts', inplace=True)
        raw[sym] = df[['O','H','L','C','V']].astype(float).sort_index()
        time.sleep(0.3)
    with open(CACHE, 'wb') as f: pickle.dump(raw, f)
    return raw


def ema_val(data, period):
    """Compute EMA from array of closes."""
    k = 2 / (period + 1)
    val = data[0]
    for v in data[1:]:
        val = v * k + val * (1 - k)
    return val


def compute_indicators_at(closes, highs, lows, idx, rf, rm, ef, es):
    """Compute indicators using only data up to idx (inclusive)."""
    if idx < max(rm, es) + 14:
        return None

    c = closes[:idx+1]
    h = highs[:idx+1]
    lo = lows[:idx+1]
    n = len(c)

    # ROC
    roc_f = (c[-1] / c[-rf] - 1) * 100 if n > rf else None
    roc_s = (c[-1] / c[-rm] - 1) * 100 if n > rm else None

    # EMA
    ema_f = ema_val(c[-ef:], ef)
    ema_s = ema_val(c[-es:], es)

    # ATR (14-period)
    trs = []
    for i in range(1, min(15, n)):
        tr = max(
            h[-i] - lo[-i],
            abs(h[-i] - c[-(i+1)]),
            abs(lo[-i] - c[-(i+1)])
        )
        trs.append(tr)
    atr = sum(trs) / len(trs) if trs else 0

    # ADX (Wilder smoothing)
    period = 14
    if n >= period + 2:
        plus_dm_arr = []
        minus_dm_arr = []
        tr_arr = []
        for i in range(1, n):
            up = h[i] - h[i-1]
            dn = lo[i-1] - lo[i]
            plus_dm_arr.append(max(up, 0) if up > dn else 0)
            minus_dm_arr.append(max(dn, 0) if dn > up else 0)
            tr_arr.append(max(h[i] - lo[i],
                              abs(h[i] - c[i-1]),
                              abs(lo[i] - c[i-1])))
        atr_w = sum(tr_arr[:period])
        plus_dm_w = sum(plus_dm_arr[:period])
        minus_dm_w = sum(minus_dm_arr[:period])
        pdi_arr = []
        mdi_arr = []
        for i in range(period, len(tr_arr)):
            atr_w = atr_w - atr_w / period + tr_arr[i]
            plus_dm_w = plus_dm_w - plus_dm_w / period + plus_dm_arr[i]
            minus_dm_w = minus_dm_w - minus_dm_w / period + minus_dm_arr[i]
            pdi = 100 * plus_dm_w / atr_w if atr_w > 0 else 0
            mdi = 100 * minus_dm_w / atr_w if atr_w > 0 else 0
            pdi_arr.append(pdi)
            mdi_arr.append(mdi)
        dx_arr = []
        for p, m in zip(pdi_arr, mdi_arr):
            s = p + m
            dx_arr.append(abs(p - m) / s * 100 if s > 0 else 0)
        adx = sum(dx_arr[-period:]) / min(period, len(dx_arr)) if dx_arr else 0
        plus_di = pdi_arr[-1] if pdi_arr else 0
        minus_di = mdi_arr[-1] if mdi_arr else 0
    else:
        adx = 0; plus_di = 0; minus_di = 0

    return {
        'roc_f': roc_f, 'roc_s': roc_s,
        'ema_f': ema_f, 'ema_s': ema_s,
        'atr': atr, 'adx': adx,
        'pdi': plus_di, 'mdi': minus_di,
        'price': c[-1]
    }


def walk_forward(raw, rf, rm, ef, es, init_stop_mult, trail_pct, adxt, mm, mp, rp):
    """Walk-forward: at each candle, only use data up to that candle."""
    # Align all symbols by date
    btc_idx = raw['BTC-USDT'].index
    start_idx = max(rm, es) + 14  # need enough history for indicators

    eq = IC
    pos = {s: None for s in SYMS}  # [entry, stop, peak, size, atr]
    cd = {s: 0 for s in SYMS}
    trades = []
    equity_curve = [IC]
    dates_used = []

    for i in range(start_idx, len(btc_idx)):
        date = btc_idx[i]

        for s in SYMS:
            df = raw[s]
            # Find this date in the symbol's data
            if date not in df.index:
                continue
            j = list(df.index).index(date)
            if j < 1:
                continue

            closes = df['C'].values
            highs = df['H'].values
            lows = df['L'].values
            opens = df['O'].values

            ps = pos[s]

            # --- MANAGE OPEN POSITION (skip entry day — can't exit same day on daily bars) ---
            if ps is not None and ps[5] is False:  # [entry, stop, peak, size, atr, entry_day_passed]
                entry, stop, peak, size, atr_val = ps[:5]
                cur_high = highs[j]
                cur_low = lows[j]

                # Update peak
                if cur_high > peak:
                    peak = cur_high
                    new_stop = peak * (1 - trail_pct)
                    if new_stop > stop:
                        stop = new_stop

                # Check stop hit
                if cur_low <= stop:
                    pnl = size * (stop - entry) - (size * entry + size * stop) * 0.001
                    eq += pnl
                    trades.append({
                        'date': str(date.date()),
                        'symbol': s,
                        'entry': entry,
                        'exit': stop,
                        'pnl': pnl,
                        'pnl_pct': (stop/entry - 1) * 100,
                        'peak': peak,
                        'peak_pct': (peak/entry - 1) * 100,
                    })
                    if pnl < 0:
                        cd[s] = 2
                    pos[s] = None
                else:
                    pos[s] = [entry, stop, peak, size, atr_val, False]

            # Mark entry day as passed
            elif ps is not None and ps[5] is True:
                pos[s][5] = False  # now manageable from next bar

            # --- CHECK ENTRY ---
            # Signal: use closes[j-1] (yesterday's close) to avoid look-ahead
            # Entry: opens[j] (today's open) — realistic: signal at prev close, enter next day
            if pos[s] is None:
                if sum(1 for v in pos.values() if v) >= mp:
                    continue
                if cd[s] > 0:
                    cd[s] -= 1
                    continue

                if j < 1:
                    continue
                ind = compute_indicators_at(closes, highs, lows, j - 1, rf, rm, ef, es)
                if ind is None:
                    continue

                if (ind['roc_f'] is not None and ind['roc_s'] is not None
                    and ind['roc_f'] > 0 and ind['roc_s'] > 0
                    and (ind['roc_f'] * 0.5 + ind['roc_s'] * 0.5) > mm
                    and ind['ema_f'] > ind['ema_s']
                    and ind['adx'] > adxt
                    and ind['pdi'] > ind['mdi']):

                    ep = opens[j]  # enter at open of today (signal was prev close)
                    st = ep - init_stop_mult * ind['atr']
                    rk = ep - st
                    if rk <= 0:
                        continue
                    sh = (eq * rp) / rk
                    mx = (eq * 0.3) / ep
                    sh = min(sh, mx)
                    if sh <= 0:
                        continue

                    pos[s] = [ep, st, ep, sh, ind['atr'], True]  # True = entry_day, skip management

        equity_curve.append(eq + sum(
            ps[3] * (ps[2] - ps[0]) for ps in pos.values() if ps
        ))
        dates_used.append(date)

    # Close any remaining positions at last close
    for s in SYMS:
        ps = pos[s]
        if ps is not None:
            last_close = raw[s]['C'].values[-1]
            pnl = ps[3] * (last_close - ps[0]) - (ps[3] * ps[0] + ps[3] * last_close) * 0.001
            eq += pnl
            trades.append({
                'date': 'OPEN',
                'symbol': s,
                'entry': ps[0],
                'exit': last_close,
                'pnl': pnl,
                'pnl_pct': (last_close/ps[0] - 1) * 100,
                'peak': ps[2],
                'peak_pct': (ps[2]/ps[0] - 1) * 100,
            })

    if not trades:
        return None

    nt = len(trades)
    w = len([t for t in trades if t['pnl'] > 0])
    total_pnl = sum(t['pnl'] for t in trades)
    yrs = (btc_idx[-1] - btc_idx[start_idx]) / np.timedelta64(365, 'D')
    cagr = ((eq / IC) ** (1 / yrs) - 1) * 100 if yrs > 0 else 0

    # Max DD
    peak_eq = IC
    max_dd = 0
    for v in equity_curve:
        if v > peak_eq:
            peak_eq = v
        dd = (peak_eq - v) / peak_eq * 100
        if dd > max_dd:
            max_dd = dd

    avg_pnl_pct = np.mean([t['pnl_pct'] for t in trades])
    avg_hold = np.mean([t['peak_pct'] for t in trades])  # avg peak reached

    return {
        'cagr': cagr, 'trades': nt, 'tpy': nt / yrs if yrs > 0 else 0,
        'win': w / nt * 100, 'pnl': total_pnl, 'final': eq,
        'max_dd': max_dd, 'avg_pnl_pct': avg_pnl_pct,
        'trade_log': trades, 'equity_curve': equity_curve,
    }


if __name__ == '__main__':
    print("Fetching data..."); raw = fetch_all()
    btc_idx = raw['BTC-USDT'].index
    print(f"Period: {btc_idx[0].date()} to {btc_idx[-1].date()} ({len(btc_idx)} days)")

    rf, rm, ef, es = 5, 50, 15, 30
    adxt, mm, mp, rp = 20, 0, 4, 0.03

    configs = [
        ("OLD: ATR TP x3.0", None, None),  # special handling
        ("Trail 3% init=1.5", 1.5, 0.03),
        ("Trail 5% init=1.5", 1.5, 0.05),
        ("Trail 6% init=1.0", 1.0, 0.06),
        ("Trail 8% init=1.0", 1.0, 0.08),
        ("Trail 10% init=1.0", 1.0, 0.10),
    ]

    print(f"\n{'='*90}")
    print(f"  WALK-FORWARD BACKTEST (no future data)")
    print(f"  Period: {btc_idx[0].date()} → {btc_idx[-1].date()}")
    print(f"  Starting capital: ${IC:,}")
    print(f"{'='*90}\n")

    all_results = []

    for name, init_st, trail in configs:
        if name.startswith("OLD"):
            # Old ATR-based: use walk_forward with ATR TP
            # We'll simulate it as trail = very wide (effectively no trailing, use ATR TP)
            # Actually let's just implement it separately
            continue

        r = walk_forward(raw, rf, rm, ef, es, init_st, trail, adxt, mm, mp, rp)
        if r:
            all_results.append((name, r))
            print(f"  {name}:")
            print(f"    CAGR={r['cagr']:+6.1f}%  Trades={r['trades']}  TPY={r['tpy']:.0f}  "
                  f"Win={r['win']:.1f}%  PnL=${r['pnl']:+,.0f}  Final=${r['final']:,.0f}  "
                  f"MaxDD={r['max_dd']:.1f}%  AvgPnL={r['avg_pnl_pct']:.2f}%")

    # Detailed trade log for best config
    if all_results:
        all_results.sort(key=lambda x: x[1]['cagr'], reverse=True)
        best_name, best = all_results[0]
        print(f"\n{'='*90}")
        print(f"  TRADE LOG: {best_name}")
        print(f"{'='*90}")
        print(f"  {'Date':>12} {'Symbol':>8} {'Entry':>10} {'Exit':>10} {'Peak':>10} {'PnL%':>7} {'PnL$':>10}")
        print(f"  {'-'*70}")
        for t in best['trade_log']:
            flag = "+" if t['pnl'] > 0 else " "
            print(f"  {t['date']:>12} {t['symbol']:>8} {t['entry']:>10.2f} {t['exit']:>10.2f} "
                  f"{t['peak']:>10.2f} {flag}{t['pnl_pct']:6.2f}% {flag}${abs(t['pnl']):>8,.0f}")

        # Per-symbol breakdown
        print(f"\n  PER-SYMBOL BREAKDOWN:")
        for s in SYMS:
            st = [t for t in best['trade_log'] if t['symbol'] == s]
            if st:
                sw = len([t for t in st if t['pnl'] > 0])
                sp = sum(t['pnl'] for t in st)
                print(f"    {s}: {len(st)} trades, {sw} wins ({sw/len(st)*100:.0f}%), PnL=${sp:+,.0f}")

        # Monthly returns
        print(f"\n  EQUITY CURVE (last 12 months):")
        ec = best['equity_curve']
        step = len(ec) // 12
        for i in range(max(0, len(ec)-step*12), len(ec), step):
            v = ec[i]
            day_idx = i
            if day_idx < len(btc_idx):
                dt = btc_idx[min(day_idx, len(btc_idx)-1)]
                ret = (v / IC - 1) * 100
                print(f"    {dt.strftime('%Y-%m')}: ${v:>10,.0f} ({ret:+.1f}%)")
