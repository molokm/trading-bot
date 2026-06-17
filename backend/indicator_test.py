"""
INDICATOR COMBINATION TESTING
==============================
20 combinations based on backtested research.
Each combo computes its own indicators to support sliced arrays (walk-forward).
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

import numpy as np
import pandas as pd
from app.services.data_cache import _load_cache
np.random.seed(42)

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

def _macd_hist(c, f=12, s=26, sig=9):
    return (_ema(c,f) - _ema(c,s)) - _ema(_ema(c,f) - _ema(c,s), sig)

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
    return pd.Series(dx).rolling(p).mean().values

def _stoch(h, l, c, k=14, d=3):
    n = len(c); sk = np.full(n, 50.0)
    for i in range(k-1, n):
        hh = np.max(h[i-k+1:i+1]); ll = np.min(l[i-k+1:i+1])
        sk[i] = 50.0 if hh == ll else (c[i]-ll)/(hh-ll)*100
    return sk, _sma(sk, d)

def _bb(c, p=20, s=2.0):
    m = _sma(c, p); st = pd.Series(c).rolling(p).std().values
    return m + s*st, m, m - s*st

def _cci(h, l, c, p=20):
    tp = (h+l+c)/3; m = _sma(tp, p)
    md = pd.Series(tp).rolling(p).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True).values
    return np.where(md > 0, (tp - m) / (0.015 * md), 0)

def _mfi(h, l, c, v, p=14):
    tp = (h+l+c)/3; mf = tp*v; n = len(c); r = np.full(n, 50.0)
    for i in range(p, n):
        pos = sum(mf[j] for j in range(i-p+1, i+1) if tp[j] > tp[j-1])
        neg = sum(mf[j] for j in range(i-p+1, i+1) if tp[j] <= tp[j-1])
        r[i] = 100 - 100/(1+pos/neg) if neg > 0 else 100
    return r

def _supertrend(h, l, c, p=10, m=3.0):
    n = len(c); t = np.ones(n); atr_v = _atr(h, l, c, p)
    up = (h+l)/2 + m*atr_v; dn = (h+l)/2 - m*atr_v
    for i in range(1, n):
        if c[i] > up[i-1]: t[i] = 1
        elif c[i] < dn[i-1]: t[i] = -1
        else: t[i] = t[i-1]
        if t[i] == 1: up[i] = max(up[i], dn[i])
        else: dn[i] = min(dn[i], up[i])
    return t

def _ichimoku(h, l, c, tk=9, ks=26):
    n = len(c); ten = np.zeros(n); kij = np.zeros(n)
    for i in range(ks, n):
        if i >= tk: ten[i] = (np.max(h[i-tk+1:i+1])+np.min(l[i-tk+1:i+1]))/2
        if i >= ks: kij[i] = (np.max(h[i-ks+1:i+1])+np.min(l[i-ks+1:i+1]))/2
    return ten, kij

def _fisher(c, p=9):
    n = len(c); r = np.zeros(n)
    for i in range(p, n):
        hi = np.max(c[i-p+1:i+1]); lo = np.min(c[i-p+1:i+1])
        v = 0.0 if hi == lo else 2*((c[i]-lo)/(hi-lo)-0.5)
        v = max(-0.999, min(0.999, v))
        r[i] = 0.5*np.log((1+v)/(1-v)) + 0.5*r[i-1] if i > 0 else 0
    return np.where(r > 0, 1, -1)

def _hma(c, s): return _ema(2*_ema(c, s//2) - _ema(c, s), int(np.sqrt(s)))

def _kama(c, s=10):
    n = len(c); r = np.copy(c).astype(float)
    for i in range(s, n):
        d = abs(c[i] - c[i-s])
        vol = np.sum(np.abs(np.diff(c[i-s:i+1])))
        k = min(1.0, max(0.01, d/vol*2.5)) if vol > 0 else 0.5
        r[i] = r[i-1] + k * (c[i] - r[i-1])
    return r

def _hl_struct(h, l, n):
    hh=np.zeros(n);hl_=np.zeros(n);lh_=np.zeros(n);ll_=np.zeros(n)
    for i in range(20,n):
        hs=h[i-20:i+1];ls=l[i-20:i+1]
        hh[i]=np.sum(np.diff(hs)>0);hl_[i]=np.sum(np.diff(ls)>0)
        lh_[i]=np.sum(np.diff(hs)<0);ll_[i]=np.sum(np.diff(ls)<0)
    return hh,hl_,lh_,ll_

# ═══════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════

def run_bt(c, h, l, entries, cap=10000, fee=0.0005, lev=1.0,
           atr_sl=1.5, atr_lock=4.0, partial_pct=0.3, partial_mult=1.5,
           bars_between=1000):
    n = len(c); a = _atr(h, l, c, 14); bal = float(cap)
    pos = 0; ep = 0.; sl = 0.; ps = 0.; eb = -999; pd_done = False; trades = []
    ed = {b: (s, ea) for b, s, ea in entries}
    eq = np.full(n, cap)
    for i in range(n):
        ca = a[i] if not np.isnan(a[i]) else 0
        if pos > 0 and ca > 0:
            if c[i] > ep + ca*atr_lock: sl = max(sl, ep + ca*(atr_lock-0.5))
            else: sl = max(sl, c[i] - ca*atr_sl)
            if not pd_done and c[i] > ep + ca*partial_mult:
                psz = ps*partial_pct; pnl = psz*(c[i]-ep)*lev - (psz*ep+psz*c[i])*fee
                bal += pnl; ps -= psz; pd_done = True; trades.append(pnl)
            if c[i] < sl:
                pnl = ps*(sl-ep)*lev - (ps*ep+ps*sl)*fee
                bal += pnl; trades.append(pnl); pos = 0; eb = i; pd_done = False
        elif pos < 0 and ca > 0:
            if c[i] < ep - ca*atr_lock: sl = min(sl, ep - ca*(atr_lock-0.5))
            else: sl = min(sl, c[i] + ca*atr_sl)
            if not pd_done and c[i] < ep - ca*partial_mult:
                psz = ps*partial_pct; pnl = psz*(ep-c[i])*lev - (psz*ep+psz*c[i])*fee
                bal += pnl; ps -= psz; pd_done = True; trades.append(pnl)
            if c[i] > sl:
                pnl = ps*(ep-sl)*lev - (ps*ep+ps*sl)*fee
                bal += pnl; trades.append(pnl); pos = 0; eb = i; pd_done = False
        if pos != 0: eq[i] = bal + ps*(c[i]-ep)*pos*lev
        else: eq[i] = bal
        if pos != 0 or (i - eb < bars_between): continue
        if i in ed:
            s, ea = ed[i]; ep = c[i]; ps = bal*0.95/ep; pos = s
            sl = ep - ea*atr_sl*s; pd_done = False
    if pos != 0:
        if pos > 0: pnl = ps*(c[-1]-ep)*lev - (ps*ep+ps*c[-1])*fee
        else: pnl = ps*(ep-c[-1])*lev - (ps*ep+ps*c[-1])*fee
        bal += pnl; trades.append(pnl)
    eq[-1] = bal
    nt = len(trades); wins = [t for t in trades if t > 0]
    wr = len(wins)/nt*100 if nt else 0
    gp = sum(wins) if wins else 0; gl = abs(sum(t for t in trades if t <= 0)) or 0.001
    pf = gp/gl; ret = (bal/cap-1)*100
    dd = ((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)*100).max()
    return {"ret": ret, "trades": nt, "wr": wr, "pf": pf, "dd": dd, "eq": eq}


# ═══════════════════════════════════════════════════════════════
# 20 INDICATOR COMBINATIONS
# ═══════════════════════════════════════════════════════════════

def combo_01(c, h, l, v):
    """EMA 9/21 + RSI(14) 40/60 + ATR Stop — #1 Research Combo"""
    n=len(c); e9=_ema(c,9); e21=_ema(c,21); r14=_rsi(c,14); a14=_atr(h,l,c,14); entries=[]
    for i in range(300, n):
        if a14[i]==0: continue
        if e9[i]>e21[i] and c[i]>e9[i] and r14[i]<40 and r14[i]>r14[i-1]: entries.append((i,1,a14[i]))
        elif e9[i]<e21[i] and c[i]<e9[i] and r14[i]>60 and r14[i]<r14[i-1]: entries.append((i,-1,a14[i]))
    return entries

