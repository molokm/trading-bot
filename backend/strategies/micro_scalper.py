# @name: 1M Micro Scalper
# @description: Скальпинг на 1m: BB + RSI + объём. Вход у границ Боллинджера с подтверждением RSI и объёма. Выход при возврате к средней BB. size_pct=0.15 (15% кап-ла на сделку).
# @timeframe: 1m
# @symbol: BTC-USDT-SWAP
# @params: {"ema_trend": 200, "bb_period": 20, "bb_std": 2.0, "rsi_period": 7, "rsi_oversold": 25, "rsi_overbought": 75, "vol_mult": 1.5, "size_pct": 0.15, "fee": 0.0005, "stop_loss_pct": 0.0015}

import pandas as pd
import numpy as np


def generate_signals(df, params):
    ema_trend = params.get("ema_trend", 200)
    bb_period = int(params.get("bb_period", 20))
    bb_std = params.get("bb_std", 2.0)
    rsi_period = int(params.get("rsi_period", 7))
    rsi_oversold = params.get("rsi_oversold", 25)
    rsi_overbought = params.get("rsi_overbought", 75)
    vol_mult = params.get("vol_mult", 1.5)
    stop_loss = params.get("stop_loss_pct", 0.0015)

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    vol = df["vol"].values
    n = len(df)

    ema = pd.Series(close).ewm(span=ema_trend).mean().values

    sma20 = pd.Series(close).rolling(bb_period).mean().values
    std20 = pd.Series(close).rolling(bb_period).std().values
    bb_upper = sma20 + bb_std * std20
    bb_lower = sma20 - bb_std * std20

    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean().values
    loss = (-delta.where(delta < 0, 0)).rolling(rsi_period).mean().values
    rsi = np.full(n, 50.0)
    for i in range(rsi_period, n):
        if loss[i] == 0:
            rsi[i] = 100.0
        else:
            rsi[i] = 100.0 - 100.0 / (1.0 + gain[i] / loss[i])

    vol_sma = pd.Series(vol).rolling(bb_period).mean().values

    signals = np.zeros(n)
    pos = 0
    entry_price = 0.0

    for i in range(ema_trend + bb_period, n):
        if np.isnan(ema[i]) or np.isnan(sma20[i]):
            continue

        uptrend = close[i] > ema[i]
        downtrend = close[i] < ema[i]

        vol_spike = vol[i] > vol_sma[i] * vol_mult

        # Exit logic
        if pos == 1:
            if close[i] >= sma20[i] or rsi[i] >= 50:
                signals[i] = 0
                pos = 0
                continue
            loss_pct = (entry_price - close[i]) / entry_price
            if loss_pct > stop_loss:
                signals[i] = 0
                pos = 0
                continue
            signals[i] = 1
            continue

        if pos == -1:
            if close[i] <= sma20[i] or rsi[i] <= 50:
                signals[i] = 0
                pos = 0
                continue
            loss_pct = (close[i] - entry_price) / entry_price
            if loss_pct > stop_loss:
                signals[i] = 0
                pos = 0
                continue
            signals[i] = -1
            continue

        # Entry logic
        if uptrend and close[i] < bb_lower[i] and rsi[i] < rsi_oversold and vol_spike:
            signals[i] = 1
            pos = 1
            entry_price = close[i]

        elif downtrend and close[i] > bb_upper[i] and rsi[i] > rsi_overbought and vol_spike:
            signals[i] = -1
            pos = -1
            entry_price = close[i]

    return pd.Series(signals, index=df.index)
