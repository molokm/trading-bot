# @name: EMA Crossover Aggressive
# @description: 1m агрессивный скальпер. EMA(9)/EMA(21) crossover.
# @timeframe: 1m
# @symbol: BTC-USDT
# @params: {"fast_ema": 9, "slow_ema": 21, "risk_per_trade": 0.01, "atr_sl_mult": 2, "atr_tp_mult": 3, "cooldown_bars": 1}

import pandas as pd
import numpy as np


def generate_signals(df, params):
    fast = int(params.get("fast_ema", 9))
    slow = int(params.get("slow_ema", 21))
    cooldown = int(params.get("cooldown_bars", 1))

    close = df["close"].values
    if len(close) < slow + 5:
        return [0] * len(close)

    ema_fast = pd.Series(close).ewm(span=fast).mean().values
    ema_slow = pd.Series(close).ewm(span=slow).mean().values
    n = len(close)

    signals = np.zeros(n, dtype=np.int64)
    last_bar = -cooldown
    last_sig = 0

    for i in range(slow + 5, n):
        if i - last_bar < cooldown:
            continue

        prev_fast = ema_fast[i - 1]
        prev_slow = ema_slow[i - 1]

        fast_above = ema_fast[i] > ema_slow[i]
        fast_crossed_above = fast_above and prev_fast <= prev_slow
        fast_crossed_below = not fast_above and prev_fast >= prev_slow

        if fast_crossed_above and close[i] > ema_fast[i] and last_sig != 1:
            signals[i] = 1
            last_sig = 1
            last_bar = i
        elif fast_crossed_below and close[i] < ema_fast[i] and last_sig != -1:
            signals[i] = -1
            last_sig = -1
            last_bar = i

    return signals.tolist()