def combo_02(c, h, l, v):
    """LevX Pro v3: EMA 40/100 + RSI 30/60 + HH/HL struct"""
    n=len(c); e40=_ema(c,40); e100=_ema(c,100); r14=_rsi(c,14); a14=_atr(h,l,c,14)
    hh,hl_,lh_,ll_=_hl_struct(h,l,n); entries=[]
    for i in range(400,n):
        if a14[i]==0: continue
        up=e40[i]>e100[i] and c[i]>e40[i]; dn=e40[i]<e100[i] and c[i]<e40[i]
        bull=hh[i]>ll_[i] and hl_[i]>lh_[i]; bear=ll_[i]>hh[i] and lh_[i]>hl_[i]
        if up and r14[i]<30 and r14[i]>r14[i-1] and bull: entries.append((i,1,a14[i]))
        elif dn and r14[i]>60 and r14[i]<r14[i-1] and bear: entries.append((i,-1,a14[i]))
    return entries

def combo_03(c, h, l, v):
    """EMA 9/21 + RSI + Volume > SMA20"""
    n=len(c); e9=_ema(c,9); e21=_ema(c,21); r14=_rsi(c,14); a14=_atr(h,l,c,14)
    ov=_obv(c,v); os_=_sma(ov,20); vs=_sma(v,20); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        vol=v[i]>vs[i]*1.2
        if e9[i]>e21[i] and c[i]>e9[i] and r14[i]<40 and r14[i]>r14[i-1] and vol: entries.append((i,1,a14[i]))
        elif e9[i]<e21[i] and c[i]<e9[i] and r14[i]>60 and r14[i]<r14[i-1] and vol: entries.append((i,-1,a14[i]))
    return entries

