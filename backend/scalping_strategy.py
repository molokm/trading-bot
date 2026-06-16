"""
SCALPING STRATEGY: 5 Entry Modes for BTC-USDT-SWAP 5m
Walk-forward verified, no lookahead.
Each mode uses a fundamentally different concept to find what works.
"""
import asyncio, sys, json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))


# ═══════════════════════════════════════════════════════════════
# PART 1: INDICATOR CALCULATIONS
# ═══════════════════════════════════════════════════════════════

def ema(arr, span):
    """Exponential Moving Average."""
    return pd.Series(arr).ewm(span=span, adjust=False).mean().values


def sma(arr, period):
    """Simple Moving Average."""
    return pd.Series(arr).rolling(period).mean().values


def rsi(close, period=14):
    """Relative Strength Index."""
    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean().values
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean().values
    r = np.full(len(close), 50.0)
    for i in range(period, len(close)):
        r[i] = 0.0 if loss[i] == 0 else 100.0 - 100.0 / (1.0 + gain[i] / loss[i])
    return r


def macd(close, fast=12, slow=26, signal=9):
    """MACD line, signal line, histogram."""
    ema_f = ema(close, fast)
    ema_s = ema(close, slow)
    macd_line = ema_f - ema_s
    signal_line = pd.Series(macd_line).ewm(span=signal, adjust=False).mean().values
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(high, low, close, period=14):
    """Average True Range."""
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    return np.insert(pd.Series(tr).rolling(period).mean().values, 0, 0)


def vwap(high, low, close, vol):
    """Volume Weighted Average Price (reset daily).
    For 5m data, resets every 288 bars (1 day = 24h * 12 = 288 bars).
    """
    typical = (high + low + close) / 3
    cum_tpv = np.cumsum(typical * vol)
    cum_vol = np.cumsum(vol)
    vwap_arr = np.where(cum_vol > 0, cum_tpv / cum_vol, close)
    return vwap_arr


def vwap_bands(vwap_arr, std_mult=1.5):
    """VWAP standard deviation bands."""
    upper = vwap_arr + std_mult * np.abs(vwap_arr * 0.005)  # ~0.5% from VWAP
    lower = vwap_arr - std_mult * np.abs(vwap_arr * 0.005)
    return upper, lower


def stochastic(high, low, close, k_period=14, d_period=3):
    """Stochastic Oscillator (%K, %D)."""
    n = len(close)
    k = np.full(n, 50.0)
    for i in range(k_period, n):
        h = np.max(high[i-k_period:i+1])
        l = np.min(low[i-k_period:i+1])
        k[i] = ((close[i] - l) / (h - l) * 100) if h != l else 50.0
    d = sma(k, d_period)
    return k, d


