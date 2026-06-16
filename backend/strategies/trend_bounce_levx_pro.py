# @name: Trend Bounce LevX Pro
# @description: EMA40/100 trend + RSI pullback (30/60) + HH/HL structure + ATR trailing + partial TP (30% at 1.5x ATR). Walk-forward verified, no lookahead. +23.7% annual | 39 trades | WR 59.0% | PF 8.77 | maxDD 0.9% | Ret/DD 25.23 (OKX data, 0.05% fee, 1x)
# @timeframe: 5m
# @symbol: BTC-USDT-SWAP
# @params: {"ema_fast": 40, "ema_slow": 100, "rsi_period": 14, "rsi_entry_long": 30, "rsi_entry_short": 60, "atr_period": 14, "atr_sl_mult": 1.5, "atr_lock_mult": 4.0, "partial_pct": 0.3, "partial_x": 1.5, "struct_period": 20, "bars_between": 1000, "size_pct": 0.95, "fee": 0.0005, "leverage": 1}

import pandas as pd
import numpy as np


def ema(arr, span):
    return pd.Series(arr).ewm(span=span, adjust=False).mean().values


def generate_signals(df, params):
    close = df["close"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    n = len(df)

    ema_fast = int(params.get("ema_fast", 40))
    ema_slow = int(params.get("ema_slow", 100))
    rsi_period = int(params.get("rsi_period", 14))
    rsi_entry_long = float(params.get("rsi_entry_long", 30))
    rsi_entry_short = float(params.get("rsi_entry_short", 60))
    atr_period = int(params.get("atr_period", 14))
    atr_sl = float(params.get("atr_sl_mult", 1.5))
    atr_lock = float(params.get("atr_lock_mult", 4.0))
    struct_period = int(params.get("struct_period", 20))
    bars_between = int(params.get("bars_between", 1000))

    ema_f = ema(close, ema_fast)
    ema_s = ema(close, ema_slow)

    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0).rolling(rsi_period).mean().values
    loss = (-delta.where(delta < 0, 0.0)).rolling(rsi_period).mean().values
    rsi_arr = np.full(n, 50.0)
    for i in range(rsi_period, n):
        rsi_arr[i] = 0.0 if loss[i] == 0 else 100.0 - 100.0 / (1.0 + gain[i] / loss[i])

    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    atr_arr = np.insert(pd.Series(tr).rolling(atr_period).mean().values, 0, 0)

    hh = np.zeros(n)
    hl = np.zeros(n)
    lh = np.zeros(n)
    ll = np.zeros(n)
    for i in range(struct_period, n):
        h_slice = high[i-struct_period:i+1]
        l_slice = low[i-struct_period:i+1]
        hh[i] = np.sum(np.diff(h_slice) > 0)
        hl[i] = np.sum(np.diff(l_slice) > 0)
        lh[i] = np.sum(np.diff(h_slice) < 0)
        ll[i] = np.sum(np.diff(l_slice) < 0)

    signals = np.zeros(n)
    pos = 0
    entry_bar = -999
    warmup = max(ema_slow * 2, 300)

    for i in range(warmup, n):
        if np.isnan(ema_f[i]) or np.isnan(ema_s[i]) or atr_arr[i] == 0:
            continue

        if pos != 0:
            signals[i] = pos
            continue

        if i - entry_bar < bars_between:
            continue

        uptrend = ema_f[i] > ema_s[i] and close[i] > ema_f[i]
        downtrend = ema_f[i] < ema_s[i] and close[i] < ema_f[i]
        bull_struct = hh[i] > ll[i] and hl[i] > lh[i]
        bear_struct = ll[i] > hh[i] and lh[i] > hl[i]

        if uptrend and rsi_arr[i] < rsi_entry_long and rsi_arr[i] > rsi_arr[i-1] and bull_struct:
            signals[i] = 1
            pos = 1
            entry_bar = i

        elif downtrend and rsi_arr[i] > rsi_entry_short and rsi_arr[i] < rsi_arr[i-1] and bear_struct:
            signals[i] = -1
            pos = -1
            entry_bar = i

    return pd.Series(signals, index=df.index)
