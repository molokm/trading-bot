"""
Walk-forward backtest v2 — test multiple LevX Pro redesigns.
All use ONLY past data (no lookahead).
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


def rolling_high(high, window):
    """Highest high of the last `window` bars — NO lookahead."""
    return pd.Series(high).rolling(window).max().values


def rolling_low(low, window):
    """Lowest low of the last `window` bars — NO lookahead."""
    return pd.Series(low).rolling(window).min().values


def stats(balance, initial, trades):
    if not trades:
        return {"ret": 0, "n": 0, "wr": 0, "pf": 0}
    ret = (balance / initial - 1) * 100
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    wr = len(wins) / len(trades) * 100
    gp = sum(wins) if wins else 0
    gl = abs(sum(losses)) if losses else 0.001
    return {"ret": ret, "n": len(trades), "wr": wr, "pf": gp / gl}


# ─── Strategy A: Rolling high/low instead of swing levels ───
def strat_a_rolling_hl(close, high, low, cap=1000):
    """Use rolling high/low as support/resistance. No lookahead."""
    n = len(close)
    ema200 = ema(close, 200)
    rsi14 = rsi(close, 14)
    rh = rolling_high(high, 40)
    rl = rolling_low(low, 40)
    fee = 0.0005

    bal = cap
    pos = 0.0
    ep = 0.0
    ebar = -999
    trend = 0
    trades = []

    for i in range(250, n):
        if close[i] > ema200[i] * 1.003:
            trend = 1
        elif close[i] < ema200[i] * 0.997:
            trend = -1

        # Exit
        if pos > 0 and (close[i] < rl[i] or rsi14[i] > 80):
            pnl = pos * (close[i] - ep)
            fee_c = abs(pos) * (ep + close[i]) * fee
            bal += pnl - fee_c
            trades.append(pnl - fee_c)
            pos = 0
            ebar = i
        elif pos < 0 and (close[i] > rh[i] or rsi14[i] < 20):
            pnl = pos * (close[i] - ep)
            fee_c = abs(pos) * (ep + close[i]) * fee
            bal += pnl - fee_c
            trades.append(pnl - fee_c)
            pos = 0
            ebar = i

        if i - ebar < 3 or pos != 0:
            continue

        # Entry: pullback to rolling low in uptrend
        if trend == 1:
            dropped = close[i] < rh[i] * 0.993
            near = close[i] <= rl[i] * 1.003
            bounce = i > 0 and low[i] > low[i-1]
            if dropped and near and bounce:
                ep = close[i]
                pos = bal * 0.95 / ep
                ebar = i
        elif trend == -1:
            climbed = close[i] > rl[i] * 1.003
            near = close[i] >= rh[i] * 0.997
            reject = i > 0 and high[i] < high[i-1]
            if climbed and near and reject:
                ep = close[i]
                pos = -bal * 0.95 / ep
                ebar = i

    if pos != 0:
        pnl = pos * (close[-1] - ep)
        fee_c = abs(pos) * (ep + close[-1]) * fee
        bal += pnl - fee_c
        trades.append(pnl - fee_c)
    return bal, trades


# ─── Strategy B: RSI reversal + EMA trend + ATR stop ───
def strat_b_rsi_reversal(close, high, low, cap=1000):
    """Enter on RSI reversal in trend direction with ATR stop. No swing levels."""
    n = len(close)
    ema200 = ema(close, 200)
    ema20 = ema(close, 20)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)
    fee = 0.0005

    bal = cap
    pos = 0.0
    ep = 0.0
    sl = 0.0
    ebar = -999
    trades = []

    for i in range(250, n):
        # Trailing stop
        if pos > 0:
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
            sl = min(sl, close[i] + atr14[i] * 2)
            if close[i] > sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue

        if i - ebar < 5 or pos != 0:
            continue

        uptrend = close[i] > ema200[i]
        # RSI oversold reversal in uptrend
        if uptrend and rsi14[i-1] < 35 and rsi14[i] > 35 and close[i] > ema20[i]:
            ep = close[i]
            pos = bal * 0.95 / ep
            sl = ep - atr14[i] * 2
            ebar = i
        elif not uptrend and rsi14[i-1] > 65 and rsi14[i] < 65 and close[i] < ema20[i]:
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


# ─── Strategy C: MACD cross + EMA trend + ATR stop ───
def strat_c_macd_cross(close, high, low, cap=1000):
    """MACD crossover in trend direction. No swing levels."""
    n = len(close)
    ema200 = ema(close, 200)
    macd_line = ema(close, 12) - ema(close, 26)
    signal_line = pd.Series(macd_line).ewm(span=9, adjust=False).mean().values
    hist = macd_line - signal_line
    atr14 = atr(high, low, close, 14)
    rsi14 = rsi(close, 14)
    fee = 0.0005

    bal = cap
    pos = 0.0
    ep = 0.0
    sl = 0.0
    ebar = -999
    trades = []

    for i in range(250, n):
        if pos > 0:
            sl = max(sl, close[i] - atr14[i] * 2)
            if close[i] < sl or hist[i] < 0:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue
        elif pos < 0:
            sl = min(sl, close[i] + atr14[i] * 2)
            if close[i] > sl or hist[i] > 0:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue

        if i - ebar < 5 or pos != 0:
            continue

        uptrend = close[i] > ema200[i]
        if uptrend and hist[i-1] < 0 and hist[i] > 0 and rsi14[i] < 70:
            ep = close[i]
            pos = bal * 0.95 / ep
            sl = ep - atr14[i] * 2
            ebar = i
        elif not uptrend and hist[i-1] > 0 and hist[i] < 0 and rsi14[i] > 30:
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


# ─── Strategy D: Breakout with retest + ATR trail ───
def strat_d_breakout_retest(close, high, low, cap=1000):
    """Wait for breakout above rolling high, enter on retest. No lookahead."""
    n = len(close)
    ema200 = ema(close, 200)
    ema20 = ema(close, 20)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)
    rh20 = rolling_high(high, 20)
    rl20 = rolling_low(low, 20)
    fee = 0.0005

    bal = cap
    pos = 0.0
    ep = 0.0
    sl = 0.0
    ebar = -999
    broke_up = False
    broke_dn = False
    broke_bar = -999
    trades = []

    for i in range(250, n):
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

        if i - ebar < 5 or pos != 0:
            continue

        # Detect breakout
        if close[i] > rh20[i-1] and close[i-1] <= rh20[i-2]:
            broke_up = True
            broke_bar = i
        if close[i] < rl20[i-1] and close[i-1] >= rl20[i-2]:
            broke_dn = True
            broke_bar = i

        # Retest entry (within 10 bars of breakout)
        if broke_up and i - broke_bar <= 10 and i - broke_bar >= 2:
            uptrend = close[i] > ema200[i]
            retest = close[i] <= rh20[i-1] * 1.002 and close[i] >= rl20[i] * 0.998
            if uptrend and retest and rsi14[i] < 65:
                ep = close[i]
                pos = bal * 0.95 / ep
                sl = ep - atr14[i] * 2.5
                ebar = i
                broke_up = False
        elif broke_dn and i - broke_bar <= 10 and i - broke_bar >= 2:
            downtrend = close[i] < ema200[i]
            retest = close[i] >= rl20[i-1] * 0.998 and close[i] <= rh20[i] * 1.002
            if downtrend and retest and rsi14[i] > 35:
                ep = close[i]
                pos = -bal * 0.95 / ep
                sl = ep + atr14[i] * 2.5
                ebar = i
                broke_dn = False

        if i - broke_bar > 10:
            broke_up = False
            broke_dn = False

    if pos != 0:
        pnl = pos * (close[-1] - ep)
        fee_c = abs(pos) * (ep + close[-1]) * fee
        bal += pnl - fee_c
        trades.append(pnl - fee_c)
    return bal, trades


# ─── Strategy E: Mean reversion Bollinger + RSI ───
def strat_e_mean_reversion(close, high, low, cap=1000):
    """Bollinger band mean reversion with RSI confirmation. No swing levels."""
    n = len(close)
    ema200 = ema(close, 200)
    rsi14 = rsi(close, 14)
    atr14 = atr(high, low, close, 14)

    # Bollinger Bands
    sma20 = pd.Series(close).rolling(20).mean().values
    std20 = pd.Series(close).rolling(20).std().values
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bb_mid = sma20

    fee = 0.0005
    bal = cap
    pos = 0.0
    ep = 0.0
    sl = 0.0
    tp = 0.0
    ebar = -999
    trades = []

    for i in range(250, n):
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

        if i - ebar < 3 or pos != 0:
            continue

        uptrend = close[i] > ema200[i]

        # Long: price touches lower BB + RSI oversold + uptrend
        if uptrend and close[i] <= bb_lower[i] and rsi14[i] < 35:
            ep = close[i]
            pos = bal * 0.95 / ep
            sl = ep - atr14[i] * 2
            tp = bb_mid[i]
            ebar = i
        # Short: price touches upper BB + RSI overbought + downtrend
        elif not uptrend and close[i] >= bb_upper[i] and rsi14[i] > 65:
            ep = close[i]
            pos = -bal * 0.95 / ep
            sl = ep + atr14[i] * 2
            tp = bb_mid[i]
            ebar = i

    if pos != 0:
        pnl = pos * (close[-1] - ep)
        fee_c = abs(pos) * (ep + close[-1]) * fee
        bal += pnl - fee_c
        trades.append(pnl - fee_c)
    return bal, trades


# ─── Strategy F: Momentum + pullback with EMA stack ───
def strat_f_ema_stack(close, high, low, cap=1000):
    """EMA20 > EMA50 > EMA200 alignment + RSI pullback entry."""
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
    trades = []

    for i in range(250, n):
        if pos > 0:
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
            sl = min(sl, close[i] + atr14[i] * 2)
            if close[i] > sl:
                pnl = pos * (close[i] - ep)
                fee_c = abs(pos) * (ep + close[i]) * fee
                bal += pnl - fee_c
                trades.append(pnl - fee_c)
                pos = 0
                ebar = i
                continue

        if i - ebar < 5 or pos != 0:
            continue

        bull_stack = ema20[i] > ema50[i] > ema200[i]
        bear_stack = ema20[i] < ema50[i] < ema200[i]

        # Long: bull stack + RSI pullback to 35-45 zone + price above EMA20
        if bull_stack and 35 < rsi14[i] < 45 and close[i] > ema20[i]:
            # Wait for RSI to start rising
            if rsi14[i] > rsi14[i-1]:
                ep = close[i]
                pos = bal * 0.95 / ep
                sl = ep - atr14[i] * 2
                ebar = i
        # Short: bear stack + RSI rally to 55-65 zone + price below EMA20
        elif bear_stack and 55 < rsi14[i] < 65 and close[i] < ema20[i]:
            if rsi14[i] < rsi14[i-1]:
                ep = close[i]
                pos = -bal * 0.95 / ep
                sl = ema200[i]  # wider stop for shorts
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

        print(f"\n{'='*70}")
        print(f" WALK-FORWARD BACKTEST — {tf} (NO LOOKAHEAD)")
        print(f" Candles: {len(cache)}")
        print(f"{'='*70}")

        strategies = [
            ("A: Rolling HL", strat_a_rolling_hl),
            ("B: RSI Reversal", strat_b_rsi_reversal),
            ("C: MACD Cross", strat_c_macd_cross),
            ("D: Breakout Retest", strat_d_breakout_retest),
            ("E: Mean Reversion", strat_e_mean_reversion),
            ("F: EMA Stack", strat_f_ema_stack),
        ]

        for name, func in strategies:
            bal, trades = func(close, high, low, 1000)
            s = stats(bal, 1000, trades)
            print(f" {name:25s} {s['n']:4d} trades  "
                  f"{s['ret']:+7.1f}%  WR {s['wr']:5.1f}%  PF {s['pf']:5.2f}")


if __name__ == "__main__":
    asyncio.run(main())