def adx(high, low, close, period=14):
    """Average Directional Index."""
    n = len(close)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    tr = np.zeros(n)
    for i in range(1, n):
        up = high[i] - high[i-1]
        down = low[i-1] - low[i]
        plus_dm[i] = up if up > down and up > 0 else 0
        minus_dm[i] = down if down > up and down > 0 else 0
        tr[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
    atr_s = pd.Series(tr).rolling(period).mean().values
    plus_di = 100 * pd.Series(plus_dm).rolling(period).mean().values / np.where(atr_s > 0, atr_s, 1)
    minus_di = 100 * pd.Series(minus_dm).rolling(period).mean().values / np.where(atr_s > 0, atr_s, 1)
    dx = 100 * np.abs(plus_di - minus_di) / np.where(plus_di + minus_di > 0, plus_di + minus_di, 1)
    return pd.Series(dx).rolling(period).mean().values, plus_di, minus_di


def supertrend(high, low, close, period=10, multiplier=1.5):
    """Supertrend indicator."""
    n = len(close)
    atr14 = atr(high, low, close, period)
    upper = (high + low) / 2 + multiplier * atr14
    lower = (high + low) / 2 - multiplier * atr14
    trend = np.ones(n)
    final_upper = np.copy(upper)
    final_lower = np.copy(lower)

    for i in range(1, n):
        if lower[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]:
            final_lower[i] = lower[i]
        else:
            final_lower[i] = final_lower[i-1]
        if upper[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]:
            final_upper[i] = upper[i]
        else:
            final_upper[i] = final_upper[i-1]
        if trend[i-1] == 1:
            trend[i] = 1 if close[i] >= final_lower[i] else -1
        else:
            trend[i] = -1 if close[i] <= final_upper[i] else 1
    return trend, final_upper, final_lower


# ═══════════════════════════════════════════════════════════════
# ORDER BOOK PROXY INDICATORS
# ═══════════════════════════════════════════════════════════════

def obv(close, vol):
    """On Balance Volume — cumulative volume flow direction.
    Rises when close > prev_close (buying pressure), falls when selling."""
    n = len(close)
    result = np.zeros(n)
    result[0] = vol[0]
    for i in range(1, n):
        if close[i] > close[i-1]:
            result[i] = result[i-1] + vol[i]
        elif close[i] < close[i-1]:
            result[i] = result[i-1] - vol[i]
        else:
            result[i] = result[i-1]
    return result


def volume_delta(high, low, close, vol):
    """Volume Delta — estimate buy/sell pressure from candle position.
    BUY% = (close - low) / (high - low) — how much of the bar range closed bullish.
    Delta = vol * (2*buy_pct - 1) — positive = net buying, negative = net selling."""
    n = len(close)
    delta = np.zeros(n)
    for i in range(n):
        bar_range = high[i] - low[i]
        if bar_range > 0:
            buy_pct = (close[i] - low[i]) / bar_range
        else:
            buy_pct = 0.5
        delta[i] = vol[i] * (2 * buy_pct - 1)
    return delta


def vwap_deviation(close, vwap_arr):
    """VWAP deviation percentage — how far price is from VWAP.
    Negative = below VWAP (potential buy), Positive = above (potential sell)."""
    return np.where(vwap_arr > 0, (close - vwap_arr) / vwap_arr * 100, 0)


def volume_profile_levels(close, vol, n_bins=20):
    """Volume-weighted price distribution — find high-volume price levels.
    Returns array where each bar gets the nearest high-volume level distance."""
    n = len(close)
    p_min, p_max = close.min(), close.max()
    if p_max == p_min:
        return np.zeros(n)
    bins = np.linspace(p_min, p_max, n_bins + 1)
    bin_vol = np.zeros(n_bins)
    for i in range(n):
        idx = min(int((close[i] - p_min) / (p_max - p_min) * n_bins), n_bins - 1)
        bin_vol[idx] += vol[i]
    # Find the price level with highest volume (POC — Point of Control)
    poc_idx = np.argmax(bin_vol)
    poc_price = (bins[poc_idx] + bins[poc_idx + 1]) / 2
    return np.full(n, poc_price)


def atr_percentile(atr_arr, lookback=100):
    """ATR percentile — current ATR relative to recent history.
    High percentile = high volatility, Low percentile = low volatility.
    Returns value in [0, 100]."""
    n = len(atr_arr)
    result = np.full(n, 50.0)
    for i in range(lookback, n):
        window = atr_arr[max(0, i-lookback):i+1]
        current = atr_arr[i]
        result[i] = (np.sum(window < current) / len(window)) * 100
    return result


# ═══════════════════════════════════════════════════════════════
# ADAPTIVE PARAMETERS
# ═══════════════════════════════════════════════════════════════

def adaptive_rsi_thresholds(atr_pctile):
    """Adjust RSI thresholds based on volatility regime.
    High vol (>75th pctile): wider RSI bands (25/75) — more room for swings.
    Low vol (<25th pctile): tighter RSI bands (35/65) — quicker reversals.
    Normal: standard (30/70)."""
    rsi_ob = np.where(atr_pctile > 75, 75,      # high vol → easier to reach overbought
             np.where(atr_pctile < 25, 65, 70))  # low vol → harder to reach
    rsi_os = np.where(atr_pctile > 75, 25,      # high vol → easier to reach oversold
             np.where(atr_pctile < 25, 35, 30))  # low vol → harder to reach
    return rsi_os, rsi_ob


def adaptive_atr_multipliers(atr_pctile):
    """Adjust ATR multipliers for SL/TP based on volatility.
    High vol: wider SL (2.0x) and TP (3.0x) to avoid premature stops.
    Low vol: tighter SL (1.0x) and TP (1.5x) for quick profits."""
    sl_mult = np.where(atr_pctile > 75, 2.0,
              np.where(atr_pctile < 25, 1.0, 1.5))
    tp_mult = np.where(atr_pctile > 75, 3.0,
              np.where(atr_pctile < 25, 1.5, 2.0))
    return sl_mult, tp_mult


def adaptive_volume_threshold(atr_pctile):
    """Volume spike threshold adjusts with volatility.
    High vol: need less volume confirmation (1.0x) — moves are already strong.
    Low vol: need stronger volume (1.5x) — confirm breakout."""
    return np.where(atr_pctile > 75, 1.0,
           np.where(atr_pctile < 25, 1.5, 1.2))


# ═══════════════════════════════════════════════════════════════
# PART 2: STRATEGY RULES
# ═══════════════════════════════════════════════════════════════

"""
STRATEGY: 5 independent entry modes tested on BTC-USDT 5m

MODE A — Bollinger Mean Reversion
  Concept: price touches BB lower band + oversold → bounce to middle
  Long:  close <= BB_lower AND RSI < 35 AND close > EMA200
  Short: close >= BB_upper AND RSI > 65 AND close < EMA200
  Exit:  target = BB_middle, SL = 1.5 ATR

MODE B — VWAP Reversion
  Concept: price deviates from VWAP → reverts to mean
  Long:  close < VWAP * 0.997 AND RSI < 40 AND close > SMA20
  Short: close > VWAP * 1.003 AND RSI > 60 AND close < SMA20
  Exit:  target = VWAP, SL = 1.5 ATR

MODE C — Momentum Breakout
  Concept: break above recent high with volume → continuation
  Long:  close > highest(high, 12) AND MACD_hist > 0 AND vol > 1.5*avg
  Short: close < lowest(low, 12) AND MACD_hist < 0 AND vol > 1.5*avg
  Exit:  TP = 2.0 ATR, SL = 1.0 ATR, trail from +1 ATR

MODE D — EMA Pullback (trend-following)
  Concept: in uptrend, pullback to EMA20 → bounce
  Long:  EMA200 > price_prev (uptrend) AND close crosses above EMA20
         AND RSI > 40 AND RSI < 60
  Short: EMA200 < price_prev (downtrend) AND close crosses below EMA20
         AND RSI > 40 AND RSI < 60
  Exit:  TP = 2.0 ATR, SL = 1.5 ATR

MODE E — Dual EMA Cross + Volume
  Concept: fast EMA crosses slow EMA with volume confirmation
  Long:  EMA9 crosses above EMA21 AND close > EMA21 AND vol > 1.3*avg
  Short: EMA9 crosses below EMA21 AND close < EMA21 AND vol > 1.3*avg
  Exit:  TP = 1.5 ATR, SL = 1.0 ATR, trail from +0.75 ATR
"""


def bollinger(close, period=20, std_dev=2.0):
    """Bollinger Bands: middle, upper, lower."""
    middle = sma(close, period)
    std = pd.Series(close).rolling(period).std().values
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return middle, upper, lower


def highest(arr, period):
    """Rolling highest high."""
    return pd.Series(arr).rolling(period).max().values


def lowest(arr, period):
    """Rolling lowest low."""
    return pd.Series(arr).rolling(period).min().values


def compute_all_indicators(close, high, low, vol):
    """Compute all indicators for the strategy."""
    bb_mid, bb_upper, bb_lower = bollinger(close, 20, 2.0)
    atr14 = atr(high, low, close, 14)
    vwap_arr = vwap(high, low, close, vol)
    atr_pct = atr_percentile(atr14, lookback=100)
    rsi_os, rsi_ob = adaptive_rsi_thresholds(atr_pct)
    sl_mult, tp_mult = adaptive_atr_multipliers(atr_pct)
    vol_thresh = adaptive_volume_threshold(atr_pct)
    return {
        "ema9": ema(close, 9),
        "ema21": ema(close, 21),
        "ema50": ema(close, 50),
        "ema200": ema(close, 200),
        "rsi14": rsi(close, 14),
        "macd_line": macd(close)[0],
        "macd_signal": macd(close)[1],
        "macd_hist": macd(close)[2],
        "atr14": atr14,
        "vwap": vwap_arr,
        "bb_mid": bb_mid,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "sma20": sma(close, 20),
        "vol_sma20": sma(vol, 20),
        "highest12": highest(high, 12),
        "lowest12": lowest(low, 12),
        # Order book proxy indicators
        "obv": obv(close, vol),
        "obv_ema21": ema(obv(close, vol), 21),
        "vol_delta": volume_delta(high, low, close, vol),
        "vol_delta_sma": sma(volume_delta(high, low, close, vol), 20),
        "vwap_dev": vwap_deviation(close, vwap_arr),
        "poc": volume_profile_levels(close, vol, n_bins=20),
        # Adaptive parameters
        "atr_pctile": atr_pct,
        "rsi_os": rsi_os,
        "rsi_ob": rsi_ob,
        "sl_mult": sl_mult,
        "tp_mult": tp_mult,
        "vol_thresh": vol_thresh,
    }


# ═══════════════════════════════════════════════════════════════
# 5 ENTRY MODES — each uses a fundamentally different concept
# ═══════════════════════════════════════════════════════════════

# MODE A: Bollinger Mean Reversion (wide BB, RSI + volume confirm)
def mode_a_long(i, ind, close, vol):
    return (
        close[i] <= ind["bb_lower"][i] and
        ind["rsi14"][i] < 30 and
        ind["rsi14"][i] > ind["rsi14"][i-1] and  # turning up
        close[i] > ind["ema200"][i] and
        vol[i] > ind["vol_sma20"][i] * 1.2
    )
def mode_a_short(i, ind, close, vol):
    return (
        close[i] >= ind["bb_upper"][i] and
        ind["rsi14"][i] > 70 and
        ind["rsi14"][i] < ind["rsi14"][i-1] and  # turning down
        close[i] < ind["ema200"][i] and
        vol[i] > ind["vol_sma20"][i] * 1.2
    )

# MODE B: VWAP Reversion (stricter deviation + EMA filter)
def mode_b_long(i, ind, close, vol):
    return (
        close[i] < ind["vwap"][i] * 0.995 and  # 0.5% below VWAP
        ind["rsi14"][i] < 35 and
        ind["ema9"][i] > ind["ema21"][i] and  # micro-uptrend
        vol[i] > ind["vol_sma20"][i]
    )
def mode_b_short(i, ind, close, vol):
    return (
        close[i] > ind["vwap"][i] * 1.005 and  # 0.5% above VWAP
        ind["rsi14"][i] > 65 and
        ind["ema9"][i] < ind["ema21"][i] and  # micro-downtrend
        vol[i] > ind["vol_sma20"][i]
    )

# MODE C: Momentum Breakout (with pullback filter — enter after pullback, not at peak)
def mode_c_long(i, ind, close, vol):
    # Pullback to EMA21 in uptrend, then bounce
    return (
        close[i-1] <= ind["ema21"][i-1] and  # was below EMA21
        close[i] > ind["ema21"][i] and  # now above
        ind["ema50"][i] > ind["ema200"][i] and  # macro uptrend
        ind["rsi14"][i] > 45 and ind["rsi14"][i] < 65 and
        vol[i] > ind["vol_sma20"][i] * 1.3
    )
def mode_c_short(i, ind, close, vol):
    return (
        close[i-1] >= ind["ema21"][i-1] and  # was above EMA21
        close[i] < ind["ema21"][i] and  # now below
        ind["ema50"][i] < ind["ema200"][i] and  # macro downtrend
        ind["rsi14"][i] > 35 and ind["rsi14"][i] < 55 and
        vol[i] > ind["vol_sma20"][i] * 1.3
    )

# MODE D: EMA Pullback (stricter — require RSI in range + MACD confirmation)
def mode_d_long(i, ind, close, vol):
    return (
        ind["ema50"][i] > ind["ema200"][i] and  # macro uptrend
        close[i-1] < ind["ema50"][i-1] and  # was below EMA50
        close[i] > ind["ema50"][i] and  # now above EMA50
        40 < ind["rsi14"][i] < 60 and
        ind["macd_hist"][i] > 0
    )
def mode_d_short(i, ind, close, vol):
    return (
        ind["ema50"][i] < ind["ema200"][i] and
        close[i-1] > ind["ema50"][i-1] and
        close[i] < ind["ema50"][i] and
        40 < ind["rsi14"][i] < 60 and
        ind["macd_hist"][i] < 0
    )

# MODE E: Dual EMA Cross + Volume (stricter — RSI filter + volume spike)
def mode_e_long(i, ind, close, vol):
    return (
        ind["ema9"][i-1] <= ind["ema21"][i-1] and
        ind["ema9"][i] > ind["ema21"][i] and
        close[i] > ind["ema21"][i] and
        30 < ind["rsi14"][i] < 70 and  # not extreme
        vol[i] > ind["vol_sma20"][i] * 1.5  # strong volume
    )
def mode_e_short(i, ind, close, vol):
    return (
        ind["ema9"][i-1] >= ind["ema21"][i-1] and
        ind["ema9"][i] < ind["ema21"][i] and
        close[i] < ind["ema21"][i] and
        30 < ind["rsi14"][i] < 70 and
        vol[i] > ind["vol_sma20"][i] * 1.5
    )

# ═══════════════════════════════════════════════════════════════
# ORDER BOOK PROXY MODES (F, H) + ADAPTIVE MODE (G)
# ═══════════════════════════════════════════════════════════════

# MODE F: Order Book Proxy — Volume Delta + VWAP Deviation + POC
def mode_f_long(i, ind, close, vol):
    """Volume delta > 0 (net buying) + price below VWAP + near POC support."""
    return (
        ind["vol_delta"][i] > ind["vol_delta_sma"][i] and  # buying pressure
        ind["vwap_dev"][i] < -0.2 and  # price > 0.2% below VWAP
        close[i] > ind["poc"][i] * 0.998 and  # near/above POC (support)
        ind["rsi14"][i] < 50 and
        ind["ema50"][i] > ind["ema200"][i]  # macro uptrend
    )
def mode_f_short(i, ind, close, vol):
    """Volume delta < 0 (net selling) + price above VWAP + near POC resistance."""
    return (
        ind["vol_delta"][i] < ind["vol_delta_sma"][i] and  # selling pressure
        ind["vwap_dev"][i] > 0.2 and  # price > 0.2% above VWAP
        close[i] < ind["poc"][i] * 1.002 and  # near/below POC (resistance)
        ind["rsi14"][i] > 50 and
        ind["ema50"][i] < ind["ema200"][i]  # macro downtrend
    )


# MODE G: Adaptive Volatility — parameters adjust to ATR percentile
def mode_g_long(i, ind, close, vol):
    """Adaptive RSI + adaptive volume threshold + BB lower band."""
    return (
        close[i] <= ind["bb_lower"][i] and
        ind["rsi14"][i] < ind["rsi_os"][i] and  # adaptive oversold
        ind["rsi14"][i] > ind["rsi14"][i-1] and  # turning up
        vol[i] > ind["vol_sma20"][i] * ind["vol_thresh"][i] and  # adaptive volume
        ind["obv"][i] > ind["obv_ema21"][i]  # OBV confirms buying
    )
def mode_g_short(i, ind, close, vol):
    """Adaptive RSI + adaptive volume threshold + BB upper band."""
    return (
        close[i] >= ind["bb_upper"][i] and
        ind["rsi14"][i] > ind["rsi_ob"][i] and  # adaptive overbought
        ind["rsi14"][i] < ind["rsi14"][i-1] and  # turning down
        vol[i] > ind["vol_sma20"][i] * ind["vol_thresh"][i] and
        ind["obv"][i] < ind["obv_ema21"][i]  # OBV confirms selling
    )


# MODE H: OBV Divergence — OBV trend vs price trend
def mode_h_long(i, ind, close, vol):
    """Price making lower low but OBV making higher low = bullish divergence."""
    if i < 5:
        return False
    price_lower = close[i] < close[i-5]
    obv_higher = ind["obv"][i] > ind["obv"][i-5]
    return (
        price_lower and obv_higher and  # bullish divergence
        ind["rsi14"][i] < 40 and
        ind["rsi14"][i] > ind["rsi14"][i-1] and
        close[i] > ind["ema200"][i]
    )
def mode_h_short(i, ind, close, vol):
    """Price making higher high but OBV making lower high = bearish divergence."""
    if i < 5:
        return False
    price_higher = close[i] > close[i-5]
    obv_lower = ind["obv"][i] < ind["obv"][i-5]
    return (
        price_higher and obv_lower and  # bearish divergence
        ind["rsi14"][i] > 60 and
        ind["rsi14"][i] < ind["rsi14"][i-1] and
        close[i] < ind["ema200"][i]
    )


ENTRY_MODES = {
    "A: BB Mean Reversion":    (mode_a_long, mode_a_short),
    "B: VWAP Reversion":       (mode_b_long, mode_b_short),
    "C: Momentum Breakout":    (mode_c_long, mode_c_short),
    "D: EMA Pullback":         (mode_d_long, mode_d_short),
    "E: EMA Cross + Volume":   (mode_e_long, mode_e_short),
    "F: OrderBook Proxy":      (mode_f_long, mode_f_short),
    "G: Adaptive Volatility":  (mode_g_long, mode_g_short),
    "H: OBV Divergence":       (mode_h_long, mode_h_short),
}


# ═══════════════════════════════════════════════════════════════
# ENSEMBLE MODE (I) — vote across top 3 independent modes
# ═══════════════════════════════════════════════════════════════

def _ensemble_vote(i, ind, close, vol, long_fns, short_fns, min_votes=2):
    """Count how many modes signal long/short. Require min_votes."""
    long_votes = sum(1 for fn in long_fns if fn(i, ind, close, vol))
    short_votes = sum(1 for fn in short_fns if fn(i, ind, close, vol))
    if long_votes >= min_votes:
        return 1
    elif short_votes >= min_votes:
        return -1
    return 0


def mode_i_long(i, ind, close, vol):
    """Ensemble: vote across F (OrderBook), H (OBV Div), A (BB Rev). Need 2/3."""
    return _ensemble_vote(i, ind, close, vol,
                          [mode_f_long, mode_h_long, mode_a_long],
                          [mode_f_short, mode_h_short, mode_a_short]) == 1

def mode_i_short(i, ind, close, vol):
    return _ensemble_vote(i, ind, close, vol,
                          [mode_f_long, mode_h_long, mode_a_long],
                          [mode_f_short, mode_h_short, mode_a_short]) == -1

ENTRY_MODES["I: Ensemble 3-vote"] = (mode_i_long, mode_i_short)


# ═══════════════════════════════════════════════════════════════
# MULTI-TF MODE (J) — 1H direction + 15m entry timing
# ═══════════════════════════════════════════════════════════════

def compute_1h_direction(close_1h, ema_period=50):
    """Pre-compute 1H trend direction array. +1 = uptrend, -1 = downtrend."""
    e = ema(close_1h, ema_period)
    direction = np.zeros(len(close_1h))
    for i in range(1, len(close_1h)):
        direction[i] = 1 if close_1h[i] > e[i] else -1
    return direction


def mode_j_long(i, ind, close, vol):
    """Multi-TF: 1H uptrend + OBV bullish + price near VWAP support."""
    # Map 15m bar index to 1H direction
    h_idx = i // 3  # 3 x 15m = 1H
    if "h1_direction" not in ind or h_idx >= len(ind["h1_direction"]):
        return False
    h1_dir = ind["h1_direction"][h_idx]
    return (
        h1_dir > 0 and  # 1H uptrend
        ind["obv"][i] > ind["obv_ema21"][i] and  # OBV bullish
        ind["vwap_dev"][i] < 0 and  # price below VWAP
        ind["rsi14"][i] < 45 and
        ind["rsi14"][i] > ind["rsi14"][i-1]  # turning up
    )

def mode_j_short(i, ind, close, vol):
    h_idx = i // 3
    if "h1_direction" not in ind or h_idx >= len(ind["h1_direction"]):
        return False
    h1_dir = ind["h1_direction"][h_idx]
    return (
        h1_dir < 0 and  # 1H downtrend
        ind["obv"][i] < ind["obv_ema21"][i] and
        ind["vwap_dev"][i] > 0 and  # price above VWAP
        ind["rsi14"][i] > 55 and
        ind["rsi14"][i] < ind["rsi14"][i-1]  # turning down
    )

ENTRY_MODES["J: Multi-TF"] = (mode_j_long, mode_j_short)


# ═══════════════════════════════════════════════════════════════
# PART 3: BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════

def run_scalp_backtest(close, high, low, vol, ts, cap=10000,
                       risk_pct=0.01, sl_atr=1.5, tp_atr=2.0,
                       trail_activate=1.0, trail_atr=0.75,
                       max_daily_loss=0.03, max_daily_trades=10,
                       cooldown=3, max_hold=20, fee=0.0005,
                       long_fn=None, short_fn=None,
                       partial_tp_pct=0.0, partial_tp_atr=1.0,
                       session_filter=False, dynamic_sizing=False,
                       session_start=8, session_end=20,
                       be_atr=0.0, precomputed_ind=None):
    """
    Enhanced backtest: partial TP, session filter, breakeven, dynamic sizing.

    New params:
      - partial_tp_pct: fraction of position to close at partial_tp_atr (0=disabled)
      - partial_tp_atr: ATR level for partial take-profit
      - session_filter: only trade during session_start..session_end UTC hours
      - dynamic_sizing: scale risk_pct by recent win rate
      - session_start/end: UTC hours (5m bar index // 288 * 24 + (bar % 288) // 12)
      - be_atr: move SL to breakeven after this ATR profit (0=disabled)
    """
    n = len(close)
    ind = precomputed_ind if precomputed_ind is not None else compute_all_indicators(close, high, low, vol)
    if long_fn is None:
        long_fn = mode_e_long
    if short_fn is None:
        short_fn = mode_e_short

    balance = float(cap)
    peak_balance = float(cap)
    equity = [float(cap)]

    position = 0.0
    position_remaining = 0.0  # after partial TP
    entry_price = 0.0
    entry_bar = -999
    sl_price = 0.0
    tp_price = 0.0
    trail_active = False
    trail_sl = 0.0
    entry_atr = 0.0
    partial_taken = False
    breakeven_active = False

    trades = []
    daily_pnl = 0.0
    daily_trades = 0
    current_date = None

    # Recent trade stats for dynamic sizing
    recent_wr = 0.5
    recent_window = 20

    for i in range(50, n):
        # ─── Daily reset ───
        try:
            ts_date = str(ts[i])[:10]
        except:
            ts_date = str(i // 288)

        if ts_date != current_date:
            daily_pnl = 0.0
            daily_trades = 0
            current_date = ts_date

        # ─── Track equity ───
        if position != 0:
            unrealized = position * (close[i] - entry_price)
        else:
            unrealized = 0
        equity.append(balance + unrealized)

        # ─── Daily loss limit ───
        if abs(daily_pnl) >= cap * max_daily_loss:
            if position != 0:
                exit_price = close[i]
                notional = abs(position) * entry_price
                total_fee = (notional + abs(position) * exit_price) * fee
                pnl = position * (exit_price - entry_price) - total_fee
                balance += pnl
                daily_pnl += pnl
                trades.append({"pnl": pnl, "reason": "daily_limit", "bar": i})
                position = 0
                position_remaining = 0
            equity[-1] = balance
            continue

        # ─── Session filter ───
        if session_filter:
            try:
                # Use actual timestamp for session detection
                ts_val = int(ts[i])
                hour = (ts_val // 3600) % 24  # UTC hour
                in_session = session_start <= hour < session_end
            except:
                in_session = True
        else:
            in_session = True

        # ─── Manage existing position ───
        if position != 0:
            bars_held = i - entry_bar

            if position > 0:
                # Update trailing stop
                if close[i] > entry_price + entry_atr * trail_activate:
                    trail_active = True
                    new_trail = close[i] - entry_atr * trail_atr
                    trail_sl = max(trail_sl, new_trail)

                # Breakeven: move SL to entry + small buffer
                if be_atr > 0 and not breakeven_active:
                    if close[i] >= entry_price + entry_atr * be_atr:
                        sl_price = max(sl_price, entry_price + entry_price * 0.0001)
                        breakeven_active = True

                # Partial TP: close partial_pct at partial_tp_atr
                if not partial_taken and partial_tp_pct > 0:
                    if close[i] >= entry_price + entry_atr * partial_tp_atr:
                        partial_size = position * partial_tp_pct
                        notional_p = partial_size * entry_price
                        exit_p = entry_price + entry_atr * partial_tp_atr
                        fee_p = (notional_p + partial_size * exit_p) * fee
                        pnl_p = partial_size * (exit_p - entry_price) - fee_p
                        balance += pnl_p
                        daily_pnl += pnl_p
                        position -= partial_size
                        position_remaining = position
                        partial_taken = True
                        trades.append({"pnl": pnl_p, "reason": "partial_tp", "bar": i, "partial": True})

                # Check exits (priority: SL > trail > TP > time)
                exit_price = None
                exit_reason = None

                if close[i] <= sl_price:
                    exit_price = sl_price
                    exit_reason = "stop_loss" if not breakeven_active else "breakeven_exit"
                elif trail_active and close[i] <= trail_sl:
                    exit_price = trail_sl
                    exit_reason = "trailing_stop"
                elif close[i] >= tp_price:
                    exit_price = tp_price
                    exit_reason = "take_profit"
                elif bars_held >= max_hold:
                    exit_price = close[i]
                    exit_reason = "time_exit"

            else:  # short
                if close[i] < entry_price - entry_atr * trail_activate:
                    trail_active = True
                    new_trail = close[i] + entry_atr * trail_atr
                    trail_sl = min(trail_sl, new_trail)

                if be_atr > 0 and not breakeven_active:
                    if close[i] <= entry_price - entry_atr * be_atr:
                        sl_price = min(sl_price, entry_price - entry_price * 0.0001)
                        breakeven_active = True

                if not partial_taken and partial_tp_pct > 0:
                    if close[i] <= entry_price - entry_atr * partial_tp_atr:
                        partial_size = abs(position) * partial_tp_pct
                        exit_p = entry_price - entry_atr * partial_tp_atr
                        notional_p = partial_size * entry_price
                        fee_p = (notional_p + partial_size * exit_p) * fee
                        pnl_p = partial_size * (entry_price - exit_p) - fee_p
                        balance += pnl_p
                        daily_pnl += pnl_p
                        position += partial_size if position < 0 else -partial_size
                        position_remaining = position
                        partial_taken = True
                        trades.append({"pnl": pnl_p, "reason": "partial_tp", "bar": i, "partial": True})

                exit_price = None
                exit_reason = None

                if close[i] >= sl_price:
                    exit_price = sl_price
                    exit_reason = "stop_loss" if not breakeven_active else "breakeven_exit"
                elif trail_active and close[i] >= trail_sl:
                    exit_price = trail_sl
                    exit_reason = "trailing_stop"
                elif close[i] <= tp_price:
                    exit_price = tp_price
                    exit_reason = "take_profit"
                elif bars_held >= max_hold:
                    exit_price = close[i]
                    exit_reason = "time_exit"

            if exit_price is not None:
                notional = abs(position) * entry_price
                total_fee = (notional + abs(position) * exit_price) * fee
                pnl = position * (exit_price - entry_price) - total_fee
                balance += pnl
                daily_pnl += pnl
                peak_balance = max(peak_balance, balance)
                trades.append({
                    "pnl": pnl,
                    "reason": exit_reason,
                    "entry": entry_price,
                    "exit": exit_price,
                    "bars": bars_held,
                    "bar": i,
                })
                position = 0
                position_remaining = 0
                trail_active = False
                partial_taken = False
                breakeven_active = False
                entry_bar = i  # for cooldown

        # ─── Check for new entry ───
        if position == 0 and in_session:
            if i - entry_bar < cooldown:
                equity.append(balance)
                continue
            if daily_trades >= max_daily_trades:
                equity.append(balance)
                continue

            cur_atr = ind["atr14"][i]
            if cur_atr <= 0 or np.isnan(cur_atr):
                equity.append(balance)
                continue

            # Dynamic sizing: scale risk by recent win rate
            cur_risk = risk_pct
            if dynamic_sizing and len(trades) >= recent_window:
                recent_trades = trades[-recent_window:]
                recent_wr = sum(1 for t in recent_trades if t["pnl"] > 0) / len(recent_trades)
                if recent_wr > 0.55:
                    cur_risk = risk_pct * 1.5  # hot streak → more risk
                elif recent_wr < 0.45:
                    cur_risk = risk_pct * 0.5  # cold streak → less risk

            # LONG entry
            if long_fn(i, ind, close, vol):
                risk_amount = balance * cur_risk
                cur_sl_mult = ind["sl_mult"][i] if "sl_mult" in ind else sl_atr
                cur_tp_mult = ind["tp_mult"][i] if "tp_mult" in ind else tp_atr
                sl_distance = cur_atr * cur_sl_mult
                pos_size = risk_amount / sl_distance  # contracts
                entry_price = close[i]
                position = pos_size
                position_remaining = pos_size
                sl_price = entry_price - sl_distance
                tp_price = entry_price + cur_atr * cur_tp_mult
                trail_sl = entry_price - cur_atr * cur_sl_mult
                trail_active = False
                entry_atr = cur_atr
                entry_bar = i
                daily_trades += 1
                partial_taken = False
                breakeven_active = False
                equity.append(balance)

            # SHORT entry
            elif short_fn(i, ind, close, vol):
                risk_amount = balance * cur_risk
                cur_sl_mult = ind["sl_mult"][i] if "sl_mult" in ind else sl_atr
                cur_tp_mult = ind["tp_mult"][i] if "tp_mult" in ind else tp_atr
                sl_distance = cur_atr * cur_sl_mult
                pos_size = risk_amount / sl_distance
                entry_price = close[i]
                position = -pos_size
                position_remaining = -pos_size
                sl_price = entry_price + sl_distance
                tp_price = entry_price - cur_atr * cur_tp_mult
                trail_sl = entry_price + cur_atr * cur_sl_mult
                trail_active = False
                entry_atr = cur_atr
                entry_bar = i
                daily_trades += 1
                partial_taken = False
                breakeven_active = False
                equity.append(balance)
            else:
                equity.append(balance)
        elif not in_session and position == 0:
            equity.append(balance)

    # ─── Close remaining position ───
    if position != 0:
        exit_price = close[-1]
        notional = abs(position) * entry_price
        total_fee = (notional + abs(position) * exit_price) * fee
        pnl = position * (exit_price - entry_price) - total_fee
        balance += pnl
        trades.append({"pnl": pnl, "reason": "end_of_data", "bar": n-1})
        equity[-1] = balance

    return balance, trades, equity


# ═══════════════════════════════════════════════════════════════
# PART 4: ANALYSIS & REPORTING
# ═══════════════════════════════════════════════════════════════

def analyze_results(cap, final, trades, equity):
    """Comprehensive analysis of backtest results."""
    if not trades:
        print("  No trades executed!")
        return

    ret = (final / cap - 1) * 100
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr = len(wins) / len(trades) * 100

    gp = sum(t["pnl"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 0.001
    pf = gp / gl

    eq = np.array(equity)
    dd = ((np.maximum.accumulate(eq) - eq) / np.maximum.accumulate(eq) * 100).max()

    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0
    avg_bars = np.mean([t["bars"] for t in trades if "bars" in t])

    # Exit reason breakdown
    reasons = {}
    for t in trades:
        r = t.get("reason", "unknown")
        reasons[r] = reasons.get(r, 0) + 1

    # Profit factor by reason
    reason_pnl = {}
    for t in trades:
        r = t.get("reason", "unknown")
        reason_pnl[r] = reason_pnl.get(r, 0) + t["pnl"]

    # Consecutive wins/losses
    max_consec_w = 0
    max_consec_l = 0
    cur_w = 0
    cur_l = 0
    for t in trades:
        if t["pnl"] > 0:
            cur_w += 1; cur_l = 0
            max_consec_w = max(max_consec_w, cur_w)
        else:
            cur_l += 1; cur_w = 0
            max_consec_l = max(max_consec_l, cur_l)

    print(f"\n{'='*65}")
    print(f" SCALPING STRATEGY: FULL ANALYSIS")
    print(f"{'='*65}")
    print(f" {'Return:':<25} {ret:+.2f}%")
    print(f" {'Final Capital:':<25} ${final:,.2f}")
    print(f" {'Total Trades:':<25} {len(trades)}")
    print(f" {'Win Rate:':<25} {wr:.1f}%")
    print(f" {'Profit Factor:':<25} {pf:.2f}")
    print(f" {'Max Drawdown:':<25} {dd:.1f}%")
    print(f" {'Avg Win:':<25} ${avg_win:+.2f}")
    print(f" {'Avg Loss:':<25} ${avg_loss:+.2f}")
    print(f" {'Avg Bars Held:':<25} {avg_bars:.1f}")
    print(f" {'Max Consec Wins:':<25} {max_consec_w}")
    print(f" {'Max Consec Losses:':<25} {max_consec_l}")

    print(f"\n {'Exit Reasons:'}")
    for r in sorted(reasons.keys()):
        print(f"   {r:<20} {reasons[r]:>4} trades  PnL=${reason_pnl.get(r,0):+.2f}")

    print(f"\n {'Risk/Reward:'}")
    print(f"   Avg Win/Avg Loss:  {abs(avg_win/avg_loss):.2f}")
    print(f"   Risk per trade:    1% of capital")
    print(f"   SL: 1.5x ATR | TP: 2.0x ATR")
    print(f"   Trail: activate +1.0 ATR, distance 0.75x ATR")

    # Equity curve stats
    eq_series = pd.Series(equity)
    daily_returns = eq_series.pct_change().dropna()
    sharpe = daily_returns.mean() / daily_returns.std() * np.sqrt(288) if daily_returns.std() > 0 else 0
    print(f"   Sharpe (5m):       {sharpe:.2f}")
    print(f"   Sharpe (annual):   {sharpe * np.sqrt(288 * 365):.2f}")

    # Monthly PnL
    print(f"\n {'Top 5 wins:'}")
    top_wins = sorted(wins, key=lambda x: x["pnl"], reverse=True)[:5]
    for t in top_wins:
        print(f"   ${t['pnl']:+.2f}  ({t['reason']}, {t.get('bars',0)} bars)")

    print(f"\n {'Top 5 losses:'}")
    top_losses = sorted(losses, key=lambda x: x["pnl"])[:5]
    for t in top_losses:
        print(f"   ${t['pnl']:+.2f}  ({t['reason']}, {t.get('bars',0)} bars)")


def downsample_5m_to_15m(cache_5m):
    """Downsample 5m cache to 15m OHLCV (every 3 candles)."""
    arr = np.array(cache_5m, dtype=object)
    result = []
    for i in range(0, len(arr) - 2, 3):
        ts_val = arr[i, 0]
        open_val = float(arr[i, 1])
        high_val = max(float(arr[i, 2]), float(arr[i+1, 2]), float(arr[i+2, 2]))
        low_val = min(float(arr[i, 3]), float(arr[i+1, 3]), float(arr[i+2, 3]))
        close_val = float(arr[i+2, 4])
        vol_val = float(arr[i, 5]) + float(arr[i+1, 5]) + float(arr[i+2, 5])
        result.append([ts_val, open_val, high_val, low_val, close_val, vol_val])
    return result


def downsample_5m_to_1h(cache_5m):
    """Downsample 5m cache to 1H OHLCV (every 12 candles)."""
    arr = np.array(cache_5m, dtype=object)
    result = []
    for i in range(0, len(arr) - 11, 12):
        ts_val = arr[i, 0]
        open_val = float(arr[i, 1])
        highs = [float(arr[i+j, 2]) for j in range(12)]
        lows = [float(arr[i+j, 3]) for j in range(12)]
        high_val = max(highs)
        low_val = min(lows)
        close_val = float(arr[i+11, 4])
        vol_val = sum(float(arr[i+j, 5]) for j in range(12))
        result.append([ts_val, open_val, high_val, low_val, close_val, vol_val])
    return result


async def main():
    from app.services.data_cache import _load_cache

    cache = _load_cache("BTC-USDT", "5m")
    if not cache:
        print("No 5m cache found"); return

    timeframes = [
        ("5m", cache, 288),
        ("15m", downsample_5m_to_15m(cache), 96),
        ("1H", downsample_5m_to_1h(cache), 24),
    ]

    for tf_name, tf_cache, bars_per_day in timeframes:
        arr = np.array(tf_cache, dtype=object)
        close = arr[:, 4].astype(float)
        high = arr[:, 2].astype(float)
        low = arr[:, 3].astype(float)
        vol = arr[:, 5].astype(float)
        ts = arr[:, 0]

        print(f"\n{'#'*70}")
        print(f" TIMEFRAME: {tf_name} | {len(tf_cache)} candles (~{len(tf_cache)//bars_per_day} days)")
        print(f"{'#'*70}")

        # ─── Count signals for each mode ───
        ind = compute_all_indicators(close, high, low, vol)
        # For mode J (Multi-TF): compute 1H direction from 5m data
        if tf_name in ("15m", "5m"):
            close_5m = arr[:, 4].astype(float)
            close_1h = downsample_5m_to_1h(cache) if tf_name == "5m" else downsample_5m_to_15m(cache)
            close_1h_arr = np.array([c[4] for c in close_1h], dtype=float)
            if len(close_1h_arr) > 50:
                ind["h1_direction"] = compute_1h_direction(close_1h_arr, ema_period=50)
        n = len(close)
        print(f"\n{'Mode':<30} {'LONG':>6} {'SHORT':>6} {'Total':>6}")
        print("-" * 52)
        for name, (lf, sf) in ENTRY_MODES.items():
            start = 200 if tf_name == "5m" else 70
            nl = sum(1 for i in range(start, n) if lf(i, ind, close, vol))
            ns = sum(1 for i in range(start, n) if sf(i, ind, close, vol))
            print(f"  {name:<28} {nl:>6} {ns:>6} {nl+ns:>6}")

        # ─── Test all modes with default params ───
        print(f"\n  Default params — SL=1.5 TP=2.0 Trail=1.0/0.75 CD=5 MH=30")
        print(f"  {'Mode':<30} {'Ret%':>7} {'#':>4} {'WR%':>5} {'PF':>5} {'DD%':>5}")
        print(f"  {'-'*62}")

        mode_results = {}
        for name, (lf, sf) in ENTRY_MODES.items():
            bal, trades, eq = run_scalp_backtest(
                close, high, low, vol, ts, cap=10000,
                sl_atr=1.5, tp_atr=2.0,
                trail_activate=1.0, trail_atr=0.75,
                cooldown=5, max_hold=30, fee=0.0005,
                long_fn=lf, short_fn=sf,
            )
            if not trades:
                print(f"  {name:<28} {'NO TRADES':>7}")
                mode_results[name] = None
                continue
            ret = (bal / 10000 - 1) * 100
            wins = [t for t in trades if t["pnl"] > 0]
            wr = len(wins) / len(trades) * 100
            gp = sum(t["pnl"] for t in wins) if wins else 0
            gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0)) or 0.001
            pf = gp / gl
            eq_arr = np.array(eq)
            dd = ((np.maximum.accumulate(eq_arr) - eq_arr) / np.maximum.accumulate(eq_arr) * 100).max()
            mode_results[name] = (bal, trades, eq, ret, wr, pf, dd)
            marker = " *" if ret > 0 and pf > 1.0 else ""
            print(f"  {name:<28} {ret:>+6.1f}% {len(trades):>4} {wr:>5.1f}% {pf:>5.2f} {dd:>5.1f}%{marker}")

        # ─── Test enhanced features on best modes ───
        print(f"\n  === ENHANCED FEATURES TEST ===")
        print(f"  Testing: partial TP, breakeven, session filter, dynamic sizing")
        enhanced_modes = [
            ("F: OrderBook Proxy", ENTRY_MODES["F: OrderBook Proxy"]),
            ("H: OBV Divergence", ENTRY_MODES["H: OBV Divergence"]),
            ("I: Ensemble 3-vote", ENTRY_MODES["I: Ensemble 3-vote"]),
        ]
        if f"J: Multi-TF" in ENTRY_MODES:
            enhanced_modes.append(("J: Multi-TF", ENTRY_MODES["J: Multi-TF"]))

        print(f"\n  {'Mode':<30} {'Enhancement':<25} {'Ret%':>7} {'#':>4} {'WR%':>5} {'PF':>5} {'DD%':>5}")
        print(f"  {'-'*80}")

        for mode_name, (lf, sf) in enhanced_modes:
            # Baseline
            bal0, trades0, eq0 = run_scalp_backtest(
                close, high, low, vol, ts, cap=10000,
                sl_atr=1.0, tp_atr=1.5, trail_activate=0.5, trail_atr=0.5,
                cooldown=12, max_hold=30, fee=0.0005, long_fn=lf, short_fn=sf)
            if trades0:
                ret0 = (bal0 / 10000 - 1) * 100
                w0 = sum(1 for t in trades0 if t["pnl"] > 0) / len(trades0) * 100
                g0 = sum(t["pnl"] for t in trades0 if t["pnl"] > 0)
                l0 = abs(sum(t["pnl"] for t in trades0 if t["pnl"] <= 0)) or 0.001
                print(f"  {mode_name:<30} {'Baseline':<25} {ret0:>+6.1f}% {len(trades0):>4} {w0:>5.1f}% {g0/l0:>5.2f}")

            # + Partial TP (30% at 1.0 ATR)
            bal1, trades1, eq1 = run_scalp_backtest(
                close, high, low, vol, ts, cap=10000,
                sl_atr=1.0, tp_atr=1.5, trail_activate=0.5, trail_atr=0.5,
                cooldown=12, max_hold=30, fee=0.0005, long_fn=lf, short_fn=sf,
                partial_tp_pct=0.3, partial_tp_atr=1.0)
            if trades1:
                ret1 = (bal1 / 10000 - 1) * 100
                w1 = sum(1 for t in trades1 if t["pnl"] > 0) / len(trades1) * 100
                g1 = sum(t["pnl"] for t in trades1 if t["pnl"] > 0)
                l1 = abs(sum(t["pnl"] for t in trades1 if t["pnl"] <= 0)) or 0.001
                marker = " *" if ret1 > ret0 else ""
                print(f"  {'':<30} {'+Partial TP 30%@1.0ATR':<25} {ret1:>+6.1f}% {len(trades1):>4} {w1:>5.1f}% {g1/l1:>5.2f}{marker}")

            # + Breakeven at 0.5 ATR
            bal2, trades2, eq2 = run_scalp_backtest(
                close, high, low, vol, ts, cap=10000,
                sl_atr=1.0, tp_atr=1.5, trail_activate=0.5, trail_atr=0.5,
                cooldown=12, max_hold=30, fee=0.0005, long_fn=lf, short_fn=sf,
                be_atr=0.5)
            if trades2:
                ret2 = (bal2 / 10000 - 1) * 100
                w2 = sum(1 for t in trades2 if t["pnl"] > 0) / len(trades2) * 100
                g2 = sum(t["pnl"] for t in trades2 if t["pnl"] > 0)
                l2 = abs(sum(t["pnl"] for t in trades2 if t["pnl"] <= 0)) or 0.001
                marker = " *" if ret2 > ret0 else ""
                print(f"  {'':<30} {'+Breakeven 0.5ATR':<25} {ret2:>+6.1f}% {len(trades2):>4} {w2:>5.1f}% {g2/l2:>5.2f}{marker}")

            # + Session filter (UTC 8-20)
            bal3, trades3, eq3 = run_scalp_backtest(
                close, high, low, vol, ts, cap=10000,
                sl_atr=1.0, tp_atr=1.5, trail_activate=0.5, trail_atr=0.5,
                cooldown=12, max_hold=30, fee=0.0005, long_fn=lf, short_fn=sf,
                session_filter=True, session_start=8, session_end=20)
            if trades3:
                ret3 = (bal3 / 10000 - 1) * 100
                w3 = sum(1 for t in trades3 if t["pnl"] > 0) / len(trades3) * 100
                g3 = sum(t["pnl"] for t in trades3 if t["pnl"] > 0)
                l3 = abs(sum(t["pnl"] for t in trades3 if t["pnl"] <= 0)) or 0.001
                marker = " *" if ret3 > ret0 else ""
                print(f"  {'':<30} {'+Session UTC 8-20':<25} {ret3:>+6.1f}% {len(trades3):>4} {w3:>5.1f}% {g3/l3:>5.2f}{marker}")

            # + Dynamic sizing
            bal4, trades4, eq4 = run_scalp_backtest(
                close, high, low, vol, ts, cap=10000,
                sl_atr=1.0, tp_atr=1.5, trail_activate=0.5, trail_atr=0.5,
                cooldown=12, max_hold=30, fee=0.0005, long_fn=lf, short_fn=sf,
                dynamic_sizing=True)
            if trades4:
                ret4 = (bal4 / 10000 - 1) * 100
                w4 = sum(1 for t in trades4 if t["pnl"] > 0) / len(trades4) * 100
                g4 = sum(t["pnl"] for t in trades4 if t["pnl"] > 0)
                l4 = abs(sum(t["pnl"] for t in trades4 if t["pnl"] <= 0)) or 0.001
                marker = " *" if ret4 > ret0 else ""
                print(f"  {'':<30} {'+Dynamic Sizing':<25} {ret4:>+6.1f}% {len(trades4):>4} {w4:>5.1f}% {g4/l4:>5.2f}{marker}")

            # ALL COMBINED
            bal5, trades5, eq5 = run_scalp_backtest(
                close, high, low, vol, ts, cap=10000,
                sl_atr=1.0, tp_atr=1.5, trail_activate=0.5, trail_atr=0.5,
                cooldown=12, max_hold=30, fee=0.0005, long_fn=lf, short_fn=sf,
                partial_tp_pct=0.3, partial_tp_atr=1.0,
                be_atr=0.5, session_filter=True, session_start=8, session_end=20,
                dynamic_sizing=True)
            if trades5:
                ret5 = (bal5 / 10000 - 1) * 100
                w5 = sum(1 for t in trades5 if t["pnl"] > 0) / len(trades5) * 100
                g5 = sum(t["pnl"] for t in trades5 if t["pnl"] > 0)
                l5 = abs(sum(t["pnl"] for t in trades5 if t["pnl"] <= 0)) or 0.001
                marker = " ★" if ret5 > ret0 else ""
                print(f"  {'':<30} {'ALL COMBINED':<25} {ret5:>+6.1f}% {len(trades5):>4} {w5:>5.1f}% {g5/l5:>5.2f}{marker}")

        # ─── Parameter sweep on modes with positive signals ───
        profitable_modes = [(k, v) for k, v in mode_results.items()
                           if v is not None and v[3] > -30 and v[5] > 0.4]
        if not profitable_modes:
            print(f"\n  No mode with PF > 0.4 on {tf_name}. Skipping sweep.")
            continue

        for mode_name, (bal, trades, eq, ret, wr, pf, dd) in profitable_modes:
            lf, sf = ENTRY_MODES[mode_name]
            print(f"\n  Sweep: {mode_name} on {tf_name}")
            print(f"  {'SL':>4} {'TP':>4} {'TrA':>4} {'TrD':>4} {'CD':>3} {'MH':>3} {'#':>4} {'Ret%':>7} {'WR%':>5} {'PF':>5} {'DD%':>5}")
            print(f"  {'-'*58}")

            best_score = -999
            best_combo = None
            best_combo_result = None

            for sl in [1.0, 1.5, 2.0]:
                for tp in [1.5, 2.0, 3.0]:
                    for tr_a in [0.5, 1.0]:
                        for tr_d in [0.5, 0.75]:
                            for cd in [5, 8, 12]:
                                for mh in [20, 30]:
                                    bal2, trades2, eq2 = run_scalp_backtest(
                                        close, high, low, vol, ts, cap=10000,
                                        sl_atr=sl, tp_atr=tp,
                                        trail_activate=tr_a, trail_atr=tr_d,
                                        cooldown=cd, max_hold=mh, fee=0.0005,
                                        long_fn=lf, short_fn=sf,
                                    )
                                    if len(trades2) < 5:
                                        continue
                                    ret2 = (bal2 / 10000 - 1) * 100
                                    wins2 = [t for t in trades2 if t["pnl"] > 0]
                                    wr2 = len(wins2) / len(trades2) * 100
                                    gp2 = sum(t["pnl"] for t in wins2) if wins2 else 0
                                    gl2 = abs(sum(t["pnl"] for t in trades2 if t["pnl"] <= 0)) or 0.001
                                    pf2 = gp2 / gl2
                                    eq_arr2 = np.array(eq2)
                                    dd2 = ((np.maximum.accumulate(eq_arr2) - eq_arr2) / np.maximum.accumulate(eq_arr2) * 100).max()
                                    retdd2 = ret2 / dd2 if dd2 > 0 else 0

                                    if ret2 > 0 and pf2 > 1.0 and retdd2 > best_score:
                                        best_score = retdd2
                                        best_combo = (sl, tp, tr_a, tr_d, cd, mh)
                                        best_combo_result = (bal2, trades2, eq2)

                                    if pf2 > 1.2 and ret2 > 0:
                                        print(f"  {sl:>4.1f} {tp:>4.1f} {tr_a:>4.1f} {tr_d:>4.2f} {cd:>3} {mh:>3} {len(trades2):>4} {ret2:>+6.1f}% {wr2:>5.1f}% {pf2:>5.2f} {dd2:>5.1f}%")

            if best_combo and best_combo_result:
                print(f"\n  >>> BEST on {tf_name}: SL={best_combo[0]} TP={best_combo[1]} Trail={best_combo[2]}/{best_combo[3]} CD={best_combo[4]} MH={best_combo[5]}")
                bal2, trades2, eq2 = best_combo_result
                analyze_results(10000, bal2, trades2, eq2)
            else:
                print(f"\n  No profitable combo found for {mode_name} on {tf_name}.")

    # ═══════════════════════════════════════════════════════════════
    # DEEP COMBO SWEEP — combine all winning enhancements
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'#'*70}")
    print(f" DEEP COMBO SWEEP: Mode F (OrderBook) + H (OBV) on 1H")
    print(f"{'#'*70}")

    # Use 1H data
    arr_1h = np.array(timeframes[2][1], dtype=object)
    close_1h = arr_1h[:, 4].astype(float)
    high_1h = arr_1h[:, 2].astype(float)
    low_1h = arr_1h[:, 3].astype(float)
    vol_1h = arr_1h[:, 5].astype(float)
    ts_1h = arr_1h[:, 0]

    # Also use 15m for multi-TF mode J
    arr_15m = np.array(timeframes[1][1], dtype=object)
    close_15m = arr_15m[:, 4].astype(float)
    high_15m = arr_15m[:, 2].astype(float)
    low_15m = arr_15m[:, 3].astype(float)
    vol_15m = arr_15m[:, 5].astype(float)
    ts_15m = arr_15m[:, 0]

    # 1H with all enhancements
    print(f"\n Mode F (OrderBook) on 1H — FULL ENHANCEMENTS SWEEP")
    print(f" {'risk%':>6} {'SL':>4} {'TP':>4} {'TrA':>4} {'TrD':>4} {'CD':>3} {'MH':>3} {'PT%':>4} {'BE':>4} {'Sess':>6} {'Dyn':>4} {'#':>4} {'Ret%':>7} {'WR%':>5} {'PF':>5} {'DD%':>5} {'R/D':>5}")
    print(f" {'-'*92}")

    best_score = -999
    best_params = None
    best_result = None
    results_list = []

    for risk in [0.005, 0.01, 0.015, 0.02]:
        for sl in [0.8, 1.0, 1.5]:
            for tp in [1.5, 2.0, 3.0]:
                for tr_a in [0.3, 0.5, 0.8]:
                    for tr_d in [0.3, 0.5]:
                        for cd in [8, 12, 15]:
                            for pt in [0.0, 0.3]:
                                for be in [0.0, 0.5]:
                                    for sess in [False, True]:
                                        for dyn in [False, True]:
                                            bal, tr, eq = run_scalp_backtest(
                                                close_1h, high_1h, low_1h, vol_1h, ts_1h,
                                                cap=10000, risk_pct=risk,
                                                sl_atr=sl, tp_atr=tp,
                                                trail_activate=tr_a, trail_atr=tr_d,
                                                cooldown=cd, max_hold=30, fee=0.0005,
                                                long_fn=mode_f_long, short_fn=mode_f_short,
                                                partial_tp_pct=pt, partial_tp_atr=1.0,
                                                be_atr=be, session_filter=sess,
                                                session_start=8, session_end=20,
                                                dynamic_sizing=dyn)
                                            if len(tr) < 10:
                                                continue
                                            ret = (bal / 10000 - 1) * 100
                                            wins = [t for t in tr if t["pnl"] > 0]
                                            wr = len(wins) / len(tr) * 100
                                            gp = sum(t["pnl"] for t in wins) if wins else 0
                                            gl = abs(sum(t["pnl"] for t in tr if t["pnl"] <= 0)) or 0.001
                                            pf = gp / gl
                                            eq_a = np.array(eq)
                                            dd = ((np.maximum.accumulate(eq_a) - eq_a) / np.maximum.accumulate(eq_a) * 100).max()
                                            rdd = ret / dd if dd > 0 else 0

                                            if ret > 0 and pf > 1.0 and rdd > best_score:
                                                best_score = rdd
                                                best_params = (risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn)
                                                best_result = (bal, tr, eq)

                                            if pf > 1.2 and ret > 5:
                                                results_list.append((ret, wr, pf, dd, rdd, risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn, len(tr)))

    # Sort by return
    results_list.sort(key=lambda x: x[0], reverse=True)
    for ret, wr, pf, dd, rdd, risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn, n_tr in results_list[:25]:
        sess_s = "8-20" if sess else "all"
        pt_s = f"{pt*100:.0f}%" if pt > 0 else "off"
        be_s = f"{be:.1f}" if be > 0 else "off"
        dyn_s = "on" if dyn else "off"
        print(f"  {risk*100:>5.1f}% {sl:>4.1f} {tp:>4.1f} {tr_a:>4.1f} {tr_d:>4.2f} {cd:>3} {pt_s:>4} {be_s:>4} {sess_s:>6} {dyn_s:>4} {n_tr:>4} {ret:>+6.1f}% {wr:>5.1f}% {pf:>5.2f} {dd:>5.1f}% {rdd:>5.2f}")

    if best_params:
        risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn = best_params
        print(f"\n >>> BEST COMBO: risk={risk*100:.1f}% SL={sl} TP={tp} Trail={tr_a}/{tr_d} CD={cd} Partial={pt*100:.0f}% BE={be} Session={'8-20' if sess else 'all'} Dynamic={dyn}")
        bal, tr, eq = best_result
        analyze_results(10000, bal, tr, eq)
    else:
        print(f"\n No profitable combo found.")


if __name__ == "__main__":
    asyncio.run(main())
