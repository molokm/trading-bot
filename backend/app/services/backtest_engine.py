import json
import math
import importlib.util
import sys
from pathlib import Path
import pandas as pd
import numpy as np
from typing import List, Dict, Optional
from datetime import datetime, timedelta


def compute_sharpe(returns: List[float], rf: float = 0.02) -> float:
    if not returns or len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    excess = arr - rf / (365 * 24)
    if np.std(excess) == 0:
        return 0.0
    return float(np.mean(excess) / np.std(excess) * np.sqrt(365 * 24))


def compute_max_drawdown(equity: List[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for val in equity:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd * 100


class BacktestEngine:
    def __init__(self, strategy_code: str, strategy_name: str = "MA Crossover"):
        self.strategy_code = strategy_code
        self.strategy_name = strategy_name
        self._namespace = {}

    def _detect_strategy(self) -> str:
        name_lo = self.strategy_name.lower()
        if "scalpel" in name_lo and "trend" not in name_lo:
            return "scalpel"
        if "gerchik" in name_lo or "false breakout" in name_lo:
            return "gerchik_false_breakout"
        if "trend bounce" in name_lo or "bounce" in name_lo:
            return "trend_bounce"
        code_lo = self.strategy_code.lower()
        if "generate_signals" in code_lo:
            return "custom"
        return "default"

    def _exec_scalpel_signals(self, df: pd.DataFrame, params: dict) -> list:
        fast_ema = params.get("fast_ema", 9)
        slow_ema = params.get("slow_ema", 21)
        rsi_period = params.get("rsi_period", 14)
        rsi_ob = params.get("rsi_overbought", 70)
        rsi_os = params.get("rsi_oversold", 30)
        vol_mult = params.get("volume_mult", 1.5)
        atr_sl_mult = params.get("atr_sl_mult", 1.5)
        atr_tp_mult = params.get("atr_tp_mult", 2.0)
        risk_per_trade = params.get("risk_per_trade", 0.01)
        cooldown = params.get("cooldown_bars", 3)

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        volume = df["vol"].values
        ts = df["ts"].values
        n = len(df)

        ema_fast = pd.Series(close).ewm(span=fast_ema).mean().values
        ema_slow = pd.Series(close).ewm(span=slow_ema).mean().values

        delta = pd.Series(close).diff()
        gain = delta.where(delta > 0, 0).rolling(rsi_period).mean().values
        loss = (-delta.where(delta < 0, 0)).rolling(rsi_period).mean().values
        rsi = np.full(n, np.nan)
        for i in range(rsi_period, n):
            if loss[i] == 0:
                rsi[i] = 100.0
            else:
                rs_val = gain[i] / loss[i]
                rsi[i] = 100.0 - (100.0 / (1.0 + rs_val))

        tr = np.maximum(high - low,
            np.maximum(
                np.abs(high - np.roll(close, 1)),
                np.abs(low - np.roll(close, 1))
            ))
        tr[0] = high[0] - low[0]
        atr = pd.Series(tr).rolling(slow_ema).mean().values

        vol_sma = pd.Series(volume).rolling(slow_ema).mean().values

        signals = [0] * n
        for i in range(slow_ema + 1, n):
            if np.isnan(rsi[i]) or np.isnan(atr[i]) or np.isnan(vol_sma[i]) or vol_sma[i] == 0:
                continue

            above_ema = close[i] > ema_slow[i]
            below_ema = close[i] < ema_slow[i]
            ema_bull = ema_fast[i] > ema_slow[i]
            ema_bear = ema_fast[i] < ema_slow[i]

            vol_spike = volume[i] > vol_sma[i] * vol_mult
            rsi_low = rsi[i] < rsi_os
            rsi_high = rsi[i] > rsi_ob

            if ema_bull and above_ema and rsi_low and vol_spike:
                signals[i] = 1
            elif ema_bear and below_ema and rsi_high and vol_spike:
                signals[i] = -1

        return signals

    def run(self, candles: List[dict], initial_capital: float = 10000.0,
            params: dict = None) -> dict:
        df = pd.DataFrame(candles)
        df.columns = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]
        df = df.astype({
            "open": float, "high": float, "low": float, "close": float, "vol": float
        })
        df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
        df = df.sort_values("ts").reset_index(drop=True)
        df["symbol"] = params.get("symbol", "Unknown") if params else "Unknown"

        strategy_type = self._detect_strategy()

        if strategy_type == "scalpel" or "scalpel" in self.strategy_name.lower():
            return self._run_scalpel(df, initial_capital, params or {})
        elif strategy_type == "gerchik_false_breakout" or "gerchik" in self.strategy_name.lower():
            return self._run_gerchik_false_breakout(df, initial_capital, params or {})
        elif strategy_type == "trend_bounce" or "trend bounce" in self.strategy_name.lower():
            import sys as _sys
            _sys.stderr.write(f"[BT] trend_bounce selected, type={strategy_type}, name={self.strategy_name}\n")
            return self._run_trend_bounce(df, initial_capital, params or {})
        else:
            return self._run_generic(df, initial_capital, params or {})

    def _run_scalpel(self, df: pd.DataFrame, initial_capital: float,
                     params: dict) -> dict:
        signals = self._exec_scalpel_signals(df, params)
        n = len(df)

        atr_mult_sl = params.get("atr_sl_mult", 1.5)
        atr_mult_tp = params.get("atr_tp_mult", 2.0)
        risk_per_trade = params.get("risk_per_trade", 0.01)
        cooldown = params.get("cooldown_bars", 3)
        max_loss_pct = params.get("max_daily_loss", 0.03)
        max_cons_losses = params.get("max_consecutive_losses", 3)

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        ts = df["ts"].values

        tr = np.maximum(high - low,
            np.maximum(
                np.abs(high - np.roll(close, 1)),
                np.abs(low - np.roll(close, 1))
            ))
        tr[0] = high[0] - low[0]
        atr_period = params.get("atr_period", 14)
        atr = pd.Series(tr).rolling(atr_period).mean().values

        balance = float(initial_capital)
        equity = [float(balance)]
        trades = []
        position = 0.0
        entry_price = 0.0
        entry_bar = 0
        sl_price = 0.0
        tp_price = 0.0
        last_trade_bar = -cooldown
        daily_pnl = 0.0
        cons_losses = 0
        current_date = None

        for i in range(1, n):
            ts_date = pd.Timestamp(ts[i]).date()
            if current_date is None:
                current_date = ts_date

            if ts_date != current_date:
                daily_pnl = 0.0
                current_date = ts_date

            if position == 0:
                equity.append(balance)
            else:
                unrealized = position * (close[i] - entry_price)
                equity.append(balance + unrealized)

            if abs(daily_pnl) >= initial_capital * max_loss_pct:
                continue

            # Close position if SL/TP hit
            if position != 0:
                hit_sl = (position > 0 and low[i] <= sl_price) or (position < 0 and high[i] >= sl_price)
                hit_tp = (position > 0 and high[i] >= tp_price) or (position < 0 and low[i] <= tp_price)

                if hit_sl or hit_tp:
                    exit_price = tp_price if hit_tp else sl_price
                    pnl = position * (exit_price - entry_price)
                    balance += pnl
                    daily_pnl += pnl
                    if pnl < 0:
                        cons_losses += 1
                    else:
                        cons_losses = 0
                    trades.append({
                        "time": str(ts[i]),
                        "side": "close_long" if position > 0 else "close_short",
                        "price": round(float(exit_price), 2),
                        "size": round(float(abs(position)), 6),
                        "pnl": round(float(pnl), 2),
                        "sl_hit": bool(hit_sl),
                        "tp_hit": bool(hit_tp)
                    })
                    position = 0.0
                    entry_price = 0.0
                    sl_price = 0.0
                    tp_price = 0.0
                    last_trade_bar = i
                    continue

            # Check cooldown
            if i - last_trade_bar < cooldown:
                continue

            # Check consecutive losses circuit breaker
            if cons_losses >= max_cons_losses:
                continue

            # Entry signals
            if position == 0 and signals[i] != 0:
                if np.isnan(atr[i]) or atr[i] <= 0:
                    continue

                atr_val = atr[i]
                entry_price = close[i]
                pos_size = (balance * risk_per_trade) / (atr_val * atr_mult_sl)
                pos_size = max(pos_size, 0)

                if signals[i] == 1:
                    position = pos_size
                    sl_price = entry_price - atr_val * atr_mult_sl
                    tp_price = entry_price + atr_val * atr_mult_tp
                else:
                    position = -pos_size
                    sl_price = entry_price + atr_val * atr_mult_sl
                    tp_price = entry_price - atr_val * atr_mult_tp

                entry_bar = i
                trades.append({
                    "time": str(ts[i]),
                    "side": "buy" if signals[i] == 1 else "sell",
                    "price": round(float(entry_price), 2),
                    "size": round(float(abs(position)), 6),
                    "pnl": 0
                })

        if position != 0:
            final_close = float(close[-1])
            pnl = float(position) * (final_close - float(entry_price))
            balance += pnl
            trades.append({
                "time": str(ts[-1]),
                "side": "close_final",
                "price": round(final_close, 2),
                "size": round(float(abs(position)), 6),
                "pnl": round(pnl, 2)
            })
            equity[-1] = balance
            position = 0

        return self._build_results(df, initial_capital, balance, equity, trades, params)

    def _run_gerchik_false_breakout(self, df: pd.DataFrame, initial_capital: float,
                                     params: dict) -> dict:
        swing_window = params.get("swing_window", 20)
        confirm_bars = params.get("confirm_bars", 2)
        atr_period = params.get("atr_period", 14)
        atr_sl_mult = params.get("atr_sl_mult", 1.5)
        atr_tp_mult = params.get("atr_tp_mult", 2.0)
        risk_per_trade = params.get("risk_per_trade", 0.01)
        cooldown = params.get("cooldown_bars", 3)
        max_loss_pct = params.get("max_daily_loss", 0.03)
        max_cons_losses = params.get("max_consecutive_losses", 3)

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        ts = df["ts"].values
        n = len(df)

        tr = np.maximum(high - low,
            np.maximum(
                np.abs(high - np.roll(close, 1)),
                np.abs(low - np.roll(close, 1))
            ))
        tr[0] = high[0] - low[0]
        atr = pd.Series(tr).rolling(atr_period).mean().values

        swing_highs = np.zeros(n)
        swing_lows = np.zeros(n)
        for i in range(swing_window, n - swing_window):
            if high[i] == max(high[i-swing_window:i+swing_window+1]):
                if i == 0 or high[i] > high[i-1]:
                    swing_highs[i] = high[i]
            if low[i] == min(low[i-swing_window:i+swing_window+1]):
                if i == 0 or low[i] < low[i-1]:
                    swing_lows[i] = low[i]

        balance = float(initial_capital)
        equity = [float(balance)]
        trades = []
        position = 0.0
        entry_price = 0.0
        sl_price = 0.0
        tp_price = 0.0
        last_trade_bar = -cooldown
        daily_pnl = 0.0
        cons_losses = 0
        current_date = None

        last_high = 0.0
        last_low = 0.0
        breakout_state = 0
        breakout_bar = 0

        for i in range(1, n):
            ts_date = pd.Timestamp(ts[i]).date()
            if current_date is None:
                current_date = ts_date
            if ts_date != current_date:
                daily_pnl = 0.0
                current_date = ts_date

            equity.append(balance if position == 0 else balance + position * (close[i] - entry_price))

            if abs(daily_pnl) >= initial_capital * max_loss_pct:
                continue

            if position != 0:
                hit_sl = (position > 0 and low[i] <= sl_price) or (position < 0 and high[i] >= sl_price)
                hit_tp = (position > 0 and high[i] >= tp_price) or (position < 0 and low[i] <= tp_price)
                if hit_sl or hit_tp:
                    exit_price = tp_price if hit_tp else sl_price
                    pnl = position * (exit_price - entry_price)
                    balance += pnl
                    daily_pnl += pnl
                    if pnl < 0:
                        cons_losses += 1
                    else:
                        cons_losses = 0
                    trades.append({
                        "time": str(ts[i]),
                        "side": "close_long" if position > 0 else "close_short",
                        "price": round(float(exit_price), 2),
                        "size": round(float(abs(position)), 6),
                        "pnl": round(float(pnl), 2),
                        "sl_hit": bool(hit_sl),
                        "tp_hit": bool(hit_tp)
                    })
                    position = 0.0
                    entry_price = 0.0
                    sl_price = 0.0
                    tp_price = 0.0
                    last_trade_bar = i
                    continue

            if i - last_trade_bar < cooldown or cons_losses >= max_cons_losses:
                continue

            if swing_highs[i] > 0:
                last_high = swing_highs[i]
            if swing_lows[i] > 0:
                last_low = swing_lows[i]

            if last_high > 0 and breakout_state == 0:
                if close[i] > last_high and close[i-1] <= last_high:
                    breakout_state = 1
                    breakout_bar = i
            if breakout_state == 1:
                if close[i] < last_high and i - breakout_bar <= confirm_bars:
                    breakout_state = 0
                    if position == 0 and not np.isnan(atr[i]) and atr[i] > 0:
                        entry_price = close[i]
                        pos_size = (balance * risk_per_trade) / (atr[i] * atr_sl_mult)
                        pos_size = max(pos_size, 0)
                        position = -pos_size
                        sl_price = entry_price + atr[i] * atr_sl_mult
                        tp_price = entry_price - atr[i] * atr_tp_mult
                        trades.append({
                            "time": str(ts[i]), "side": "sell",
                            "price": round(float(entry_price), 2),
                            "size": round(float(abs(position)), 6), "pnl": 0
                        })
                elif i - breakout_bar > confirm_bars:
                    breakout_state = 0

            if last_low > 0 and breakout_state == 0:
                if close[i] < last_low and close[i-1] >= last_low:
                    breakout_state = -1
                    breakout_bar = i
            if breakout_state == -1:
                if close[i] > last_low and i - breakout_bar <= confirm_bars:
                    breakout_state = 0
                    if position == 0 and not np.isnan(atr[i]) and atr[i] > 0:
                        entry_price = close[i]
                        pos_size = (balance * risk_per_trade) / (atr[i] * atr_sl_mult)
                        pos_size = max(pos_size, 0)
                        position = pos_size
                        sl_price = entry_price - atr[i] * atr_sl_mult
                        tp_price = entry_price + atr[i] * atr_tp_mult
                        trades.append({
                            "time": str(ts[i]), "side": "buy",
                            "price": round(float(entry_price), 2),
                            "size": round(float(abs(position)), 6), "pnl": 0
                        })
                elif i - breakout_bar > confirm_bars:
                    breakout_state = 0

        if position != 0:
            final_close = float(close[-1])
            pnl = float(position) * (final_close - float(entry_price))
            balance += pnl
            trades.append({
                "time": str(ts[-1]), "side": "close_final",
                "price": round(final_close, 2),
                "size": round(float(abs(position)), 6),
                "pnl": round(pnl, 2)
            })
            equity[-1] = balance
            position = 0

        return self._build_results(df, initial_capital, balance, equity, trades, params)

    def _run_trend_bounce(self, df: pd.DataFrame, initial_capital: float,
                          params: dict) -> dict:
        try:
            exec_globals = {"pd": pd, "np": np, "math": math}
            exec(self.strategy_code, exec_globals)
        except Exception as e:
            return {"error": f"Strategy compile error: {str(e)}"}

        if "generate_signals" not in exec_globals:
            return {"error": "No generate_signals(df, params) function found"}

        try:
            raw = exec_globals["generate_signals"](df, params or {})
        except Exception as e:
            return {"error": f"generate_signals failed: {str(e)}"}

        if hasattr(raw, "values"):
            raw = raw.values
        if len(raw) != len(df):
            return {"error": "Signal length mismatch"}

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        ts = df["ts"].values
        n = len(df)

        balance = float(initial_capital)
        equity = [float(balance)]
        trades = []
        position = 0.0
        entry_price = 0.0
        entry_bar = 0
        last_trade_bar = -5
        daily_pnl = 0.0
        cons_losses = 0
        current_date = None
        max_loss_pct = params.get("max_daily_loss", 0.10)
        max_cons_losses = params.get("max_consecutive_losses", 50)
        _fee_rate = params.get("fee", 0.001)
        _size_pct = params.get("size_pct", 0.95)  # fraction of capital per trade

        for i in range(1, n):
            ts_date = pd.Timestamp(ts[i]).date()
            if current_date is None:
                current_date = ts_date
            if ts_date != current_date:
                daily_pnl = 0.0
                current_date = ts_date

            equity.append(float(balance) if position == 0
                          else float(balance) + float(position) * (float(close[i]) - float(entry_price)))

            if abs(daily_pnl) >= initial_capital * max_loss_pct:
                continue

            if cons_losses >= max_cons_losses:
                continue

            sig = int(raw[i])

            # Exit: signal drops to 0 while in position
            if position != 0 and sig == 0:
                exit_price = close[i]
                entry_notional = abs(position) * entry_price
                exit_notional = abs(position) * exit_price
                total_fee = (entry_notional + exit_notional) * _fee_rate
                pnl = position * (exit_price - entry_price) - total_fee
                balance += pnl
                daily_pnl += pnl
                cons_losses = cons_losses + 1 if pnl < 0 else 0
                trades.append({
                    "time": str(ts[i]),
                    "side": "close_long" if position > 0 else "close_short",
                    "price": round(float(exit_price), 2),
                    "size": round(float(abs(position)), 6),
                    "pnl": round(float(pnl), 2),
                    "fee": round(float(total_fee), 6),
                    "exit_reason": "signal_close"
                })
                position = 0.0
                entry_price = 0.0
                last_trade_bar = i
                continue

            # Exit: signal flips (1 -> -1 or -1 -> 1)
            if position > 0 and sig == -1:
                exit_price = close[i]
                entry_notional = abs(position) * entry_price
                exit_notional = abs(position) * exit_price
                total_fee = (entry_notional + exit_notional) * _fee_rate
                pnl = position * (exit_price - entry_price) - total_fee
                balance += pnl
                daily_pnl += pnl
                cons_losses = cons_losses + 1 if pnl < 0 else 0
                trades.append({
                    "time": str(ts[i]),
                    "side": "close_long",
                    "price": round(float(exit_price), 2),
                    "size": round(float(abs(position)), 6),
                    "pnl": round(float(pnl), 2),
                    "fee": round(float(total_fee), 6),
                    "exit_reason": "signal_flip"
                })
                position = 0.0
                entry_price = 0.0
                # Fall through to open short below

            if position < 0 and sig == 1:
                exit_price = close[i]
                entry_notional = abs(position) * entry_price
                exit_notional = abs(position) * exit_price
                total_fee = (entry_notional + exit_notional) * _fee_rate
                pnl = position * (exit_price - entry_price) - total_fee
                balance += pnl
                daily_pnl += pnl
                cons_losses = cons_losses + 1 if pnl < 0 else 0
                trades.append({
                    "time": str(ts[i]),
                    "side": "close_short",
                    "price": round(float(exit_price), 2),
                    "size": round(float(abs(position)), 6),
                    "pnl": round(float(pnl), 2),
                    "fee": round(float(total_fee), 6),
                    "exit_reason": "signal_flip"
                })
                position = 0.0
                entry_price = 0.0

            # Cooldown
            if i - entry_bar < 3:
                continue

            # Entry: use reduced size (size_pct) to leave room for fees
            if position == 0 and sig != 0:
                entry_price = close[i]
                pos_size = balance * _size_pct / entry_price
                position = pos_size if sig == 1 else -pos_size
                entry_bar = i
                trades.append({
                    "time": str(ts[i]),
                    "side": "buy" if sig == 1 else "sell",
                    "price": round(float(entry_price), 2),
                    "size": round(float(abs(position)), 6),
                    "pnl": 0,
                })

        if position != 0:
            final_close = float(close[-1])
            entry_notional = abs(position) * entry_price
            exit_notional = abs(position) * final_close
            total_fee = (entry_notional + exit_notional) * _fee_rate
            pnl = float(position) * (final_close - float(entry_price)) - total_fee
            balance += pnl
            trades.append({
                "time": str(ts[-1]),
                "side": "close_final",
                "price": round(final_close, 2),
                "size": round(float(abs(position)), 6),
                "pnl": round(pnl, 2),
                "fee": round(float(total_fee), 6),
                "exit_reason": "end_of_data"
            })
            equity[-1] = balance
            position = 0

        return self._build_results(df, initial_capital, balance, equity, trades, params)

    def _run_generic(self, df: pd.DataFrame, initial_capital: float,
                     params: dict) -> dict:
        try:
            exec_globals = {"pd": pd, "np": np, "math": math}
            exec(self.strategy_code, exec_globals)
        except Exception as e:
            return {"error": f"Strategy compile error: {str(e)}"}

        if "generate_signals" not in exec_globals:
            return {"error": "No generate_signals(df, params) function found"}

        try:
            raw = exec_globals["generate_signals"](df, params or {})
        except Exception as e:
            return {"error": f"generate_signals failed: {str(e)}"}

        if hasattr(raw, "values"):
            raw = raw.values
        if len(raw) != len(df):
            return {"error": "Signal length mismatch"}

        atr_period = params.get("atr_period", 14)
        atr_sl_mult = params.get("atr_sl_mult", 1.5)
        atr_tp_mult = params.get("atr_tp_mult", 2.0)
        risk_per_trade = params.get("risk_per_trade", 0.01)
        cooldown = params.get("cooldown_bars", 3)
        max_loss_pct = params.get("max_daily_loss", 0.03)
        max_cons_losses = params.get("max_consecutive_losses", 5)
        use_atr_stops = params.get("use_atr_stops", True)

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        ts = df["ts"].values
        n = len(df)

        tr = np.maximum(high - low,
            np.maximum(
                np.abs(high - np.roll(close, 1)),
                np.abs(low - np.roll(close, 1))
            ))
        tr[0] = high[0] - low[0]
        atr = pd.Series(tr).rolling(atr_period).mean().values

        balance = float(initial_capital)
        equity = [float(balance)]
        trades = []
        position = 0.0
        entry_price = 0.0
        sl_price = 0.0
        tp_price = 0.0
        last_trade_bar = -cooldown
        daily_pnl = 0.0
        cons_losses = 0
        current_date = None

        for i in range(1, n):
            ts_date = pd.Timestamp(ts[i]).date()
            if current_date is None:
                current_date = ts_date
            if ts_date != current_date:
                daily_pnl = 0.0
                current_date = ts_date

            equity.append(balance if position == 0 else balance + position * (close[i] - entry_price))

            if abs(daily_pnl) >= initial_capital * max_loss_pct:
                continue

            if position != 0 and use_atr_stops and not np.isnan(atr[i]) and atr[i] > 0:
                hit_sl = (position > 0 and low[i] <= sl_price) or (position < 0 and high[i] >= sl_price)
                hit_tp = (position > 0 and high[i] >= tp_price) or (position < 0 and low[i] <= tp_price)
                if hit_sl or hit_tp:
                    exit_price = tp_price if hit_tp else sl_price
                    pnl = position * (exit_price - entry_price)
                    balance += pnl
                    daily_pnl += pnl
                    cons_losses = cons_losses + 1 if pnl < 0 else 0
                    trades.append({
                        "time": str(ts[i]), "side": "close_long" if position > 0 else "close_short",
                        "price": round(float(exit_price), 2),
                        "size": round(float(abs(position)), 6),
                        "pnl": round(float(pnl), 2),
                        "sl_hit": bool(hit_sl), "tp_hit": bool(hit_tp)
                    })
                    position = 0.0
                    entry_price = 0.0
                    sl_price = 0.0
                    tp_price = 0.0
                    last_trade_bar = i
                    continue

            if i - last_trade_bar < cooldown:
                continue
            if cons_losses >= max_cons_losses:
                continue

            sig = int(raw[i])
            if sig == 0:
                continue

            if use_atr_stops and not np.isnan(atr[i]) and atr[i] > 0:
                if position == 0:
                    entry_price = close[i]
                    pos_size = (balance * risk_per_trade) / (atr[i] * atr_sl_mult)
                    pos_size = max(pos_size, 0)
                    if sig == 1:
                        position = pos_size
                        sl_price = entry_price - atr[i] * atr_sl_mult
                        tp_price = entry_price + atr[i] * atr_tp_mult
                        side_label = "buy"
                    else:
                        position = -pos_size
                        sl_price = entry_price + atr[i] * atr_sl_mult
                        tp_price = entry_price - atr[i] * atr_tp_mult
                        side_label = "sell"
                    trades.append({
                        "time": str(ts[i]), "side": side_label,
                        "price": round(float(entry_price), 2),
                        "size": round(float(abs(position)), 6), "pnl": 0
                    })
            else:
                if sig == 1 and position <= 0:
                    if position < 0:
                        pnl = position * (entry_price - close[i])
                        balance += pnl
                        cons_losses = cons_losses + 1 if pnl < 0 else 0
                        trades.append({
                            "time": str(ts[i]), "side": "close_short",
                            "price": close[i], "size": abs(position), "pnl": pnl
                        })
                    pos_sz = balance * 0.95 / close[i]
                    position = pos_sz
                    entry_price = close[i]
                    trades.append({
                        "time": str(ts[i]), "side": "buy",
                        "price": close[i], "size": pos_sz, "pnl": 0
                    })
                elif sig == -1 and position >= 0:
                    if position > 0:
                        pnl = position * (close[i] - entry_price)
                        balance += pnl
                        cons_losses = cons_losses + 1 if pnl < 0 else 0
                        trades.append({
                            "time": str(ts[i]), "side": "close_long",
                            "price": close[i], "size": position, "pnl": pnl
                        })
                    pos_sz = balance * 0.95 / close[i]
                    position = -pos_sz
                    entry_price = close[i]
                    trades.append({
                        "time": str(ts[i]), "side": "sell",
                        "price": close[i], "size": pos_sz, "pnl": 0
                    })

        if position != 0:
            final_close = float(close[-1])
            pnl = float(position) * (final_close - float(entry_price))
            balance += pnl
            trades.append({
                "time": str(ts[-1]), "side": "close_final",
                "price": round(final_close, 2),
                "size": round(float(abs(position)), 6),
                "pnl": round(pnl, 2)
            })
            equity[-1] = balance
            position = 0

        return self._build_results(df, initial_capital, balance, equity, trades, params)

    def _build_results(self, df, initial_capital, final_balance, equity, trades, params):
        total_return = final_balance - initial_capital
        total_return_pct = (total_return / initial_capital) * 100

        returns_list = []
        for j in range(1, len(equity)):
            if equity[j - 1] > 0:
                returns_list.append((equity[j] - equity[j - 1]) / equity[j - 1])

        winning = [t for t in trades if t.get("pnl", 0) > 0]
        losing = [t for t in trades if t.get("pnl", 0) < 0]

        equity_curve = [
            {"time": str(df.loc[i, "ts"]), "equity": round(float(equity[i]), 2)}
            for i in range(len(equity))
        ]

        entry_trades = [t for t in trades if t["side"] in ("buy", "sell")]
        close_trades = [t for t in trades if t["side"] in ("close_long", "close_short", "close_final")]

        return {
            "strategy_name": params.get("name", self.strategy_name),
            "symbol": str(df["symbol"].iloc[0]) if "symbol" in df.columns else params.get("symbol", "Unknown"),
            "timeframe": params.get("timeframe", "1H"),
            "period": f"{str(df.loc[0, 'ts'])} - {str(df.loc[len(df)-1, 'ts'])}",
            "initial_capital": initial_capital,
            "final_capital": round(final_balance, 2),
            "total_return": round(total_return, 2),
            "total_return_pct": round(total_return_pct, 2),
            "sharpe_ratio": round(compute_sharpe(returns_list), 4),
            "max_drawdown": round(compute_max_drawdown(equity), 2),
            "win_rate": round(len(winning) / max(len(close_trades), 1) * 100, 2),
            "total_trades": len(close_trades),
            "total_entry_signals": len(entry_trades),
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "avg_win": round(float(np.mean([t["pnl"] for t in winning])) if winning else 0, 2),
            "avg_loss": round(float(np.mean([t["pnl"] for t in losing])) if losing else 0, 2),
            "profit_factor": round(
                abs(sum(t["pnl"] for t in winning) / min(sum(t["pnl"] for t in losing), -0.01))
                if losing and sum(t["pnl"] for t in losing) < 0 else 0, 2
            ),
            "equity_curve": equity_curve,
            "trades": trades,
            "risk_params": {
                "sl_atr_mult": params.get("atr_sl_mult", 1.5),
                "tp_atr_mult": params.get("atr_tp_mult", 2.0),
                "risk_per_trade_pct": params.get("risk_per_trade", 0.01) * 100,
                "max_daily_loss_pct": params.get("max_daily_loss", 0.03) * 100,
            }
        }


def load_strategy_file(filepath: str) -> Optional[dict]:
    path = Path(filepath)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        meta = {"filename": path.name}
        if path.suffix == ".json":
            data = json.loads(content)
            meta.update(data)
        else:
            lines = content.split("\n")
            for line in lines[:20]:
                if line.startswith("# ") and ":" in line:
                    key, val = line[2:].split(":", 1)
                    meta[key.strip().lower()] = val.strip()
            meta["code"] = content
        return meta
    except Exception as e:
        return {"error": str(e)}
