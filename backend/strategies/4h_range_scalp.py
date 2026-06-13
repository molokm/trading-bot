# @name: 4H Range Scalp
# @description: Data Trader's 5M scalping. First N hours range = key level.
#   Price breaks range high → closes back in = SHORT
#   Price breaks range low → closes back in = LONG
#   83% win rate claimed on forex
# @timeframe: 5m
# @symbol: BTC-USDT
# @params: {"range_hours": 4, "tp_range_mult": 2.0, "sl_atr_mult": 1.5, "risk_per_trade": 0.01, "cooldown_bars": 6, "max_daily_loss": 0.03, "max_cons_losses": 3}

import pandas as pd
import numpy as np


def generate_signals(df, params):
    range_hours = int(params.get("range_hours", 4))
    cooldown = int(params.get("cooldown_bars", 6))
    range_bars = (range_hours * 60) // 5

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    ts = df["ts"].values
    n = len(df)

    signals = np.zeros(n, dtype=np.int64)

    i = 0
    while i < n:
        current_date = pd.Timestamp(ts[i]).date()

        day_start = i
        while day_start > 0 and pd.Timestamp(ts[day_start - 1]).date() == current_date:
            day_start -= 1

        day_end = i + 1
        while day_end < n and pd.Timestamp(ts[day_end]).date() == current_date:
            day_end += 1

        day_bars = day_end - day_start
        if day_bars < range_bars + 2:
            i = day_end
            continue

        range_high = float(np.max(high[day_start:day_start + range_bars]))
        range_low = float(np.min(low[day_start:day_start + range_bars]))

        last_trade_bar = -cooldown

        for j in range(day_start + range_bars, day_end):
            if j - last_trade_bar < cooldown:
                continue

            prev_close = close[j - 1] if j > day_start else close[j]
            prev_high = high[j - 1] if j > day_start else high[j]
            prev_low = low[j - 1] if j > day_start else low[j]
            curr_close = close[j]
            curr_high = high[j]
            curr_low = low[j]

            # Short: price broke above range_high, now closed back inside
            if prev_high > range_high and prev_close > range_high and curr_close < range_high:
                signals[j] = -1
                last_trade_bar = j

            # Long: price broke below range_low, now closed back inside
            if prev_low < range_low and prev_close < range_low and curr_close > range_low:
                signals[j] = 1
                last_trade_bar = j

        i = day_end

    return signals.tolist()
