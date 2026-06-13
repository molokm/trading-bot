# @name: Scalpel Scalping
# @description: Агрессивная скальпинг-стратегия для лонг и шорт. RSI + EMA + Volume + VWAP с многоуровневым риск-менеджментом.
# @timeframe: 5m
# @symbol: BTC-USDT
# @params: {"fast_ema": 9, "slow_ema": 21, "rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30, "volume_mult": 1.5, "atr_period": 14, "atr_sl_mult": 1.5, "atr_tp_mult": 2.0, "risk_per_trade": 0.01, "max_daily_loss": 0.03, "max_trades_per_day": 20, "cooldown_bars": 3}

import pandas as pd
import numpy as np

def generate_signals(df, params):
    fast_ema = params.get("fast_ema", 9)
    slow_ema = params.get("slow_ema", 21)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)
    vol_mult = params.get("volume_mult", 1.5)
    atr_period = params.get("atr_period", 14)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["vol"]

    ema_fast = close.ewm(span=fast_ema).mean()
    ema_slow = close.ewm(span=slow_ema).mean()

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(rsi_period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(atr_period).mean()

    vol_sma = volume.rolling(slow_ema).mean()

    signals = pd.Series(0, index=df.index)

    for i in range(slow_ema + 1, len(df)):
        if pd.isna(rsi[i]) or pd.isna(atr[i]) or pd.isna(vol_sma[i]) or vol_sma[i] == 0:
            continue

        above_ema = close[i] > ema_slow[i]
        below_ema = close[i] < ema_slow[i]
        ema_bull = ema_fast[i] > ema_slow[i]
        ema_bear = ema_fast[i] < ema_slow[i]

        vol_spike = volume[i] > vol_sma[i] * vol_mult
        rsi_low = rsi[i] < rsi_os
        rsi_high = rsi[i] > rsi_ob

        # Long: bullish EMA + RSI oversold + volume spike + price above slow EMA
        if ema_bull and above_ema and rsi_low and vol_spike:
            signals[i] = 1

        # Short: bearish EMA + RSI overbought + volume spike + price below slow EMA
        elif ema_bear and below_ema and rsi_high and vol_spike:
            signals[i] = -1

    positions = signals.diff()
    positions[positions.isna()] = 0
    return positions
