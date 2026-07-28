#!/usr/bin/env python3
"""Backtest: breakeven + partial close + trailing stop exit strategy."""
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


def bt_be(pc, rf, rm, ef, es, init_stop_mult, breakeven_pct, tp1_pct, tp1_frac, trail_pct, adxt, mm, mp, rp):
    """
    Multi-stage exit (honest — no future data):
    1. Signal at close of day j-1 → enter at open of day j
    2. Initial: stop at -init_stop_mult*ATR
    3. Breakeven: if price reaches +breakeven_pct, move stop to entry
    4. TP1: if price reaches +tp1_pct, close tp1_frac of position
    5. Trail: remaining position trails at trail_pct below peak
    """
    eq = IC
    pos = {}  # symbol -> [entry, stop, peak, size_remaining, size_initial, stage]
    cd = {s: 0 for s in SYMS}
    trades = []
    btc_dates = pc['BTC-USDT']['idx']
    n = len(btc_dates)

    for i in range(1, n):
        date = btc_dates[i]
        for s in SYMS:
            p = pc[s]
            j = np.searchsorted(p['idx'], date)
            if j >= len(p['idx']) or p['idx'][j] != date: continue
            if j < 1: continue

            ps = pos.get(s)
            if ps is not None:
                entry, stop, peak, size_rem, size_init, stage = ps
                cur_high = p['H'][j]
                cur_low = p['L'][j]

                if stage == 'initial':
                    if cur_high > peak:
                        peak = cur_high
                        new_stop = peak * (1 - trail_pct/100)
                        if new_stop > stop:
                            stop = new_stop
                        ps[1], ps[2] = stop, peak
                    if cur_low <= stop:
                        pnl = size_rem * (stop - entry) - (size_rem * entry + size_rem * stop) * 0.001
                        eq += pnl
                        trades.append({'pnl': pnl, 'pnl_pct': (stop/entry - 1)*100, 'exit_reason': 'init_stop'})
                        if pnl < 0: cd[s] = 2
                        del pos[s]
                        continue
                    if cur_high >= entry * (1 + breakeven_pct/100):
                        if stop < entry:
                            stop = entry
                            ps[1] = stop
                        stage = 'breakeven'
                        ps[5] = stage

                if stage == 'breakeven':
                    if cur_low <= stop:
                        pnl = size_rem * (stop - entry) - (size_rem * entry + size_rem * stop) * 0.001
                        eq += pnl
                        trades.append({'pnl': pnl, 'pnl_pct': (stop/entry - 1)*100, 'exit_reason': 'breakeven'})
                        del pos[s]
                        continue
                    peak = max(peak, cur_high)
                    if cur_high >= entry * (1 + tp1_pct/100):
                        close_sz = size_rem * tp1_frac
                        size_rem -= close_sz
                        pnl = close_sz * (cur_high - entry) - (close_sz * entry + close_sz * cur_high) * 0.001
                        eq += pnl
                        trades.append({'pnl': pnl, 'pnl_pct': (cur_high/entry - 1)*100, 'exit_reason': f'TP1_{tp1_pct}'})
                        if size_rem <= 0:
                            del pos[s]
                            continue
                        stage = 'trailing'
                        new_stop = peak * (1 - trail_pct/100)
                        if new_stop > stop:
                            stop = new_stop
                        ps[1], ps[2], ps[3], ps[5] = stop, peak, size_rem, stage

                if stage == 'trailing':
                    if cur_high > peak:
                        peak = cur_high
                        new_stop = peak * (1 - trail_pct/100)
                        if new_stop > stop:
                            stop = new_stop
                        ps[1], ps[2] = stop, peak
                    if cur_low <= stop:
                        pnl = size_rem * (stop - entry) - (size_rem * entry + size_rem * stop) * 0.001
                        eq += pnl
                        trades.append({'pnl': pnl, 'pnl_pct': (stop/entry - 1)*100, 'exit_reason': 'trail_stop'})
                        del pos[s]
                        continue

            if pos.get(s) is None:
                if sum(1 for v in pos.values() if v is not None) >= mp: continue
                if cd.get(s, 0) > 0: cd[s] -= 1; continue
                # Signal at j-1 (yesterday's close), enter at opens[j] (today's open)
                if j < 2: continue
                rf_v = p[f'R{rf}'][j-1]; rm_v = p[f'R{rm}'][j-1]
                ef_v = p[f'E{ef}'][j-1]; es_v = p[f'E{es}'][j-1]
                ad = p['ADX'][j-1]; pdi_v = p['PDI'][j-1]; mdi_v = p['MDI'][j-1]
                at = p['A'][j-1]
                if np.isnan(at) or at <= 0 or np.isnan(ef_v) or np.isnan(es_v): continue
                if rf_v > 0 and rm_v > 0 and (rf_v * .5 + rm_v * .5) > mm and ef_v > es_v and ad > adxt and pdi_v > mdi_v:
                    ep = p['O'][j]
                    st = ep - init_stop_mult * at
                    rk = ep - st
                    if rk <= 0: continue
                    sh = (eq * rp) / rk
                    mx = (eq * 0.3) / ep
                    sh = min(sh, mx)
                    if sh <= 0: continue
                    pos[s] = [ep, st, ep, sh, sh, 'initial']

    # Close remaining
    for s, ps in list(pos.items()):
        if ps is None: continue
        entry, stop, peak, size_rem, size_init, stage = ps
        last_close = pc[s]['C'][-1]
        pnl = size_rem * (last_close - entry) - (size_rem * entry + size_rem * last_close) * 0.001
        eq += pnl
        trades.append({'pnl': pnl, 'pnl_pct': (last_close/entry - 1)*100, 'exit_reason': 'open_close'})

    if not trades:
        return None

    nt = len(trades)
    win_trades = [t for t in trades if t['pnl'] > 0]
    w = len(win_trades)
    total_pnl = sum(t['pnl'] for t in trades)
    yrs = (btc_dates[-1] - btc_dates[1]) / np.timedelta64(365, 'D')
    cagr = ((eq / IC) ** (1 / yrs) - 1) * 100 if yrs > 0 else 0

    peak_eq = IC; max_dd = 0
    # compute equity curve approx
    # simple: use trades to compute running equity
    running = IC
    for t in trades:
        running += t['pnl']
        if running > peak_eq: peak_eq = running
        dd = (peak_eq - running) / peak_eq * 100
        if dd > max_dd: max_dd = dd

    return {
        'cagr': cagr, 'trades': nt, 'tpy': nt / yrs if yrs > 0 else 0,
        'win': w / nt * 100 if nt else 0, 'pnl': total_pnl, 'final': eq,
        'max_dd': max_dd,
    }


