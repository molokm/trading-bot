#!/usr/bin/env python3
"""Walk-forward backtest: LONG + SHORT both directions."""
import httpx, pandas as pd, numpy as np, time, pickle, os

IC = 10000
SYMS = ['BTC-USDT', 'ETH-USDT', 'BNB-USDT', 'SOL-USDT']
CACHE = '/tmp/okx_cache.pkl'


def fetch_all():
    if os.path.exists(CACHE):
        with open(CACHE, 'rb') as f:
            return pickle.load(f)
    raw = {}
    for sym in SYMS:
        ac, a = [], []
        for attempt in range(5):
            try:
                while len(ac) < 1100:
                    p = {'instId': sym, 'bar': '1D', 'limit': '300'}
                    if a:
                        p['after'] = a
                    r = httpx.get('https://www.okx.com/api/v5/market/candles', params=p, timeout=30).json()
                    if r.get('code') != '0' or not r.get('data'):
                        break
                    ac.extend(r['data'])
                    a = r['data'][-1][0]
                    if len(r['data']) < 300:
                        break
                    time.sleep(0.3)
                break
            except:
                time.sleep(2)
        df = pd.DataFrame(ac, columns=['ts','O','H','L','C','V','x1','x2','x3'])
        df['ts'] = pd.to_datetime(df['ts'].astype(int), unit='ms')
        df.set_index('ts', inplace=True)
        raw[sym] = df[['O','H','L','C','V']].astype(float).sort_index()
        time.sleep(0.3)
    with open(CACHE, 'wb') as f:
        pickle.dump(raw, f)
    return raw


def ema_val(data, period):
    k = 2 / (period + 1)
    val = data[0]
    for v in data[1:]:
        val = v * k + val * (1 - k)
    return val


def compute_indicators_at(closes, highs, lows, idx, rf, rm, ef, es):
    if idx < max(rm, es) + 14:
        return None

    c = closes[:idx+1]
    h = highs[:idx+1]
    lo = lows[:idx+1]
    n = len(c)

    roc_f = (c[-1] / c[-rf] - 1) * 100 if n > rf else None
    roc_s = (c[-1] / c[-rm] - 1) * 100 if n > rm else None

    ema_f = ema_val(c[-ef:], ef)
    ema_s = ema_val(c[-es:], es)

    trs = []
    for i in range(1, min(15, n)):
        tr = max(h[-i] - lo[-i], abs(h[-i] - c[-(i+1)]), abs(lo[-i] - c[-(i+1)]))
        trs.append(tr)
    atr = sum(trs) / len(trs) if trs else 0

    period = 14
    if n >= period + 2:
        plus_dm_arr, minus_dm_arr, tr_arr = [], [], []
        for i in range(1, n):
            up = h[i] - h[i-1]
            dn = lo[i-1] - lo[i]
            plus_dm_arr.append(max(up, 0) if up > dn else 0)
            minus_dm_arr.append(max(dn, 0) if dn > up else 0)
            tr_arr.append(max(h[i] - lo[i], abs(h[i] - c[i-1]), abs(lo[i] - c[i-1])))
        atr_w = sum(tr_arr[:period])
        plus_dm_w = sum(plus_dm_arr[:period])
        minus_dm_w = sum(minus_dm_arr[:period])
        pdi_arr, mdi_arr = [], []
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


