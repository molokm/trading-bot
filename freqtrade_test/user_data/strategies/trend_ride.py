# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""TrendRide — покупка откатов в сильном восходящем тренде.

Идея: входить в монету, когда она в устойчивом аптренде (цена > 200MA, EMA20>EMA50)
и происходит откат (RSI снижается), затем восстанавливается. Широкий ATR-трейлинг
позволяет тренду идти. В медвежий режим (BTC < SMA200) — кэш (без шортов, т.к.
шорты на этих данных систематически сливали). Лонг-онли.
"""

import numpy as np
import pandas as pd
from datetime import datetime
from pandas import DataFrame
from typing import Optional

import talib.abstract as ta

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter


class TrendRide(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short: bool = False
    startup_candle_count = 250
    process_only_new_candles = True

    minimal_roi = {"0": 100}
    stoploss = -0.25
    use_custom_stoploss = True
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    rsi_pull_low = IntParameter(low=35, high=55, default=45, space="buy", optimize=True, load=True)
    rsi_pull_high = IntParameter(low=50, high=70, default=60, space="buy", optimize=True, load=True)
    adx_min = IntParameter(low=15, high=30, default=20, space="buy", optimize=True, load=True)
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
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        btc_df = self.dp.get_pair_dataframe("BTC/USDT:USDT", "1d")
        if btc_df is not None and not btc_df.empty and "close" in btc_df:
            btc_df["sma200"] = ta.SMA(btc_df, timeperiod=200)
            btc_df["date"] = pd.to_datetime(btc_df["date"])
            btc_map = btc_df.set_index("date")["sma200"]
            dataframe["date"] = pd.to_datetime(dataframe["date"])
            daily = dataframe["date"].dt.normalize().map(btc_map)
            dataframe["btc_sma200"] = daily.ffill()
        else:
            dataframe["btc_sma200"] = np.nan
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        uptrend = (
            (dataframe["close"] > dataframe["sma200"])
            & (dataframe["ema20"] > dataframe["ema50"])
            & (dataframe["close"] > dataframe["btc_sma200"])
        )
        pullback = (
            (dataframe["rsi"] > self.rsi_pull_low.value)
            & (dataframe["rsi"] < self.rsi_pull_high.value)
        )
        dataframe.loc[
            uptrend & pullback & (dataframe["adx"] >= self.adx_min.value) & (dataframe["volume"] > 0),
            ["enter_long", "enter_tag"],
        ] = (1, "trend_pullback")
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
        if current_rate > peak:
            trade.set_custom_data("peak", current_rate)
            peak = current_rate
        stop = peak - atr * self.atr_stop_mult.value
        ratio = lev * (stop / current_rate - 1.0)
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
        return min(margin, equity * 0.5, max_stake)
