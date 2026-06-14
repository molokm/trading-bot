# @name: Trend Bounce Rapid
# @description: Trend Bounce (частотный) — EMA100 + swing=20 + oversold/overbought entry. Два пути входа: структурный (by swing) + импульсный (by RSI near EMA). size_pct=0.20, без плеча.
# @timeframe: 5m
# @symbol: BTC-USDT-SWAP
# @params: {"ema_trend": 100, "swing_window": 20, "size_pct": 0.20, "fee": 0.0005, "rsi_exit": 72, "rsi_entry": 40, "pullback_pct": 0.995, "near_sl_pct": 1.005}

import pandas as pd
import numpy as np


def find_swing_levels(high, low, window=10):
    n = len(high)
    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)
    for i in range(window, n - window):
        if high[i] == max(high[i - window: i + window + 1]):
            sh[i] = high[i]
        if low[i] == min(low[i - window: i + window + 1]):
            sl[i] = low[i]
    return sh, sl


def generate_signals(df, params):
    ema_trend = int(params.get("ema_trend", 100))
    swing_window = int(params.get("swing_window", 20))
    rsi_exit = params.get("rsi_exit", 72)
    rsi_entry_val = params.get("rsi_entry", 40)
    pullback_pct = params.get("pullback_pct", 0.995)
    near_sl_pct = params.get("near_sl_pct", 1.005)

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    n = len(df)

    ema = pd.Series(close).ewm(span=ema_trend).mean().values
    sh, sl = find_swing_levels(high, low, swing_window)

    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean().values
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean().values
    rsi = np.full(n, 50.0)
    for i in range(14, n):
        if loss[i] == 0:
            rsi[i] = 100.0
        else:
            rsi[i] = 100.0 - 100.0 / (1.0 + gain[i] / loss[i])

    signals = np.zeros(n)
    csh, csl = 0.0, 0.0
    pos = 0
    entry_bar = 0

    for i in range(max(swing_window * 2, 100), n):
        if np.isnan(ema[i]):
            continue
        if not np.isnan(sh[i]):
            csh = sh[i]
        if not np.isnan(sl[i]):
            csl = sl[i]

        uptrend = close[i] > ema[i]
        downtrend = close[i] < ema[i]

        if pos == 1:
            exit_signal = rsi[i] > rsi_exit
            if csl != 0:
                exit_signal = exit_signal or close[i] < csl
            else:
                exit_signal = exit_signal or close[i] < ema[i] * 0.98
            if exit_signal:
                signals[i] = 0
                pos = 0
                continue
            signals[i] = 1
            continue

        if pos == -1:
            exit_signal = rsi[i] < 100 - rsi_exit
            if csh != 0:
                exit_signal = exit_signal or close[i] > csh
            else:
                exit_signal = exit_signal or close[i] > ema[i] * 1.02
            if exit_signal:
                signals[i] = 0
                pos = 0
                continue
            signals[i] = -1
            continue

        if i - entry_bar < 3:
            continue

        if uptrend:
            bounce = low[i] > low[i - 1]

            path_a = False
            if csh != 0 and csl != 0:
                dropped = close[i] < csh * pullback_pct
                near_sl = close[i] <= csl * near_sl_pct
                path_a = dropped and near_sl and bounce

            near_ema = abs(close[i] / ema[i] - 1) < 0.005
            path_b = rsi[i] < rsi_entry_val and bounce and near_ema

            if path_a or path_b:
                signals[i] = 1
                pos = 1
                entry_bar = i

        elif downtrend:
            reject = high[i] < high[i - 1]

            path_a = False
            if csh != 0 and csl != 0:
                climbed = close[i] > csl * near_sl_pct
                near_sh = close[i] >= csh * pullback_pct
                path_a = climbed and near_sh and reject

            near_ema = abs(close[i] / ema[i] - 1) < 0.005
            path_b = rsi[i] > 100 - rsi_entry_val and reject and near_ema

            if path_a or path_b:
                signals[i] = -1
                pos = -1
                entry_bar = i

    return pd.Series(signals, index=df.index)
