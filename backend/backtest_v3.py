"""
Walk-forward v3: Focus on quality over quantity.
- Higher timeframe confirmation
- Wider stops, bigger targets
- Fewer, better trades
"""
import asyncio, json, sys
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


def adx(high, low, close, period=14):
    """Average Directional Index — trend strength."""
    n = len(close)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    tr = np.zeros(n)
    for i in range(1, n):
        up = high[i] - high[i-1]
        down = low[i-1] - low[i]
        plus_dm[i] = up if up > down and up > 0 else 0
        minus_dm[i] = down if down > up and down > 0 else 0
        tr[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
    atr_s = pd.Series(tr).rolling(period).mean().values
    plus_di = 100 * pd.Series(plus_dm).rolling(period).mean().values / np.where(atr_s > 0, atr_s, 1)
    minus_di = 100 * pd.Series(minus_dm).rolling(period).mean().values / np.where(atr_s > 0, atr_s, 1)
    dx = 100 * np.abs(plus_di - minus_di) / np.where(plus_di + minus_di > 0, plus_di + minus_di, 1)
    return pd.Series(dx).rolling(period).mean().values


def stats(cap, initial, trades):
    if not trades:
        return {"ret": 0, "n": 0, "wr": 0, "pf": 0, "avg": 0, "max_dd": 0}
    ret = (cap / initial - 1) * 100
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    wr = len(wins) / len(trades) * 100
    gp = sum(wins) if wins else 0
    gl = abs(sum(losses)) if losses else 0.001
    avg = np.mean(trades)
    # Max drawdown
    eq = [initial]
    for t in trades:
        eq.append(eq[-1] + t)
    eq = np.array(eq)
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / peak * 100
    return {"ret": ret, "n": len(trades), "wr": wr, "pf": gp/gl,
            "avg": avg, "max_dd": dd.max()}


# ─── G1: Strong trend only — EMA slope + ADX filter + wide ATR trail ───
def g1_strong_trend(close, high, low, cap=1000):
    """Only trade when ADX > 25 (strong trend). Wide stops."""
    n = len(close)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)
    atr14 = atr(high, low, close, 14)
    adx14 = adx(high, low, close, 14)
    rsi14 = rsi(close, 14)
    fee = 0.0005

    bal = cap
    pos = 0.0
    ep = 0.0
    sl = 0.0
    ebar = -999
    trades = []

    for i in range(300, n):
        # Trailing stop
        if pos > 0:
            new_sl = close[i] - atr14[i] * 3
            sl = max(sl, new_sl)
            if close[i] < sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue
        elif pos < 0:
            new_sl = close[i] + atr14[i] * 3
            sl = min(sl, new_sl)
            if close[i] > sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue

        if i - ebar < 10 or pos != 0:
            continue

        strong_trend = adx14[i] > 25
        if not strong_trend:
            continue

        uptrend = ema50[i] > ema200[i] and close[i] > ema50[i]

        # Long: strong uptrend + RSI pullback to 40-50 + rising
        if uptrend and 38 < rsi14[i] < 50 and rsi14[i] > rsi14[i-1]:
            ep = close[i]
            pos = bal * 0.95 / ep
            sl = ep - atr14[i] * 3
            ebar = i
        # Short: strong downtrend + RSI rally to 50-60 + falling
        elif not uptrend and ema50[i] < ema200[i] and close[i] < ema50[i]:
            if 50 < rsi14[i] < 62 and rsi14[i] < rsi14[i-1]:
                ep = close[i]
                pos = -bal * 0.95 / ep
                sl = ep + atr14[i] * 3
                ebar = i

    if pos != 0:
        pnl = pos * (close[-1] - ep)
        fee_c = abs(pos) * (ep + close[-1]) * fee
        bal += pnl - fee_c
        trades.append(pnl - fee_c)
    return bal, trades


