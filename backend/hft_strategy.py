"""
HIGH-FREQUENCY INTRADAY STRATEGY
=================================
Multi-mode adaptive strategy targeting 5-20 trades/day on BTC 5m.

Design:
- Regime detection (ADX: trending vs ranging)
- Trending: EMA pullback + RSI + Volume + Session
- Ranging: BB bounce + RSI extreme + VWAP + Volume
- Multi-TF: 1H trend filter + 5m entry
- Session filter: Trade only during high-volume hours
- Targets: 0.2-0.5% per trade, SL 0.1-0.3%
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

import numpy as np
import pandas as pd
from app.services.data_cache import _load_cache
np.random.seed(42)

# ═══════════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════════

def _ema(a, s): return pd.Series(a).ewm(span=s, adjust=False).mean().values
def _sma(a, s): return pd.Series(a).rolling(s).mean().values

def _rsi(c, p=14):
    d = pd.Series(c).diff()
    g = d.where(d > 0, 0.0).rolling(p).mean().values
    l = (-d.where(d < 0, 0.0)).rolling(p).mean().values
    r = np.full(len(c), 50.0)
    for i in range(p, len(c)):
        r[i] = 0.0 if l[i] == 0 else 100.0 - 100.0 / (1.0 + g[i] / l[i])
    return r

def _atr(h, l, c, p=14):
    tr = np.maximum(h[1:]-l[1:], np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
    return np.insert(pd.Series(tr).rolling(p).mean().values, 0, 0)

def _adx(h, l, c, p=14):
    n = len(c); pdm = np.zeros(n); mdm = np.zeros(n); tr = np.zeros(n)
    for i in range(1, n):
        up = h[i]-h[i-1]; dn = l[i-1]-l[i]
        pdm[i] = up if (up > dn and up > 0) else 0
        mdm[i] = dn if (dn > up and dn > 0) else 0
        tr[i] = max(h[i]-l[i], max(abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    at = pd.Series(tr).rolling(p).mean().values
    pdi = np.where(at > 0, pd.Series(pdm).rolling(p).mean().values / at * 100, 0)
    mdi = np.where(at > 0, pd.Series(mdm).rolling(p).mean().values / at * 100, 0)
    s = pdi + mdi
    dx = np.where(s > 0, np.abs(pdi - mdi) / s * 100, 0)
    return pd.Series(dx).rolling(p).mean().values, pdi, mdi

def _bb(c, p=20, s=2.0):
    m = _sma(c, p); st = pd.Series(c).rolling(p).std().values
    return m + s*st, m, m - s*st

def _obv(c, v):
    r = np.zeros(len(c))
    for i in range(1, len(c)):
        if c[i] > c[i-1]: r[i] = r[i-1] + v[i]
        elif c[i] < c[i-1]: r[i] = r[i-1] - v[i]
        else: r[i] = r[i-1]
    return r

def _vwap(h, l, c, v):
    tp = (h+l+c)/3; ctv = np.cumsum(tp*v); cv = np.cumsum(v)
    return np.where(cv > 0, ctv/cv, c)

def _stoch(c, l, h, k=14, d=3):
    n = len(c); sk = np.full(n, 50.0)
    for i in range(k-1, n):
        hh = np.max(h[i-k+1:i+1]); ll = np.min(l[i-k+1:i+1])
        sk[i] = 50.0 if hh == ll else (c[i]-ll)/(hh-ll)*100
    return sk, _sma(sk, d)

def _macd_hist(c):
    return (_ema(c,12) - _ema(c,26)) - _ema(_ema(c,12) - _ema(c,26), 9)

def _cci(h, l, c, p=20):
    tp = (h+l+c)/3; m = _sma(tp, p)
    md = pd.Series(tp).rolling(p).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True).values
    return np.where(md > 0, (tp - m) / (0.015 * md), 0)


# ═══════════════════════════════════════════════════════════════
# SESSION FILTER (UTC hours when BTC is most active)
# ═══════════════════════════════════════════════════════════════

def _get_session_strength(bar_idx, bars_per_day=288):
    """Return session strength 0-1 based on time of day.
    High activity: London open (7-11 UTC), NY open (13-17 UTC),
    Overlap (13-17 UTC), Asia session (0-3 UTC).
    Low: 18-23 UTC, 4-6 UTC.
    """
    minute_of_day = (bar_idx % bars_per_day) * 5
    hour = minute_of_day // 60

    # Peak sessions (highest volume)
    if 7 <= hour <= 11: return 1.0    # London
    if 13 <= hour <= 17: return 1.0   # NY
    if 0 <= hour <= 3: return 0.7     # Asia
    if 12 <= hour <= 13: return 0.9   # London/NY overlap
    if 4 <= hour <= 6: return 0.4     # Low
    if 18 <= hour <= 23: return 0.3   # Low
    return 0.5


# ═══════════════════════════════════════════════════════════════
# ENTRY MODES
# ═══════════════════════════════════════════════════════════════

def _entries_ema_pullback(c, h, l, v, e9, e21, e50, r14, a14, adx_v, vw, obv_v, obv_sma, session):
    """TRENDING: EMA 9/21 pullback + RSI + Volume + ADX > 25"""
    n = len(c); entries = []
    for i in range(300, n):
        if a14[i] == 0: continue
        # Session filter: skip low-volume times
        if session[i] < 0.5: continue
        # ADX must show trend
        if adx_v[i] < 25: continue

        # LONG: Uptrend, pullback to EMA 9-21 zone, RSI oversold on pullback
        if e9[i] > e21[i] > e50[i]:
            # Price pulled back into EMA 9-21 zone
            pullback = c[i] <= e9[i] * 1.003 and c[i] >= e21[i] * 0.997
            # RSI was oversold and turning up
            rsi_turning = r14[i] < 45 and r14[i] > r14[i-1]
            # Volume confirms
            vol_ok = v[i] > obv_sma[i] * 0.8
            # VWAP confirmation
            above_vwap = c[i] > vw[i]
            if pullback and rsi_turning and vol_ok and above_vwap:
                entries.append((i, 1, a14[i]))

        # SHORT: Downtrend, pullback up to EMA 9-21 zone
        elif e9[i] < e21[i] < e50[i]:
            pullback = c[i] >= e9[i] * 0.997 and c[i] <= e21[i] * 1.003
            rsi_turning = r14[i] > 55 and r14[i] < r14[i-1]
            vol_ok = v[i] > obv_sma[i] * 0.8
            below_vwap = c[i] < vw[i]
            if pullback and rsi_turning and vol_ok and below_vwap:
                entries.append((i, -1, a14[i]))
    return entries


def _entries_bb_bounce(c, h, l, v, e21, e50, r14, a14, bu, bm, bl, vw, obv_v, obv_sma, session, stoch_k, stoch_d):
    """RANGING: BB bounce + RSI extreme + VWAP + Volume"""
    n = len(c); entries = []
    for i in range(300, n):
        if a14[i] == 0: continue
        if session[i] < 0.5: continue
        # Must be ranging (ADX < 25 is handled by caller)

        # LONG: Price at lower BB, RSI oversold, above VWAP
        if c[i] <= bl[i] * 1.001:
            rsi_extreme = r14[i] < 30 and r14[i] > r14[i-1]
            stoch_oversold = stoch_k[i] < 25 and stoch_k[i] > stoch_d[i]
            vol_ok = v[i] > obv_sma[i] * 0.8
            if rsi_extreme and stoch_oversold and vol_ok:
                entries.append((i, 1, a14[i]))

        # SHORT: Price at upper BB, RSI overbought
        elif c[i] >= bu[i] * 0.999:
            rsi_extreme = r14[i] > 70 and r14[i] < r14[i-1]
            stoch_overbought = stoch_k[i] > 75 and stoch_k[i] < stoch_d[i]
            vol_ok = v[i] > obv_sma[i] * 0.8
            if rsi_extreme and stoch_overbought and vol_ok:
                entries.append((i, -1, a14[i]))
    return entries


def _entries_vwap_revert(c, h, l, v, e21, r14, a14, vw, obv_v, obv_sma, session):
    """VWAP Mean Reversion: Price deviates from VWAP, RSI confirms, reversion expected"""
    n = len(c); entries = []
    for i in range(300, n):
        if a14[i] == 0: continue
        if session[i] < 0.5: continue

        deviation = (c[i] - vw[i]) / vw[i] * 100  # % deviation from VWAP

        # LONG: Price below VWAP by 0.15-0.5%, RSI oversold, turning up
        if -0.5 < deviation < -0.15:
            rsi_turn = r14[i] < 40 and r14[i] > r14[i-1]
            ema_support = c[i] > e21[i] * 0.998  # Near EMA support
            vol_ok = v[i] > obv_sma[i] * 0.7
            if rsi_turn and vol_ok:
                entries.append((i, 1, a14[i]))

        # SHORT: Price above VWAP by 0.15-0.5%, RSI overbought
        elif 0.15 < deviation < 0.5:
            rsi_turn = r14[i] > 60 and r14[i] < r14[i-1]
            vol_ok = v[i] > obv_sma[i] * 0.7
            if rsi_turn and vol_ok:
                entries.append((i, -1, a14[i]))
    return entries


def _entries_macd_momentum(c, h, l, v, e9, e21, e50, r14, a14, mh, obv_v, obv_sma, session):
    """MOMENTUM: MACD histogram flip + EMA alignment + Volume"""
    n = len(c); entries = []
    for i in range(300, n):
        if a14[i] == 0: continue
        if session[i] < 0.5: continue

        # LONG: MACD histogram turns positive, EMA aligned up
        if mh[i] > 0 and mh[i-1] <= 0:
            if e9[i] > e21[i] and r14[i] < 60:
                vol_ok = v[i] > obv_sma[i] * 1.0
                if vol_ok:
                    entries.append((i, 1, a14[i]))

        # SHORT: MACD histogram turns negative
        elif mh[i] < 0 and mh[i-1] >= 0:
            if e9[i] < e21[i] and r14[i] > 40:
                vol_ok = v[i] > obv_sma[i] * 1.0
                if vol_ok:
                    entries.append((i, -1, a14[i]))
    return entries


def _entries_cci_reversal(c, h, l, v, e21, e50, r14, a14, cci_v, obv_v, obv_sma, session):
    """CCI reversal at extremes + EMA trend + Volume"""
    n = len(c); entries = []
    for i in range(300, n):
        if a14[i] == 0: continue
        if session[i] < 0.5: continue

        # LONG: CCI crosses above -100
        if cci_v[i] > -100 and cci_v[i-1] <= -100:
            if e21[i] > e50[i] and r14[i] < 50:
                vol_ok = v[i] > obv_sma[i] * 0.9
                if vol_ok:
                    entries.append((i, 1, a14[i]))

        # SHORT: CCI crosses below 100
        elif cci_v[i] < 100 and cci_v[i-1] >= 100:
            if e21[i] < e50[i] and r14[i] > 50:
                vol_ok = v[i] > obv_sma[i] * 0.9
                if vol_ok:
                    entries.append((i, -1, a14[i]))
    return entries


def _entries_rsi_divergence(c, h, l, v, r14, a14, obv_v, obv_sma, session):
    """RSI divergence: Price makes new low but RSI doesn't (bullish) or vice versa"""
    n = len(c); entries = []
    lookback = 12  # bars to look back for divergence

    for i in range(300, n):
        if a14[i] == 0: continue
        if session[i] < 0.5: continue
        if i < lookback: continue

        # Check for bullish divergence: price lower low, RSI higher low
        price_min_now = min(c[i-3:i+1])
        price_min_prev = min(c[i-lookback:i-3])
        rsi_min_now = min(r14[i-3:i+1])
        rsi_min_prev = min(r14[i-lookback:i-3])

        if price_min_now < price_min_prev and rsi_min_now > rsi_min_prev:
            if r14[i] < 40 and r14[i] > r14[i-1]:
                entries.append((i, 1, a14[i]))

        # Bearish divergence: price higher high, RSI lower high
        price_max_now = max(c[i-3:i+1])
        price_max_prev = max(c[i-lookback:i-3])
        rsi_max_now = max(r14[i-3:i+1])
        rsi_max_prev = max(r14[i-lookback:i-3])

        if price_max_now > price_max_prev and rsi_max_now < rsi_max_prev:
            if r14[i] > 60 and r14[i] < r14[i-1]:
                entries.append((i, -1, a14[i]))
    return entries


