# @name: Trend Bounce LevX
# @description: Trend Bounce (структурный) + EMA200 + RSI. Адаптирована под плечо 10x: size_pct=0.35 (35% кап-ла), rsi_exit=72 (ранний выход), stop_loss_pct=0 (выход по структуре). leverage=10. Цель: >280% годовых на маржу $1k, макс.просадка маржи ~20%.
# @timeframe: 5m
# @symbol: BTC-USDT-SWAP
# @params: {"ema_trend": 200, "swing_window": 40, "pullback_bars": 5, "size_pct": 0.25, "fee": 0.0005, "rsi_exit": 70, "stop_loss_pct": 0.0, "leverage": 10, "pullback_pct": 0.997, "near_sl_pct": 1.003}

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
    ema_trend = params.get("ema_trend", 200)
    swing_window = int(params.get("swing_window", 40))
    rsi_exit = params.get("rsi_exit", 72)
    stop_loss_pct = params.get("stop_loss_pct", 0.0)
    pullback_pct = params.get("pullback_pct", 0.997)
    near_sl_pct = params.get("near_sl_pct", 1.003)

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
    entry_price = 0.0

    for i in range(swing_window * 2, n):
        if np.isnan(ema[i]):
            continue
        if not np.isnan(sh[i]):
            csh = sh[i]
        if not np.isnan(sl[i]):
            csl = sl[i]
        if csh == 0 or csl == 0:
            continue

        uptrend = close[i] > ema[i]
        downtrend = close[i] < ema[i]

        if pos == 1:
            loss_pct = (entry_price - close[i]) / entry_price
            if stop_loss_pct > 0 and loss_pct > stop_loss_pct:
                signals[i] = 0
                pos = 0
                continue
            if close[i] < csl or rsi[i] > rsi_exit:
                signals[i] = 0
                pos = 0
                continue
            signals[i] = 1
            continue

        if pos == -1:
            loss_pct = (close[i] - entry_price) / entry_price
            if stop_loss_pct > 0 and loss_pct > stop_loss_pct:
                signals[i] = 0
                pos = 0
                continue
            if close[i] > csh or rsi[i] < 100 - rsi_exit:
                signals[i] = 0
                pos = 0
                continue
            signals[i] = -1
            continue

        if i - entry_bar < 3:
            continue

        if uptrend:
            dropped = close[i] < csh * pullback_pct
            near_sl = close[i] <= csl * near_sl_pct
            bounce = i > 0 and low[i] > low[i - 1]
            if dropped and near_sl and bounce:
                signals[i] = 1
                pos = 1
                entry_bar = i
                entry_price = close[i]

        elif downtrend:
            climbed = close[i] > csl * near_sl_pct
            near_sh = close[i] >= csh * pullback_pct
            reject = i > 0 and high[i] < high[i - 1]
            if climbed and near_sh and reject:
                signals[i] = -1
                pos = -1
                entry_bar = i
                entry_price = close[i]

    return pd.Series(signals, index=df.index)
