"""
OPTIMIZED SCALPING STRATEGY v2 — Practical Logic from 2024-2026 Research

Key fixes from v1:
- BB Mean Reversion: Only in RANGES (ADX<20), relaxed RSI, no conflicting trend filter
- VWAP Pullback: Trade pullback TO VWAP in trend direction (not band reversion)
- EMA Trend Pullback: Classic pullback to EMA21 in macro trend direction
- Proper risk: ATR-based SL/TP, 2:1 R:R, 0.8% risk, daily limits

Production modes (all on 15m):
1. BB_RANGE_REV — BB(20,2) mean reversion in low-ADX ranges
2. VWAP_TREND_PULLBACK — Pullback to VWAP in trend direction
3. EMA_PULLBACK — Pullback to EMA21 in macro trend (EMA50>EMA200)
"""

from __future__ import annotations

import asyncio
import math
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd

from .risk_guard import assert_can_open
from .analysis_logger import get_logger
from .pnl_utils import extract_fill_avg, close_pnl, fee_cost

SCALP_BOT_ID = "scalp_optimized"
STRATEGY_NAME = "Scalping Optimized"
STRATEGY_VERSION = "v2.0"

# ─── Config ──────────────────────────────────────────────────────────────
@dataclass
class ScalpConfig:
    # ── General ──
    symbols: list = field(default_factory=lambda: ["BTC"])
    timeframe: str = "15m"
    capital: float = 5000.0
    max_leverage: float = 2.0
    risk_per_trade: float = 0.008       # 0.8% per trade
    max_daily_loss_pct: float = 0.02    # 2% daily loss limit
    max_daily_trades: int = 30
    execute: bool = None

# ── Mode 1: BB Range Reversion ──
    bb_period: int = 20
    bb_std: float = 2.0
    bb_rsi_period: int = 14
    bb_rsi_os: float = 30.0             # strict oversold
    bb_rsi_ob: float = 70.0             # strict overbought
    bb_adx_max: float = 20.0            # strict range only
    bb_vol_mult: float = 1.2            # volume confirmation
    bb_sl_atr: float = 1.0
    bb_tp_atr: float = 1.5
    bb_trail_act: float = 1.0
    bb_trail_atr: float = 0.5
    bb_cooldown: int = 12               # 3 hours
    bb_max_hold: int = 20

    # ── Mode 2: BB Breakout ──
    bb_break_sl_atr: float = 1.0
    bb_break_tp_atr: float = 2.0
    bb_break_trail_act: float = 1.5
    bb_break_trail_atr: float = 1.0
    bb_break_cooldown: int = 6
    bb_break_max_hold: int = 16

    # ── Mode 3: VWAP Mean Reversion ──
    vwap_std: float = 1.5
    vwap_rev_sl_atr: float = 1.0
    vwap_rev_tp_atr: float = 1.5
    vwap_rev_trail_act: float = 1.0
    vwap_rev_trail_atr: float = 0.5
    vwap_rev_cooldown: int = 8
    vwap_rev_max_hold: int = 20

    # ── Mode 4: VWAP Trend Pullback ──
    vwap_anchor: str = "1D"
    vwap_adx_min: float = 25.0
    vwap_ema_fast: int = 9
    vwap_ema_slow: int = 21
    vwap_slope_bars: int = 12
    vwap_max_dev_pct: float = 0.003
    vwap_pull_sl_atr: float = 1.0
    vwap_pull_tp_atr: float = 2.0
    vwap_pull_trail_act: float = 1.0
    vwap_pull_trail_atr: float = 0.75
    vwap_pull_cooldown: int = 6
    vwap_pull_max_hold: int = 24

    # ── Mode 5: EMA Crossover ──
    ema_trend_fast: int = 50
    ema_trend_slow: int = 200
    ema_cross_sl_atr: float = 1.5
    ema_cross_tp_atr: float = 2.5
    ema_cross_trail_act: float = 1.5
    ema_cross_trail_atr: float = 1.0
    ema_cross_cooldown: int = 10
    ema_cross_max_hold: int = 30

    # ── Mode 6: Regime Aware ──
    regime_sl_atr: float = 1.0
    regime_tp_atr: float = 1.5
    regime_trail_act: float = 1.0
    regime_trail_atr: float = 0.5
    regime_cooldown: int = 10
    regime_max_hold: int = 20

    # ── Common ──
    fee_rate: float = 0.0005
    slippage_bps: float = 1.0
    use_limit_orders: bool = True

    def __post_init__(self):
        if self.symbols is None:
            self.symbols = ["BTC"]


