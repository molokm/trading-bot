#!/usr/bin/env python3
"""Backtest momentum with fixed % TP (0.85%) vs old ATR-based TP."""
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
        print(f"  {sym}: {len(raw[sym])}")
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

# Fixed % TP version
def bt_fixed_tp(pc, rf, rm, ef, es, ast, tp_pct, adxt, mm, mp, rp):
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
                rd = ps[0] - ps[1]  # entry - stop
                if rd > 0:
                    pk = max(ps[4], (p['H'][j] - ps[0]) / rd)
                    ps[4] = pk
                    if pk >= 1: ps[1] = max(ps[1], ps[0])  # breakeven
                    if pk >= 2: ps[1] = max(ps[1], ps[0] + pk * rd * 0.5)  # 50% trail
                    if pk >= 3: ps[1] = max(ps[1], p['C'][j] - ps[5] * 1.5)  # ATR trail
                if p['L'][j] <= ps[1]:
                    pnl = ps[3] * (ps[1] - ps[0]) - (ps[3] * ps[0] + ps[3] * ps[1]) * .001
                    eq += pnl; trades.append(pnl)
                    if pnl < 0: cd[s] = 2
                    pos[s] = None
                elif p['H'][j] >= ps[2]:
                    pnl = ps[3] * (ps[2] - ps[0]) - (ps[3] * ps[0] + ps[3] * ps[2]) * .001
                    eq += pnl; trades.append(pnl); pos[s] = None
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
                    st = ep - ast * at
                    tg = ep * (1 + tp_pct)  # Fixed % TP
                    rk = ep - st
                    if rk <= 0: continue
                    sh = (eq * rp) / rk; mx = (eq * .3) / ep; sh = min(sh, mx)
                    if sh <= 0: continue
                    pos[s] = [ep, st, tg, sh, 0, at]
    if not trades: return None
    nt = len(trades); w = len([t for t in trades if t > 0])
    yrs = (btc_dates[-1] - btc_dates[0]) / np.timedelta64(365, 'D')
    cagr = ((eq / IC) ** (1 / yrs) - 1) * 100
    return (cagr, nt, w / nt * 100, sum(trades), eq, nt / yrs)

# Old ATR-based TP version (for comparison)
def bt_atr_tp(pc, rf, rm, ef, es, ast, atg, adxt, mm, mp, rp):
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
                rd = ps[0] - ps[1]
                if rd > 0:
                    pk = max(ps[4], (p['H'][j] - ps[0]) / rd)
                    ps[4] = pk
                    if pk >= 1: ps[1] = max(ps[1], ps[0])
                    if pk >= 2: ps[1] = max(ps[1], ps[0] + pk * rd * .5)
                    if pk >= 3: ps[1] = max(ps[1], p['C'][j] - ps[5] * 1.5)
                if p['L'][j] <= ps[1]:
                    pnl = ps[3] * (ps[1] - ps[0]) - (ps[3] * ps[0] + ps[3] * ps[1]) * .001
                    eq += pnl; trades.append(pnl)
                    if pnl < 0: cd[s] = 2
                    pos[s] = None
                elif p['H'][j] >= ps[2]:
                    pnl = ps[3] * (ps[2] - ps[0]) - (ps[3] * ps[0] + ps[3] * ps[2]) * .001
                    eq += pnl; trades.append(pnl); pos[s] = None
            if pos[s] is None:
                if sum(1 for v in pos.values() if v) >= mp: continue
                if cd[s] > 0: cd[s] -= 1; continue
                rf_v = p[f'R{rf}'][j]; rm_v = p[f'R{rm}'][j]
                ef_v = p[f'E{ef}'][j]; es_v = p[f'E{es}'][j]
                ad = p['ADX'][j]; pdi_v = p['PDI'][j]; mdi_v = p['MDI'][j]
                at = p['A'][j]
                if np.isnan(at) or at <= 0 or np.isnan(ef_v) or np.isnan(es_v): continue
                if rf_v > 0 and rm_v > 0 and (rf_v * .5 + rm_v * .5) > mm and ef_v > es_v and ad > adxt and pdi_v > mdi_v:
                    ep = p['O'][j]; st = ep - ast * at; tg = ep + atg * at; rk = ep - st
                    if rk <= 0: continue
                    sh = (eq * rp) / rk; mx = (eq * .3) / ep; sh = min(sh, mx)
                    if sh <= 0: continue
                    pos[s] = [ep, st, tg, sh, 0, at]
    if not trades: return None
    nt = len(trades); w = len([t for t in trades if t > 0])
    yrs = (btc_dates[-1] - btc_dates[0]) / np.timedelta64(365, 'D')
    cagr = ((eq / IC) ** (1 / yrs) - 1) * 100
    return (cagr, nt, w / nt * 100, sum(trades), eq, nt / yrs)


