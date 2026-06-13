# @name: EMA Cross Scalper
# @description: 1m скальпер. Цена пересекает EMA(20) = сигнал.
#   Close > EMA → long, Close < EMA → short.
#   RSI(7) фильтр: лонг при RSI<70, шорт при RSI>30.
#   Выход: противоположный сигнал или ATR стоп
# @timeframe: 1m
# @symbol: BTC-USDT
# @params: {"ema_period": 20, "rsi_period": 7, "rsi_ob": 75, "rsi_os": 25, "atr_sl_mult": 2.0, "atr_tp_mult": 3.0, "risk_per_trade": 0.005, "cooldown_bars": 5}

import pandas as pd
import numpy as np


def generate_signals(df, params):
    ema_period = int(params.get("ema_period", 20))
    rsi_period = int(params.get("rsi_period", 7))
    rsi_ob = float(params.get("rsi_ob", 75))
    rsi_os = float(params.get("rsi_os", 25))
    cooldown = int(params.get("cooldown_bars", 5))

    close = df["close"].values
    n = len(df)

    ema = pd.Series(close).ewm(span=ema_period).mean().values

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

    signals = np.zeros(n, dtype=np.int64)
    last_bar = -cooldown
    last_dir = 0

    for i in range(max(ema_period, rsi_period) + 5, n):
        if i - last_bar < cooldown:
            continue
        if np.isnan(rsi[i]):
            continue

        crossed_above = close[i] > ema[i] and close[i - 1] <= ema[i - 1]
        crossed_below = close[i] < ema[i] and close[i - 1] >= ema[i - 1]

        if crossed_above and rsi[i] < rsi_ob and last_dir != 1:
            signals[i] = 1
            last_dir = 1
            last_bar = i
        elif crossed_below and rsi[i] > rsi_os and last_dir != -1:
            signals[i] = -1
            last_dir = -1
            last_bar = i

    return signals.tolist()