@dataclass
class ScalpPosition:
    coin: str
    inst_id: str
    side: str
    size: float
    entry_price: float
    stop_price: float
    take_price: float
    leverage: float
    opened_at: float
    mode: str
    signal: dict = field(default_factory=dict)


# ─── Indicators ──────────────────────────────────────────────────────────
def ema(arr: np.ndarray, span: int) -> np.ndarray:
    return pd.Series(arr).ewm(span=span, adjust=False).mean().values


def sma(arr: np.ndarray, period: int) -> np.ndarray:
    return pd.Series(arr).rolling(period).mean().values


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean().values
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean().values
    r = np.full(len(close), 50.0)
    for i in range(period, len(close)):
        r[i] = 0.0 if loss[i] == 0 else 100.0 - 100.0 / (1.0 + gain[i] / loss[i])
    return r


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    return np.insert(pd.Series(tr).rolling(period).mean().values, 0, 0)


def vwap_daily(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               vol: np.ndarray, ts: np.ndarray) -> np.ndarray:
    typical = (high + low + close) / 3
    n = len(close)
    vwap_arr = np.zeros(n)
    session_tpv = session_vol = 0.0
    last_session = -1
    for i in range(n):
        dt = datetime.fromtimestamp(ts[i] / 1000, tz=timezone.utc)
        cur_session = dt.toordinal()
        if cur_session != last_session:
            session_tpv = session_vol = 0.0
            last_session = cur_session
        session_tpv += typical[i] * vol[i]
        session_vol += vol[i]
        vwap_arr[i] = session_tpv / session_vol if session_vol > 0 else close[i]
    return vwap_arr


def bollinger(close: np.ndarray, period: int = 20, std_dev: float = 2.0) -> tuple:
    middle = sma(close, period)
    std = pd.Series(close).rolling(period).std().values
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return middle, upper, lower


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(close)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]  # FIX: first bar TR = H-L
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
    return pd.Series(dx).rolling(period).mean().values


def slope(arr: np.ndarray, lookback: int) -> np.ndarray:
    n = len(arr)
    result = np.zeros(n)
    for i in range(lookback, n):
        y = arr[i-lookback:i]
        x = np.arange(lookback)
        if np.std(y) > 0:
            result[i] = np.polyfit(x, y, 1)[0]
    return result


def volume_sma(vol: np.ndarray, period: int = 20) -> np.ndarray:
    return pd.Series(vol).rolling(period).mean().values


# ─── Compute Indicators ──────────────────────────────────────────────────
def compute_indicators(close: np.ndarray, high: np.ndarray, low: np.ndarray,
                       vol: np.ndarray, ts: np.ndarray, cfg: ScalpConfig) -> dict:
    ind = {}
    ind["bb_mid"], ind["bb_upper"], ind["bb_lower"] = bollinger(close, cfg.bb_period, cfg.bb_std)
    ind["rsi"] = rsi(close, cfg.bb_rsi_period)
    ind["adx"] = adx(high, low, close, 14)
    ind["vol_sma"] = volume_sma(vol, 20)
    ind["atr14"] = atr(high, low, close, 14)

    # VWAP with bands
    ind["vwap"] = vwap_daily(high, low, close, vol, ts)
    typical = (high + low + close) / 3
    tpv = typical * vol
    period = 96  # 24h on 15m
    cum_tpv = pd.Series(tpv).rolling(period).sum().values
    cum_vol = pd.Series(vol).rolling(period).sum().values
    vwap_roll = np.where(cum_vol > 0, cum_tpv / cum_vol, ind["vwap"])
    dev = typical - vwap_roll
    std = pd.Series(dev).rolling(period).std().values
    ind["vwap_upper"] = vwap_roll + cfg.vwap_std * std
    ind["vwap_lower"] = vwap_roll - cfg.vwap_std * std

    # EMAs
    ind["ema9"] = ema(close, 9)
    ind["ema21"] = ema(close, 21)
    ind["ema50"] = ema(close, 50)
    ind["ema200"] = ema(close, 200)

    return ind


