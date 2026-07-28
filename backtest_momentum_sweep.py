#!/usr/bin/env python3
"""Ultra-fast momentum sweep — minimal grid."""
import httpx, pandas as pd, numpy as np, time, itertools

IC = 10_000
SYMS = ['BTC-USDT','ETH-USDT','BNB-USDT','SOL-USDT']

def fetch(inst_id):
    ac,a=[], ''
    while len(ac)<1100:
        p={'instId':inst_id,'bar':'1D','limit':'300'}
        if a: p['after']=a
        r=httpx.get('https://www.okx.com/api/v5/market/candles',params=p,timeout=15).json()
        if r.get('code')!='0' or not r.get('data'): break
        ac.extend(r['data']); a=r['data'][-1][0]
        if len(r['data'])<300: break
        time.sleep(0.15)
    df=pd.DataFrame(ac,columns=['ts','O','H','L','C','V','x1','x2','x3'])
    df['ts']=pd.to_datetime(df['ts'].astype(int),unit='ms')
    df.set_index('ts',inplace=True)
    return df[['O','H','L','C','V']].astype(float).sort_index().rename(columns={'O':'Open','H':'High','L':'Low','C':'Close','V':'Volume'})

def precomp(raw):
    out={}
    for s,df in raw.items():
        d={'C':df['Close'].values,'H':df['High'].values,'L':df['Low'].values,'V':df['Volume'].values}
        c=df['Close'].values
        for p in [5,10,14,20,30,50]:
            roc=np.full_like(c,np.nan); roc[p:]=c[p:]/c[:-p]-1
            d[f'r{p}']=roc*100
        for p in [15,21,30,40,55,70]:
            d[f'e{p}']=df['Close'].ewm(span=p,adjust=False).mean().values
        h=df['High'].values; lo=df['Low'].values
        tr=np.maximum(h-lo,np.maximum(np.abs(h-np.roll(c,1)),np.abs(lo-np.roll(c,1)))); tr[0]=h[0]-lo[0]
        av=pd.Series(tr).ewm(span=14,adjust=False).mean().values
        d['atr']=av
        up=h-np.roll(h,1); up[0]=0; dn=np.roll(lo,1)-lo; dn[0]=0
        pdm=np.where((up>dn)&(up>0),up,0); mdm=np.where((dn>up)&(dn>0),dn,0)
        pdi=100*pd.Series(pdm).ewm(span=14,adjust=False).mean().values/av
        mdi=100*pd.Series(mdm).ewm(span=14,adjust=False).mean().values/av
        dx=100*np.abs(pdi-mdi)/np.where((pdi+mdi)==0,np.nan,pdi+mdi)
        d['adx']=pd.Series(dx).ewm(span=14,adjust=False).mean().values
        d['pdi']=pdi; d['mdi']=mdi
        d['vma']=pd.Series(df['Volume'].values).rolling(20).mean().values
        d['idx_map']=df.index
        out[s]=d
    return out

def bt(pc, dates_map, rf,rm,ef,es,ast,atg,adxt,mm):
    eq=IC; pos={s:None for s in SYMS}; cd={s:0 for s in SYMS}
    trades=[]; btc_dates=dates_map['BTC-USDT']
    for i,date in enumerate(btc_dates):
        for s in SYMS:
            if s not in dates_map: continue
            # find index
            idx_map=dates_map[s]
            try:
                j=np.searchsorted(idx_map,date)
                if j>=len(idx_map) or idx_map[j]!=date: continue
            except: continue
            if j<1: continue
            p=pc[s]; ps=pos[s]
            if ps:
                rd=ps['e']-ps['s']
                if rd>0:
                    pk=max(ps['pk'],(p['H'][j]-ps['e'])/rd); ps['pk']=pk
                    if pk>=1: ps['s']=max(ps['s'],ps['e'])
                    if pk>=2: ps['s']=max(ps['s'],ps['e']+pk*rd*0.5)
                    if pk>=3: ps['s']=max(ps['s'],p['C'][j]-ps['a']*1.5)
                if p['L'][j]<=ps['s']:
                    pnl=ps['sh']*(ps['s']-ps['e'])-(ps['sh']*ps['e']+ps['sh']*ps['s'])*.001
                    eq+=pnl; trades.append(pnl)
                    if pnl<0: cd[s]=3
                    pos[s]=None
                elif p['H'][j]>=ps['t']:
                    pnl=ps['sh']*(ps['t']-ps['e'])-(ps['sh']*ps['e']+ps['sh']*ps['t'])*.001
                    eq+=pnl; trades.append(pnl); pos[s]=None
            if pos[s] is None and j>0:
                if sum(1 for v in pos.values() if v)>=3: continue
                if cd[s]>0: cd[s]-=1; continue
                pi=j-1
                rf_v=p[f'r{rf}'][pi]; rm_v=p[f'r{rm}'][pi]
                ef_v=p[f'e{ef}'][pi]; es_v=p[f'e{es}'][pi]
                ad=p['adx'][pi]; pdi_v=p['pdi'][pi]; mdi_v=p['mdi'][pi]
                at=p['atr'][pi]
                vm=p['vma'][pi]
                vr=p['V'][pi]/vm if vm>0 else 0
                if np.isnan(rf_v) or np.isnan(rm_v) or np.isnan(ef_v) or np.isnan(es_v) or np.isnan(at) or at<=0: continue
                ms=rf_v*0.5+rm_v*0.5
                if rf_v>0 and rm_v>0 and ms>mm and ef_v>es_v and ad>adxt and pdi_v>mdi_v and vr>0.8:
                    ep=p['C'][j]; st=ep-ast*at; tg=ep+atg*at; rk=ep-st
                    if rk<=0: continue
                    sh=(eq*.02)/rk; mx=(eq*.25)/ep; sh=min(sh,mx)
                    if sh<=0: continue
                    pos[s]={'e':ep,'s':st,'t':tg,'sh':sh,'pk':0,'a':at}
    if not trades: return None
    n=len(trades); w=len([t for t in trades if t>0])
    yrs=(pd.Timestamp(btc_dates[-1])-pd.Timestamp(btc_dates[0])).days/365.25
    cagr=((eq/IC)**(1/yrs)-1)*100
    return {'cagr':cagr,'n':n,'wr':w/n*100,'pnl':sum(trades),'final':eq}

