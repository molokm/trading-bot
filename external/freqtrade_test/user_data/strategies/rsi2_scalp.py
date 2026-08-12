# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""Rsi2Scalp — высокочастотный скальп по RSI(2) в дневном аптренде.

Вход: RSI(2) экстремально низкий внутри долгосрочного аптренда (цена > дневного
SMA200 монеты и BTC). Выход: RSI(2) восстанавливается выше порога, либо тайм-стоп
в барах, либо ATR-стоп (страховка). Много сделок, небольшой профит каждой —
главное перекрыть комиссию. Таймфрейм задаётся конфигом (1h / 15m / 5m).
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


class Rsi2Scalp(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short: bool = False
    startup_candle_count = 1200
    process_only_new_candles = True

    minimal_roi = {"0": 100}
    stoploss = -0.05
    use_custom_stoploss = True
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    rsi2_enter = IntParameter(low=3, high=20, default=10, space="buy", optimize=True, load=True)
    rsi2_exit = IntParameter(low=45, high=75, default=58, space="sell", optimize=True, load=True)
    max_hold_bars = IntParameter(low=2, high=24, default=6, space="sell", optimize=True, load=True)
    atr_stop_mult = DecimalParameter(low=1.0, high=3.0, default=2.0, decimals=1, space="sell", optimize=True, load=True)
    risk_per_trade = DecimalParameter(low=0.02, high=0.10, default=0.05, decimals=2, space="buy", optimize=True, load=True)
    max_leverage = DecimalParameter(low=1.0, high=3.0, default=2.0, decimals=1, space="buy", optimize=True, load=True)

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    def _daily_trend(self, dataframe: DataFrame, metadata: dict) -> None:
        def daily_sma200(pair):
            df = self.dp.get_pair_dataframe(pair, "1d")
            if df is None or df.empty or "close" not in df:
                return None
            df["sma200"] = ta.SMA(df, timeperiod=200)
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date")["sma200"].shift(1)

        dataframe["date"] = pd.to_datetime(dataframe["date"])
        coin_map = daily_sma200(metadata["pair"])
        btc_map = daily_sma200("BTC/USDT:USDT")
        if coin_map is not None:
            dataframe["sma200"] = dataframe["date"].dt.normalize().map(coin_map).ffill()
        else:
            dataframe["sma200"] = np.nan
        if btc_map is not None:
            dataframe["btc_sma200"] = dataframe["date"].dt.normalize().map(btc_map).ffill()
        else:
            dataframe["btc_sma200"] = np.nan

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi2"] = ta.RSI(dataframe, timeperiod=2)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        self._daily_trend(dataframe, metadata)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        uptrend = (
            (dataframe["close"] > dataframe["sma200"])
            & (dataframe["close"] > dataframe["btc_sma200"])
        )
        oversold = dataframe["rsi2"] < self.rsi2_enter.value
        dataframe.loc[
            uptrend & oversold & (dataframe["volume"] > 0),
            ["enter_long", "enter_tag"],
        ] = (1, "rsi2_scalp")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            dataframe["rsi2"] > self.rsi2_exit.value,
            ["exit_long", "exit_tag"],
        ] = (1, "rsi2_recovered")
        return dataframe

    def custom_exit(
        self, pair: str, trade: Trade, current_time: datetime,
        current_rate: float, current_profit: float, **kwargs,
    ) -> Optional[str]:
        tf_min = timeframe_to_minutes(self.timeframe)
        bars = int((current_time - trade.open_date_utc).total_seconds() / (tf_min * 60))
        if bars >= self.max_hold_bars.value:
            return "time_stop"
        return None

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
            atr = current_rate * 0.02
        else:
            atr = float(analyzed_df["atr"].iloc[-1])
        stop_pct = (atr * self.atr_stop_mult.value) / current_rate if current_rate > 0 else 0.02
        stop_pct = min(max(stop_pct, 0.005), 0.5)
        lev = leverage if leverage and leverage > 0 else 1.0
        notional = (equity * self.risk_per_trade.value) / stop_pct
        margin = notional / lev
        return min(margin, equity * 0.5, max_stake)
