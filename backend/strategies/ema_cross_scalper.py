# @name: EMA Cross Scalper
# @description:  1m скальпер на пересечении EMA 9/21. 
#   Long: EMA9 > EMA21 + RSI < 70 + объём > среднего
#   Short: EMA9 < EMA21 + RSI > 30 + объём > среднего
#   Выход: противоположный сигнал или ATR стоп
# @timeframe: 1m
# @symbol: BTC-USDT
# @params: {"ema_fast": 9, "ema_slow": 21, "rsi_period": 14, "vol_mult": 1.0, "atr_sl_mult": 1.5, "atr_tp_mult": 2.5, "risk_per_trade": 0.008, "cooldown_bars": 5}

import pandas as pd
import numpy as np


def generate_signals(df, params):
    ema_fast = int(params.get("ema_fast", 9))
    ema_slow = int(params.get("ema_slow", 21))
    rsi_period = int(params.get("rsi_period", 14))
    vol_mult = float(params.get("vol_mult", 1.0))
    cooldown = int(params.get("cooldown_bars", 5))

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["vol"].values
    n = len(df)

    ema_f = pd.Series(close).ewm(span=ema_fast).mean().values
    ema_s = pd.Series(close).ewm(span=ema_slow).mean().values

    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean().values
    loss = (-delta.where(delta < 0, 0)).rolling(rsi_period).mean().values
    rsi = np.full(n, 50.0)
    for i in range(rsi_period, n):
        if loss[i] == 0:
            rsi[i] = 100.0
        elif gain[i] == 0:
            rsi[i] = 0.0
        else:
            rsi[i] = 100.0 - (100.0 / (1.0 + gain[i] / loss[i]))

    vol_sma = pd.Series(volume).rolling(ema_slow).mean().values

    signals = np.zeros(n, dtype=np.int64)
    last_signal = 0
    last_bar = -cooldown

    for i in range(ema_slow + 5, n):
        if i - last_bar < cooldown:
            continue
        if np.isnan(rsi[i]) or vol_sma[i] == 0:
            continue

        vol_ok = volume[i] >= vol_sma[i] * vol_mult
        if not vol_ok:
            continue

        crossed_above = ema_f[i - 1] <= ema_s[i - 1] and ema_f[i] > ema_s[i]
        crossed_below = ema_f[i - 1] >= ema_s[i - 1] and ema_f[i] < ema_s[i]

        if crossed_above and rsi[i] < 70 and last_signal != 1:
            signals[i] = 1
            last_signal = 1
            last_bar = i
        elif crossed_below and rsi[i] > 30 and last_signal != -1:
            signals[i] = -1
            last_signal = -1
            last_bar = i

    return signals.tolist()
