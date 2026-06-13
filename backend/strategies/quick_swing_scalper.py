# @name: Quick Swing Scalper
# @description: 5m скальпер на микро-свингах.
#   Long: цена упала на N% за 3 бара → покупаем отскок
#   Short: цена выросла на N% за 3 бара → продаём откат
#   Выход: противоположный сигнал или ATR стоп
# @timeframe: 5m
# @symbol: BTC-USDT
# @params: {"swing_pct": 0.01, "lookback": 3, "risk_per_trade": 0.005, "atr_sl_mult": 1.5, "atr_tp_mult": 2.0, "cooldown_bars": 2}
# DISCLAIMER: This strategy is experimental and for demo use only.

import pandas as pd
import numpy as np


def generate_signals(df, params):
    swing_pct = float(params.get("swing_pct", 0.01)) / 100.0
    lookback = int(params.get("lookback", 3))
    cooldown = int(params.get("cooldown_bars", 2))

    close = df["close"].values
    n = len(df)

    signals = np.zeros(n, dtype=np.int64)
    last_bar = -cooldown
    last_sig = 0

    for i in range(lookback + 2, n):
        if i - last_bar < cooldown:
            continue

        change = (close[i] - close[i - lookback]) / close[i - lookback]

        if change < -swing_pct and last_sig != 1:
            signals[i] = 1
            last_sig = 1
            last_bar = i
        elif change > swing_pct and last_sig != -1:
            signals[i] = -1
            last_sig = -1
            last_bar = i

    return signals.tolist()
