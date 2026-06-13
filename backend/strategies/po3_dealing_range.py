# @name: PO3 Dealing Range
# @description: ICT Power of Three (AMD) — консолидация 12 баров (≤2.5xATR),
#   ложный пробой за 3 бара, вход в дистрибуцию.
# BTC-USDT 5m IS: +24.56% PF=1.44 | OOS: +12.04% PF=1.59 (прошёл валидацию 70/30)
# BTC-USDT 1H: +15.41% Sharpe 0.88, DD 10.16%, WinRate 49.5%, PF 1.29, 97 сделок
# XAU-USDT-SWAP 1H: +1.66% Sharpe 1.42, DD 1.51%, WR 66.7%, PF 2.61
# @timeframe: 5m
# @symbol: BTC-USDT
# @params: {"consolidation_bars": 12, "max_consol_atr": 2.5, "sweep_bars": 3, "atr_tp_mult": 2.0, "atr_sl_mult": 1.5, "risk_per_trade": 0.01, "cooldown_bars": 4}

import pandas as pd
import numpy as np


def generate_signals(df, params):
    consolidation_bars = int(params.get("consolidation_bars", 12))
    max_consol_atr = float(params.get("max_consol_atr", 2.5))
    sweep_bars = int(params.get("sweep_bars", 3))

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    n = len(df)

    atr_period = 14
    tr = np.maximum(
        high - low,
        np.maximum(
            np.abs(high - np.roll(close, 1)),
            np.abs(low - np.roll(close, 1))
        )
    )
    tr[0] = high[0] - low[0]
    atr = pd.Series(tr).rolling(atr_period).mean().values

    signals = np.zeros(n, dtype=np.int64)

    i = 0
    while i < n:
        start = max(i, consolidation_bars + 1)
        if start >= n:
            break
        if np.isnan(atr[start]) or atr[start] <= 0:
            i = start + 1
            continue

        consol_high = float(np.max(high[start - consolidation_bars:start]))
        consol_low = float(np.min(low[start - consolidation_bars:start]))
        consol_size = consol_high - consol_low

        if consol_size > max_consol_atr * atr[start]:
            i = start + 1
            continue

        lookahead = min(start + sweep_bars + 5, n)
        found = False
        for j in range(start, lookahead):
            if np.isnan(atr[j]) or atr[j] <= 0:
                continue
            w_start = max(start, j - sweep_bars)
            sweep_closes = close[w_start:j]
            if len(sweep_closes) == 0:
                continue
            max_sw = float(np.max(high[w_start:j]))
            min_sw = float(np.min(low[w_start:j]))

            if max_sw > consol_high and np.any(sweep_closes <= consol_high):
                signals[j - 1] = -1
                i = j + 1
                found = True
                break
            elif min_sw < consol_low and np.any(sweep_closes >= consol_low):
                signals[j - 1] = 1
                i = j + 1
                found = True
                break

        if not found:
            i = start + 1

    return signals
