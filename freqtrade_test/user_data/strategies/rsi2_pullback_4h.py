# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""Rsi2Pullback4h — RSI(2) откат в аптренде на 4h барах.

Долгосрочный тренд (цена > дневного SMA200) оценивается по дневным свечам —
как для монеты, так и для BTC (со сдвигом на день, без lookahead).
"""

import numpy as np
import pandas as pd
from pandas import DataFrame

import talib.abstract as ta

from rsi2_pullback import Rsi2Pullback


class Rsi2Pullback4h(Rsi2Pullback):
    timeframe = "4h"
    startup_candle_count = 1200

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi2"] = ta.RSI(dataframe, timeperiod=2)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["date"] = pd.to_datetime(dataframe["date"])

        def daily_sma200(pair):
            df = self.dp.get_pair_dataframe(pair, "1d")
            if df is None or df.empty or "close" not in df:
                return None
            df["sma200"] = ta.SMA(df, timeperiod=200)
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date")["sma200"].shift(1)

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
        return dataframe