# ─── Entry Rules ─────────────────────────────────────────────────────────
# MODE 1: BB Range Reversion — ADX < 20, strict range only
def bb_range_long(i: int, ind: dict, close: np.ndarray, vol: np.ndarray, cfg: ScalpConfig) -> bool:
    """BB lower touch + RSI oversold + ADX low (strict range) + RSI turning up"""
    return (close[i] <= ind["bb_lower"][i] and
            ind["rsi"][i] < cfg.bb_rsi_os and
            ind["rsi"][i] > ind["rsi"][i-1] and  # turning up
            ind["adx"][i] < cfg.bb_adx_max and
            vol[i] > ind["vol_sma"][i] * cfg.bb_vol_mult)


def bb_range_short(i: int, ind: dict, close: np.ndarray, vol: np.ndarray, cfg: ScalpConfig) -> bool:
    return (close[i] >= ind["bb_upper"][i] and
            ind["rsi"][i] > cfg.bb_rsi_ob and
            ind["rsi"][i] < ind["rsi"][i-1] and  # turning down
            ind["adx"][i] < cfg.bb_adx_max and
            vol[i] > ind["vol_sma"][i] * cfg.bb_vol_mult)


# MODE 2: BB Breakout — ADX > 25, BB squeeze + volume breakout
def bb_breakout_long(i: int, ind: dict, close: np.ndarray, vol: np.ndarray, cfg: ScalpConfig) -> bool:
    """BB squeeze (width < 50% of 20-bar avg) + close above upper + volume + RSI momentum"""
    if i < 20:
        return False
    bb_width = ind["bb_upper"][i] - ind["bb_lower"][i]
    avg_width = np.mean(ind["bb_upper"][i-20:i] - ind["bb_lower"][i-20:i])
    return (bb_width < avg_width * 0.5 and  # squeeze
            close[i] > ind["bb_upper"][i] and
            close[i-1] <= ind["bb_upper"][i-1] and  # breakout this bar
            ind["rsi"][i] > 50 and
            ind["adx"][i] > 25 and
            vol[i] > ind["vol_sma"][i] * 1.5)


def bb_breakout_short(i: int, ind: dict, close: np.ndarray, vol: np.ndarray, cfg: ScalpConfig) -> bool:
    if i < 20:
        return False
    bb_width = ind["bb_upper"][i] - ind["bb_lower"][i]
    avg_width = np.mean(ind["bb_upper"][i-20:i] - ind["bb_lower"][i-20:i])
    return (bb_width < avg_width * 0.5 and
            close[i] < ind["bb_lower"][i] and
            close[i-1] >= ind["bb_lower"][i-1] and
            ind["rsi"][i] < 50 and
            ind["adx"][i] > 25 and
            vol[i] > ind["vol_sma"][i] * 1.5)


# MODE 3: VWAP Mean Reversion — ADX < 20, touch VWAP bands, target VWAP middle
def vwap_rev_long(i: int, ind: dict, close: np.ndarray, vol: np.ndarray, cfg: ScalpConfig) -> bool:
    """Price at VWAP lower band (1.5σ) + ADX low + RSI oversold + volume"""
    return (close[i] <= ind["vwap_lower"][i] and
            ind["adx"][i] < 20 and
            ind["rsi"][i] < 35 and
            vol[i] > ind["vol_sma"][i] * 1.2)


def vwap_rev_short(i: int, ind: dict, close: np.ndarray, vol: np.ndarray, cfg: ScalpConfig) -> bool:
    return (close[i] >= ind["vwap_upper"][i] and
            ind["adx"][i] < 20 and
            ind["rsi"][i] > 65 and
            vol[i] > ind["vol_sma"][i] * 1.2)


