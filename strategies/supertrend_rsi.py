# @name: Supertrend RSI
# @description: Supertrend trend filter + RSI momentum confirmation. Enters long in uptrend when RSI bounces from oversold, short in downtrend when RSI drops from overbought.
# @timeframe: 4H
# @symbol: BTC-USDT
# @params: {"supertrend_atr": 10, "supertrend_mult": 3.0, "rsi_period": 14, "rsi_oversold": 30, "rsi_overbought": 70, "use_atr_stops": true, "atr_sl_mult": 2.0, "atr_tp_mult": 3.0, "risk_per_trade": 0.01, "cooldown_bars": 5}

import pandas as pd
import numpy as np

def generate_signals(df, params):
    st_atr_period = params.get("supertrend_atr", 10)
    st_mult = params.get("supertrend_mult", 3.0)
    rsi_period = params.get("rsi_period", 14)
    rsi_os = params.get("rsi_oversold", 30)
    rsi_ob = params.get("rsi_overbought", 70)

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    n = len(df)

    tr = np.maximum(high - low,
        np.maximum(
            np.abs(high - np.roll(close, 1)),
            np.abs(low - np.roll(close, 1))
        ))
    tr[0] = high[0] - low[0]
    atr_st = pd.Series(tr).rolling(st_atr_period).mean().values

    src = (high + low) / 2.0
    upper_band = src + st_mult * atr_st
    lower_band = src - st_mult * atr_st

    trend = np.zeros(n)
    for i in range(1, n):
        if np.isnan(upper_band[i]) or np.isnan(lower_band[i]):
            trend[i] = trend[i-1]
            continue
        if close[i] > upper_band[i-1]:
            trend[i] = 1.0
        elif close[i] < lower_band[i-1]:
            trend[i] = -1.0
        else:
            trend[i] = trend[i-1]
            if trend[i] == 1.0 and lower_band[i] > lower_band[i-1]:
                lower_band[i] = lower_band[i-1]
            if trend[i] == -1.0 and upper_band[i] < upper_band[i-1]:
                upper_band[i] = upper_band[i-1]

    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean().values
    loss = (-delta.where(delta < 0, 0)).rolling(rsi_period).mean().values
    rsi = np.full(n, np.nan)
    for i in range(rsi_period, n):
        if loss[i] == 0:
            rsi[i] = 100.0
        elif np.isnan(loss[i]):
            rsi[i] = 50.0
        else:
            rs_val = gain[i] / loss[i]
            rsi[i] = 100.0 - (100.0 / (1.0 + rs_val))

    signals = pd.Series(0, index=df.index)

    for i in range(max(st_atr_period, rsi_period) + 2, n):
        if np.isnan(rsi[i]) or np.isnan(trend[i]):
            continue
        if trend[i] == 1.0 and rsi[i-1] <= rsi_os and rsi[i] > rsi_os:
            signals[i] = 1
        elif trend[i] == -1.0 and rsi[i-1] >= rsi_ob and rsi[i] < rsi_ob:
            signals[i] = -1

    return signals
