#!/usr/bin/env python3
"""Backtest momentum with pure trailing stop (no fixed TP)."""
import httpx, pandas as pd, numpy as np, itertools, time, pickle, os

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

def precomp(raw):
    out = {}
    for sym, df in raw.items():
        c = df['C'].values; h = df['H'].values; lo = df['L'].values
        d = {'C': c, 'H': h, 'L': lo, 'O': df['O'].values, 'V': df['V'].values}
        for p in [3, 5, 7, 10, 14, 20, 30, 50]:
            roc = np.full_like(c, np.nan); roc[p:] = c[p:] / c[:-p] - 1; d[f'R{p}'] = roc * 100
        for p in [12, 15, 21, 30, 40, 55]:
            d[f'E{p}'] = df['C'].ewm(span=p, adjust=False).mean().values
        tr = np.maximum(h - lo, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(lo - np.roll(c, 1)))); tr[0] = h[0] - lo[0]
        av = pd.Series(tr).ewm(span=14, adjust=False).mean().values
        d['A'] = av
        up = h - np.roll(h, 1); up[0] = 0; dn = np.roll(lo, 1) - lo; dn[0] = 0
        pdm = np.where((up > dn) & (up > 0), up, 0)
        mdm = np.where((dn > up) & (dn > 0), dn, 0)
        pdi = 100 * pd.Series(pdm).ewm(span=14, adjust=False).mean().values / av
        mdi = 100 * pd.Series(mdm).ewm(span=14, adjust=False).mean().values / av
        d['PDI'] = pdi; d['MDI'] = mdi
        dx = 100 * np.abs(pdi - mdi) / np.where((pdi + mdi) == 0, np.nan, pdi + mdi)
        d['ADX'] = pd.Series(dx).ewm(span=14, adjust=False).mean().values
        d['idx'] = df.index.values
        out[sym] = d
    return out


def bt_trailing(pc, rf, rm, ef, es, init_stop_mult, trail_pct, adxt, mm, mp, rp):
    """Pure trailing stop: initial stop at -init_stop_mult*ATR, then trail at trail_pct below peak."""
    eq = IC; pos = {s: None for s in SYMS}; cd = {s: 0 for s in SYMS}
    trades = []; btc_dates = pc['BTC-USDT']['idx']
    n = len(btc_dates)
    for i in range(1, n):
        date = btc_dates[i]
        for s in SYMS:
            p = pc[s]
            j = np.searchsorted(p['idx'], date)
            if j >= len(p['idx']) or p['idx'][j] != date: continue
            if j < 1: continue
            ps = pos[s]
            if ps:
                # ps = [entry, stop, high_since_entry, size, atr, trail_pct]
                peak = ps[2]
                cur_high = p['H'][j]
                if cur_high > peak:
                    peak = cur_high
                    ps[2] = peak
                    # Trail stop: peak * (1 - trail_pct)
                    new_stop = peak * (1 - trail_pct)
                    if new_stop > ps[1]:
                        ps[1] = new_stop

                if p['L'][j] <= ps[1]:
                    pnl = ps[3] * (ps[1] - ps[0]) - (ps[3] * ps[0] + ps[3] * ps[1]) * .001
                    eq += pnl; trades.append(pnl)
                    if pnl < 0: cd[s] = 2
                    pos[s] = None
            if pos[s] is None:
                if sum(1 for v in pos.values() if v) >= mp: continue
                if cd[s] > 0: cd[s] -= 1; continue
                rf_v = p[f'R{rf}'][j]; rm_v = p[f'R{rm}'][j]
                ef_v = p[f'E{ef}'][j]; es_v = p[f'E{es}'][j]
                ad = p['ADX'][j]; pdi_v = p['PDI'][j]; mdi_v = p['MDI'][j]
                at = p['A'][j]
                if np.isnan(at) or at <= 0 or np.isnan(ef_v) or np.isnan(es_v): continue
                if rf_v > 0 and rm_v > 0 and (rf_v * .5 + rm_v * .5) > mm and ef_v > es_v and ad > adxt and pdi_v > mdi_v:
                    ep = p['O'][j]
                    st = ep - init_stop_mult * at
                    rk = ep - st
                    if rk <= 0: continue
                    sh = (eq * rp) / rk; mx = (eq * .3) / ep; sh = min(sh, mx)
                    if sh <= 0: continue
                    pos[s] = [ep, st, ep, sh, at, trail_pct]  # [entry, stop, peak, size, atr, trail%]
    if not trades: return None
    nt = len(trades); w = len([t for t in trades if t > 0])
    yrs = (btc_dates[-1] - btc_dates[0]) / np.timedelta64(365, 'D')
    cagr = ((eq / IC) ** (1 / yrs) - 1) * 100
    
    # Compute avg hold and avg PnL
    avg_pnl = sum(trades) / len(trades) if trades else 0
    max_dd = 0; peak_eq = IC
    running_eq = IC
    for t in trades:
        running_eq += t
        if running_eq > peak_eq: peak_eq = running_eq
        dd = (peak_eq - running_eq) / peak_eq * 100
        if dd > max_dd: max_dd = dd
    
    return (cagr, nt, w / nt * 100, sum(trades), eq, nt / yrs, max_dd, avg_pnl)