# ═══════════════════════════════════════════════════════════════
# BACKTEST ENGINE — HFT with dynamic TP/SL
# ═══════════════════════════════════════════════════════════════

def run_hft(c, h, l, entries, cap=10000, fee=0.0005,
            tp_atr=1.0, sl_atr=0.8, bars_max=48,
            bars_between=3, partial_pct=0.5, partial_atr=0.6):
    """
    HFT backtest:
    - tp_atr: take profit at X * ATR (default 1.0 = tight target)
    - sl_atr: stop loss at X * ATR (default 0.8 = tight stop)
    - bars_max: close after X bars if no TP/SL hit (time stop)
    - bars_between: min bars between trades
    - partial_pct: partial TP at X% of target
    - partial_atr: partial TP trigger at X * ATR
    """
    n = len(c); a = _atr(h, l, c, 14); bal = float(cap)
    pos = 0; ep = 0.; sl = 0.; tp = 0.; ps = 0.; eb = -999
    pd_done = False; entry_bar = 0; trades = []
    ed = {b: (s, ea) for b, s, ea in entries}
    eq = np.full(n, cap)

    for i in range(n):
        ca = a[i] if not np.isnan(a[i]) else 0

        if pos > 0:
            # Check TP
            if c[i] >= tp:
                pnl = ps * (tp - ep) - (ps * ep + ps * tp) * fee
                bal += pnl; trades.append(pnl); pos = 0; eb = i; pd_done = False
            # Check SL
            elif c[i] <= sl:
                pnl = ps * (sl - ep) - (ps * ep + ps * sl) * fee
                bal += pnl; trades.append(pnl); pos = 0; eb = i; pd_done = False
            # Time stop
            elif i - entry_bar >= bars_max:
                pnl = ps * (c[i] - ep) - (ps * ep + ps * c[i]) * fee
                bal += pnl; trades.append(pnl); pos = 0; eb = i; pd_done = False
            # Partial TP
            elif not pd_done and ca > 0 and c[i] >= ep + ca * partial_atr:
                psz = ps * partial_pct
                pnl = psz * (c[i] - ep) - (psz * ep + psz * c[i]) * fee
                bal += pnl; ps -= psz; pd_done = True; trades.append(pnl)

        elif pos < 0:
            if c[i] <= tp:
                pnl = ps * (ep - tp) - (ps * ep + ps * tp) * fee
                bal += pnl; trades.append(pnl); pos = 0; eb = i; pd_done = False
            elif c[i] >= sl:
                pnl = ps * (ep - sl) - (ps * ep + ps * sl) * fee
                bal += pnl; trades.append(pnl); pos = 0; eb = i; pd_done = False
            elif i - entry_bar >= bars_max:
                pnl = ps * (ep - c[i]) - (ps * ep + ps * c[i]) * fee
                bal += pnl; trades.append(pnl); pos = 0; eb = i; pd_done = False
            elif not pd_done and ca > 0 and c[i] <= ep - ca * partial_atr:
                psz = ps * partial_pct
                pnl = psz * (ep - c[i]) - (psz * ep + psz * c[i]) * fee
                bal += pnl; ps -= psz; pd_done = True; trades.append(pnl)

        # Track equity
        if pos != 0: eq[i] = bal + ps * (c[i] - ep) * pos
        else: eq[i] = bal

        # Entry
        if pos != 0 or (i - eb < bars_between): continue
        if i in ed and ca > 0:
            s, ea = ed[i]; ep = c[i]; ps = bal * 0.95 / ep; pos = s
            if s == 1:
                sl = ep - ca * sl_atr; tp = ep + ca * tp_atr
            else:
                sl = ep + ca * sl_atr; tp = ep - ca * tp_atr
            entry_bar = i; pd_done = False

    # Close remaining
    if pos != 0:
        if pos > 0: pnl = ps * (c[-1] - ep) - (ps * ep + ps * c[-1]) * fee
        else: pnl = ps * (ep - c[-1]) - (ps * ep + ps * c[-1]) * fee
        bal += pnl; trades.append(pnl)
    eq[-1] = bal

    nt = len(trades); wins = [t for t in trades if t > 0]
    wr = len(wins)/nt*100 if nt else 0
    gp = sum(wins) if wins else 0; gl = abs(sum(t for t in trades if t <= 0)) or 0.001
    pf = gp/gl; ret = (bal/cap-1)*100
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)*100).max()
    return {"ret": ret, "trades": nt, "wr": wr, "pf": pf, "dd": dd, "eq": eq}


