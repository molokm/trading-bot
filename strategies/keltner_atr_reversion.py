# @name: Keltner ATR Reversion
# @description: Mean reversion on Keltner Channel bands. Enters long below lower band, short above upper band. Uses ATR volatility filter to avoid noise.
# @timeframe: 1H
# @symbol: BTC-USDT
# @params: {"ema_period": 20, "atr_period": 10, "keltner_mult": 2.0, "vol_filter": true, "use_atr_stops": true, "atr_sl_mult": 1.5, "atr_tp_mult": 2.0, "risk_per_trade": 0.01, "cooldown_bars": 3}

import pandas as pd
import numpy as np

def generate_signals(df, params):
    ema_period = params.get("ema_period", 20)
    atr_period = params.get("atr_period", 10)
    keltner_mult = params.get("keltner_mult", 2.0)
    vol_filter = params.get("vol_filter", True)

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    n = len(df)

    ema = pd.Series(close).ewm(span=ema_period, adjust=False).mean().values

    tr = np.maximum(high - low,
        np.maximum(
            np.abs(high - np.roll(close, 1)),
            np.abs(low - np.roll(close, 1))
        ))
    tr[0] = high[0] - low[0]
    atr = pd.Series(tr).rolling(atr_period).mean().values

    upper = ema + keltner_mult * atr
    lower = ema - keltner_mult * atr

    atr_median = pd.Series(atr).rolling(50).median().values

    signals = pd.Series(0, index=df.index)

    for i in range(max(ema_period, atr_period, 50) + 1, n):
        if np.isnan(upper[i]) or np.isnan(lower[i]) or np.isnan(ema[i]):
            continue
        if vol_filter and not np.isnan(atr_median[i]) and atr[i] < atr_median[i] * 0.7:
            continue

        if close[i] < lower[i] and close[i-1] >= lower[i-1]:
            signals[i] = 1
        elif close[i] > upper[i] and close[i-1] <= upper[i-1]:
            signals[i] = -1

    return signals
