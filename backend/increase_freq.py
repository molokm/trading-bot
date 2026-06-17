"""INCREASE TRADE FREQUENCY — relaxed LevX + multi-mode + 15m + lower fees"""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))
import numpy as np, pandas as pd
from app.services.data_cache import _load_cache
from scalping_strategy import downsample_5m_to_15m
np.random.seed(42)

def _ema(a, s): return pd.Series(a).ewm(span=s, adjust=False).mean().values
def _sma(a, s): return pd.Series(a).rolling(s).mean().values
def _rsi(c, p=14):
    d = pd.Series(c).diff(); g = d.where(d > 0, 0.0).rolling(p).mean().values
    l = (-d.where(d < 0, 0.0)).rolling(p).mean().values; r = np.full(len(c), 50.0)
    for i in range(p, len(c)):
        r[i] = 0.0 if l[i] == 0 else 100.0 - 100.0 / (1.0 + g[i] / l[i])
    return r
def _atr(h, l, c, p=14):
    tr = np.maximum(h[1:]-l[1:], np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
    return np.insert(pd.Series(tr).rolling(p).mean().values, 0, 0)
def _adx(h, l, c, p=14):
    n=len(c); pdm=np.zeros(n); mdm=np.zeros(n); tr=np.zeros(n)
    for i in range(1,n):
        up=h[i]-h[i-1]; dn=l[i-1]-l[i]
        pdm[i]=up if(up>dn and up>0) else 0; mdm[i]=dn if(dn>up and dn>0) else 0
        tr[i]=max(h[i]-l[i],max(abs(h[i]-c[i-1]),abs(l[i]-c[i-1])))
    at=pd.Series(tr).rolling(p).mean().values
    pdi=np.where(at>0,pd.Series(pdm).rolling(p).mean().values/at*100,0)
    mdi=np.where(at>0,pd.Series(mdm).rolling(p).mean().values/at*100,0)
    s=pdi+mdi; dx=np.where(s>0,np.abs(pdi-mdi)/s*100,0)
    return pd.Series(dx).rolling(p).mean().values
def _bb(c, p=20, s=2.0):
    m=_sma(c,p); st=pd.Series(c).rolling(p).std().values; return m+s*st,m,m-s*st
def _obv(c, v):
    r=np.zeros(len(c))
    for i in range(1,len(c)):
        if c[i]>c[i-1]: r[i]=r[i-1]+v[i]
        elif c[i]<c[i-1]: r[i]=r[i-1]-v[i]
        else: r[i]=r[i-1]
    return r
def _vwap(h, l, c, v):
    tp=(h+l+c)/3; ctv=np.cumsum(tp*v); cv=np.cumsum(v)
    return np.where(cv>0,ctv/cv,c)
def _stoch(c, l, h, k=14, d=3):
    n=len(c); sk=np.full(n,50.0)
    for i in range(k-1,n):
        hh=np.max(h[i-k+1:i+1]); ll=np.min(l[i-k+1:i+1])
        sk[i]=50.0 if hh==ll else (c[i]-ll)/(hh-ll)*100
    return sk,_sma(sk,d)
def _macd_hist(c):
    return (_ema(c,12)-_ema(c,26))-_ema(_ema(c,12)-_ema(c,26),9)
def _cci(h, l, c, p=20):
    tp=(h+l+c)/3; m=_sma(tp,p)
    md=pd.Series(tp).rolling(p).apply(lambda x:np.mean(np.abs(x-np.mean(x))),raw=True).values
    return np.where(md>0,(tp-m)/(0.015*md),0)
def _hl_struct(h, l, n, period=20):
    hh=np.zeros(n);hl_=np.zeros(n);lh_=np.zeros(n);ll_=np.zeros(n)
    for i in range(period,n):
        hs=h[i-period:i+1];ls=l[i-period:i+1]
        hh[i]=np.sum(np.diff(hs)>0);hl_[i]=np.sum(np.diff(ls)>0)
        lh_[i]=np.sum(np.diff(hs)<0);ll_[i]=np.sum(np.diff(ls)<0)
    return hh,hl_,lh_,ll_


def run_bt(c, h, l, entries, cap=10000, fee=0.0005, lev=1.0,
           atr_sl=1.5, atr_tp=None, atr_lock=4.0, partial_pct=0.3, partial_mult=1.5,
           bars_between=100, use_trailing=True, use_partial=True):
    n=len(c); a=_atr(h,l,c,14); bal=float(cap)
    pos=0;ep=0.;sl=0.;tp=0.;ps=0.;eb=-999;pd_done=False;trades=[]
    ed={b:(s,ea) for b,s,ea in entries}; eq=np.full(n,cap)
    for i in range(n):
        ca=a[i] if not np.isnan(a[i]) else 0
        if pos>0 and ca>0:
            if use_trailing:
                if c[i]>ep+ca*atr_lock: sl=max(sl,ep+ca*(atr_lock-0.5))
                else: sl=max(sl,c[i]-ca*atr_sl)
            if atr_tp and tp>0 and c[i]>=tp:
                pnl=ps*(tp-ep)*lev-(ps*ep+ps*tp)*fee; bal+=pnl;trades.append(pnl);pos=0;eb=i;pd_done=False;eq[i]=bal;continue
            if use_partial and not pd_done and c[i]>ep+ca*partial_mult:
                psz=ps*partial_pct; pnl=psz*(c[i]-ep)*lev-(psz*ep+psz*c[i])*fee
                bal+=pnl;ps-=psz;pd_done=True;trades.append(pnl)
            if c[i]<sl:
                pnl=ps*(sl-ep)*lev-(ps*ep+ps*sl)*fee; bal+=pnl;trades.append(pnl);pos=0;eb=i;pd_done=False
        elif pos<0 and ca>0:
            if use_trailing:
                if c[i]<ep-ca*atr_lock: sl=min(sl,ep-ca*(atr_lock-0.5))
                else: sl=min(sl,c[i]+ca*atr_sl)
            if atr_tp and tp>0 and c[i]<=tp:
                pnl=ps*(ep-tp)*lev-(ps*ep+ps*tp)*fee; bal+=pnl;trades.append(pnl);pos=0;eb=i;pd_done=False;eq[i]=bal;continue
            if use_partial and not pd_done and c[i]<ep-ca*partial_mult:
                psz=ps*partial_pct; pnl=psz*(ep-c[i])*lev-(psz*ep+psz*c[i])*fee
                bal+=pnl;ps-=psz;pd_done=True;trades.append(pnl)
            if c[i]>sl:
                pnl=ps*(ep-sl)*lev-(ps*ep+ps*sl)*fee; bal+=pnl;trades.append(pnl);pos=0;eb=i;pd_done=False
        if pos!=0: eq[i]=bal+ps*(c[i]-ep)*pos*lev
        else: eq[i]=bal
        if pos!=0 or (i-eb<bars_between): continue
        if i in ed and ca>0:
            s,ea=ed[i]; ep=c[i]; ps=bal*0.95/ep; pos=s
            sl=ep-ea*atr_sl*s; tp=ep+ea*atr_tp*s if atr_tp else 0; pd_done=False
    if pos!=0:
        if pos>0: pnl=ps*(c[-1]-ep)*lev-(ps*ep+ps*c[-1])*fee
        else: pnl=ps*(ep-c[-1])*lev-(ps*ep+ps*c[-1])*fee
        bal+=pnl;trades.append(pnl)
    eq[-1]=bal
    nt=len(trades); wins=[t for t in trades if t>0]
    wr=len(wins)/nt*100 if nt else 0
    gp=sum(wins) if wins else 0; gl=abs(sum(t for t in trades if t<=0)) or 0.001
    pf=gp/gl; ret=(bal/cap-1)*100
    dd=((np.maximum.accumulate(eq)-eq)/np.maximum.accumulate(eq)*100).max()
    return {"ret":ret,"trades":nt,"wr":wr,"pf":pf,"dd":dd,"eq":eq}


def _filter_cd(entries, cooldown):
    if cooldown<=0: return entries
    f=[]; last=-cooldown-1
    for b,s,ea in entries:
        if b-last>=cooldown: f.append((b,s,ea)); last=b
    return f


def gen_levx_relaxed(c, h, l, v, cooldown=50):
    n=len(c); e40=_ema(c,40); e100=_ema(c,100); r14=_rsi(c,14); a14=_atr(h,l,c,14)
    entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        up=e40[i]>e100[i] and c[i]>e40[i]; dn=e40[i]<e100[i] and c[i]<e40[i]
        if up and r14[i]<30 and r14[i]>r14[i-1]: entries.append((i,1,a14[i]))
        elif dn and r14[i]>60 and r14[i]<r14[i-1]: entries.append((i,-1,a14[i]))
    return _filter_cd(entries, cooldown)


def gen_levx_fast(c, h, l, v, cooldown=20):
    n=len(c); e21=_ema(c,21); e50=_ema(c,50); r14=_rsi(c,14); a14=_atr(h,l,c,14)
    hh,hl_,lh_,ll_=_hl_struct(h,l,n); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        up=e21[i]>e50[i] and c[i]>e21[i]; dn=e21[i]<e50[i] and c[i]<e21[i]
        bull=hh[i]>ll_[i] and hl_[i]>lh_[i]; bear=ll_[i]>hh[i] and lh_[i]>hl_[i]
        if up and r14[i]<40 and r14[i]>r14[i-1] and bull: entries.append((i,1,a14[i]))
        elif dn and r14[i]>60 and r14[i]<r14[i-1] and bear: entries.append((i,-1,a14[i]))
    return _filter_cd(entries, cooldown)


def gen_mean_rev(c, h, l, v, cooldown=30):
    n=len(c); bu,_,bl=_bb(c); r14=_rsi(c,14); a14=_atr(h,l,c,14); sk,sd=_stoch(c,l,h)
    entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        if c[i]<=bl[i] and r14[i]<30 and sk[i]<20: entries.append((i,1,a14[i]))
        elif c[i]>=bu[i] and r14[i]>70 and sk[i]>80: entries.append((i,-1,a14[i]))
    return _filter_cd(entries, cooldown)


def gen_momentum(c, h, l, v, cooldown=30):
    n=len(c); e9=_ema(c,9); e21=_ema(c,21); e50=_ema(c,50)
    mh=_macd_hist(c); a14=_atr(h,l,c,14); ov=_obv(c,v); os_=_sma(ov,20)
    entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        if mh[i]>0 and mh[i-1]<=0 and e9[i]>e21[i]>e50[i] and v[i]>os_[i]*0.9:
            entries.append((i,1,a14[i]))
        elif mh[i]<0 and mh[i-1]>=0 and e9[i]<e21[i]<e50[i] and v[i]>os_[i]*0.9:
            entries.append((i,-1,a14[i]))
    return _filter_cd(entries, cooldown)


def gen_all_modes(c, h, l, v, cooldown=50):
    n=len(c)
    e9=_ema(c,9);e21=_ema(c,21);e40=_ema(c,40);e50=_ema(c,50);e100=_ema(c,100)
    r14=_rsi(c,14);a14=_atr(h,l,c,14);adx_v=_adx(h,l,c,14)
    bu,_,bl=_bb(c);ov=_obv(c,v);os_=_sma(ov,20);vs_=_sma(v,20)
    vw=_vwap(h,l,c,v);sk,sd=_stoch(c,l,h);mh=_macd_hist(c);ci=_cci(h,l,c)
    hh,hl_,lh_,ll_=_hl_struct(h,l,n); entries=[]
    for i in range(300,n):
        if a14[i]==0: continue
        up=e40[i]>e100[i] and c[i]>e40[i]; dn=e40[i]<e100[i] and c[i]<e40[i]
        bull=hh[i]>ll_[i] and hl_[i]>lh_[i]; bear=ll_[i]>hh[i] and lh_[i]>hl_[i]
        if up and r14[i]<30 and r14[i]>r14[i-1] and bull: entries.append((i,1,a14[i]))
        elif dn and r14[i]>60 and r14[i]<r14[i-1] and bear: entries.append((i,-1,a14[i]))
        if e9[i]>e21[i] and c[i]>e9[i]*0.998 and c[i]<e9[i]*1.002:
            if r14[i]<45 and r14[i]>r14[i-1] and v[i]>vs_[i]*0.8: entries.append((i,1,a14[i]))
        elif e9[i]<e21[i] and c[i]<e9[i]*1.002 and c[i]>e9[i]*0.998:
            if r14[i]>55 and r14[i]<r14[i-1] and v[i]>vs_[i]*0.8: entries.append((i,-1,a14[i]))
        if c[i]<=bl[i]*1.002 and r14[i]<35 and r14[i]>r14[i-1]: entries.append((i,1,a14[i]))
        elif c[i]>=bu[i]*0.998 and r14[i]>65 and r14[i]<r14[i-1]: entries.append((i,-1,a14[i]))
        if mh[i]>0 and mh[i-1]<=0 and e21[i]>e50[i] and r14[i]<60: entries.append((i,1,a14[i]))
        elif mh[i]<0 and mh[i-1]>=0 and e21[i]<e50[i] and r14[i]>40: entries.append((i,-1,a14[i]))
        dev=(c[i]-vw[i])/vw[i]*100
        if -0.4<dev<-0.15 and r14[i]<45 and r14[i]>r14[i-1]: entries.append((i,1,a14[i]))
        elif 0.15<dev<0.4 and r14[i]>55 and r14[i]<r14[i-1]: entries.append((i,-1,a14[i]))
        if sk[i]>sd[i] and sk[i-1]<=sd[i-1] and sk[i]<25: entries.append((i,1,a14[i]))
        elif sk[i]<sd[i] and sk[i-1]>=sd[i-1] and sk[i]>75: entries.append((i,-1,a14[i]))
        if ci[i]>-100 and ci[i-1]<=-100: entries.append((i,1,a14[i]))
        elif ci[i]<100 and ci[i-1]>=100: entries.append((i,-1,a14[i]))
        if e9[i]>e21[i]>e50[i] and c[i]>e9[i] and r14[i]<50 and r14[i]>r14[i-1]: entries.append((i,1,a14[i]))
        elif e9[i]<e21[i]<e50[i] and c[i]<e9[i] and r14[i]>50 and r14[i]<r14[i-1]: entries.append((i,-1,a14[i]))
    return _filter_cd(entries, cooldown)


def main():
    cache = _load_cache("BTC-USDT", "5m")
    if not cache: print("No cache"); return
    arr = np.array(cache, dtype=object)
    c5=arr[:,4].astype(float); h5=arr[:,2].astype(float)
    l5=arr[:,3].astype(float); v5=arr[:,5].astype(float)
    days5=len(c5)//288
    m15=downsample_5m_to_15m(cache)
    m15a=np.array(m15, dtype=object)
    c15=m15a[:,4].astype(float);h15=m15a[:,2].astype(float)
    l15=m15a[:,3].astype(float);v15=m15a[:,5].astype(float)
    days15=len(c15)//96

    print(f"{'='*95}")
    print(f" INCREASING TRADE FREQUENCY")
    print(f" 5m: {len(c5)} bars ({days5}d) | 15m: {len(c15)} bars ({days15}d)")
    print(f"{'='*95}")

    strats=[
        ("LevX Strict CD=1000", gen_levx_relaxed, {"cooldown":1000}, c5, h5, l5, v5, days5, 288),
        ("LevX Relaxed CD=50", gen_levx_relaxed, {"cooldown":50}, c5, h5, l5, v5, days5, 288),
        ("LevX Relaxed CD=20", gen_levx_relaxed, {"cooldown":20}, c5, h5, l5, v5, days5, 288),
        ("LevX Fast CD=20", gen_levx_fast, {"cooldown":20}, c5, h5, l5, v5, days5, 288),
        ("MeanRev CD=30", gen_mean_rev, {"cooldown":30}, c5, h5, l5, v5, days5, 288),
        ("Momentum CD=30", gen_momentum, {"cooldown":30}, c5, h5, l5, v5, days5, 288),
        ("AllModes CD=50", gen_all_modes, {"cooldown":50}, c5, h5, l5, v5, days5, 288),
        ("AllModes CD=20", gen_all_modes, {"cooldown":20}, c5, h5, l5, v5, days5, 288),
        ("AllModes CD=10", gen_all_modes, {"cooldown":10}, c5, h5, l5, v5, days5, 288),
        ("AllModes CD=5", gen_all_modes, {"cooldown":5}, c5, h5, l5, v5, days5, 288),
        ("LevX Relaxed 15m CD=20", gen_levx_relaxed, {"cooldown":20}, c15, h15, l15, v15, days15, 96),
        ("LevX Fast 15m CD=10", gen_levx_fast, {"cooldown":10}, c15, h15, l15, v15, days15, 96),
        ("AllModes 15m CD=10", gen_all_modes, {"cooldown":10}, c15, h15, l15, v15, days15, 96),
        ("AllModes 15m CD=5", gen_all_modes, {"cooldown":5}, c15, h15, l15, v15, days15, 96),
        ("MeanRev 15m CD=10", gen_mean_rev, {"cooldown":10}, c15, h15, l15, v15, days15, 96),
        ("Momentum 15m CD=10", gen_momentum, {"cooldown":10}, c15, h15, l15, v15, days15, 96),
    ]

    print(f"\n{'Strategy':<28} {'Fee':>5} {'TP':>5} {'Ret%':>7} {'T':>5} {'T/mo':>5} {'WR%':>6} {'PF':>6} {'DD%':>6} {'Ann%':>7}")
    print(f"{'-'*95}")
    all_results=[]

    for name, gen_fn, kwargs, c_, h_, l_, v_, days_, bpd in strats:
        entries=gen_fn(c_, h_, l_, v_, **kwargs)
        nent=len(entries)
        for fl, fv in [("0.05%",0.0005),("0.02%",0.0002)]:
            best=None
            for sl_m in [1.0,1.5,2.0]:
                for tp_m in [None,1.0,1.5,2.0]:
                    for bb in [0,3,5,10]:
                        r=run_bt(c_,h_,l_,entries,cap=10000,fee=fv,atr_sl=sl_m,atr_tp=tp_m,
                                bars_between=bb,use_trailing=(tp_m is None),use_partial=(tp_m is None))
                        tpm=r["trades"]/days_*30 if days_>0 else 0
                        ann=r["ret"]*365/days_ if days_>0 else 0
                        if r["trades"]<10: continue
                        score=r["ret"]/max(r["dd"],0.1)
                        if best is None or score>best[0]:
                            best=(score,r,sl_m,tp_m,tpm,ann,bb)
            if best:
                _,r,sl_m,tp_m,tpm,ann,bb=best
                all_results.append((name,gen_fn,kwargs,tp_m,r,tpm,ann,fl,sl_m,bb,c_,h_,l_,v_,days_,bpd))
                tpl=f"{tp_m:.1f}" if tp_m else "trail"
                print(f"{name:<28} {fl:>5} {tpl:>5} {r['ret']:>+6.1f}% {r['trades']:>5} {tpm:>4.1f} {r['wr']:>5.1f}% {r['pf']:>6.2f} {r['dd']:>5.1f}% {ann:>+6.1f}%")

    # Walk-forward top 5
    print(f"\n{'='*95}")
    print(f" WALK-FORWARD (3-fold) — TOP 5 by R/DD")
    print(f"{'='*95}")

    all_results.sort(key=lambda x: x[4]["ret"]/max(x[4]["dd"],0.1), reverse=True)
    shown=0
    for name,gen_fn,kwargs,tp_m,r,tpm,ann,fl,sl_m,bb,c_,h_,l_,v_,days_,bpd in all_results:
        if shown>=5: break
        if r["trades"]<15: continue
        kf=3; sz=len(c_)//kf; folds=[]
        for k in range(kf):
            s=k*sz; e=min((k+1)*sz,len(c_))
            fe=gen_fn(c_[s:e],h_[s:e],l_[s:e],v_[s:e],**kwargs)
            r_=run_bt(c_[s:e],h_[s:e],l_[s:e],fe,cap=10000,fee=0.0002 if fl=="0.02%" else 0.0005,
                     atr_sl=sl_m,atr_tp=tp_m,bars_between=bb,
                     use_trailing=(tp_m is None),use_partial=(tp_m is None))
            f_ann=r_["ret"]*365/((e-s)//bpd) if (e-s)//bpd>0 else 0
            folds.append((r_["ret"],r_["wr"],r_["pf"],r_["dd"],f_ann,r_["trades"]))
        avg_ret=np.mean([f[0] for f in folds]); avg_wr=np.mean([f[1] for f in folds])
        avg_pf=np.mean([f[2] for f in folds]); avg_dd=np.max([f[3] for f in folds])
        avg_ann=np.mean([f[4] for f in folds]); total_t=sum(f[5] for f in folds)
        all_pos=all(f[0]>0 for f in folds)
        status="PASS" if all_pos and avg_pf>1.0 else "FAIL"
        shown+=1
        print(f"\n  {name} [{fl}] TP={'trail' if not tp_m else tp_m}")
        for k_,f in enumerate(folds):
            print(f"    Fold {k_+1}: Ret={f[0]:>+6.1f}% WR={f[1]:>5.1f}% PF={f[2]:>5.2f} DD={f[3]:>5.1f}% T={f[5]}")
        print(f"  >>> OOS: Ret={avg_ret:+.1f}% WR={avg_wr:.1f}% PF={avg_pf:.2f} DD={avg_dd:.1f}% Ann={avg_ann:+.1f}% T={total_t} [{status}]")


if __name__=="__main__":
    main()