if __name__ == '__main__':
    print("Fetching data..."); raw = fetch_all()
    btc_idx = raw['BTC-USDT'].index
    print(f"Period: {btc_idx[0].date()} to {btc_idx[-1].date()} ({len(btc_idx)} days)")
    print("Precomputing indicators..."); pc = precomp(raw)

    rf, rm, ef, es = 5, 50, 15, 30
    adxt, mm, mp, rp = 20, 0, 4, 0.03

    # Sweep: breakeven trigger, TP1 level, TP1 fraction, trail %
    be_opts = [0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0]
    tp1_opts = [1.5, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0]
    frac_opts = [0.25, 0.33, 0.5, 0.67, 0.75]
    trail_opts = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0]
    init_stop = 1.5

    print(f"\n{'='*120}")
    print(f"  BREAKEVEN + PARTIAL CLOSE + TRAILING BACKTEST")
    print(f"  Entry: ROC5>0 ROC50>0 EMA15>EMA30 ADX>{adxt} PDI>MDI")
    print(f"  Init stop: {init_stop}x ATR | Risk/trade: {rp*100:.0f}% | Max pos: {mp}")
    print(f"  Sweeping: breakeven={be_opts}% TP1={tp1_opts}% frac={frac_opts} trail={trail_opts}%")
    print(f"{'='*120}")

    results = []
    total = len(be_opts) * len(tp1_opts) * len(frac_opts) * len(trail_opts)
    count = 0
    for be, tp1, frac, tr in itertools.product(be_opts, tp1_opts, frac_opts, trail_opts):
        r = bt_be(pc, rf, rm, ef, es, init_stop, be, tp1, frac, tr, adxt, mm, mp, rp)
        count += 1
        if r:
            results.append((be, tp1, frac, tr, r))
            if count % 20 == 0 or count == total:
                print(f"  Progress: {count}/{total} ({count/total*100:.0f}%)", flush=True)

    if not results:
        print("No results!")
        exit()

    # Sort by CAGR
    results.sort(key=lambda x: x[4]['cagr'], reverse=True)

    print(f"\n{'='*120}")
    print(f"  TOP 10 BY CAGR")
    print(f"{'='*120}")
    print(f"  {'#':>2} {'BE%':>4} {'TP1%':>5} {'Frac':>5} {'Trail%':>7} {'CAGR':>7} {'Trades':>7} {'TPY':>5} {'Win%':>6} {'PnL':>10} {'Final':>10} {'MaxDD':>7}")
    print(f"  {'-'*80}")
    for idx, (be, tp1, frac, tr, r) in enumerate(results[:15]):
        print(f"  {idx+1:>2} {be:>4.1f}% {tp1:>5.1f}% {frac:>5.0%} {tr:>6.1f}% "
              f"{r['cagr']:>6.1f}% {r['trades']:>6} {r['tpy']:>4.0f} {r['win']:>5.1f}% "
              f"${r['pnl']:>+8,.0f} ${r['final']:>8,.0f} {r['max_dd']:>5.1f}%")

    print(f"\n{'='*120}")
    print(f"  TOP 15 BY SHARPE-LIKE (CAGR / MaxDD)")
    print(f"{'='*120}")
    results_by_risk = sorted(results, key=lambda x: x[4]['cagr'] / max(x[4]['max_dd'], 0.1), reverse=True)
    print(f"  {'#':>2} {'BE%':>4} {'TP1%':>5} {'Frac':>5} {'Trail%':>7} {'CAGR':>7} {'Trades':>7} {'TPY':>5} {'Win%':>6} {'PnL':>10} {'Final':>10} {'MaxDD':>7} {'C/MD':>6}")
    print(f"  {'-'*90}")
    for idx, (be, tp1, frac, tr, r) in enumerate(results_by_risk[:15]):
        ratio = r['cagr'] / max(r['max_dd'], 0.1)
        print(f"  {idx+1:>2} {be:>4.1f}% {tp1:>5.1f}% {frac:>5.0%} {tr:>6.1f}% "
              f"{r['cagr']:>6.1f}% {r['trades']:>6} {r['tpy']:>4.0f} {r['win']:>5.1f}% "
              f"${r['pnl']:>+8,.0f} ${r['final']:>8,.0f} {r['max_dd']:>5.1f}% {ratio:>5.2f}")

    # Compare with pure trailing (no breakeven, no partial close)
    print(f"\n{'='*120}")
    print(f"  REFERENCE: Pure Trail 3% (current strategy)")
    print(f"{'='*120}")
    ref = bt_be(pc, rf, rm, ef, es, init_stop, 999, 999, 0, 3.0, adxt, mm, mp, rp)
    if ref:
        print(f"  CAGR={ref['cagr']:>6.1f}% Trades={ref['trades']} TPY={ref['tpy']:.0f} "
              f"Win={ref['win']:.1f}% PnL=${ref['pnl']:+,.0f} Final=${ref['final']:,.0f} MaxDD={ref['max_dd']:.1f}%")
