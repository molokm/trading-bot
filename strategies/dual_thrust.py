# @name: Dual Thrust
# @description: Classic Dual Thrust breakout strategy. Computes dynamic range from HH/LC/HC/LL and enters on breakout with asymmetric K1/K2 coefficients.
# @timeframe: 1H
# @symbol: BTC-USDT
# @params: {"lookback": 20, "k1_long": 0.5, "k2_short": 0.5, "use_atr_stops": true, "atr_sl_mult": 2.0, "atr_tp_mult": 3.0, "risk_per_trade": 0.01, "cooldown_bars": 5}

import pandas as pd
import numpy as np

def generate_signals(df, params):
    lookback = params.get("lookback", 20)
    k1_long = params.get("k1_long", 0.5)
    k2_short = params.get("k2_short", 0.5)

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    open_ = df["open"].values
    n = len(df)

    rng_vals = np.zeros(n)
    for i in range(lookback + 1, n):
        hh = np.max(high[i-lookback:i])
        lc = np.min(close[i-lookback:i])
        hc = np.max(close[i-lookback:i])
        ll = np.min(low[i-lookback:i])
        rng_vals[i] = max(hh - lc, hc - ll)

    signals = pd.Series(0, index=df.index)

    for i in range(lookback + 2, n):
        if rng_vals[i-1] <= 0:
            continue
        upper_band = open_[i] + k1_long * rng_vals[i-1]
        lower_band = open_[i] - k2_short * rng_vals[i-1]
        if close[i] > upper_band and close[i-1] <= open_[i-1] + k1_long * rng_vals[i-2]:
            signals[i] = 1
        elif close[i] < lower_band and close[i-1] >= open_[i-1] - k2_short * rng_vals[i-2]:
            signals[i] = -1

    return signals
