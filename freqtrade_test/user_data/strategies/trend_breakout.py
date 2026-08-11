# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""TrendBreakout — трендовая система на пробое канала Дончиана.

Отличается от MomentumRotation: входит на пробое N-дневного максимума/минимума
(а не по скорингу ротации), держит позицию широким ATR-трейлингом (ловит большие
тренды), шортит в медвежьем режиме. Направление фильтруется по BTC vs SMA200.
"""

import numpy as np
import pandas as pd
from datetime import datetime
from pandas import DataFrame
from typing import Optional

import talib.abstract as ta

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter


class TrendBreakout(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short: bool = True
    startup_candle_count = 250
    process_only_new_candles = True

    minimal_roi = {"0": 100}
    stoploss = -0.30
    use_custom_stoploss = True
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    entry_period = IntParameter(low=20, high=60, default=30, space="buy", optimize=True, load=True)
    adx_min = IntParameter(low=15, high=30, default=20, space="buy", optimize=True, load=True)
    min_roc = DecimalParameter(low=1.0, high=6.0, default=2.0, decimals=1, space="buy", optimize=True, load=True)
    risk_per_trade = DecimalParameter(low=0.05, high=0.15, default=0.10, decimals=2, space="buy", optimize=True, load=True)
    max_leverage = DecimalParameter(low=1.0, high=3.0, default=2.0, decimals=1, space="buy", optimize=True, load=True)
    atr_stop_mult = DecimalParameter(low=2.0, high=4.5, default=3.0, decimals=1, space="sell", optimize=True, load=True)

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        n = self.entry_period.value
        dataframe["dc_high"] = dataframe["high"].rolling(n).max().shift(1)
        dataframe["dc_low"] = dataframe["low"].rolling(n).min().shift(1)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["roc"] = ta.ROC(dataframe, timeperiod=14)

        btc_df = self.dp.get_pair_dataframe("BTC/USDT:USDT", self.timeframe)
        if btc_df is not None and not btc_df.empty and "close" in btc_df:
            btc_df["sma200"] = ta.SMA(btc_df, timeperiod=200)
            btc_df["date"] = pd.to_datetime(btc_df["date"])
            merged = dataframe[["date"]].merge(
                btc_df[["date", "sma200"]], on="date", how="left"
            )["sma200"]
            dataframe["btc_sma200"] = merged.ffill()
        else:
            dataframe["btc_sma200"] = np.nan
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        btc_bull = dataframe["close"] > dataframe["btc_sma200"]
        btc_bear = dataframe["close"] < dataframe["btc_sma200"]

        dataframe.loc[
            (dataframe["close"] > dataframe["dc_high"])
            & btc_bull
            & (dataframe["adx"] >= self.adx_min.value)
            & (dataframe["roc"] >= self.min_roc.value)
            & (dataframe["volume"] > 0),
            ["enter_long", "enter_tag"],
        ] = (1, "dc_break_long")

        dataframe.loc[
            (dataframe["close"] < dataframe["dc_low"])
            & btc_bear
            & (dataframe["adx"] >= self.adx_min.value)
            & (dataframe["roc"] <= -self.min_roc.value)
            & (dataframe["volume"] > 0),
            ["enter_short", "enter_tag"],
        ] = (1, "dc_break_short")

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def custom_stoploss(
        self, pair: str, trade: Trade, current_time: datetime,
        current_rate: float, current_profit: float, after_fill: bool, **kwargs,
    ) -> Optional[float]:
        atr = trade.get_custom_data("atr")
        if atr is None:
            analyzed_df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if analyzed_df is None or analyzed_df.empty or "atr" not in analyzed_df:
                return self.stoploss
            atr = float(analyzed_df["atr"].iloc[-1])
            if atr <= 0:
                return self.stoploss
            trade.set_custom_data("atr", atr)
            trade.set_custom_data("peak", current_rate)

        peak = trade.get_custom_data("peak")
        lev = trade.leverage or 1.0

        if not trade.is_short:
            if current_rate > peak:
                trade.set_custom_data("peak", current_rate)
                peak = current_rate
            stop = peak - atr * self.atr_stop_mult.value
            ratio = lev * (stop / current_rate - 1.0)
        else:
            if current_rate < peak:
                trade.set_custom_data("peak", current_rate)
                peak = current_rate
            stop = peak + atr * self.atr_stop_mult.value
            ratio = -lev * (stop / current_rate - 1.0)

        return max(ratio, self.stoploss)

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
        stop_pct = (atr * self.atr_stop_mult.value) / current_rate if current_rate > 0 else 0.05
        stop_pct = min(max(stop_pct, 0.01), 0.5)
        lev = leverage if leverage and leverage > 0 else 1.0
        notional = (equity * self.risk_per_trade.value) / stop_pct
        margin = notional / lev
        max_margin = equity * 0.5
        return min(margin, max_margin, max_stake)