# MODE 4: VWAP Trend Pullback — ADX > 25, pullback to VWAP WITH confirmation
def vwap_pullback_long(i: int, ind: dict, close: np.ndarray, vol: np.ndarray, cfg: ScalpConfig) -> bool:
    """Pullback to VWAP in uptrend:
    - Macro: EMA50 > EMA200
    - Micro: EMA9 > EMA21
    - Pullback: close was below VWAP, now reclaims with STRONG candle
    - RSI 40-70 (not extreme)
    - Volume spike on entry
    - VWAP slope positive
    """
    vwap_slope = ind["vwap_slope"][i]
    strong_candle = (close[i] - close[i-1]) / close[i-1] > 0.001  # 0.1% up candle
    return (ind["ema50"][i] > ind["ema200"][i] and
            ind["ema9"][i] > ind["ema21"][i] and
            close[i-1] < ind["vwap"][i-1] and
            close[i] > ind["vwap"][i] and
            strong_candle and  # confirmation
            40 < ind["rsi"][i] < 70 and
            vol[i] > ind["vol_sma"][i] * 1.5 and  # volume spike
            vwap_slope > 0 and
            ind["adx"][i] > cfg.vwap_adx_min)


def vwap_pullback_short(i: int, ind: dict, close: np.ndarray, vol: np.ndarray, cfg: ScalpConfig) -> bool:
    vwap_slope = ind["vwap_slope"][i]
    strong_candle = (close[i-1] - close[i]) / close[i-1] > 0.001  # 0.1% down candle
    return (ind["ema50"][i] < ind["ema200"][i] and
            ind["ema9"][i] < ind["ema21"][i] and
            close[i-1] > ind["vwap"][i-1] and
            close[i] < ind["vwap"][i] and
            strong_candle and
            30 < ind["rsi"][i] < 60 and
            vol[i] > ind["vol_sma"][i] * 1.5 and
            vwap_slope < 0 and
            ind["adx"][i] > cfg.vwap_adx_min)


# MODE 5: EMA Crossover + Volume — classic trend entry
def ema_cross_long(i: int, ind: dict, close: np.ndarray, vol: np.ndarray, cfg: ScalpConfig) -> bool:
    """EMA9 crosses above EMA21 + volume + macro trend + RSI not overbought"""
    return (ind["ema9"][i-1] <= ind["ema21"][i-1] and
            ind["ema9"][i] > ind["ema21"][i] and
            ind["ema50"][i] > ind["ema200"][i] and
            ind["rsi"][i] < 70 and
            vol[i] > ind["vol_sma"][i] * 1.5)


def ema_cross_short(i: int, ind: dict, close: np.ndarray, vol: np.ndarray, cfg: ScalpConfig) -> bool:
    return (ind["ema9"][i-1] >= ind["ema21"][i-1] and
            ind["ema9"][i] < ind["ema21"][i] and
            ind["ema50"][i] < ind["ema200"][i] and
            ind["rsi"][i] > 30 and
            vol[i] > ind["vol_sma"][i] * 1.5)


# Add VWAP slope to indicators
def add_vwap_slope(ind: dict, cfg: ScalpConfig):
    ind["vwap_slope"] = slope(ind["vwap"], cfg.vwap_slope_bars)


# MODE 6: REGIME AWARE — ADX-based regime switch
# ADX < 20: Range → BB Range Reversion + VWAP Mean Reversion
# ADX > 25: Trend → BB Breakout + EMA Crossover
# ADX 20-25: No trade (chop)
def regime_long(i: int, ind: dict, close: np.ndarray, vol: np.ndarray, cfg: ScalpConfig) -> bool:
    adx_val = ind["adx"][i]
    if adx_val < 20:
        # Range: BB Range Reversion OR VWAP Mean Reversion
        return (bb_range_long(i, ind, close, vol, cfg) or
                vwap_rev_long(i, ind, close, vol, cfg))
    elif adx_val > 25:
        # Trend: BB Breakout OR EMA Crossover
        return (bb_breakout_long(i, ind, close, vol, cfg) or
                ema_cross_long(i, ind, close, vol, cfg))
    return False


def regime_short(i: int, ind: dict, close: np.ndarray, vol: np.ndarray, cfg: ScalpConfig) -> bool:
    adx_val = ind["adx"][i]
    if adx_val < 20:
        return (bb_range_short(i, ind, close, vol, cfg) or
                vwap_rev_short(i, ind, close, vol, cfg))
    elif adx_val > 25:
        return (bb_breakout_short(i, ind, close, vol, cfg) or
                ema_cross_short(i, ind, close, vol, cfg))
    return False


