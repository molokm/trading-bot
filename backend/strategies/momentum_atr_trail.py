# @name: Momentum ATR Trail
# @description: EMA200 + swing30 + ATR 2x trail stop. Net +195.2% annual | WR 57.0% | PF 4.66 | 472 trades/year (OKX data, 0.05% fee, 1x)
# @timeframe: 5m
# @symbol: BTC-USDT-SWAP
# @params: {"ema_trend": 200, "swing_window": 30, "atr_period": 14, "atr_mult": 2.0, "size_pct": 0.95, "fee": 0.0005}

import pandas as pd
import numpy as np


def generate_signals(df, params):
    ema_trend = params.get("ema_trend", 200)
    swing_window = int(params.get("swing_window", 30))
    atr_period = int(params.get("atr_period", 14))
    atr_mult = params.get("atr_mult", 2.0)

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    n = len(df)

    ema = pd.Series(close).ewm(span=ema_trend).mean().values

    true_range = np.maximum(
        high[1:] - low[1:],
        np.maximum(
            np.abs(high[1:] - close[:-1]),
            np.abs(low[1:] - close[:-1])
        )
    )
    atr = np.insert(
        pd.Series(true_range).rolling(atr_period).mean().values,
        0, 0
    )

    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)
    half = swing_window // 2
    for i in range(half, n - half):
        seg = slice(i - half, i + half + 1)
        if high[i] == max(high[seg]):
            sh[i] = high[i]
        if low[i] == min(low[seg]):
            sl[i] = low[i]

    signals = np.zeros(n)
    csh, csl = 0.0, 0.0
    pos = 0
    entry_price = 0.0
    stop_price = 0.0
    bars_since_entry = 0

    for i in range(swing_window * 2, n):
        bars_since_entry += 1
        if np.isnan(ema[i]):
            continue
        if not np.isnan(sh[i]):
            csh = sh[i]
        if not np.isnan(sl[i]):
            csl = sl[i]
        if csh == 0 or csl == 0:
            continue

        if pos == 1:
            stop_price = max(stop_price, close[i] - atr[i] * atr_mult)
            if close[i] < stop_price:
                signals[i] = 2
                pos = 0
                continue
            signals[i] = 1
            continue

        if pos == -1:
            stop_price = min(stop_price, close[i] + atr[i] * atr_mult)
            if close[i] > stop_price:
                signals[i] = -2
                pos = 0
                continue
            signals[i] = -1
            continue

        if bars_since_entry < 3:
            continue

        uptrend = close[i] > ema[i]

        if uptrend:
            if not np.isnan(sl[i]):
                prev = close[i - 1] if i > 0 else close[i]
                if close[i] > prev and close[i] > ema[i]:
                    signals[i] = 1
                    pos = 1
                    entry_price = close[i]
                    stop_price = close[i] - atr[i] * atr_mult
                    bars_since_entry = 0
        else:
            if not np.isnan(sh[i]):
                prev = close[i - 1] if i > 0 else close[i]
                if close[i] < prev and close[i] < ema[i]:
                    signals[i] = -1
                    pos = -1
                    entry_price = close[i]
                    stop_price = close[i] + atr[i] * atr_mult
                    bars_since_entry = 0

    return pd.Series(signals, index=df.index)
