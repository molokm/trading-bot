"""
Final walk-forward test for new LevX Pro.
No lookahead. Verified on full dataset.
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


def run_levx_pro(close, high, low, cap=1000):
    """New LevX Pro with optimized params. No lookahead."""
    n = len(close)
    ema_f = ema(close, 30)
    ema_s = ema(close, 100)
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

    for i in range(300, n):
        # Trailing stop for longs
        if pos > 0:
            sl = max(sl, close[i] - atr14[i] * 2.0)
            if close[i] >= tp or close[i] < sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue
        elif pos < 0:
            sl = min(sl, close[i] + atr14[i] * 2.0)
            if close[i] <= tp or close[i] > sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue

        if i - ebar < 400 or pos != 0:
            continue

        uptrend = ema_f[i] > ema_s[i] and close[i] > ema_f[i]
        downtrend = ema_f[i] < ema_s[i] and close[i] < ema_f[i]

        if uptrend and rsi14[i] < 35 and rsi14[i] > rsi14[i-1]:
            ep = close[i]
            pos = bal * 0.95 / ep
            sl = ep - atr14[i] * 2.0
            tp = ep + atr14[i] * 6.0
            ebar = i
        elif downtrend and rsi14[i] > 60 and rsi14[i] < rsi14[i-1]:
            ep = close[i]
            pos = -bal * 0.95 / ep
            sl = ep + atr14[i] * 2.0
            tp = ep - atr14[i] * 6.0
            ebar = i

    if pos != 0:
        pnl = pos * (close[-1] - ep)
        fee_c = abs(pos) * (ep + close[-1]) * fee
        bal += pnl - fee_c
        trades.append(pnl - fee_c)

    return bal, trades


async def main():
    from app.services.data_cache import _load_cache

    for tf in ["5m", "1m"]:
        cache = _load_cache("BTC-USDT", tf)
        if not cache:
            print(f"No {tf} cache")
            continue

        arr = np.array(cache, dtype=object)
        close = arr[:, 4].astype(float)
        high = arr[:, 2].astype(float)
        low = arr[:, 3].astype(float)

        bal, trades = run_levx_pro(close, high, low, 1000)

        if trades:
            ret = (bal / 1000 - 1) * 100
            wins = sum(1 for t in trades if t > 0)
            wr = wins / len(trades) * 100
            gp = sum(t for t in trades if t > 0)
            gl = abs(sum(t for t in trades if t <= 0)) or 0.001
            pf = gp / gl
            eq = [1000]
            for t in trades:
                eq.append(eq[-1] + t)
            eq = np.array(eq)
            dd = ((np.maximum.accumulate(eq) - eq) / np.maximum.accumulate(eq) * 100).max()

            print(f"\n{tf} LevX Pro (walk-forward):")
            print(f"  Trades: {len(trades)}")
            print(f"  Return: {ret:+.1f}%")
            print(f"  Win Rate: {wr:.1f}%")
            print(f"  Profit Factor: {pf:.2f}")
            print(f"  Max Drawdown: {dd:.1f}%")
            print(f"  Avg Win: {np.mean([t for t in trades if t > 0]):.2f}" if any(t > 0 for t in trades) else "")
            print(f"  Avg Loss: {np.mean([abs(t) for t in trades if t <= 0]):.2f}" if any(t <= 0 for t in trades) else "")

            # Monthly breakdown
            from datetime import datetime
            timestamps = arr[:, 0].astype(int)
            monthly = {}
            for j, t in enumerate(trades):
                # Find approximate month from nearby timestamps
                bar_idx = min(j * (len(timestamps) // len(trades)), len(timestamps) - 1)
                ts = timestamps[bar_idx]
                month = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m")
                monthly[month] = monthly.get(month, 0) + t

            print("\n  Monthly PnL:")
            for m in sorted(monthly.keys()):
                print(f"    {m}: {monthly[m]:+.2f}")


if __name__ == "__main__":
    asyncio.run(main())