# ─── Backtest Engine ─────────────────────────────────────────────────────
def run_backtest(close: np.ndarray, high: np.ndarray, low: np.ndarray,
                 vol: np.ndarray, ts: np.ndarray, cfg: ScalpConfig,
                 mode: str = "BB") -> tuple:
    ind = compute_indicators(close, high, low, vol, ts, cfg)
    add_vwap_slope(ind, cfg)
    n = len(close)

    if mode == "BB":
        long_fn, short_fn = bb_range_long, bb_range_short
        sl_atr, tp_atr = cfg.bb_sl_atr, cfg.bb_tp_atr
        trail_act, trail_atr = cfg.bb_trail_act, cfg.bb_trail_atr
        cooldown, max_hold = cfg.bb_cooldown, cfg.bb_max_hold
    elif mode == "BB_BREAK":
        long_fn, short_fn = bb_breakout_long, bb_breakout_short
        sl_atr, tp_atr = cfg.bb_break_sl_atr, cfg.bb_break_tp_atr
        trail_act, trail_atr = cfg.bb_break_trail_act, cfg.bb_break_trail_atr
        cooldown, max_hold = cfg.bb_break_cooldown, cfg.bb_break_max_hold
    elif mode == "VWAP_REV":
        long_fn, short_fn = vwap_rev_long, vwap_rev_short
        sl_atr, tp_atr = cfg.vwap_rev_sl_atr, cfg.vwap_rev_tp_atr
        trail_act, trail_atr = cfg.vwap_rev_trail_act, cfg.vwap_rev_trail_atr
        cooldown, max_hold = cfg.vwap_rev_cooldown, cfg.vwap_rev_max_hold
    elif mode == "VWAP_PULL":
        long_fn, short_fn = vwap_pullback_long, vwap_pullback_short
        sl_atr, tp_atr = cfg.vwap_pull_sl_atr, cfg.vwap_pull_tp_atr
        trail_act, trail_atr = cfg.vwap_pull_trail_act, cfg.vwap_pull_trail_atr
        cooldown, max_hold = cfg.vwap_pull_cooldown, cfg.vwap_pull_max_hold
    elif mode == "EMA_CROSS":
        long_fn, short_fn = ema_cross_long, ema_cross_short
        sl_atr, tp_atr = cfg.ema_cross_sl_atr, cfg.ema_cross_tp_atr
        trail_act, trail_atr = cfg.ema_cross_trail_act, cfg.ema_cross_trail_atr
        cooldown, max_hold = cfg.ema_cross_cooldown, cfg.ema_cross_max_hold
    elif mode == "REGIME":
        long_fn, short_fn = regime_long, regime_short
        sl_atr, tp_atr = cfg.regime_sl_atr, cfg.regime_tp_atr
        trail_act, trail_atr = cfg.regime_trail_act, cfg.regime_trail_atr
        cooldown, max_hold = cfg.regime_cooldown, cfg.regime_max_hold
    else:
        raise ValueError(f"Unknown mode: {mode}")

    balance = float(cfg.capital)
    equity = [float(cfg.capital)]
    position = entry_price = sl_price = tp_price = trail_sl = entry_atr = 0.0
    entry_bar = -999
    trail_active = False
    trades = []
    daily_pnl = daily_trades = 0
    current_date = last_trade_bar = None
    last_trade_bar = -999

    for i in range(max(200, cfg.ema_trend_slow), n):
        try:
            ts_date = str(ts[i])[:10]
        except:
            ts_date = str(i // 96)

        if ts_date != current_date:
            daily_pnl = daily_trades = 0
            current_date = ts_date

        # Equity
        unrealized = position * (close[i] - entry_price) if position != 0 else 0
        equity.append(balance + unrealized)

        # Daily limits
        if abs(daily_pnl) >= cfg.capital * cfg.max_daily_loss_pct or daily_trades >= cfg.max_daily_trades:
            if position != 0:
                exit_price = close[i]
                notional = abs(position) * entry_price
                total_fee = (notional + abs(position) * exit_price) * cfg.fee_rate
                pnl = position * (exit_price - entry_price) - total_fee
                balance += pnl
                daily_pnl += pnl
                trades.append({"pnl": pnl, "reason": "daily_limit", "bar": i, "mode": mode})
                position = 0
            equity[-1] = balance
            continue

        # Manage position
        if position != 0:
            bars_held = i - entry_bar
            atr_val = ind["atr14"][i]

            if position > 0:
                if close[i] > entry_price + entry_atr * trail_act:
                    trail_active = True
                    trail_sl = max(trail_sl, close[i] - entry_atr * trail_atr)
                exit_price = exit_reason = None
                if close[i] <= sl_price: exit_price, exit_reason = sl_price, "sl"
                elif trail_active and close[i] <= trail_sl: exit_price, exit_reason = trail_sl, "trail"
                elif close[i] >= tp_price: exit_price, exit_reason = tp_price, "tp"
                elif bars_held >= max_hold: exit_price, exit_reason = close[i], "time"
            else:
                if close[i] < entry_price - entry_atr * trail_act:
                    trail_active = True
                    trail_sl = min(trail_sl, close[i] + entry_atr * trail_atr)
                exit_price = exit_reason = None
                if close[i] >= sl_price: exit_price, exit_reason = sl_price, "sl"
                elif trail_active and close[i] >= trail_sl: exit_price, exit_reason = trail_sl, "trail"
                elif close[i] <= tp_price: exit_price, exit_reason = tp_price, "tp"
                elif bars_held >= max_hold: exit_price, exit_reason = close[i], "time"

            if exit_price is not None:
                notional = abs(position) * entry_price
                total_fee = (notional + abs(position) * exit_price) * cfg.fee_rate
                pnl = position * (exit_price - entry_price) - total_fee
                balance += pnl
                daily_pnl += pnl
                trades.append({"pnl": round(pnl,4), "reason": exit_reason, "bar": i, "mode": mode})
                position = trail_active = trail_sl = 0
                equity[-1] = balance
                continue

        # New entries
        if position == 0 and i - last_trade_bar >= cooldown:
            if long_fn(i, ind, close, vol, cfg):
                atr_val = ind["atr14"][i]
                if atr_val > 0:
                    sl_price = close[i] - atr_val * sl_atr
                    # Mode-specific TP
                    if mode == "VWAP_REV":
                        tp_price = ind["vwap"][i]  # Target VWAP middle for mean reversion
                    else:
                        tp_price = close[i] + atr_val * tp_atr
                    trail_sl = sl_price; trail_active = False; entry_atr = atr_val
                    risk_amt = balance * cfg.risk_per_trade
                    sl_dist = close[i] - sl_price
                    if sl_dist > 0:
                        size = risk_amt / sl_dist
                        notional = size * close[i]
                        if notional > balance * cfg.max_leverage:
                            size = balance * cfg.max_leverage / close[i]
                        position = size
                        entry_price = close[i]; entry_bar = i; last_trade_bar = i; daily_trades += 1
            elif short_fn(i, ind, close, vol, cfg):
                atr_val = ind["atr14"][i]
                if atr_val > 0:
                    sl_price = close[i] + atr_val * sl_atr
                    if mode == "VWAP_REV":
                        tp_price = ind["vwap"][i]  # Target VWAP middle
                    else:
                        tp_price = close[i] - atr_val * tp_atr
                    trail_sl = sl_price; trail_active = False; entry_atr = atr_val
                    risk_amt = balance * cfg.risk_per_trade
                    sl_dist = sl_price - close[i]
                    if sl_dist > 0:
                        size = risk_amt / sl_dist
                        notional = size * close[i]
                        if notional > balance * cfg.max_leverage:
                            size = balance * cfg.max_leverage / close[i]
                        position = -size
                        entry_price = close[i]; entry_bar = i; last_trade_bar = i; daily_trades += 1

    if position != 0:
        exit_price = close[-1]
        notional = abs(position) * entry_price
        total_fee = (notional + abs(position) * exit_price) * cfg.fee_rate
        pnl = position * (exit_price - entry_price) - total_fee
        balance += pnl
        trades.append({"pnl": round(pnl,4), "reason": "end", "bar": n-1, "mode": mode})

    return balance, trades, equity


def analyze(init_capital: float, final_bal: float, trades: list, equity: list):
    if not trades:
        print("NO TRADES")
        return
    ret = (final_bal / init_capital - 1) * 100
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr = len(wins) / len(trades) * 100
    gp = sum(t["pnl"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 0.001
    pf = gp / gl
    avg_win = gp / len(wins) if wins else 0
    avg_loss = -gl / len(losses) if losses else 0
    eq_arr = np.array(equity)
    dd_arr = (np.maximum.accumulate(eq_arr) - eq_arr) / np.maximum.accumulate(eq_arr) * 100
    max_dd = dd_arr.max()
    years = len(equity) / (365 * 96)
    cagr = (final_bal / init_capital) ** (1 / max(years, 0.01)) - 1 if years > 0 else 0
    daily_rets = np.diff(eq_arr[::96]) / eq_arr[::96][:-1]
    sharpe = daily_rets.mean() / daily_rets.std() * np.sqrt(365) if len(daily_rets) > 1 and daily_rets.std() > 0 else 0

    print(f"Return: {ret:+.1f}% | CAGR: {cagr*100:+.1f}% | MaxDD: {max_dd:.1f}% | Sharpe: {sharpe:.2f}")
    print(f"Trades: {len(trades)} | WR: {wr:.1f}% | PF: {pf:.2f} | AvgW: ${avg_win:.2f} | AvgL: ${avg_loss:.2f}")
    print("-" * 60)


# ─── CLI ─────────────────────────────────────────────────────────────────
async def main():
    import httpx

    print("=" * 70)
    print("SCALPING OPTIMIZED v2 — 15m Backtest (Binance BTCUSDT 3 years)")
    print("=" * 70)

    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": "BTCUSDT", "interval": "15m", "limit": "1500"}
    all_data = []
    end_time = None
    for page in range(200):
        if end_time: params["endTime"] = end_time
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200 or not resp.json(): break
        data = resp.json()
        all_data.extend(data)
        end_time = data[0][0] - 1

    close = np.array([float(d[4]) for d in all_data])[::-1]
    high = np.array([float(d[2]) for d in all_data])[::-1]
    low = np.array([float(d[3]) for d in all_data])[::-1]
    vol = np.array([float(d[5]) for d in all_data])[::-1]
    ts = np.array([int(d[0]) for d in all_data])[::-1]

    print(f"Data: {len(close)} bars ({len(close)/96/365:.1f} years)")
    cfg = ScalpConfig()

    # Test all 5 modes
    for mode_name, mode_key in [("BB RANGE REVERSION", "BB"),
                                 ("BB BREAKOUT", "BB_BREAK"),
                                 ("VWAP MEAN REVERSION", "VWAP_REV"),
                                 ("VWAP TREND PULLBACK", "VWAP_PULL"),
                                 ("EMA CROSSOVER", "EMA_CROSS")]:
        print(f"\n{'='*70}")
        print(f"{mode_name}")
        print(f"{'='*70}")
        bal, trades, eq = run_backtest(close, high, low, vol, ts, cfg, mode=mode_key)
        analyze(cfg.capital, bal, trades, eq)

    # Signal counts
    ind = compute_indicators(close, high, low, vol, ts, cfg)
    add_vwap_slope(ind, cfg)
    n = len(close)
    for name, (lf, sf) in [("BB Range", (bb_range_long, bb_range_short)),
                            ("BB Breakout", (bb_breakout_long, bb_breakout_short)),
                            ("VWAP Rev", (vwap_rev_long, vwap_rev_short)),
                            ("VWAP Pullback", (vwap_pullback_long, vwap_pullback_short)),
                            ("EMA Cross", (ema_cross_long, ema_cross_short))]:
        nl = sum(1 for i in range(200, n) if lf(i, ind, close, vol, cfg))
        ns = sum(1 for i in range(200, n) if sf(i, ind, close, vol, cfg))
        print(f"{name}: Long={nl}, Short={ns}, Total={nl+ns}")


if __name__ == "__main__":
    asyncio.run(main())