def combo_04(c, h, l, v):
    """MACD Histogram + RSI 45/55 + ATR"""
    n=len(c); mh=_macd_hist(c); r14=_rsi(c,14); a14=_atr(h,l,c,14); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        if mh[i]>0 and mh[i]>mh[i-1] and r14[i]<45 and r14[i]>r14[i-1]: entries.append((i,1,a14[i]))
        elif mh[i]<0 and mh[i]<mh[i-1] and r14[i]>55 and r14[i]<r14[i-1]: entries.append((i,-1,a14[i]))
    return entries

def combo_05(c, h, l, v):
    """SuperTrend flip + RSI + Volume"""
    n=len(c); st=_supertrend(h,l,c); r14=_rsi(c,14); a14=_atr(h,l,c,14); vs=_sma(v,20); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        vol=v[i]>vs[i]*1.1
        if st[i]==1 and st[i-1]==-1 and r14[i]<50 and vol: entries.append((i,1,a14[i]))
        elif st[i]==-1 and st[i-1]==1 and r14[i]>50 and vol: entries.append((i,-1,a14[i]))
    return entries

def combo_06(c, h, l, v):
    """BB Mean Reversion + RSI 30/70 + ATR"""
    n=len(c); bu,_,bl=_bb(c); r14=_rsi(c,14); a14=_atr(h,l,c,14); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        if c[i]<=bl[i] and r14[i]<30 and r14[i]>r14[i-1]: entries.append((i,1,a14[i]))
        elif c[i]>=bu[i] and r14[i]>70 and r14[i]<r14[i-1]: entries.append((i,-1,a14[i]))
    return entries

def combo_07(c, h, l, v):
    """ADX>25 + EMA 9/21 + RSI — Trend Strength Filter"""
    n=len(c); e9=_ema(c,9); e21=_ema(c,21); r14=_rsi(c,14); a14=_atr(h,l,c,14)
    adx=_adx(h,l,c,14); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        if adx[i]>25 and e9[i]>e21[i] and c[i]>e9[i] and r14[i]<40: entries.append((i,1,a14[i]))
        elif adx[i]>25 and e9[i]<e21[i] and c[i]<e9[i] and r14[i]>60: entries.append((i,-1,a14[i]))
    return entries

def combo_08(c, h, l, v):
    """VWAP + RSI + EMA21"""
    n=len(c); e21=_ema(c,21); r14=_rsi(c,14); a14=_atr(h,l,c,14); vw=_vwap(h,l,c,v); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        if c[i]>vw[i] and c[i]>e21[i] and r14[i]<40 and r14[i]>r14[i-1]: entries.append((i,1,a14[i]))
        elif c[i]<vw[i] and c[i]<e21[i] and r14[i]>60 and r14[i]<r14[i-1]: entries.append((i,-1,a14[i]))
    return entries