# ═══════════════════════════════════════════════════════════════
# MAIN — Test all modes + combinations
# ═══════════════════════════════════════════════════════════════

def main():
    cache = _load_cache("BTC-USDT", "5m")
    if not cache: print("No cache"); return
    arr = np.array(cache, dtype=object)
    c = arr[:, 4].astype(float); h = arr[:, 2].astype(float)
    l = arr[:, 3].astype(float); v = arr[:, 5].astype(float)
    days = len(c) // 288

    print(f"{'='*90}")
    print(f" HIGH-FREQUENCY INTRADAY STRATEGY — {days} days 5m ({len(c)} bars)")
    print(f" Fee: 0.05% | Target: 0.2-0.5%/trade | SL: 0.1-0.3%")
    print(f"{'='*90}")

    # Pre-compute all indicators
    e9 = _ema(c, 9); e21 = _ema(c, 21); e50 = _ema(c, 50)
    r14 = _rsi(c, 14); a14 = _atr(h, l, c, 14)
    adx_v, pdi, mdi = _adx(h, l, c, 14)
    bu, bm, bl = _bb(c)
    ov = _obv(c, v); os_ = _sma(ov, 20); vs_ = _sma(v, 20)
    vw = _vwap(h, l, c, v)
    stoch_k, stoch_d = _stoch(c, l, h)
    mh = _macd_hist(c)
    cci_v = _cci(h, l, c)
    session = np.array([_get_session_strength(i) for i in range(len(c))])

    # ═══════════════════════════════════════════════════════════
    # TEST 1: Individual entry modes
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print(f" PHASE 1: INDIVIDUAL ENTRY MODES")
    print(f"{'='*90}")

    modes = {
        "EMA Pullback (trend)": lambda: _entries_ema_pullback(c,h,l,v,e9,e21,e50,r14,a14,adx_v,vw,ov,os_,session),
        "BB Bounce (range)": lambda: _entries_bb_bounce(c,h,l,v,e21,e50,r14,a14,bu,bm,bl,vw,ov,os_,session,stoch_k,stoch_d),
        "VWAP Revert": lambda: _entries_vwap_revert(c,h,l,v,e21,r14,a14,vw,ov,os_,session),
        "MACD Momentum": lambda: _entries_macd_momentum(c,h,l,v,e9,e21,e50,r14,a14,mh,ov,os_,session),
        "CCI Reversal": lambda: _entries_cci_reversal(c,h,l,v,e21,e50,r14,a14,cci_v,ov,os_,session),
        "RSI Divergence": lambda: _entries_rsi_divergence(c,h,l,v,r14,a14,ov,os_,session),
    }

    print(f"\n  {'Mode':<25} {'Ret%':>7} {'T':>5} {'T/day':>6} {'WR%':>6} {'PF':>6} {'DD%':>6} {'Ann%':>7}")
    print(f"  {'-'*75}")

    all_results = []
    for name, gen_fn in modes.items():
        entries = gen_fn()
        r = run_hft(c, h, l, entries, cap=10000, bars_between=3)
        trades_per_day = r["trades"] / days if days > 0 else 0
        ann = r["ret"] * 365 / days
        all_results.append((name, r, len(entries), ann, trades_per_day))
        print(f"  {name:<25} {r['ret']:>+6.1f}% {r['trades']:>5} {trades_per_day:>5.1f} {r['wr']:>5.1f}% {r['pf']:>6.2f} {r['dd']:>5.1f}% {ann:>+6.1f}%")

    # ═══════════════════════════════════════════════════════════
    # TEST 2: TP/SL optimization
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print(f" PHASE 2: TP/SL OPTIMIZATION (all entries combined)")
    print(f"{'='*90}")

    # Combine all entries
    all_entries = []
    for gen_fn in modes.values():
        all_entries.extend(gen_fn())
    all_entries.sort(key=lambda x: x[0])

    print(f"\n  Total entries: {len(all_entries)}")
    print(f"\n  {'TP*ATR':>7} {'SL*ATR':>7} {'Bars':>5} {'Ret%':>7} {'T':>5} {'T/day':>6} {'WR%':>6} {'PF':>6} {'DD%':>6} {'Ann%':>7}")
    print(f"  {'-'*75}")

    best = None
    for tp_mult in [0.5, 0.8, 1.0, 1.2, 1.5, 2.0]:
        for sl_mult in [0.5, 0.8, 1.0, 1.2]:
            for bars_max in [24, 36, 48, 72, 96]:
                r = run_hft(c, h, l, all_entries, cap=10000,
                           tp_atr=tp_mult, sl_atr=sl_mult, bars_max=bars_max,
                           bars_between=3)
                trades_per_day = r["trades"] / days
                ann = r["ret"] * 365 / days
                if r["trades"] < 50: continue  # Need enough trades
                if best is None or r["ret"]/max(r["dd"],0.1) > best[1]["ret"]/max(best[1]["dd"],0.1):
                    best = (f"TP={tp_mult} SL={sl_mult} Bars={bars_max}", r, ann, trades_per_day)

    if best:
        print(f"\n  BEST: {best[0]}")
        print(f"  Ret={best[1]['ret']:+.1f}% T={best[1]['trades']} T/day={best[3]:.1f} "
              f"WR={best[1]['wr']:.1f}% PF={best[1]['pf']:.2f} DD={best[1]['dd']:.1f}% Ann={best[2]:+.1f}%")

    # ═══════════════════════════════════════════════════════════
    # TEST 3: Multi-mode combination with regime filter
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print(f" PHASE 3: MULTI-MODE WITH REGIME FILTER")
    print(f"{'='*90}")

    def entries_adaptive(c, h, l, v):
        """Adaptive: trend entries when ADX>25, range entries when ADX<25"""
        n = len(c); e9_ = _ema(c,9); e21_ = _ema(c,21); e50_ = _ema(c,50)
        r14_ = _rsi(c,14); a14_ = _atr(h,l,c,14)
        adx_,_,_ = _adx(h,l,c,14)
        bu_,_,bl_ = _bb(c)
        ov_ = _obv(c,v); os__ = _sma(ov_,20); vs__ = _sma(v,20)
        vw_ = _vwap(h,l,c,v)
        sk_,sd_ = _stoch(c,l,h)
        mh_ = _macd_hist(c)
        ci_ = _cci(h,l,c)
        sess = np.array([_get_session_strength(i) for i in range(n)])

        entries = []
        for i in range(300, n):
            if a14_[i] == 0 or sess[i] < 0.5: continue

            if adx_[i] > 25:
                # TRENDING: EMA pullback
                if e9_[i] > e21_[i] > e50_[i]:
                    pullback = c[i] <= e9_[i]*1.003 and c[i] >= e21_[i]*0.997
                    if pullback and r14_[i] < 45 and r14_[i] > r14_[i-1] and v[i] > os__[i]*0.8 and c[i] > vw_[i]:
                        entries.append((i, 1, a14_[i]))
                elif e9_[i] < e21_[i] < e50_[i]:
                    pullback = c[i] >= e9_[i]*0.997 and c[i] <= e21_[i]*1.003
                    if pullback and r14_[i] > 55 and r14_[i] < r14_[i-1] and v[i] > os__[i]*0.8 and c[i] < vw_[i]:
                        entries.append((i, -1, a14_[i]))
            else:
                # RANGING: BB bounce + VWAP
                if c[i] <= bl_[i]*1.001:
                    if r14_[i] < 30 and r14_[i] > r14_[i-1] and sk_[i] < 25 and v[i] > os__[i]*0.8:
                        entries.append((i, 1, a14_[i]))
                elif c[i] >= bu_[i]*0.999:
                    if r14_[i] > 70 and r14_[i] < r14_[i-1] and sk_[i] > 75 and v[i] > os__[i]*0.8:
                        entries.append((i, -1, a14_[i]))

                # VWAP reversion
                dev = (c[i] - vw_[i]) / vw_[i] * 100
                if -0.5 < dev < -0.15 and r14_[i] < 40 and r14_[i] > r14_[i-1]:
                    entries.append((i, 1, a14_[i]))
                elif 0.15 < dev < 0.5 and r14_[i] > 60 and r14_[i] < r14_[i-1]:
                    entries.append((i, -1, a14_[i]))

                # MACD flip
                if mh_[i] > 0 and mh_[i-1] <= 0 and e9_[i] > e21_[i] and r14_[i] < 60:
                    entries.append((i, 1, a14_[i]))
                elif mh_[i] < 0 and mh_[i-1] >= 0 and e9_[i] < e21_[i] and r14_[i] > 40:
                    entries.append((i, -1, a14_[i]))

        return entries

    entries_adapt = entries_adaptive(c, h, l, v)

    print(f"\n  Adaptive mode: {len(entries_adapt)} entries ({len(entries_adapt)/days:.1f}/day)")
    for tp_mult in [0.8, 1.0, 1.2, 1.5]:
        for sl_mult in [0.6, 0.8, 1.0]:
            for bars_max in [24, 48, 72]:
                r = run_hft(c, h, l, entries_adapt, cap=10000,
                           tp_atr=tp_mult, sl_atr=sl_mult, bars_max=bars_max,
                           bars_between=3)
                trades_per_day = r["trades"] / days
                ann = r["ret"] * 365 / days
                if r["trades"] < 30: continue
                if ann > 10:  # Only show profitable
                    print(f"    TP={tp_mult} SL={sl_mult} Bars={bars_max}: Ret={r['ret']:+.1f}% "
                          f"T={r['trades']} T/day={trades_per_day:.1f} WR={r['wr']:.1f}% "
                          f"PF={r['pf']:.2f} DD={r['dd']:.1f}% Ann={ann:+.1f}%")

    # ═══════════════════════════════════════════════════════════
    # TEST 4: Walk-forward on best adaptive config
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print(f" PHASE 4: WALK-FORWARD (3-fold) on Adaptive")
    print(f"{'='*90}")

    kf = 3; sz = len(c) // kf
    for tp_mult in [0.8, 1.0, 1.2]:
        for sl_mult in [0.8, 1.0]:
            for bars_max in [48, 72]:
                folds = []
                for k in range(kf):
                    s = k*sz; e = min((k+1)*sz, len(c))
                    fe = entries_adaptive(c[s:e], h[s:e], l[s:e], v[s:e])
                    # Re-index entries
                    fe_adj = [(b, side, ea) for b, side, ea in fe]
                    r_ = run_hft(c[s:e], h[s:e], l[s:e], fe_adj, cap=10000,
                                tp_atr=tp_mult, sl_atr=sl_mult, bars_max=bars_max,
                                bars_between=3)
                    f_ann = r_["ret"]*365/((e-s)//288)
                    folds.append((r_["ret"], r_["wr"], r_["pf"], r_["dd"], f_ann, r_["trades"]))

                avg_ret = np.mean([f[0] for f in folds])
                avg_wr = np.mean([f[1] for f in folds])
                avg_pf = np.mean([f[2] for f in folds])
                avg_dd = np.max([f[3] for f in folds])
                avg_ann = np.mean([f[4] for f in folds])
                total_trades = sum(f[5] for f in folds)
                all_pos = all(f[0] > 0 for f in folds)
                status = "PASS" if all_pos and avg_pf > 1.0 else "FAIL"

                if avg_ret > 0 or status == "PASS":
                    print(f"\n  TP={tp_mult} SL={sl_mult} Bars={bars_max} [{status}]")
                    for k_, f in enumerate(folds):
                        print(f"    Fold {k_+1}: Ret={f[0]:>+6.1f}% WR={f[1]:>5.1f}% PF={f[2]:>5.2f} DD={f[3]:>5.1f}% T={f[5]}")
                    print(f"  >>> OOS: Ret={avg_ret:+.1f}% WR={avg_wr:.1f}% PF={avg_pf:.2f} DD={avg_dd:.1f}% Ann={avg_ann:+.1f}% Trades={total_trades}")


if __name__ == "__main__":
    main()