# ─── G2: Multi-timeframe alignment — EMA200 trend + EMA20 pullback ───
def g2_mtf_alignment(close, high, low, cap=1000):
    """Wait for EMA20 to cross back toward EMA200 in trend direction."""
    n = len(close)
    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)
    fee = 0.0005

    bal = cap
    pos = 0.0
    ep = 0.0
    sl = 0.0
    ebar = -999
    prev_ema20_slope = 0
    trades = []

    for i in range(300, n):
        if pos > 0:
            sl = max(sl, close[i] - atr14[i] * 2.5)
            if close[i] < sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue
        elif pos < 0:
            sl = min(sl, close[i] + atr14[i] * 2.5)
            if close[i] > sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue

        if i - ebar < 10 or pos != 0:
            continue

        ema20_slope = ema20[i] - ema20[i-5] if i >= 5 else 0

        uptrend = ema50[i] > ema200[i]
        # Long: uptrend + EMA20 was falling, now turning up + RSI 40-55
        if uptrend:
            if prev_ema20_slope < 0 and ema20_slope > 0 and 40 < rsi14[i] < 55:
                ep = close[i]
                pos = bal * 0.95 / ep
                sl = ep - atr14[i] * 2.5
                ebar = i
        else:
            if prev_ema20_slope > 0 and ema20_slope < 0 and 45 < rsi14[i] < 60:
                ep = close[i]
                pos = -bal * 0.95 / ep
                sl = ep + atr14[i] * 2.5
                ebar = i

        prev_ema20_slope = ema20_slope

    if pos != 0:
        pnl = pos * (close[-1] - ep)
        fee_c = abs(pos) * (ep + close[-1]) * fee
        bal += pnl - fee_c
        trades.append(pnl - fee_c)
    return bal, trades


# ─── G3: Volatility squeeze breakout ───
def g3_volatility_squeeze(close, high, low, cap=1000):
    """Low ATR period → breakout with volume confirmation."""
    n = len(close)
    ema200 = ema(close, 200)
    atr14 = atr(high, low, close, 14)
    atr50 = atr(high, low, close, 50)
    rsi14 = rsi(close, 14)
    fee = 0.0005

    bal = cap
    pos = 0.0
    ep = 0.0
    sl = 0.0
    ebar = -999
    trades = []

    for i in range(300, n):
        if pos > 0:
            sl = max(sl, close[i] - atr14[i] * 3)
            if close[i] < sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue
        elif pos < 0:
            sl = min(sl, close[i] + atr14[i] * 3)
            if close[i] > sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue

        if i - ebar < 10 or pos != 0:
            continue

        # Squeeze: ATR14 < 60% of ATR50 (low volatility)
        squeeze = atr14[i] < atr50[i] * 0.6
        # Expansion: ATR14 > 120% of ATR50
        expansion = atr14[i] > atr50[i] * 1.2

        if squeeze:
            # Wait for expansion after squeeze
            pass

        if expansion and i - ebar >= 10:
            uptrend = close[i] > ema200[i]
            if uptrend and close[i] > high[i-1]:
                ep = close[i]
                pos = bal * 0.95 / ep
                sl = ep - atr14[i] * 3
                ebar = i
            elif not uptrend and close[i] < low[i-1]:
                ep = close[i]
                pos = -bal * 0.95 / ep
                sl = ep + atr14[i] * 3
                ebar = i

    if pos != 0:
        pnl = pos * (close[-1] - ep)
        fee_c = abs(pos) * (ep + close[-1]) * fee
        bal += pnl - fee_c
        trades.append(pnl - fee_c)
    return bal, trades


