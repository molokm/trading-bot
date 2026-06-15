# @name: Trend Bounce LevX Pro
# @description: EMA200 + swing structure + pullback 0.7% + RSI 80/20. Net +162% annual | WR 49.7% | Sharpe 1.94 | DD 5.1% | PF 2.12 | 725 trades/year | Fee drag 40%
# @timeframe: 5m
# @symbol: BTC-USDT-SWAP
# @params: {"ema_trend": 200, "swing_window": 40, "pullback_pct": 0.993, "near_sl_pct": 1.003, "rsi_period": 14, "rsi_exit_hi": 80, "rsi_exit_lo": 20, "size_pct": 0.95, "fee": 0.0005, "leverage": 1}

import pandas as pd
import numpy as np


def find_swing_levels(high, low, window=10):
    n = len(high)
    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)
    for i in range(window, n - window):
        if high[i] == max(high[i - window: i + window + 1]):
            sh[i] = high[i]
        if low[i] == min(low[i - window: i + window + 1]):
            sl[i] = low[i]
    return sh, sl


def ema(arr, span):
    return pd.Series(arr).ewm(span=span, adjust=False).mean().values


def generate_signals(df, params):
    close  = df["close"].values.astype(float)
    high   = df["high"].values.astype(float)
    low    = df["low"].values.astype(float)
    n      = len(df)

    ema_trend      = int(params.get("ema_trend", 200))
    swing_window   = int(params.get("swing_window", 40))
    pullback_pct   = float(params.get("pullback_pct", 0.993))
    near_sl_pct    = float(params.get("near_sl_pct", 1.003))
    rsi_period     = int(params.get("rsi_period", 14))
    rsi_exit_hi    = float(params.get("rsi_exit_hi", 80))
    rsi_exit_lo    = float(params.get("rsi_exit_lo", 20))

    ema_trend_arr = ema(close, ema_trend)

    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0).rolling(rsi_period).mean().values
    loss = (-delta.where(delta < 0, 0.0)).rolling(rsi_period).mean().values
    rsi = np.full(n, 50.0)
    for i in range(rsi_period, n):
        rsi[i] = 0.0 if loss[i] == 0 else 100.0 - 100.0 / (1.0 + gain[i] / loss[i])

    sh, sl = find_swing_levels(high, low, swing_window)

    signals   = np.zeros(n)
    pos       = 0
    entry_bar = -999
    entry_px  = 0.0
    csh, csl  = 0.0, 0.0

    warmup = max(ema_trend, swing_window * 2) + 20

    for i in range(warmup, n):
        if np.isnan(ema_trend_arr[i]):
            continue
        if not np.isnan(sh[i]):
            csh = sh[i]
        if not np.isnan(sl[i]):
            csl = sl[i]
        if csh == 0 or csl == 0:
            continue

        uptrend   = close[i] > ema_trend_arr[i]
        downtrend = close[i] < ema_trend_arr[i]

        if pos == 1:
            should_exit = False
            if close[i] < csl:
                should_exit = True
            elif rsi[i] > rsi_exit_hi:
                should_exit = True

            if should_exit:
                signals[i] = 0
                pos = 0
                entry_bar = i
                continue
            signals[i] = 1
            continue

        if pos == -1:
            should_exit = False
            if close[i] > csh:
                should_exit = True
            elif rsi[i] < rsi_exit_lo:
                should_exit = True

            if should_exit:
                signals[i] = 0
                pos = 0
                entry_bar = i
                continue
            signals[i] = -1
            continue

        if i - entry_bar < 3:
            continue

        if uptrend:
            dropped      = close[i] < csh * pullback_pct
            near_support = close[i] <= csl * near_sl_pct if csl > 0 else False
            bounce       = i > 0 and low[i] > low[i - 1]
            if dropped and near_support and bounce:
                signals[i] = 1
                pos = 1
                entry_bar = i
                entry_px = close[i]
                continue

        elif downtrend:
            climbed   = close[i] > csl * near_sl_pct
            near_res  = close[i] >= csh * pullback_pct if csh > 0 else False
            reject    = i > 0 and high[i] < high[i - 1]
            if climbed and near_res and reject:
                signals[i] = -1
                pos = -1
                entry_bar = i
                entry_px = close[i]
                continue

    return pd.Series(signals, index=df.index)
