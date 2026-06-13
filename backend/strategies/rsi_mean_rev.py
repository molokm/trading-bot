# @name: RSI Mean Rev
# @description: 5m RSI-реверсия (среднее возвращение).
#   RSI(7) < 25 → long (перепроданность)
#   RSI(7) > 75 → short (перекупленность)
#   Выход: RSI пересекает 50 (нейтраль) или ATR стоп
# @timeframe: 5m
# @symbol: BTC-USDT
# @params: {"rsi_period": 7, "rsi_os": 25, "rsi_ob": 75, "atr_sl_mult": 2.0, "atr_tp_mult": 3.0, "risk_per_trade": 0.005, "cooldown_bars": 3}

import pandas as pd
import numpy as np


def generate_signals(df, params):
    rsi_period = int(params.get("rsi_period", 7))
    rsi_os = float(params.get("rsi_os", 25))
    rsi_ob = float(params.get("rsi_ob", 75))
    cooldown = int(params.get("cooldown_bars", 3))

    close = df["close"].values
    n = len(df)

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
    last_sig = 0

    for i in range(rsi_period + 5, n):
        if i - last_bar < cooldown:
            continue
        if np.isnan(rsi[i]):
            continue

        if rsi[i] < rsi_os and last_sig != 1:
            signals[i] = 1
            last_sig = 1
            last_bar = i
        elif rsi[i] > rsi_ob and last_sig != -1:
            signals[i] = -1
            last_sig = -1
            last_bar = i

    return signals.tolist()