# ─── G4: Only long in strong bull, only short in strong bear ───
def g4_regime_split(close, high, low, cap=1000):
    """Different rules for bull/bear. Conservative entries."""
    n = len(close)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)
    fee = 0.0005

    bal = cap
    pos = 0.0
    ep = 0.0
    sl = 0.0
    ebar = -999
    trades = []

    for i in range(300, n):
        if pos > 0:
            # Trailing: move SL to breakeven after 1.5x risk
            if close[i] > ep + atr14[i] * 1.5:
                sl = max(sl, ep)
            sl = max(sl, close[i] - atr14[i] * 2)
            if close[i] < sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue
        elif pos < 0:
            if close[i] < ep - atr14[i] * 1.5:
                sl = min(sl, ep)
            sl = min(sl, close[i] + atr14[i] * 2)
            if close[i] > sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue

        if i - ebar < 15 or pos != 0:
            continue

        bull = ema50[i] > ema200[i] and ema50[i-20] > ema200[i-20]
        bear = ema50[i] < ema200[i] and ema50[i-20] < ema200[i-20]

        if bull:
            # Long only: RSI dip to 35-45 + close > EMA50 + candle bullish
            if 35 < rsi14[i] < 45 and close[i] > ema50[i]:
                if close[i] > open_val(close, i) and rsi14[i] > rsi14[i-1]:
                    ep = close[i]
                    pos = bal * 0.95 / ep
                    sl = ep - atr14[i] * 2
                    ebar = i
        elif bear:
            if 55 < rsi14[i] < 65 and close[i] < ema50[i]:
                if close[i] < open_val(close, i) and rsi14[i] < rsi14[i-1]:
                    ep = close[i]
                    pos = -bal * 0.95 / ep
                    sl = ep + atr14[i] * 2
                    ebar = i

    if pos != 0:
        pnl = pos * (close[-1] - ep)
        fee_c = abs(pos) * (ep + close[-1]) * fee
        bal += pnl - fee_c
        trades.append(pnl - fee_c)
    return bal, trades


def open_val(close, i):
    """Approximate open as previous close."""
    return close[i-1] if i > 0 else close[i]


# ─── G5: Very selective — only trade 1 setup per day ───
def g5_selective(close, high, low, cap=1000):
    """Best setup per day only. Maximum 1 trade/day."""
    n = len(close)
    ema200 = ema(close, 200)
    ema50 = ema(close, 50)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)
    fee = 0.0005

    bal = cap
    pos = 0.0
    ep = 0.0
    sl = 0.0
    tp = 0.0
    ebar = -999
    last_trade_day = -1
    trades = []

    for i in range(300, n):
        if pos > 0:
            if close[i] >= tp or close[i] < sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue
        elif pos < 0:
            if close[i] <= tp or close[i] > sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue

        if i - ebar < 20 or pos != 0:
            continue

        # Only 1 trade per ~288 bars (1 day of 5m)
        bar_day = i // 288
        if bar_day == last_trade_day:
            continue

        uptrend = ema50[i] > ema200[i] and close[i] > ema50[i]

        # Best setup: RSI extreme + trend confirmation + big candle
        if uptrend and rsi14[i] < 30 and rsi14[i] > rsi14[i-1]:
            ep = close[i]
            pos = bal * 0.95 / ep
            sl = ep - atr14[i] * 3
            tp = ep + atr14[i] * 6  # 2:1 R:R
            ebar = i
            last_trade_day = bar_day
        elif not uptrend and ema50[i] < ema200[i] and close[i] < ema50[i]:
            if rsi14[i] > 70 and rsi14[i] < rsi14[i-1]:
                ep = close[i]
                pos = -bal * 0.95 / ep
                sl = ep + atr14[i] * 3
                tp = ep - atr14[i] * 6
                ebar = i
                last_trade_day = bar_day

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

        print(f"\n{'='*70}")
        print(f" WALK-FORWARD v3 — {tf} (NO LOOKAHEAD)")
        print(f" Candles: {len(cache)}")
        print(f"{'='*70}")

        strategies = [
            ("G1: Strong Trend", g1_strong_trend),
            ("G2: MTF Alignment", g2_mtf_alignment),
            ("G3: Vol Squeeze", g3_volatility_squeeze),
            ("G4: Regime Split", g4_regime_split),
            ("G5: 1/Day Selective", g5_selective),
        ]

        for name, func in strategies:
            bal, trades = func(close, high, low, 1000)
            s = stats(bal, 1000, trades)
            print(f" {name:25s} {s['n']:4d} trades  "
                  f"{s['ret']:+7.1f}%  WR {s['wr']:5.1f}%  PF {s['pf']:5.2f}  "
                  f"avg {s['avg']:+.2f}  maxDD {s['max_dd']:.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
