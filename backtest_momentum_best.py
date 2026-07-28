#!/usr/bin/env python3
"""Detailed backtest of best momentum config from sweep."""
import httpx, pandas as pd, numpy as np, time

IC = 10_000
SYMS = ['BTC-USDT','ETH-USDT','BNB-USDT','SOL-USDT']
NAMES = {'BTC-USDT':'BTC','ETH-USDT':'ETH','BNB-USDT':'BNB','SOL-USDT':'SOL'}

# Best config from sweep
BEST = {'rf':20,'rm':50,'ef':15,'es':70,'ast':2.5,'atg':5.0,'adxt':25,'mm':3}

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

def enrich(df):
    d=df.copy()
    for p in [5,10,14,20,30,50,90]: d[f'ROC_{p}']=df['Close'].pct_change(p)*100
    for p in [15,21,30,40,55,70,100]: d[f'EMA_{p}']=df['Close'].ewm(span=p,adjust=False).mean()
    h,l,c=df['High'],df['Low'],df['Close']
    tr=pd.concat([(h-l),(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    d['ATR']=tr.ewm(span=14,adjust=False).mean()
    up=h-h.shift(1); dn=l.shift(1)-l
    pdm=np.where((up>dn)&(up>0),up,0); mdm=np.where((dn>up)&(dn>0),dn,0)
    av=tr.ewm(span=14,adjust=False).mean()
    pdi=100*pd.Series(pdm,index=df.index).ewm(span=14,adjust=False).mean()/av
    mdi=100*pd.Series(mdm,index=df.index).ewm(span=14,adjust=False).mean()/av
    dx=100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    d['ADX']=dx.ewm(span=14,adjust=False).mean()
    d['PDI']=pdi; d['MDI']=mdi
    d['Vol_MA']=df['Volume'].rolling(20).mean()
    return d

def run_detailed(data, p, label=""):
    rf,rm,ef,es,ast,atg,adxt,mm = p['rf'],p['rm'],p['ef'],p['es'],p['ast'],p['atg'],p['adxt'],p['mm']
    eq=IC; pos={s:None for s in data}; cd={s:0 for s in data}
    trades=[]; eqs=[]; by_sym={s:[] for s in SYMS}
    
    for date in data['BTC-USDT'].index:
        for s,df in data.items():
            if date not in df.index: continue
            idx=df.index.get_loc(date)
            if idx<1: continue
            row=df.iloc[idx]
            ps=pos[s]
            if ps:
                rd=ps['e']-ps['s']
                if rd>0:
                    pk=max(ps['pk'],(row['High']-ps['e'])/rd); ps['pk']=pk
                    if pk>=1: ps['s']=max(ps['s'],ps['e'])
                    if pk>=2: ps['s']=max(ps['s'],ps['e']+pk*rd*0.5)
                    if pk>=3: ps['s']=max(ps['s'],row['Close']-ps['a']*1.5)
                if row['Low']<=ps['s']:
                    pnl=ps['sh']*(ps['s']-ps['e'])-(ps['sh']*ps['e']+ps['sh']*ps['s'])*.001
                    eq+=pnl; trades.append({'sym':NAMES[s],'pnl':pnl,'reason':'stop','entry':ps['e'],'exit':ps['s'],'date':date})
                    by_sym[s].append(pnl)
                    if pnl<0: cd[s]=3
                    pos[s]=None
                elif row['High']>=ps['t']:
                    pnl=ps['sh']*(ps['t']-ps['e'])-(ps['sh']*ps['e']+ps['sh']*ps['t'])*.001
                    eq+=pnl; trades.append({'sym':NAMES[s],'pnl':pnl,'reason':'target','entry':ps['e'],'exit':ps['t'],'date':date})
                    by_sym[s].append(pnl)
                    pos[s]=None
                elif row['Close']<ps['e'] and ps['pk']<0.5:
                    # Momentum exit: if we never got 0.5R and price drops below entry
                    pass  # Let it run to stop
            
            if pos[s] is None and idx>0:
                if sum(1 for v in pos.values() if v)>=3: continue
                if cd[s]>0: cd[s]-=1; continue
                prev=df.iloc[idx-1]
                rf_v=prev[f'ROC_{rf}']; rm_v=prev[f'ROC_{rm}']
                ef_v=prev[f'EMA_{ef}']; es_v=prev[f'EMA_{es}']
                ad=prev['ADX']; pdi_v=prev['PDI']; mdi_v=prev['MDI']
                at=prev['ATR']; vr=row['Volume']/prev['Vol_MA'] if prev['Vol_MA']>0 else 0
                if pd.isna(rf_v) or pd.isna(rm_v) or pd.isna(ef_v) or pd.isna(es_v) or pd.isna(at) or at<=0: continue
                ms=rf_v*0.5+rm_v*0.5
                if rf_v>0 and rm_v>0 and ms>mm and ef_v>es_v and ad>adxt and pdi_v>mdi_v and vr>0.8:
                    ep=row['Open']; st=ep-ast*at; tg=ep+atg*at; rk=ep-st
                    if rk<=0: continue
                    sh=(eq*.02)/rk; mx=(eq*.25)/ep; sh=min(sh,mx)
                    if sh<=0: continue
                    pos[s]={'e':ep,'s':st,'t':tg,'sh':sh,'pk':0,'a':at}
        eqs.append(eq)
    
    if not trades: return None
    
    eq_s=pd.Series(eqs,index=data['BTC-USDT'].index)
    dr=eq_s.pct_change().dropna()
    sharpe=(dr.mean()/dr.std()*np.sqrt(365)) if dr.std()>0 else 0
    pk=eq_s.cummax(); dd=((eq_s-pk)/pk).min()*100
    n=len(trades); wins=[t for t in trades if t['pnl']>0]
    pnls=[t['pnl'] for t in trades]
    winners=[p for p in pnls if p>0]; losers=[p for p in pnls if p<=0]
    yrs=(data['BTC-USDT'].index[-1]-data['BTC-USDT'].index[0]).days/365.25
    cagr=((eq/IC)**(1/yrs)-1)*100
    
    stops=[t for t in trades if t['reason']=='stop']
    targets=[t for t in trades if t['reason']=='target']
    
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  ROC={rf}/{rm} EMA={ef}/{es} ATR={ast}/{atg} ADX>{adxt} Mom>{mm}")
    print(f"{'='*60}")
    print(f"  Period: {data['BTC-USDT'].index[0].date()} to {data['BTC-USDT'].index[-1].date()} ({yrs:.1f}y)")
    print(f"  Capital: ${IC:,}  |  Final: ${eq:,.2f}")
    print(f"{'='*60}")
    print(f"  CAGR:       {cagr:+.2f}%")
    print(f"  Sharpe:     {sharpe:.2f}")
    print(f"  Max DD:     {dd:.2f}%")
    print(f"{'='*60}")
    print(f"  Trades:     {n}")
    print(f"  Win Rate:   {len(wins)/n*100:.1f}%")
    print(f"  Profit F:   {sum(winners)/abs(sum(losers)):.2f}" if losers else "  Profit F:   inf")
    print(f"  Targets:    {len(targets)}  |  Stops: {len(stops)}")
    print(f"  Avg Win:    ${np.mean(winners):.2f}" if winners else "")
    print(f"  Avg Loss:   ${abs(np.mean(losers)):.2f}" if losers else "")
    print(f"{'='*60}")
    print(f"  BY COIN:")
    for s in SYMS:
        sp=by_sym[s]
        if sp:
            sw=[p for p in sp if p>0]
            print(f"    {NAMES[s]:4s}  trades={len(sp):3d}  win={len(sw)/len(sp)*100:.0f}%  pnl=${sum(sp):+,.2f}")
    print(f"{'='*60}")
    
    monthly=eq_s.resample('ME').last().pct_change().dropna()
    print(f"\n  MONTHLY (last 12):")
    for dt,ret in monthly.tail(12).items():
        print(f"    {dt.strftime('%Y-%m')}  {ret*100:+6.2f}%")
    
    # Consecutive losing trades
    loss_streak=0; max_loss_streak=0
    for t in trades:
        if t['pnl']<=0: loss_streak+=1; max_loss_streak=max(max_loss_streak,loss_streak)
        else: loss_streak=0
    print(f"\n  Max loss streak: {max_loss_streak}")
    print()

if __name__=='__main__':
    print("Fetching..."); raw={}
    for s in SYMS:
        raw[s]=fetch(s); time.sleep(0.2); print(f"  {s}: {len(raw[s])}")
    st=max(d.index[0] for d in raw.values()); en=min(d.index[-1] for d in raw.values())
    for s in raw: raw[s]=raw[s].loc[st:en]
    
    data={s:enrich(raw[s]) for s in SYMS}
    
    # Best config
    run_detailed(data, BEST, "BEST MOMENTUM CONFIG (from 3888 sweep)")