def walk_forward_bilateral(raw, rf, rm, ef, es, init_stop_mult, trail_pct, adxt, mm, mp, rp):
    """Walk-forward with LONG + SHORT positions."""
    btc_idx = raw['BTC-USDT'].index
    start_idx = max(rm, es) + 14

    eq = IC
    # pos[s] = [direction, entry, stop, peak/trough, size, atr, entry_day_passed]
    # direction: 1 = long, -1 = short
    pos = {s: None for s in SYMS}
    cd = {s: 0 for s in SYMS}
    trades = []
    equity_curve = [IC]
    dates_used = []

    for i in range(start_idx, len(btc_idx)):
        date = btc_idx[i]

        for s in SYMS:
            df = raw[s]
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

            # --- MANAGE OPEN POSITION ---
            if ps is not None and ps[6] is False:
                direction, entry, stop, extrema, size, atr_val = ps[:6]
                cur_high = highs[j]
                cur_low = lows[j]

                if direction == 1:  # LONG
                    # Update peak
                    if cur_high > extrema:
                        extrema = cur_high
                        new_stop = extrema * (1 - trail_pct)
                        if new_stop > stop:
                            stop = new_stop
                    # Check stop hit
                    if cur_low <= stop:
                        exit_px = stop
                        pnl = size * (exit_px - entry) - (size * entry + size * exit_px) * 0.001
                        eq += pnl
                        trades.append({
                            'date': str(date.date()), 'symbol': s, 'dir': 'LONG',
                            'entry': entry, 'exit': exit_px, 'pnl': pnl,
                            'pnl_pct': (exit_px / entry - 1) * 100,
                            'extrema': extrema, 'extrema_pct': (extrema / entry - 1) * 100,
                        })
                        if pnl < 0:
                            cd[s] = 2
                        pos[s] = None
                    else:
                        pos[s] = [direction, entry, stop, extrema, size, atr_val, False]

                else:  # SHORT
                    # Update trough (lowest price since entry)
                    if cur_low < extrema:
                        extrema = cur_low
                        new_stop = extrema * (1 + trail_pct)
                        if new_stop < stop:
                            stop = new_stop
                    # Check stop hit (price rises above stop)
                    if cur_high >= stop:
                        exit_px = stop
                        pnl = size * (entry - exit_px) - (size * entry + size * exit_px) * 0.001
                        eq += pnl
                        trades.append({
                            'date': str(date.date()), 'symbol': s, 'dir': 'SHORT',
                            'entry': entry, 'exit': exit_px, 'pnl': pnl,
                            'pnl_pct': (entry / exit_px - 1) * 100,
                            'extrema': extrema, 'extrema_pct': (entry / extrema - 1) * 100,
                        })
                        if pnl < 0:
                            cd[s] = 2
                        pos[s] = None
                    else:
                        pos[s] = [direction, entry, stop, extrema, size, atr_val, False]

            elif ps is not None and ps[6] is True:
                pos[s][6] = False

            # --- CHECK ENTRY ---
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
                if ind['roc_f'] is None or ind['roc_s'] is None or ind['atr'] <= 0:
                    continue

                ep = opens[j]

                # --- LONG SIGNAL ---
                if (ind['roc_f'] > 0 and ind['roc_s'] > 0
                    and (ind['roc_f'] * 0.5 + ind['roc_s'] * 0.5) > mm
                    and ind['ema_f'] > ind['ema_s']
                    and ind['adx'] > adxt
                    and ind['pdi'] > ind['mdi']):

                    st = ep - init_stop_mult * ind['atr']
                    risk = ep - st
                    if risk <= 0:
                        continue
                    sh = (eq * rp) / risk
                    mx = (eq * 0.3) / ep
                    sh = min(sh, mx)
                    if sh <= 0:
                        continue

                    pos[s] = [1, ep, st, ep, sh, ind['atr'], True]

                # --- SHORT SIGNAL ---
                elif (ind['roc_f'] < 0 and ind['roc_s'] < 0
                      and (ind['roc_f'] * 0.5 + ind['roc_s'] * 0.5) < -mm
                      and ind['ema_f'] < ind['ema_s']
                      and ind['adx'] > adxt
                      and ind['mdi'] > ind['pdi']):

                    st = ep + init_stop_mult * ind['atr']
                    risk = st - ep
                    if risk <= 0:
                        continue
                    sh = (eq * rp) / risk
                    mx = (eq * 0.3) / ep
                    sh = min(sh, mx)
                    if sh <= 0:
                        continue

                    pos[s] = [-1, ep, st, ep, sh, ind['atr'], True]

        # Mark-to-market equity
        unrealized = 0
        for s, ps in pos.items():
            if ps is None:
                continue
            direction, entry, stop, extrema, size, atr_val = ps[:6]
            cur_c = raw[s]['C'].values[j]
            if direction == 1:
                unrealized += size * (cur_c - entry)
            else:
                unrealized += size * (entry - cur_c)
        equity_curve.append(eq + unrealized)
        dates_used.append(date)

    # Close remaining positions
    for s in SYMS:
        ps = pos[s]
        if ps is not None:
            direction, entry, stop, extrema, size, atr_val = ps[:6]
            last_close = raw[s]['C'].values[-1]
            if direction == 1:
                pnl = size * (last_close - entry) - (size * entry + size * last_close) * 0.001
            else:
                pnl = size * (entry - last_close) - (size * entry + size * last_close) * 0.001
            eq += pnl
            d = 'LONG' if direction == 1 else 'SHORT'
            trades.append({
                'date': 'OPEN', 'symbol': s, 'dir': d,
                'entry': entry, 'exit': last_close, 'pnl': pnl,
                'pnl_pct': ((last_close / entry - 1) * direction) * 100,
                'extrema': extrema,
                'extrema_pct': ((extrema / entry - 1) * direction) * 100,
            })

    if not trades:
        return None

    nt = len(trades)
    w = len([t for t in trades if t['pnl'] > 0])
    total_pnl = sum(t['pnl'] for t in trades)
    yrs = (btc_idx[-1] - btc_idx[start_idx]) / np.timedelta64(365, 'D')
    cagr = ((eq / IC) ** (1 / yrs) - 1) * 100 if yrs > 0 else 0

    peak_eq = IC
    max_dd = 0
    for v in equity_curve:
        if v > peak_eq:
            peak_eq = v
        dd = (peak_eq - v) / peak_eq * 100
        if dd > max_dd:
            max_dd = dd

    avg_pnl_pct = np.mean([t['pnl_pct'] for t in trades])

    # Long vs Short breakdown
    longs = [t for t in trades if t['dir'] == 'LONG']
    shorts = [t for t in trades if t['dir'] == 'SHORT']
    long_wins = len([t for t in longs if t['pnl'] > 0])
    short_wins = len([t for t in shorts if t['pnl'] > 0])
    long_pnl = sum(t['pnl'] for t in longs)
    short_pnl = sum(t['pnl'] for t in shorts)

    return {
        'cagr': cagr, 'trades': nt, 'tpy': nt / yrs if yrs > 0 else 0,
        'win': w / nt * 100, 'pnl': total_pnl, 'final': eq,
        'max_dd': max_dd, 'avg_pnl_pct': avg_pnl_pct,
        'trade_log': trades, 'equity_curve': equity_curve,
        'longs': len(longs), 'long_wins': long_wins,
        'long_pnl': long_pnl,
        'shorts': len(shorts), 'short_wins': short_wins,
        'short_pnl': short_pnl,
    }


