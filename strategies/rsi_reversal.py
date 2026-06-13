# @name: RSI Reversal
# @description: Вход в лонг при RSI < 30 (oversold), выход при возврате в нейтраль. Вход в шорт при RSI > 70 (overbought), выход при возврате в нейтраль. Устойчивый позиционный сигнал.
# @timeframe: 1H
# @symbol: BTC-USDT

import pandas as pd
import numpy as np

def generate_signals(df, params):
    period = params.get("rsi_period", 14)
    oversold = params.get("oversold", 30)
    overbought = params.get("overbought", 70)

    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    signals = pd.Series(0, index=df.index)
    pos = 0
    for i in range(len(df)):
        if np.isnan(rsi[i]):
            signals[i] = 0
            continue
        if rsi[i] < oversold:
            pos = 1
        elif rsi[i] > overbought:
            pos = -1
        else:
            pos = 0
        signals[i] = pos
    return signals