def combo_09(c, h, l, v):
    """Ichimoku Tenkan/Kijun + RSI + Volume"""
    n=len(c); ten,kij=_ichimoku(h,l,c); r14=_rsi(c,14); a14=_atr(h,l,c,14); vs=_sma(v,20); entries=[]
    for i in range(300,n):
        if a14[i]==0 or kij[i]==0: continue
        vol=v[i]>vs[i]*1.1
        if ten[i]>kij[i] and c[i]>ten[i] and r14[i]<45 and vol: entries.append((i,1,a14[i]))
        elif ten[i]<kij[i] and c[i]<ten[i] and r14[i]>55 and vol: entries.append((i,-1,a14[i]))
    return entries

def combo_10(c, h, l, v):
    """Fisher crossover + EMA 21/50 + ATR"""
    n=len(c); e21=_ema(c,21); e50=_ema(c,50); fs=_fisher(c); a14=_atr(h,l,c,14); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        if fs[i]==1 and fs[i-1]==-1 and e21[i]>e50[i] and c[i]>e21[i]: entries.append((i,1,a14[i]))
        elif fs[i]==-1 and fs[i-1]==1 and e21[i]<e50[i] and c[i]<e21[i]: entries.append((i,-1,a14[i]))
    return entries

def combo_11(c, h, l, v):
    """MFI<20/>80 + EMA 21/50 + Volume"""
    n=len(c); e21=_ema(c,21); e50=_ema(c,50); mf=_mfi(h,l,c,v); a14=_atr(h,l,c,14); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        if e21[i]>e50[i] and c[i]>e21[i] and mf[i]<20 and mf[i]>mf[i-1]: entries.append((i,1,a14[i]))
        elif e21[i]<e50[i] and c[i]<e21[i] and mf[i]>80 and mf[i]<mf[i-1]: entries.append((i,-1,a14[i]))
    return entries

def combo_12(c, h, l, v):
    """Stoch cross at 20/80 + EMA 21/50 + ATR"""
    n=len(c); e21=_ema(c,21); e50=_ema(c,50); sk,sd=_stoch(h,l,c); a14=_atr(h,l,c,14); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        bull=sk[i]>sd[i] and sk[i-1]<=sd[i-1] and sk[i]<20
        bear=sk[i]<sd[i] and sk[i-1]>=sd[i-1] and sk[i]>80
        if e21[i]>e50[i] and bull: entries.append((i,1,a14[i]))
        elif e21[i]<e50[i] and bear: entries.append((i,-1,a14[i]))
    return entries

def combo_13(c, h, l, v):
    """CCI<-100/>100 + EMA 21/50 + Volume"""
    n=len(c); e21=_ema(c,21); e50=_ema(c,50); ci=_cci(h,l,c); a14=_atr(h,l,c,14); vs=_sma(v,20); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        vol=v[i]>vs[i]*1.1
        if e21[i]>e50[i] and ci[i]<-100 and ci[i]>ci[i-1] and vol: entries.append((i,1,a14[i]))
        elif e21[i]<e50[i] and ci[i]>100 and ci[i]<ci[i-1] and vol: entries.append((i,-1,a14[i]))
    return entries

def combo_14(c, h, l, v):
    """HMA 50 + RSI 40/60 + ATR — Research: Sharpe 1.24"""
    n=len(c); h50=_hma(c,50); r14=_rsi(c,14); a14=_atr(h,l,c,14); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        if c[i]>h50[i] and h50[i]>h50[i-1] and r14[i]<40 and r14[i]>r14[i-1]: entries.append((i,1,a14[i]))
        elif c[i]<h50[i] and h50[i]<h50[i-1] and r14[i]>60 and r14[i]<r14[i-1]: entries.append((i,-1,a14[i]))
    return entries

def combo_15(c, h, l, v):
    """KAMA 21 + RSI + Volume"""
    n=len(c); k21=_kama(c,21); r14=_rsi(c,14); a14=_atr(h,l,c,14); vs=_sma(v,20); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        vol=v[i]>vs[i]*1.1
        if c[i]>k21[i] and k21[i]>k21[i-1] and r14[i]<40 and vol: entries.append((i,1,a14[i]))
        elif c[i]<k21[i] and k21[i]<k21[i-1] and r14[i]>60 and vol: entries.append((i,-1,a14[i]))
    return entries

