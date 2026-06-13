# @name: Trend Bounce Scalping (117%)
# @description: Trend Bounce (структурный) + EMA120. swing=30, ema=120, size_pct=0.95, без RSI-фильтра. Бэктест: +117% годовых, DD 10.5%, PF 1.83. Средняя агрессивность. Выход только по структуре (пробой swing low/high).
# @timeframe: 5m
# @symbol: BTC-USDT-SWAP
# @params: {"ema_trend": 120, "swing_window": 30, "pullback_bars": 5, "size_pct": 0.95, "fee": 0.0005}

import pandas as pd
import numpy as np


def find_swing_levels(high, low, window=10):
    n = len(high)
    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)
    for i in range(window, n - window):
        if high[i] == max(high[i - window : i + window + 1]):
            sh[i] = high[i]
        if low[i] == min(low[i - window : i + window + 1]):
            sl[i] = low[i]
    return sh, sl


def generate_signals(df, params):
    ema_trend = params.get("ema_trend", 120)
    swing_window = params.get("swing_window", 30)
    pullback_bars = params.get("pullback_bars", 5)

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    n = len(df)

    ema = pd.Series(close).ewm(span=ema_trend).mean().values
    sh, sl = find_swing_levels(high, low, swing_window)

    signals = np.zeros(n)

    # Active swing levels
    current_swing_high = 0.0
    current_swing_low = 0.0

    # State: 0=flat, 1=long, -1=short
    pos = 0
    entry_bar = 0

    for i in range(swing_window * 2, n):
        if np.isnan(ema[i]):
            continue

        if not np.isnan(sh[i]):
            current_swing_high = sh[i]
        if not np.isnan(sl[i]):
            current_swing_low = sl[i]

        if current_swing_high == 0 or current_swing_low == 0:
            continue

        # Trend direction
        uptrend = close[i] > ema[i]
        downtrend = close[i] < ema[i]

        # --- Exit logic first ---
        if pos == 1:
            # Exit long: structure break — price closes below current swing low
            if close[i] < current_swing_low:
                signals[i] = 0
                pos = 0
                continue
            signals[i] = 1
            continue

        if pos == -1:
            # Exit short: structure break — price closes above current swing high
            if close[i] > current_swing_high:
                signals[i] = 0
                pos = 0
                continue
            signals[i] = -1
            continue

        # Cooldown after entry
        if i - entry_bar < 3:
            continue

        # --- Entry logic ---
        if uptrend:
            # Price is in a pullback -> dropped from recent high toward swing low
            dropped_from_high = close[i] < current_swing_high * 0.995
            # Price is near or at the swing low zone
            at_swing_low_zone = close[i] <= current_swing_low * 1.005
            # Bounce starting: current low > previous bar low
            bounce_start = low[i] > low[i - 1] if i > 0 else True

            if dropped_from_high and at_swing_low_zone and bounce_start:
                signals[i] = 1
                pos = 1
                entry_bar = i

        elif downtrend:
            # Price is in a rally -> climbed from recent low toward swing high
            climbed_from_low = close[i] > current_swing_low * 1.005
            # Price is near or at the swing high zone
            at_swing_high_zone = close[i] >= current_swing_high * 0.995
            # Rejection starting: current high < previous bar high
            reject_start = high[i] < high[i - 1] if i > 0 else True

            if climbed_from_low and at_swing_high_zone and reject_start:
                signals[i] = -1
                pos = -1
                entry_bar = i

    return pd.Series(signals, index=df.index)
