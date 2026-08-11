# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""Momentum Rotation strategy — freqtrade port of momentum_rotation_v3_2026-08-08.py.

Daily-bar rotation over BTC/ETH/BNB/SOL USDT-M futures:
  - score = ROC(14)*0.5 + ema_spread_pct*0.3 + (ADX/50)*0.2
  - filters: vol, RSI, |ROC|>=3%, ADX>=25, EMA20/50 trend, BTC vs SMA200 regime, correlation
  - top_k=2 slots, ranking enforced in confirm_trade_entry, rotation exit in custom_exit
  - initial stop = daily ATR * 3.5, trailing = peak - ATR*0.1, breakeven after +2%, partial TP 50% at +10%
  - risk-based sizing: 10% of equity / stop_pct, dynamic leverage 1/(2*ATR%) capped at 2.0
"""

import numpy as np
import pandas as pd
from datetime import datetime
from pandas import DataFrame
from typing import Optional

import talib.abstract as ta

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter


class MomentumRotation(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short: bool = True
    startup_candle_count = 250
    process_only_new_candles = True

    minimal_roi = {"0": 100}
    stoploss = -0.30
    use_custom_stoploss = True
    trailing_stop = False
    position_adjustment_enable = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    top_k = 2
    roc_period = 14
    ema_fast = 20
    ema_slow = 50
    atr_period = 14
    sma_long = 200
    allocation_pct = 1.0
    partial_tp_ratio = 0.5
    allow_short = True
    use_regime = True
    bull_side = "long"
    bear_side = "cash"

    adx_min = IntParameter(low=18, high=30, default=22, space="buy", optimize=True, load=True)
    min_roc = DecimalParameter(low=1.0, high=5.0, default=3.0, decimals=1, space="buy", optimize=True, load=True)
    min_hold_days = IntParameter(low=3, high=20, default=10, space="sell", optimize=True, load=True)
    max_leverage = DecimalParameter(low=1.0, high=3.0, default=3.0, decimals=1, space="buy", optimize=True, load=True)
    risk_per_trade = DecimalParameter(low=0.02, high=0.15, default=0.02, decimals=2, space="buy", optimize=True, load=True)
    atr_stop_mult = DecimalParameter(low=1.5, high=4.0, default=2.0, decimals=1, space="sell", optimize=True, load=True)
    trail_atr_mult = DecimalParameter(low=0.1, high=0.5, default=0.1, decimals=1, space="sell", optimize=True, load=True)
    breakeven_pct = DecimalParameter(low=0.01, high=0.05, default=0.03, decimals=2, space="sell", optimize=True, load=True)
    partial_tp_pct = DecimalParameter(low=0.03, high=0.15, default=0.10, decimals=2, space="sell", optimize=True, load=True)
    vol_mult = DecimalParameter(low=1.0, high=2.0, default=1.5, decimals=1, space="buy", optimize=True, load=True)
    rsi_long_max = IntParameter(low=70, high=85, default=75, space="buy", optimize=True, load=True)
    rsi_short_min = IntParameter(low=20, high=30, default=25, space="buy", optimize=True, load=True)
    corr_threshold = DecimalParameter(low=0.5, high=0.8, default=0.7, decimals=1, space="buy", optimize=True, load=True)

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    def informative_pairs(self):
        return []

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=self.ema_fast)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=self.ema_slow)
        dataframe["ema_trend"] = dataframe["ema_fast"] > dataframe["ema_slow"]
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=self.atr_period)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["roc"] = ta.ROC(dataframe, timeperiod=self.roc_period)
        dataframe["avg_atr_30"] = dataframe["atr"].rolling(30).mean()
        dataframe["trend_spread"] = (
            (dataframe["ema_fast"] - dataframe["ema_slow"]) / dataframe["ema_slow"] * 100
        )
        dataframe["score"] = (
            dataframe["roc"] * 0.5
            + dataframe["trend_spread"] * 0.3
            + (dataframe["adx"] / 50) * 0.2
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        atr_ok = dataframe["atr"] > 0
        vol_ok = ~((dataframe["avg_atr_30"] > 0) & (dataframe["atr"] > dataframe["avg_atr_30"] * self.vol_mult.value))
        rsi_ok_long = dataframe["rsi"] <= self.rsi_long_max.value
        rsi_ok_short = dataframe["rsi"] >= self.rsi_short_min.value
        roc_ok = dataframe["roc"].abs() >= self.min_roc.value
        adx_ok = dataframe["adx"] >= self.adx_min.value

        dataframe.loc[
            (atr_ok & vol_ok & rsi_ok_long & roc_ok & adx_ok
             & (dataframe["roc"] > self.min_roc.value) & dataframe["ema_trend"]),
            "enter_long",
        ] = 1

        dataframe.loc[
            (self.allow_short & atr_ok & vol_ok & rsi_ok_short & roc_ok & adx_ok
             & (dataframe["roc"] < -self.min_roc.value) & ~dataframe["ema_trend"]),
            "enter_short",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    # ─── cross-pair ranking helpers ───

    def _sma(self, closes: pd.Series, period: int) -> float:
        if len(closes) < period:
            return 0.0
        return float(closes.tail(period).mean())

    def _snapshot(self, pair: str, df: DataFrame) -> Optional[dict]:
        if df is None or len(df) < 60:
            return None
        closes = df["close"]
        if "ema_fast" in df:
            ema_f = float(df["ema_fast"].iloc[-1])
            ema_s = float(df["ema_slow"].iloc[-1])
            atr = float(df["atr"].iloc[-1])
            adx = float(df["adx"].iloc[-1])
            rsi = float(df["rsi"].iloc[-1])
            roc = float(df["roc"].iloc[-1])
            avg_atr_30 = float(df["avg_atr_30"].iloc[-1])
        else:
            ema_f = float(ta.EMA(df, timeperiod=self.ema_fast).iloc[-1])
            ema_s = float(ta.EMA(df, timeperiod=self.ema_slow).iloc[-1])
            atr_arr = ta.ATR(df, timeperiod=self.atr_period)
            atr = float(atr_arr.iloc[-1])
            adx = float(ta.ADX(df, timeperiod=14).iloc[-1])
            rsi = float(ta.RSI(df, timeperiod=14).iloc[-1])
            roc = float(ta.ROC(df, timeperiod=self.roc_period).iloc[-1])
            avg_atr_30 = float(atr_arr.iloc[-30:].mean())
        trend = (ema_f - ema_s) / ema_s * 100 if ema_s > 0 else 0.0
        score = roc * 0.5 + trend * 0.3 + (adx / 50) * 0.2
        returns = closes.pct_change().dropna()
        return {
            "pair": pair,
            "roc": roc,
            "ema_trend": ema_f > ema_s,
            "atr": atr,
            "adx": adx,
            "rsi": rsi,
            "avg_atr_30": avg_atr_30,
            "close": float(closes.iloc[-1]),
            "score": score,
            "returns": returns,
            "sma200": self._sma(closes, self.sma_long),
            "sma50": self._sma(closes, 50),
            "above_sma200": self._sma(closes, self.sma_long) > 0
            and float(closes.iloc[-1]) > self._sma(closes, self.sma_long),
            "sma200_ok": len(closes) >= self.sma_long,
        }

    def _get_regime(self, snapshots: dict) -> str:
        btc = next((s for p, s in snapshots.items() if p.split("/")[0] == "BTC"), None)
        if btc is None or not btc["sma200_ok"] or btc["sma200"] <= 0:
            return "unknown"
        close = btc["close"]
        if close > btc["sma200"]:
            return "bull"
        if btc["sma50"] < btc["sma200"]:
            return "bear"
        return "chop"

    def _rank(self, current_time: datetime) -> dict:
        whitelist = self.dp.current_whitelist()
        snapshots = {}
        for p in whitelist:
            df = self.dp.get_pair_dataframe(p, self.timeframe)
            snap = self._snapshot(p, df)
            if snap is None:
                continue
            snapshots[p] = snap

        regime = self._get_regime(snapshots)

        ranked = []
        btc_above = None
        for p, ind in snapshots.items():
            if p.split("/")[0] == "BTC":
                btc_above = ind["above_sma200"] if ind["sma200_ok"] else None
            if ind["atr"] <= 0:
                continue
            if ind["avg_atr_30"] > 0 and ind["atr"] > ind["avg_atr_30"] * self.vol_mult.value:
                continue
            if ind["rsi"] > self.rsi_long_max.value and ind["ema_trend"]:
                continue
            if ind["rsi"] < self.rsi_short_min.value and not ind["ema_trend"]:
                continue
            if not self.use_regime and btc_above is not None and not btc_above \
                    and ind["roc"] > 0 and ind["ema_trend"]:
                continue
            if abs(ind["roc"]) < self.min_roc.value:
                continue
            ranked.append(ind)

        ranked.sort(key=lambda x: x["score"], reverse=True)

        targets = {}
        for ind in ranked:
            if len(targets) >= self.top_k:
                break
            if not self.use_regime:
                if ind["roc"] > self.min_roc.value and ind["ema_trend"] and ind["adx"] >= self.adx_min.value:
                    side = "long"
                elif self.allow_short and ind["roc"] < -self.min_roc.value \
                        and not ind["ema_trend"] and ind["adx"] >= self.adx_min.value:
                    side = "short"
                else:
                    continue
            elif regime in ("bull", "unknown"):
                if ind["roc"] > self.min_roc.value and ind["ema_trend"] and ind["adx"] >= self.adx_min.value:
                    side = "long"
                else:
                    continue
            elif regime == "bear":
                if self.allow_short and ind["roc"] < -self.min_roc.value \
                        and not ind["ema_trend"] and ind["adx"] >= self.adx_min.value:
                    side = "short"
                else:
                    continue
            else:
                continue
            if not self._corr_ok(ind["returns"], targets, snapshots):
                continue
            targets[ind["pair"]] = side

        return {"targets": targets, "snapshots": snapshots, "regime": regime}

    def _corr_ok(self, candidate_returns: pd.Series, targets: dict, snapshots: dict) -> bool:
        for held_pair in targets:
            held = snapshots.get(held_pair)
            if not held or held["returns"].empty:
                continue
            n = min(len(candidate_returns), len(held["returns"]))
            if n < 15:
                continue
            corr = np.corrcoef(candidate_returns.iloc[-n:], held["returns"].iloc[-n:])[0, 1]
            if abs(corr) > self.corr_threshold.value:
                return False
        return True

    # ─── entry / exit callbacks ───

    def confirm_trade_entry(
        self, pair: str, order_type: str, amount: float, rate: float,
        time_in_force: str, current_time: datetime, entry_tag: Optional[str],
        side: str, **kwargs,
    ) -> bool:
        rank = self._rank(current_time)
        targets = rank["targets"]
        if pair not in targets or targets[pair] != side:
            return False
        snapshots = rank["snapshots"]
        cand = snapshots.get(pair)
        if cand is None:
            return False
        for trade in Trade.get_open_trades():
            if trade.pair == pair:
                continue
            held = snapshots.get(trade.pair)
            if not held or held["returns"].empty or cand["returns"].empty:
                continue
            n = min(len(cand["returns"]), len(held["returns"]))
            if n < 15:
                continue
            corr = np.corrcoef(cand["returns"].iloc[-n:], held["returns"].iloc[-n:])[0, 1]
            if abs(corr) > self.corr_threshold.value:
                return False
        return True

    def custom_exit(
        self, pair: str, trade: Trade, current_time: datetime,
        current_rate: float, current_profit: float, **kwargs,
    ) -> Optional[str]:
        hold_days = (current_time - trade.open_date_utc).days
        if hold_days < self.min_hold_days.value:
            return None
        targets = self._rank(current_time)["targets"]
        side = "short" if trade.is_short else "long"
        if pair not in targets or targets[pair] != side:
            return "rotation_exit"
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
            trade.set_custom_data("be_done", False)
            trade.set_custom_data("bars", 0)
            trade.set_custom_data("next_stop", None)

        bars = trade.get_custom_data("bars", 0)
        peak = trade.get_custom_data("peak")
        be_done = trade.get_custom_data("be_done", False)
        lev = trade.leverage or 1.0

        if not trade.is_short:
            if current_rate > peak:
                trade.set_custom_data("peak", current_rate)
                peak = current_rate
            initial_stop = trade.open_rate - atr * self.atr_stop_mult.value
            if bars >= 2:
                if not be_done and current_profit >= self.breakeven_pct.value:
                    trade.set_custom_data("be_done", True)
                stop_next = trade.open_rate - atr * self.atr_stop_mult.value
                if be_done:
                    stop_next = max(stop_next, trade.open_rate * 0.999)
                stop_next = max(stop_next, peak - atr * self.trail_atr_mult.value)
            else:
                stop_next = initial_stop
        else:
            if current_rate < peak:
                trade.set_custom_data("peak", current_rate)
                peak = current_rate
            initial_stop = trade.open_rate + atr * self.atr_stop_mult.value
            if bars >= 2:
                if not be_done and current_profit <= -self.breakeven_pct.value:
                    trade.set_custom_data("be_done", True)
                stop_next = trade.open_rate + atr * self.atr_stop_mult.value
                if be_done:
                    stop_next = min(stop_next, trade.open_rate * 1.001)
                stop_next = min(stop_next, peak + atr * self.trail_atr_mult.value)
            else:
                stop_next = initial_stop

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

    def adjust_trade_position(
        self, trade: Trade, current_time: datetime, current_rate: float,
        current_profit: float, min_stake: Optional[float], max_stake: float,
        current_entry_rate: float, current_exit_rate: float,
        current_entry_profit: float, current_exit_profit: float, **kwargs,
    ):
        if trade.get_custom_data("partial_done", False):
            return None
        if not trade.is_short and current_profit < self.partial_tp_pct.value:
            return None
        if trade.is_short and current_profit > -self.partial_tp_pct.value:
            return None
        reduction = trade.stake_amount * self.partial_tp_ratio
        if min_stake and trade.stake_amount - reduction < min_stake:
            return None
        trade.set_custom_data("partial_done", True)
        return -reduction

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
        stop_pct = (atr * self.atr_stop_mult.value) / current_rate if current_rate > 0 else 0.04
        stop_pct = min(max(stop_pct, 0.01), 0.5)
        lev = leverage if leverage and leverage > 0 else 1.0
        notional = (equity * self.risk_per_trade.value) / stop_pct
        margin = notional / lev
        max_margin = equity * self.allocation_pct / self.top_k
        return min(margin, max_margin, max_stake)