def combo_16(c, h, l, v):
    """BB touch + MACD hist flip + Volume"""
    n=len(c); bu,_,bl=_bb(c); mh=_macd_hist(c); a14=_atr(h,l,c,14); vs=_sma(v,20); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        vol=v[i]>vs[i]*1.2
        if c[i]<=bl[i] and mh[i]>0 and mh[i]>mh[i-1] and vol: entries.append((i,1,a14[i]))
        elif c[i]>=bu[i] and mh[i]<0 and mh[i]<mh[i-1] and vol: entries.append((i,-1,a14[i]))
    return entries

def combo_17(c, h, l, v):
    """Triple EMA 9>21>50 + OBV > SMA + RSI"""
    n=len(c); e9=_ema(c,9); e21=_ema(c,21); e50=_ema(c,50); r14=_rsi(c,14); a14=_atr(h,l,c,14)
    ov=_obv(c,v); os_=_sma(ov,20); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        bull=e9[i]>e21[i]>e50[i] and ov[i]>os_[i]
        bear=e9[i]<e21[i]<e50[i] and ov[i]<os_[i]
        if bull and r14[i]<40 and r14[i]>r14[i-1]: entries.append((i,1,a14[i]))
        elif bear and r14[i]>60 and r14[i]<r14[i-1]: entries.append((i,-1,a14[i]))
    return entries

def combo_18(c, h, l, v):
    """SuperTrend + RSI + BB Squeeze (low BB width = volatility contraction)"""
    n=len(c); st=_supertrend(h,l,c); r14=_rsi(c,14); a14=_atr(h,l,c,14)
    bu,bm,bl=_bb(c); bw=(bu-bl)/bm*100; bws=_sma(bw,20); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        if st[i]==1 and st[i-1]==-1 and r14[i]<50: entries.append((i,1,a14[i]))
        elif st[i]==-1 and st[i-1]==1 and r14[i]>50: entries.append((i,-1,a14[i]))
    return entries

def combo_19(c, h, l, v):
    """EMA 21 + VWAP + RSI + OBV — 4 confirmations"""
    n=len(c); e21=_ema(c,21); r14=_rsi(c,14); a14=_atr(h,l,c,14); vw=_vwap(h,l,c,v)
    ov=_obv(c,v); os_=_sma(ov,20); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        above=c[i]>e21[i] and c[i]>vw[i] and ov[i]>os_[i]
        below=c[i]<e21[i] and c[i]<vw[i] and ov[i]<os_[i]
        if above and r14[i]<40 and r14[i]>r14[i-1]: entries.append((i,1,a14[i]))
        elif below and r14[i]>60 and r14[i]<r14[i-1]: entries.append((i,-1,a14[i]))
    return entries

def combo_20(c, h, l, v):
    """Trend Bounce Pro: EMA 21/50 + RSI + Vol spike + ATR expanding"""
    n=len(c); e21=_ema(c,21); e50=_ema(c,50); r14=_rsi(c,14); a14=_atr(h,l,c,14)
    vs=_sma(v,20); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        atr_exp=a14[i]>a14[i-1]*1.1; vol_spike=v[i]>vs[i]*1.3
        if e21[i]>e50[i] and c[i]>e21[i] and r14[i]<40 and r14[i]>r14[i-1] and vol_spike and atr_exp: entries.append((i,1,a14[i]))
        elif e21[i]<e50[i] and c[i]<e21[i] and r14[i]>60 and r14[i]<r14[i-1] and vol_spike and atr_exp: entries.append((i,-1,a14[i]))
    return entries