if __name__=='__main__':
    print("Fetching..."); raw={}
    for s in SYMS:
        raw[s]=fetch(s); time.sleep(0.2); print(f"  {s}: {len(raw[s])}")
    st=max(d.index[0] for d in raw.values()); en=min(d.index[-1] for d in raw.values())
    for s in raw: raw[s]=raw[s].loc[st:en]
    print(f"Range: {st.date()} to {en.date()}")
    
    pc=precomp(raw)
    dates_map={s:raw[s].index.values for s in SYMS}
    
    grid={'rf':[5,10,20],'rm':[20,30,50],'ef':[15,21,30],'es':[40,55,70],
          'ast':[1.5,2.0,2.5],'atg':[3.0,4.0,5.0],'adxt':[15,25],'mm':[1,3,5]}
    keys=list(grid.keys()); combos=list(itertools.product(*grid.values()))
    total=sum(1 for c in combos if dict(zip(keys,c))['rf']<dict(zip(keys,c))['rm'] and 
              dict(zip(keys,c))['ef']<dict(zip(keys,c))['es'] and 
              dict(zip(keys,c))['atg']>dict(zip(keys,c))['ast'])
    print(f"\n{total} valid combos")
    
    t0=time.time(); results=[]; cnt=0
    for c in combos:
        p=dict(zip(keys,c))
        if p['rf']>=p['rm'] or p['ef']>=p['es'] or p['atg']<=p['ast']: continue
        cnt+=1
        r=bt(pc,dates_map,p['rf'],p['rm'],p['ef'],p['es'],p['ast'],p['atg'],p['adxt'],p['mm'])
        if r and r['n']>=10: r['params']=p; results.append(r)
        if cnt%100==0: print(f"  {cnt}/{total} ({len(results)} valid) [{time.time()-t0:.0f}s]",flush=True)
    
    print(f"\nDone in {time.time()-t0:.0f}s")
    if not results: print("No results!"); exit(1)
    
    profitable=[r for r in results if r['cagr']>0]
    bs=sorted(results,key=lambda x:x['cagr'],reverse=True)
    bp=sorted(profitable,key=lambda x:x['cagr'],reverse=True) if profitable else bs
    
    print(f"\n{'='*72}")
    print(f"  {len(results)} configs tested, {len(profitable)} profitable ({len(profitable)/len(results)*100:.0f}%)")
    print(f"  Period: {st.date()} to {en.date()} ({(en-st).days/365:.1f}y)")
    print(f"{'='*72}")
    
    for label,rank in [("ALL: TOP 10 BY CAGR",bs),("PROFITABLE: TOP 10",bp)]:
        print(f"\n  {label}:")
        print(f"  {'#':>2} {'CAGR':>8} {'Win%':>6} {'#Tr':>5} {'PnL':>10} {'Final':>12}")
        print(f"  {'-'*55}")
        for i,r in enumerate(rank[:10]):
            p=r['params']
            print(f"  {i+1:2d} {r['cagr']:+7.1f}% {r['wr']:5.1f}% {r['n']:5d} ${r['pnl']:+9,.0f} ${r['final']:11,.0f}")
            print(f"     ROC={p['rf']}/{p['rm']} EMA={p['ef']}/{p['es']} ATR={p['ast']}/{p['atg']} ADX>{p['adxt']} Mom>{p['mm']}")
    
    cagrs=[r['cagr'] for r in results]
    print(f"\n  CAGR distribution: min={min(cagrs):+.1f}% med={np.median(cagrs):+.1f}% max={max(cagrs):+.1f}%")
