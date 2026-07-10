import numpy as np
import pandas as pd
import sys
sys.path.insert(0, '.')

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)

def calc_atr(df, n=14):
    h, l, c = df['High'], df['Low'], df['Close']
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

from backtest_rules import load_candles, load_rules, compute_indicators

print("Loading 4H candles...")
rules = load_rules()
df4 = load_candles('BTCUSDT', '4H')
df4 = compute_indicators(df4, rules)
print(f"  {len(df4)} 4H candles loaded")

# ── TrendJoin 4H (3x) ──
cap = 5000; pos = None
e20 = df4['EMA_20'].values; e50 = df4['EMA_50'].values
rv = df4['RSI_14'].values; av = df4['ADX_14'].values
atr4 = df4['ATR_14'].values; hi = df4['High'].values; lo = df4['Low'].values
cl = df4['Close'].values
trades4 = 0; wins4 = 0; total_pnl4 = 0; peak4 = cap; max_dd4 = 0

for i in range(1, len(df4)):
    if pos:
        if lo[i] <= pos[0] or hi[i] >= pos[1]:
            px = pos[0] if lo[i] <= pos[0] else pos[1]
            pnl = (px - pos[2]) * pos[3] * 3 - px * pos[3] * 3 * 0.0005
            cap += pnl; total_pnl4 += pnl; trades4 += 1
            if pnl > 0: wins4 += 1
            pos = None
    if pos is None and cap > 0:
        e2, e5, r, a, at = e20[i-1], e50[i-1], rv[i-1], av[i-1], atr4[i-1]
        if not (np.isnan(e2) or np.isnan(e5) or np.isnan(a)):
            bull = e2 > e5 and a > 20 and not np.isnan(r) and r > 50
            if bull:
                dist = (cl[i-1] - e2) / e2 * 100 if e2 > 0 else 0
                if e2 > e5 and -3.0 < dist < 2.5 and (not np.isnan(r) and r > 30) and a > 18:
                    if not np.isnan(at) and at > 0:
                        entry = cl[i]
                        sl_p = entry - 2.0 * at
                        tp_p = entry + 4.5 * at
                        rv2 = entry - sl_p
                        sz = (cap * 0.02) / rv2 if rv2 > 0 else 0
                        if sz > 0:
                            cap -= entry * sz * 3 * 0.0005
                            pos = (sl_p, tp_p, entry, sz)
    peak4 = max(peak4, cap)
    dd4 = (peak4 - cap) / peak4 * 100
    max_dd4 = max(max_dd4, dd4)

tj_final = cap
print("  TrendJoin 4H done")

# ── MeanRev 15m (3x) ──
print("Loading 15m candles...")
df15 = pd.read_csv('data/candles/BTCUSDT_15m.csv')
df15['ts'] = pd.to_datetime(df15['ts'])
for c in ['Open','High','Low','Close','Volume']:
    df15[c] = df15[c].astype(float)
df15['RSI'] = rsi(df15['Close'], 14)
df15['EMA200'] = ema(df15['Close'], 200)
df15['ATR'] = calc_atr(df15, 14)
print(f"  {len(df15)} 15m candles loaded")

cap2 = 5000; pos2 = None
r15 = df15['RSI'].values; e200 = df15['EMA200'].values
at15 = df15['ATR'].values; cl15 = df15['Close'].values
hi15 = df15['High'].values; lo15 = df15['Low'].values
trades15 = 0; wins15 = 0; peak15 = cap2; max_dd15 = 0
step = 4

for i in range(1, len(df15), step):
    if pos2:
        sl_p = pos2[0] - 2.0 * pos2[1]
        tp_p = pos2[0] + 3.0 * pos2[1]
        exit_rsi = not np.isnan(r15[i]) and r15[i] > 55
        if lo15[i] <= sl_p or hi15[i] >= tp_p or exit_rsi:
            px = sl_p if lo15[i] <= sl_p else (tp_p if hi15[i] >= tp_p else cl15[i])
            pnl = (px - pos2[0]) * pos2[2] * 3 - px * pos2[2] * 3 * 0.0005
            cap2 += pnl; trades15 += 1
            if pnl > 0: wins15 += 1
            pos2 = None
    if pos2 is None and cap2 > 0:
        if not np.isnan(r15[i-1]) and not np.isnan(e200[i-1]) and e200[i-1] > 0:
            if r15[i-1] < 30 and cl15[i-1] > e200[i-1]:
                at = at15[i]
                if not np.isnan(at) and at > 0:
                    entry = cl15[i]
                    rv3 = 2.0 * at
                    sz = (cap2 * 0.02) / rv3 if rv3 > 0 else 0
                    if sz > 0:
                        cap2 -= entry * sz * 3 * 0.0005
                        pos2 = (entry, at, sz)
    peak15 = max(peak15, cap2)
    dd15 = (peak15 - cap2) / peak15 * 100
    max_dd15 = max(max_dd15, dd15)

mr_final = cap2
print("  MeanRev 15m done")

# ── Stats ──
years_tj = len(df4) * 4 / (365.25 * 24)
years_mr = len(df15) * 15 / (365.25 * 24 * 60)
cagr_tj = ((tj_final / 5000) ** (1/max(years_tj, 0.1)) - 1) * 100
cagr_mr = ((mr_final / 5000) ** (1/max(years_mr, 0.1)) - 1) * 100
combined_final = tj_final + mr_final
total_ret = (combined_final - 10000) / 10000 * 100
wr4 = wins4 / trades4 * 100 if trades4 > 0 else 0
wr15 = wins15 / trades15 * 100 if trades15 > 0 else 0

print("")
print("=" * 65)
print("  PORTFOLIO: TrendJoin 4H (3x) + MeanRev 15m (3x)")
print("  Capital: $5,000 each = $10,000 total")
print("=" * 65)
print("")
print("  Strategy        Period     Capital   Return     Final      Trades  WR")
print("  " + "-" * 60)
ret4 = ((tj_final / 5000) - 1) * 100
ret15 = ((mr_final / 5000) - 1) * 100
print(f"  TrendJoin 4H   {years_tj:5.1f} yrs   $5,000   {ret4:>+7.1f}%   ${tj_final:>8,.0f}     {trades4:>4d}   {wr4:.0f}%")
print(f"  MeanRev 15m    {years_mr:5.1f} yrs   $5,000   {ret15:>+7.1f}%   ${mr_final:>8,.0f}     {trades15:>4d}   {wr15:.0f}%")
print("  " + "-" * 60)
print(f"  COMBINED                     $10,000   {total_ret:>+7.1f}%   ${combined_final:>8,.0f}")
print("")
print(f"  CAGR (TrendJoin):    {cagr_tj:+.1f}%")
print(f"  CAGR (MeanRev):      {cagr_mr:+.1f}%")
print(f"  MaxDD (TrendJoin):   {max_dd4:.1f}%")
print(f"  MaxDD (MeanRev):     {max_dd15:.1f}%")
print("")
print("  Comparison:")
print(f"    TrendJoin only 1x:  CAGR +3.7%   MaxDD 24.7%")
print(f"    TrendJoin only 3x:  CAGR +8.9%   MaxDD ~59%")
print(f"    Portfolio 3x+3x:    CAGR ???%    MaxDD ???%")
print("=" * 65)