COMBOS = {
    "01: EMA9/21+RSI14+ATR": combo_01,
    "02: LevX Pro v3": combo_02,
    "03: EMA9/21+RSI+Vol": combo_03,
    "04: MACD+RSI+ATR": combo_04,
    "05: SuperTrend+RSI+Vol": combo_05,
    "06: BB+RSI+ATR": combo_06,
    "07: ADX+EMA9/21+RSI": combo_07,
    "08: VWAP+RSI+EMA21": combo_08,
    "09: Ichimoku+RSI+Vol": combo_09,
    "10: Fisher+EMA21/50+ATR": combo_10,
    "11: MFI+EMA21/50+Vol": combo_11,
    "12: Stoch+EMA21/50+ATR": combo_12,
    "13: CCI+EMA21/50+Vol": combo_13,
    "14: HMA50+RSI+ATR": combo_14,
    "15: KAMA21+RSI+Vol": combo_15,
    "16: BB+MACD+Vol": combo_16,
    "17: TripleEMA+OBV+ATR": combo_17,
    "18: SuperTrend+RSI+BBsq": combo_18,
    "19: EMA21+VWAP+RSI+OBV": combo_19,
    "20: TrendBounce Pro": combo_20,
}


def main():
    cache = _load_cache("BTC-USDT", "5m")
    if not cache: print("No cache"); return
    arr = np.array(cache, dtype=object)
    c = arr[:, 4].astype(float); h = arr[:, 2].astype(float)
    l = arr[:, 3].astype(float); v = arr[:, 5].astype(float)
    days = len(c) // 288

    print(f"{'='*85}")
    print(f" INDICATOR COMBINATION TESTING — {days} days 5m ({len(c)} bars)")
    print(f" Fee: 0.05% | Leverage: 1x | Starting: $10,000")
    print(f"{'='*85}")

    results = []
    for name, fn in COMBOS.items():
        t0 = time.time()
        entries = fn(c, h, l, v)
        dt = time.time() - t0
        r = run_bt(c, h, l, entries, cap=10000, bars_between=1000)
        ann = r["ret"] * 365 / days
        results.append((name, r, len(entries), ann, dt))

    results.sort(key=lambda x: x[1]["ret"] / max(x[1]["dd"], 0.1), reverse=True)

    print(f"\n{'Name':<30} {'Ret%':>7} {'T':>4} {'WR%':>6} {'PF':>6} {'DD%':>6} {'Ann%':>7} {'R/DD':>5}")
    print(f"{'-'*85}")
    for name, r, ne, ann, dt in results:
        rdd = r["ret"] / max(r["dd"], 0.1)
        marker = " <<<" if ann >= 40 else ""
        print(f"{name:<30} {r['ret']:>+6.1f}% {r['trades']:>4} {r['wr']:>5.1f}% {r['pf']:>6.2f} {r['dd']:>5.1f}% {ann:>+6.1f}% {rdd:>5.1f}{marker}")

    print(f"\n{'='*85}")
    print(f" WALK-FORWARD VALIDATION (3-fold) — TOP 5")
    print(f"{'='*85}")

    kf = 3; sz = len(c) // kf
    for name, r, ne, ann, dt in results[:5]:
        fn = COMBOS[name]; folds = []
        for k in range(kf):
            s = k*sz; e = min((k+1)*sz, len(c))
            fe = fn(c[s:e], h[s:e], l[s:e], v[s:e])
            r_ = run_bt(c[s:e], h[s:e], l[s:e], fe, cap=10000, bars_between=1000)
            f_ann = r_["ret"]*365/((e-s)//288)
            folds.append((r_["ret"], r_["wr"], r_["pf"], r_["dd"], f_ann, r_["trades"]))

        avg_ret = np.mean([f[0] for f in folds]); avg_wr = np.mean([f[1] for f in folds])
        avg_pf = np.mean([f[2] for f in folds]); avg_dd = np.max([f[3] for f in folds])
        avg_ann = np.mean([f[4] for f in folds]); all_pos = all(f[0] > 0 for f in folds)
        status = "PASS" if all_pos and avg_pf > 1.0 else "FAIL"

        print(f"\n  {name}")
        print(f"  Full: Ret={r['ret']:+.1f}% WR={r['wr']:.1f}% PF={r['pf']:.2f} DD={r['dd']:.1f}%")
        for k_, f in enumerate(folds):
            print(f"    Fold {k_+1}: Ret={f[0]:>+6.1f}% WR={f[1]:>5.1f}% PF={f[2]:>5.2f} DD={f[3]:>5.1f}% T={f[5]}")
        print(f"  >>> OOS: Ret={avg_ret:+.1f}% WR={avg_wr:.1f}% PF={avg_pf:.2f} DD={avg_dd:.1f}% Ann={avg_ann:+.1f}% [{status}]")


if __name__ == "__main__":
    main()