if __name__ == '__main__':
    print("Fetching data..."); raw = fetch_all()
    print("Precomputing indicators..."); pc = precomp(raw)

    rf, rm, ef, es = 5, 50, 15, 30
    adxt, mm, mp, rp = 20, 0, 4, 0.03

    print(f"\n{'='*80}")
    print(f"  TRAILING STOP BACKTEST — no fixed TP, exit via trailing")
    print(f"  Config: ROC={rf}/{rm} EMA={ef}/{es} ADX>{adxt} Risk={rp} MaxPos={mp}")
    print(f"{'='*80}")

    # Sweep: initial stop multiplier × trail percentage
    init_stops = [1.0, 1.5, 2.0, 2.5, 3.0]
    trail_pcts = [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]

    results = []
    total = 0
    t0 = time.time()
    for init_st in init_stops:
        for trail in trail_pcts:
            total += 1
            r = bt_trailing(pc, rf, rm, ef, es, init_st, trail, adxt, mm, mp, rp)
            if r and r[1] >= 15:
                results.append((r, (init_st, trail)))

    print(f"\n  {total} configs tested in {time.time()-t0:.1f}s, {len(results)} valid\n")

    results.sort(key=lambda x: x[0][0], reverse=True)

    print(f"  {'#':>2} {'Init':>5} {'Trail':>6} {'CAGR':>7} {'#Tr':>5} {'TPY':>5} {'Win%':>6} {'MaxDD':>6} {'PnL':>10} {'Final':>12}")
    print(f"  {'-'*78}")
    for i, (r, params) in enumerate(results[:25]):
        cagr, nt, wr, pnl, final, tpy, mdd, avg = r
        print(f"  {i+1:3d} {params[0]:5.1f} {params[1]*100:5.1f}% {cagr:+6.1f}% {nt:5d} {tpy:5.1f} {wr:5.1f}% {mdd:5.1f}% ${pnl:+9,.0f} ${final:11,.0f}")

    # Compare with old ATR TP
    print(f"\n{'='*80}")
    print(f"  vs OLD ATR TP x3.0:")
    print(f"{'='*80}")

    from backtest_tp_compare import bt_atr_tp
    r_old = bt_atr_tp(pc, rf, rm, ef, es, 1.5, 3.0, adxt, mm, mp, rp)
    if r_old:
        cagr, nt, wr, pnl, final, tpy = r_old
        print(f"  [OLD] ATR TP x3.0:  CAGR={cagr:+6.1f}%  Trades={nt}  TPY={tpy:.1f}  Win={wr:.1f}%  PnL=${pnl:+,.0f}")

    # Best trailing
    if results:
        r, params = results[0]
        cagr, nt, wr, pnl, final, tpy, mdd, avg = r
        print(f"  [NEW] Trail {params[1]*100:.1f}% (init {params[0]}x ATR):  CAGR={cagr:+6.1f}%  Trades={nt}  TPY={tpy:.1f}  Win={wr:.1f}%  PnL=${pnl:+,.0f}")

    # Best with <100 trades/yr (more realistic)
    moderate = [(r, p) for r, p in results if r[5] < 100]
    if moderate:
        r, params = moderate[0]
        cagr, nt, wr, pnl, final, tpy, mdd, avg = r
        print(f"  [MOD] Trail {params[1]*100:.1f}% (init {params[0]}x ATR):  CAGR={cagr:+6.1f}%  Trades={nt}  TPY={tpy:.1f}  Win={wr:.1f}%  PnL=${pnl:+,.0f}  MaxDD={mdd:.1f}%")

    # Best with max DD < 20%
    low_dd = [(r, p) for r, p in results if r[6] < 20]
    if low_dd:
        print(f"\n  TOP 10 with Max DD < 20%:")
        print(f"  {'#':>2} {'Init':>5} {'Trail':>6} {'CAGR':>7} {'#Tr':>5} {'TPY':>5} {'Win%':>6} {'MaxDD':>6} {'PnL':>10}")
        print(f"  {'-'*62}")
        for i, (r, params) in enumerate(low_dd[:10]):
            cagr, nt, wr, pnl, final, tpy, mdd, avg = r
            print(f"  {i+1:3d} {params[0]:5.1f} {params[1]*100:5.1f}% {cagr:+6.1f}% {nt:5d} {tpy:5.1f} {wr:5.1f}% {mdd:5.1f}% ${pnl:+9,.0f}")
