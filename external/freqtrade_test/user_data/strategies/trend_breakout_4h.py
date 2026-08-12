# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""TrendBreakout4h — прорыв канала Дончиана на 4h барах.

Режим-фильтр по BTC использует ДНЕВНОЙ SMA200 (со сдвигом на день — без lookahead).
"""

import numpy as np
import pandas as pd
from pandas import DataFrame

import talib.abstract as ta

from trend_breakout import TrendBreakout


class TrendBreakout4h(TrendBreakout):
    timeframe = "4h"
    startup_candle_count = 600

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        n = self.entry_period.value
        dataframe["dc_high"] = dataframe["high"].rolling(n).max().shift(1)
        dataframe["dc_low"] = dataframe["low"].rolling(n).min().shift(1)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["roc"] = ta.ROC(dataframe, timeperiod=14)

        btc_df = self.dp.get_pair_dataframe("BTC/USDT:USDT", "1d")
        if btc_df is not None and not btc_df.empty and "close" in btc_df:
            btc_df["sma200"] = ta.SMA(btc_df, timeperiod=200)
            btc_df["date"] = pd.to_datetime(btc_df["date"])
            # Режим известен только по завершённому дню → сдвиг на 1 день
            btc_map = btc_df.set_index("date")["sma200"].shift(1)
            dataframe["date"] = pd.to_datetime(dataframe["date"])
            daily = dataframe["date"].dt.normalize().map(btc_map)
            dataframe["btc_sma200"] = daily.ffill()
        else:
            dataframe["btc_sma200"] = np.nan
        return dataframe
