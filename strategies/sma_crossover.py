# @name: SMA Crossover
# @description: Simple Moving Average crossover strategy. Goes long when short MA crosses above long MA, and short when it crosses below.
# @timeframe: 1H
# @symbol: BTC-USDT
# @params: {"short_window": 10, "long_window": 30}

import pandas as pd
import numpy as np

def generate_signals(df, params):
    short_window = params.get("short_window", 10)
    long_window = params.get("long_window", 30)

    short_ma = df["close"].rolling(window=short_window).mean()
    long_ma = df["close"].rolling(window=long_window).mean()

    signals = pd.Series(0, index=df.index)
    signals[short_ma > long_ma] = 1
    signals[short_ma < long_ma] = -1

    positions = signals.diff()
    positions[positions.isna()] = 0
    return positions