if __name__ == '__main__':
    print("Fetching data...")
    raw = fetch_all()
    btc_idx = raw['BTC-USDT'].index
    print(f"Period: {btc_idx[0].date()} to {btc_idx[-1].date()} ({len(btc_idx)} days)")

    rf, rm, ef, es = 5, 50, 15, 30
    adxt, mm, mp, rp = 20, 0, 4, 0.03

    configs = [
        ("Long+Short Trail 3% init=1.5", 1.5, 0.03),
        ("Long+Short Trail 5% init=1.5", 1.5, 0.05),
        ("Long+Short Trail 6% init=1.0", 1.0, 0.06),
        ("Long+Short Trail 8% init=1.0", 1.0, 0.08),
        ("Long-only Trail 3% init=1.5 (ref)", 1.5, 0.03),
    ]

    print(f"\n{'='*100}")
    print(f"  WALK-FORWARD BACKTEST: LONG + SHORT (no future data)")
    print(f"  Period: {btc_idx[0].date()} → {btc_idx[-1].date()}")
    print(f"  Starting capital: ${IC:,}")
    print(f"{'='*100}\n")

    all_results = []

    for idx, (name, init_st, trail) in enumerate(configs):
        if "(ref)" in name:
            # Long-only reference: just filter shorts out by setting extreme ADX for short
            # Actually easier: just run bilateral and then filter
            r = walk_forward_bilateral(raw, rf, rm, ef, es, init_st, trail, adxt, mm, mp, rp)
            # For long-only ref, filter out shorts
            if r:
                long_only_trades = [t for t in r['trade_log'] if t['dir'] == 'LONG']
                # Recalc with longs only
                lo_eq = IC
                for t in long_only_trades:
                    if t['date'] == 'OPEN':
                        lo_eq += t['pnl']
                lo_yrs = (btc_idx[-1] - btc_idx[max(rm, es) + 14]) / np.timedelta64(365, 'D')
                lo_cagr = ((lo_eq / IC) ** (1 / lo_yrs) - 1) * 100 if lo_yrs > 0 else 0
                lo_w = len([t for t in long_only_trades if t['pnl'] > 0])
                print(f"  {name}:")
                print(f"    CAGR={lo_cagr:+6.1f}%  Trades={len(long_only_trades)}  "
                      f"Win={lo_w/len(long_only_trades)*100:.1f}%  PnL=${sum(t['pnl'] for t in long_only_trades):+,.0f}  "
                      f"Final=${lo_eq:,.0f}")
                all_results.append((name, {**r, 'trades': len(long_only_trades), 'cagr': lo_cagr,
                                           'pnl': sum(t['pnl'] for t in long_only_trades),
                                           'final': lo_eq}))
        else:
            r = walk_forward_bilateral(raw, rf, rm, ef, es, init_st, trail, adxt, mm, mp, rp)
            if r:
                all_results.append((name, r))
                print(f"  {name}:")
                print(f"    CAGR={r['cagr']:+6.1f}%  Trades={r['trades']}  TPY={r['tpy']:.0f}  "
                      f"Win={r['win']:.1f}%  PnL=${r['pnl']:+,.0f}  Final=${r['final']:,.0f}  "
                      f"MaxDD={r['max_dd']:.1f}%")
                print(f"    Long: {r['longs']} trades, {r['long_wins']} wins, PnL=${r['long_pnl']:+,.0f}")
                print(f"    Short: {r['shorts']} trades, {r['short_wins']} wins, PnL=${r['short_pnl']:+,.0f}")

    # Best result detailed
    if all_results:
        all_results.sort(key=lambda x: x[1]['cagr'], reverse=True)
        best_name, best = all_results[0]
        print(f"\n{'='*100}")
        print(f"  TRADE LOG: {best_name}")
        print(f"{'='*100}")
        print(f"  {'Date':>12} {'Dir':>5} {'Symbol':>8} {'Entry':>10} {'Exit':>10} {'Peak/Trough':>12} {'PnL%':>7} {'PnL$':>10}")
        print(f"  {'-'*80}")
        for t in best['trade_log']:
            flag = "+" if t['pnl'] > 0 else " "
            print(f"  {t['date']:>12} {t['dir']:>5} {t['symbol']:>8} {t['entry']:>10.2f} {t['exit']:>10.2f} "
                  f"{t['extrema']:>12.2f} {flag}{t['pnl_pct']:6.2f}% {flag}${abs(t['pnl']):>8,.0f}")

        # Per-symbol
        print(f"\n  PER-SYMBOL BREAKDOWN:")
        for s in SYMS:
            st = [t for t in best['trade_log'] if t['symbol'] == s]
            if st:
                sw = len([t for t in st if t['pnl'] > 0])
                sp = sum(t['pnl'] for t in st)
                sl = [t for t in st if t['dir'] == 'LONG']
                ss = [t for t in st if t['dir'] == 'SHORT']
                print(f"    {s}: {len(st)} trades ({len(sl)}L/{len(ss)}S), {sw} wins ({sw/len(st)*100:.0f}%), PnL=${sp:+,.0f}")

        # Equity milestones
        print(f"\n  EQUITY MILESTONES:")
        ec = best['equity_curve']
        n = len(ec)
        milestones = [0, n//12, n//6, n//4, n//3, n//2, int(n*0.75), int(n*0.9), n-1]
        for mi in milestones:
            if mi < n:
                d = btc_idx[min(mi + max(rm, es) + 14, len(btc_idx)-1)]
                print(f"    {d.date()}: ${ec[mi]:,.0f}")
