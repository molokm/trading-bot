# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""Impulse1D — порт стратегии Impulse 1D (быстрый импульс + пирамидинг + каскадный TP).

Вход: |ROC(1)|>=4% + всплеск объёма (>=1.5× среднего за 24) + тренд EMA20>50.
Пирамидинг: до 2 докупок (60% текущей) на новых пиках (+0.5×ATR) с всплеском объёма, в окне 5 дней.
Стоп 5×ATR, безубыток +0.5%, трейлинг 8×ATR. Каскадный TP: 30% на +2×ATR, 30% на +6×ATR.
Выход через 30 дней. До 4 позиций, ранжирование по |ROC|.
"""

import numpy as np
import pandas as pd
from datetime import datetime
from pandas import DataFrame
from typing import Optional

import talib.abstract as ta

from freqtrade.exchange import timeframe_to_minutes
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter


class Impulse1D(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short: bool = True
    startup_candle_count = 250
    process_only_new_candles = True

    minimal_roi = {"0": 100}
    stoploss = -0.20
    use_custom_stoploss = True
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    ema_fast = 20
    ema_slow = 50
    vol_period = 24
    be_pct = 0.005
    add_window_bars = 5

    entry_roc = DecimalParameter(low=2.0, high=8.0, default=4.0, decimals=1, space="buy", optimize=True, load=True)
    vol_mult = DecimalParameter(low=1.2, high=2.5, default=1.5, decimals=1, space="buy", optimize=True, load=True)
    max_adds = IntParameter(low=0, high=3, default=2, space="buy", optimize=True, load=True)
    add_size_ratio = DecimalParameter(low=0.4, high=1.0, default=0.6, decimals=1, space="buy", optimize=True, load=True)
    add_atr_mult = DecimalParameter(low=0.3, high=1.0, default=0.5, decimals=1, space="buy", optimize=True, load=True)
    risk_per_trade = DecimalParameter(low=0.05, high=0.15, default=0.10, decimals=2, space="buy", optimize=True, load=True)
    max_leverage = DecimalParameter(low=1.0, high=3.0, default=3.0, decimals=1, space="buy", optimize=True, load=True)
    sl_atr_mult = DecimalParameter(low=3.0, high=6.0, default=5.0, decimals=1, space="sell", optimize=True, load=True)
    trail_atr_mult = DecimalParameter(low=5.0, high=12.0, default=8.0, decimals=1, space="sell", optimize=True, load=True)
    tp1_atr = DecimalParameter(low=1.0, high=4.0, default=2.0, decimals=1, space="sell", optimize=True, load=True)
    tp2_atr = DecimalParameter(low=4.0, high=10.0, default=6.0, decimals=1, space="sell", optimize=True, load=True)
    tp1_frac = DecimalParameter(low=0.2, high=0.5, default=0.3, decimals=1, space="sell", optimize=True, load=True)
    max_hold = IntParameter(low=15, high=45, default=30, space="sell", optimize=True, load=True)

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["roc"] = ta.ROC(dataframe, timeperiod=1)
        dataframe["ema_f"] = ta.EMA(dataframe, timeperiod=self.ema_fast)
        dataframe["ema_s"] = ta.EMA(dataframe, timeperiod=self.ema_slow)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["avg_vol"] = dataframe["volume"].rolling(self.vol_period).mean()
        return dataframe

    def _vol_surge(self, dataframe: DataFrame) -> pd.Series:
        return dataframe["volume"] >= dataframe["avg_vol"] * self.vol_mult.value

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        surge = self._vol_surge(dataframe)
        dataframe.loc[
            (dataframe["roc"] >= self.entry_roc.value)
            & (dataframe["ema_f"] > dataframe["ema_s"])
            & surge
            & (dataframe["volume"] > 0),
            ["enter_long", "enter_tag"],
        ] = (1, "impulse_long")
        dataframe.loc[
            (dataframe["roc"] <= -self.entry_roc.value)
            & (dataframe["ema_f"] < dataframe["ema_s"])
            & surge
            & (dataframe["volume"] > 0),
            ["enter_short", "enter_tag"],
        ] = (1, "impulse_short")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def _init_trade_state(self, trade: Trade) -> None:
        analyzed_df, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
        if analyzed_df is None or analyzed_df.empty or "atr" not in analyzed_df:
            atr = trade.open_rate * 0.04
        else:
            atr = float(analyzed_df["atr"].iloc[-1])
        trade.set_custom_data("atr", max(atr, trade.open_rate * 0.001))
        trade.set_custom_data("peak", trade.open_rate)
        trade.set_custom_data("be_done", False)
        trade.set_custom_data("adds", 0)
        trade.set_custom_data("last_add_peak", trade.open_rate)
        trade.set_custom_data("tp1_done", False)
        trade.set_custom_data("tp2_done", False)
        trade.set_custom_data("bars", 0)
        trade.set_custom_data("next_stop", None)

    def custom_stoploss(
        self, pair: str, trade: Trade, current_time: datetime,
        current_rate: float, current_profit: float, after_fill: bool, **kwargs,
    ) -> Optional[float]:
        if trade.get_custom_data("atr") is None:
            self._init_trade_state(trade)
        atr = trade.get_custom_data("atr")
        peak = trade.get_custom_data("peak")
        lev = trade.leverage or 1.0
        bars = trade.get_custom_data("bars", 0)

        if not trade.is_short:
            if current_rate > peak:
                trade.set_custom_data("peak", current_rate)
                peak = current_rate
            initial_stop = trade.open_rate - atr * self.sl_atr_mult.value
            if not trade.get_custom_data("be_done") and current_profit >= self.be_pct:
                trade.set_custom_data("be_done", True)
            stop_next = trade.open_rate - atr * self.sl_atr_mult.value
            if trade.get_custom_data("be_done"):
                stop_next = max(stop_next, trade.open_rate * 0.999)
            stop_next = max(stop_next, peak - atr * self.trail_atr_mult.value)
        else:
            if current_rate < peak:
                trade.set_custom_data("peak", current_rate)
                peak = current_rate
            initial_stop = trade.open_rate + atr * self.sl_atr_mult.value
            if not trade.get_custom_data("be_done") and current_profit <= -self.be_pct:
                trade.set_custom_data("be_done", True)
            stop_next = trade.open_rate + atr * self.sl_atr_mult.value
            if trade.get_custom_data("be_done"):
                stop_next = min(stop_next, trade.open_rate * 1.001)
            stop_next = min(stop_next, peak + atr * self.trail_atr_mult.value)

        # лаговый стоп: текущая свеча проверяется по стопу из предыдущей (без same-candle)
        prev_stop = trade.get_custom_data("next_stop")
        if prev_stop is None:
            prev_stop = initial_stop
        trade.set_custom_data("next_stop", stop_next)
        trade.set_custom_data("bars", bars + 1)

        if not trade.is_short:
            ratio = lev * (prev_stop / current_rate - 1.0)
        else:
            ratio = -lev * (prev_stop / current_rate - 1.0)
        return max(ratio, self.stoploss)

    def custom_exit(
        self, pair: str, trade: Trade, current_time: datetime,
        current_rate: float, current_profit: float, **kwargs,
    ) -> Optional[str]:
        tf_min = timeframe_to_minutes(self.timeframe)
        days = int((current_time - trade.open_date_utc).total_seconds() / (tf_min * 60))
        if days >= self.max_hold.value:
            return "time_stop"
        return None

    def adjust_trade_position(
        self, trade: Trade, current_time: datetime, current_rate: float,
        current_profit: float, min_stake: Optional[float], max_stake: float,
        current_entry_rate: float, current_exit_rate: float,
        current_entry_profit: float, current_exit_profit: float, **kwargs,
    ):
        if trade.get_custom_data("atr") is None:
            self._init_trade_state(trade)
        atr = trade.get_custom_data("atr")
        entry = trade.open_rate
        if atr <= 0 or entry <= 0:
            return None

        # каскадный TP: 30% на tp1_atr×ATR, 30% на tp2_atr×ATR (по прибыли в ATR)
        if not trade.is_short:
            dist_atr = (current_rate - entry) / atr
        else:
            dist_atr = (entry - current_rate) / atr
        stake = trade.stake_amount

        if not trade.get_custom_data("tp1_done") and dist_atr >= self.tp1_atr.value:
            trade.set_custom_data("tp1_done", True)
            reduce = stake * self.tp1_frac.value
            if min_stake and stake - reduce >= min_stake:
                return -reduce
        if not trade.get_custom_data("tp2_done") and dist_atr >= self.tp2_atr.value:
            trade.set_custom_data("tp2_done", True)
            reduce = stake * self.tp1_frac.value
            if min_stake and stake - reduce >= min_stake:
                return -reduce

        # пирамидинг: до max_adds докупок, в окне, на новом пике + всплеск объёма
        adds = trade.get_custom_data("adds")
        tf_min = timeframe_to_minutes(self.timeframe)
        days = int((current_time - trade.open_date_utc).total_seconds() / (tf_min * 60))
        if adds < self.max_adds.value and days <= self.add_window_bars:
            last_peak = trade.get_custom_data("last_add_peak")
            analyzed_df, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
            surge = False
            if analyzed_df is not None and not analyzed_df.empty and "avg_vol" in analyzed_df:
                last = analyzed_df.iloc[-1]
                surge = float(last["volume"]) >= float(last["avg_vol"]) * self.vol_mult.value
            if not trade.is_short:
                new_peak = current_rate >= last_peak + atr * self.add_atr_mult.value
            else:
                new_peak = current_rate <= last_peak - atr * self.add_atr_mult.value
            if new_peak and surge:
                trade.set_custom_data("adds", adds + 1)
                trade.set_custom_data("last_add_peak", current_rate)
                return stake * self.add_size_ratio.value
        return None

    def leverage(
        self, pair: str, current_time: datetime, current_rate: float,
        proposed_leverage: float, max_leverage: float, entry_tag: Optional[str],
        side: str, **kwargs,
    ) -> float:
        analyzed_df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if analyzed_df is None or analyzed_df.empty or "atr" not in analyzed_df:
            return 1.0
        atr = float(analyzed_df["atr"].iloc[-1])
        if atr <= 0 or current_rate <= 0:
            return 1.0
        lev = 1.0 / ((atr / current_rate) * 2.0)
        lev = max(1.0, min(lev, min(max_leverage, self.max_leverage.value)))
        return round(lev, 1)

    def custom_stake_amount(
        self, pair: str, current_time: datetime, current_rate: float,
        proposed_stake: float, min_stake: Optional[float], max_stake: float,
        leverage: float, entry_tag: Optional[str], side: str, **kwargs,
    ) -> float:
        equity = self.wallets.get_total_stake_amount()
        if equity <= 0:
            equity = proposed_stake * 10
        analyzed_df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if analyzed_df is None or analyzed_df.empty or "atr" not in analyzed_df:
            atr = current_rate * 0.04
        else:
            atr = float(analyzed_df["atr"].iloc[-1])
        stop_pct = (atr * self.sl_atr_mult.value) / current_rate if current_rate > 0 else 0.15
        stop_pct = min(max(stop_pct, 0.01), 0.5)
        lev = leverage if leverage and leverage > 0 else 1.0
        notional = (equity * self.risk_per_trade.value) / stop_pct
        margin = notional / lev
        return min(margin, equity * 0.5, max_stake)