if __name__ == '__main__':
    print("Fetching data..."); raw = fetch_all()
    print("Precomputing indicators..."); pc = precomp(raw)

    # Current config: roc_fast=5, roc_slow=50, ema_fast=15, ema_slow=30
    rf, rm, ef, es = 5, 50, 15, 30
    adxt, mm, mp, rp = 20, 0, 4, 0.03

    print(f"\n{'='*70}")
    print(f"  COMPARISON: Old ATR TP (3x) vs Fixed % TP (0.85%)")
    print(f"  Config: ROC={rf}/{rm} EMA={ef}/{es} ADX>{adxt} Risk={rp}")
    print(f"{'='*70}")

    # Old config: ATR-based TP (3x)
    print(f"\n  [OLD] ATR TP mult = 3.0 (ATR-based, variable):")
    for ast in [1.5, 2.0]:
        for atg in [3.0, 4.0]:
            r = bt_atr_tp(pc, rf, rm, ef, es, ast, atg, adxt, mm, mp, rp)
            if r:
                cagr, nt, wr, pnl, final, tpy = r
                tp_pct_est = atg * 120 / 1930 * 100  # rough ATR% estimate
                print(f"    ATR_stop={ast} ATR_tp={atg} (~{tp_pct_est:.0f}% TP): "
                      f"CAGR={cagr:+6.1f}%  #Trades={nt:4d}  TPY={tpy:4.1f}  "
                      f"Win={wr:5.1f}%  PnL=${pnl:+8,.0f}  Final=${final:10,.0f}")

    # New config: Fixed % TP
    print(f"\n  [NEW] Fixed % TP:")
    for tp_pct in [0.005, 0.007, 0.0085, 0.010, 0.012, 0.015]:
        for ast in [1.0, 1.5, 2.0]:
            r = bt_fixed_tp(pc, rf, rm, ef, es, ast, tp_pct, adxt, mm, mp, rp)
            if r:
                cagr, nt, wr, pnl, final, tpy = r
                print(f"    TP={tp_pct*100:.2f}%  ATR_stop={ast}: "
                      f"CAGR={cagr:+6.1f}%  #Trades={nt:4d}  TPY={tpy:4.1f}  "
                      f"Win={wr:5.1f}%  PnL=${pnl:+8,.0f}  Final=${final:10,.0f}")

    # Detailed comparison at the exact params we're running
    print(f"\n{'='*70}")
    print(f"  DETAILED: Current live config comparison")
    print(f"{'='*70}")
    # Old: ATR_stop=1.5, ATR_tp=3.0
    r_old = bt_atr_tp(pc, rf, rm, ef, es, 1.5, 3.0, adxt, mm, mp, rp)
    # New: ATR_stop=1.5, TP=0.85%
    r_new = bt_fixed_tp(pc, rf, rm, ef, es, 1.5, 0.0085, adxt, mm, mp, rp)

    if r_old:
        cagr, nt, wr, pnl, final, tpy = r_old
        print(f"  [OLD] ATR TP x3.0:  CAGR={cagr:+6.1f}%  Trades={nt}  TPY={tpy:.1f}  Win={wr:.1f}%  PnL=${pnl:+,.0f}  Final=${final:,.0f}")
    if r_new:
        cagr, nt, wr, pnl, final, tpy = r_new
        print(f"  [NEW] Fixed 0.85%:  CAGR={cagr:+6.1f}%  Trades={nt}  TPY={tpy:.1f}  Win={wr:.1f}%  PnL=${pnl:+,.0f}  Final=${final:,.0f}")
