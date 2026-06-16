"""
Optimize G5 Selective strategy — grid search over parameters.
"""
import asyncio, sys
import pandas as pd
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def ema(arr, span):
    return pd.Series(arr).ewm(span=span, adjust=False).mean().values


def rsi(close, period=14):
    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean().values
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean().values
    r = np.full(len(close), 50.0)
    for i in range(period, len(close)):
        r[i] = 0.0 if loss[i] == 0 else 100.0 - 100.0 / (1.0 + gain[i] / loss[i])
    return r


def atr(high, low, close, period=14):
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    return np.insert(pd.Series(tr).rolling(period).mean().values, 0, 0)


def g5_run(close, high, low, cap, rsi_lo, rsi_hi, atr_sl, atr_tp,
           bars_per_trade, ema_fast, ema_slow):
    n = len(close)
    ema_f = ema(close, ema_fast)
    ema_s = ema(close, ema_slow)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)
    fee = 0.0005

    bal = cap
    pos = 0.0
    ep = 0.0
    sl = 0.0
    tp = 0.0
    ebar = -999
    trades = []

    for i in range(max(300, ema_slow * 2), n):
        if pos > 0:
            sl = max(sl, close[i] - atr14[i] * atr_sl)
            if close[i] >= tp or close[i] < sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue
        elif pos < 0:
            sl = min(sl, close[i] + atr14[i] * atr_sl)
            if close[i] <= tp or close[i] > sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue

        if i - ebar < bars_per_trade or pos != 0:
            continue

        uptrend = ema_f[i] > ema_s[i] and close[i] > ema_f[i]

        if uptrend and rsi14[i] < rsi_lo and rsi14[i] > rsi14[i-1]:
            ep = close[i]
            pos = bal * 0.95 / ep
            sl = ep - atr14[i] * atr_sl
            tp = ep + atr14[i] * atr_tp
            ebar = i
        elif not uptrend and ema_f[i] < ema_s[i] and close[i] < ema_f[i]:
            if rsi14[i] > rsi_hi and rsi14[i] < rsi14[i-1]:
                ep = close[i]
                pos = -bal * 0.95 / ep
                sl = ep + atr14[i] * atr_sl
                tp = ep - atr14[i] * atr_tp
                ebar = i

    if pos != 0:
        pnl = pos * (close[-1] - ep)
        fee_c = abs(pos) * (ep + close[-1]) * fee
        bal += pnl - fee_c
        trades.append(pnl - fee_c)

    if not trades:
        return None
    ret = (bal / cap - 1) * 100
    wins = sum(1 for t in trades if t > 0)
    wr = wins / len(trades) * 100
    gp = sum(t for t in trades if t > 0)
    gl = abs(sum(t for t in trades if t <= 0)) or 0.001
    pf = gp / gl
    eq = [cap]
    for t in trades:
        eq.append(eq[-1] + t)
    eq = np.array(eq)
    dd = ((np.maximum.accumulate(eq) - eq) / np.maximum.accumulate(eq) * 100).max()
    return {"ret": ret, "n": len(trades), "wr": wr, "pf": pf, "dd": dd}


async def main():
    from app.services.data_cache import _load_cache

    cache = _load_cache("BTC-USDT", "5m")
    if not cache:
        print("No 5m cache")
        return

    arr = np.array(cache, dtype=object)
    close = arr[:, 4].astype(float)
    high = arr[:, 2].astype(float)
    low = arr[:, 3].astype(float)

    print("Optimizing G5 Selective on 5m data...")
    print(f"Candles: {len(close)}")

    # Parameter grid
    results = []
    for rsi_lo in [25, 30, 35, 40]:
        for rsi_hi in [60, 65, 70, 75]:
            for atr_sl in [2.0, 2.5, 3.0, 3.5]:
                for atr_tp in [4.0, 5.0, 6.0, 8.0]:
                    for bars in [100, 200, 300, 400]:
                        for ef, es in [(20, 200), (30, 100), (50, 200)]:
                            s = g5_run(close, high, low, 1000,
                                      rsi_lo, rsi_hi, atr_sl, atr_tp,
                                      bars, ef, es)
                            if s and s["n"] >= 20:
                                results.append({
                                    "rsi_lo": rsi_lo, "rsi_hi": rsi_hi,
                                    "atr_sl": atr_sl, "atr_tp": atr_tp,
                                    "bars": bars, "ema_f": ef, "ema_s": es,
                                    **s
                                })

    # Sort by return
    results.sort(key=lambda x: x["ret"], reverse=True)

    print(f"\nTop 15 by return (of {len(results)} combos):")
    print(f"{'RSI_lo':>6} {'RSI_hi':>6} {'ATRsl':>5} {'ATRtp':>5} {'bars':>5} {'EMAf':>5} {'EMAs':>5} | "
          f"{'Trades':>6} {'Return':>8} {'WR':>6} {'PF':>5} {'MaxDD':>6}")
    for r in results[:15]:
        print(f"{r['rsi_lo']:>6} {r['rsi_hi']:>6} {r['atr_sl']:>5.1f} {r['atr_tp']:>5.1f} "
              f"{r['bars']:>5} {r['ema_f']:>5} {r['ema_s']:>5} | "
              f"{r['n']:>6} {r['ret']:>+7.1f}% {r['wr']:>5.1f}% {r['pf']:>5.2f} {r['dd']:>5.1f}%")

    # Also sort by Sharpe-like metric: return / drawdown
    if results:
        for r in results:
            r["ret_dd"] = r["ret"] / r["dd"] if r["dd"] > 0 else 0
        results.sort(key=lambda x: x["ret_dd"], reverse=True)
        print(f"\nTop 10 by Return/Drawdown ratio:")
        print(f"{'RSI_lo':>6} {'RSI_hi':>6} {'ATRsl':>5} {'ATRtp':>5} {'bars':>5} {'EMAf':>5} {'EMAs':>5} | "
              f"{'Trades':>6} {'Return':>8} {'WR':>6} {'PF':>5} {'MaxDD':>6} {'Ret/DD':>6}")
        for r in results[:10]:
            print(f"{r['rsi_lo']:>6} {r['rsi_hi']:>6} {r['atr_sl']:>5.1f} {r['atr_tp']:>5.1f} "
                  f"{r['bars']:>5} {r['ema_f']:>5} {r['ema_s']:>5} | "
                  f"{r['n']:>6} {r['ret']:>+7.1f}% {r['wr']:>5.1f}% {r['pf']:>5.2f} {r['dd']:>5.1f}% "
                  f"{r['ret_dd']:>6.2f}")


if __name__ == "__main__":
    asyncio.run(main())
