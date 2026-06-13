# @name: Gerchik False Breakout
# @description: Скальпинг на ложных пробоях уровней Герчика. Вход после прокола уровня и возврата цены. Устойчивый позиционный сигнал — удерживается до противоположного входа.
# @timeframe: 5m
# @symbol: BTC-USDT
# @params: {"swing_window": 20, "confirm_bars": 2, "atr_period": 14, "atr_sl_mult": 1.5, "atr_tp_mult": 2.0, "risk_per_trade": 0.01, "max_daily_loss": 0.03, "cooldown_bars": 3}

import pandas as pd
import numpy as np

def find_swing_levels(df, window=20):
    high = df["high"].values
    low = df["low"].values
    n = len(df)
    swing_highs = np.zeros(n)
    swing_lows = np.zeros(n)
    for i in range(window, n - window):
        if high[i] == max(high[i-window:i+window+1]):
            if i == 0 or high[i] > high[i-1]:
                swing_highs[i] = high[i]
        if low[i] == min(low[i-window:i+window+1]):
            if i == 0 or low[i] < low[i-1]:
                swing_lows[i] = low[i]
    return swing_highs, swing_lows

def generate_signals(df, params):
    swing_window = params.get("swing_window", 20)
    confirm_bars = params.get("confirm_bars", 2)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    n = len(df)
    swing_highs, swing_lows = find_swing_levels(df, swing_window)
    signals = pd.Series(0, index=df.index)
    last_high = 0
    last_low = 0
    breakout_state = 0
    breakout_bar = 0
    pos = 0
    for i in range(swing_window * 2, n):
        if swing_highs[i] > 0:
            last_high = swing_highs[i]
        if swing_lows[i] > 0:
            last_low = swing_lows[i]
        if last_high > 0 and breakout_state == 0:
            if close[i] > last_high and close[i-1] <= last_high:
                breakout_state = 1
                breakout_bar = i
        if breakout_state == 1:
            if close[i] < last_high and i - breakout_bar <= confirm_bars:
                pos = -1
                breakout_state = 0
            elif i - breakout_bar > confirm_bars:
                breakout_state = 0
        if last_low > 0 and breakout_state == 0:
            if close[i] < last_low and close[i-1] >= last_low:
                breakout_state = -1
                breakout_bar = i
        if breakout_state == -1:
            if close[i] > last_low and i - breakout_bar <= confirm_bars:
                pos = 1
                breakout_state = 0
            elif i - breakout_bar > confirm_bars:
                breakout_state = 0
        signals[i] = pos
    return signals
