"""
Walk-forward backtest: no lookahead bias.
Swing levels confirmed with delay of `window` bars.
All other indicators (EMA, RSI, ATR) are causal — computed on full array but
each bar only depends on past data, so no bias.
"""
import asyncio, json, sys
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))


def ema(arr, span):
    return pd.Series(arr).ewm(span=span, adjust=False).mean().values


def rsi(close, period=14):
    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean().values
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean().values
    r = np.full(len(close), 50.0)
    for i in range(period, len(close)):
        r[i] = 0.0 if loss[i] == 0 else 100.0 - 100.0 / (1.0 + gain[i] / loss[i])
    return r


def atr(high, low, close, period=14):
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    atr_arr = np.insert(pd.Series(tr).rolling(period).mean().values, 0, 0)
    return atr_arr


def find_swing_levels(high, low, window=10):
    """Compute swing levels. Result shifted by `window` to avoid lookahead."""
    n = len(high)
    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)
    for i in range(window, n - window):
        if high[i] == max(high[i - window: i + window + 1]):
            sh[i] = high[i]
        if low[i] == min(low[i - window: i + window + 1]):
            sl[i] = low[i]
    # CRITICAL: shift forward by window — a swing level at bar i
    # is only confirmed at bar i+window
    sh_delayed = np.full(n, np.nan)
    sl_delayed = np.full(n, np.nan)
    for i in range(n):
        if not np.isnan(sh[i]) and i + window < n:
            sh_delayed[i + window] = sh[i]
        if not np.isnan(sl[i]) and i + window < n:
            sl_delayed[i + window] = sl[i]
    return sh_delayed, sl_delayed


def run_levx_pro(close, high, low, params, initial_capital=1000):
    n = len(close)
    ema_trend = int(params.get("ema_trend", 200))
    swing_window = int(params.get("swing_window", 40))
    pullback_pct = float(params.get("pullback_pct", 0.993))
    near_sl_pct = float(params.get("near_sl_pct", 1.003))
    rsi_exit_hi = float(params.get("rsi_exit_hi", 80))
    rsi_exit_lo = float(params.get("rsi_exit_lo", 20))
    trend_buffer = float(params.get("trend_buffer_pct", 0.003))
    fee = float(params.get("fee", 0.0005))
    size_pct = float(params.get("size_pct", 0.95))

    ema_arr = ema(close, ema_trend)
    rsi_arr = rsi(close, 14)
    sh, sl = find_swing_levels(high, low, swing_window)

    balance = initial_capital
    position = 0.0
    entry_price = 0.0
    entry_bar = -999
    confirmed_trend = 0
    csh, csl = 0.0, 0.0
    trades = []
    warmup = max(ema_trend, swing_window * 2) + 20

    for i in range(warmup, n):
        if np.isnan(ema_arr[i]):
            continue
        if not np.isnan(sh[i]):
            csh = sh[i]
        if not np.isnan(sl[i]):
            csl = sl[i]
        if csh == 0 or csl == 0:
            continue

        # Exit
        if position > 0:
            if close[i] < csl or rsi_arr[i] > rsi_exit_hi:
                pnl = position * (close[i] - entry_price)
                fee_cost = abs(position) * (entry_price + close[i]) * fee
                balance += pnl - fee_cost
                trades.append(pnl - fee_cost)
                position = 0
                entry_bar = i
                continue
        elif position < 0:
            if close[i] > csh or rsi_arr[i] < rsi_exit_lo:
                pnl = position * (close[i] - entry_price)
                fee_cost = abs(position) * (entry_price + close[i]) * fee
                balance += pnl - fee_cost
                trades.append(pnl - fee_cost)
                position = 0
                entry_bar = i
                continue

        if i - entry_bar < 3:
            continue

        # Trend
        raw_up = close[i] > ema_arr[i] * (1 + trend_buffer)
        raw_dn = close[i] < ema_arr[i] * (1 - trend_buffer)
        if raw_up:
            confirmed_trend = 1
        elif raw_dn:
            confirmed_trend = -1

        # Entry
        if confirmed_trend == 1:
            dropped = close[i] < csh * pullback_pct
            near_sup = close[i] <= csl * near_sl_pct if csl > 0 else False
            bounce = i > 0 and low[i] > low[i - 1]
            if dropped and near_sup and bounce and position == 0:
                entry_price = close[i]
                position = balance * size_pct / entry_price
                entry_bar = i
        elif confirmed_trend == -1:
            climbed = close[i] > csl * near_sl_pct
            near_res = close[i] >= csh * pullback_pct if csh > 0 else False
            reject = i > 0 and high[i] < high[i - 1]
            if climbed and near_res and reject and position == 0:
                entry_price = close[i]
                position = -balance * size_pct / entry_price
                entry_bar = i

    # Close final
    if position != 0:
        pnl = position * (close[-1] - entry_price)
        fee_cost = abs(position) * (entry_price + close[-1]) * fee
        balance += pnl - fee_cost
        trades.append(pnl - fee_cost)

    return balance, trades


def run_trend_bounce_pro(close, high, low, params, initial_capital=1000):
    n = len(close)
    ema_trend = int(params.get("ema_trend", 200))
    swing_window = int(params.get("swing_window", 40))
    rsi_exit = float(params.get("rsi_exit", 80))
    fee = float(params.get("fee", 0.0005))
    size_pct = float(params.get("size_pct", 0.95))

    ema_arr = ema(close, ema_trend)
    rsi_arr = rsi(close, 14)
    sh, sl = find_swing_levels(high, low, swing_window)

    balance = initial_capital
    position = 0.0
    entry_price = 0.0
    entry_bar = -999
    csh, csl = 0.0, 0.0
    trades = []
    warmup = swing_window * 2

    for i in range(warmup, n):
        if np.isnan(ema_arr[i]):
            continue
        if not np.isnan(sh[i]):
            csh = sh[i]
        if not np.isnan(sl[i]):
            csl = sl[i]
        if csh == 0 or csl == 0:
            continue

        if position > 0:
            if close[i] < csl or rsi_arr[i] > rsi_exit:
                pnl = position * (close[i] - entry_price)
                fee_cost = abs(position) * (entry_price + close[i]) * fee
                balance += pnl - fee_cost
                trades.append(pnl - fee_cost)
                position = 0
                entry_bar = i
                continue
        elif position < 0:
            if close[i] > csh or rsi_arr[i] < 100 - rsi_exit:
                pnl = position * (close[i] - entry_price)
                fee_cost = abs(position) * (entry_price + close[i]) * fee
                balance += pnl - fee_cost
                trades.append(pnl - fee_cost)
                position = 0
                entry_bar = i
                continue

        if i - entry_bar < 3:
            continue

        uptrend = close[i] > ema_arr[i]
        downtrend = close[i] < ema_arr[i]

        if uptrend and position == 0:
            dropped = close[i] < csh * 0.995
            near_sl = close[i] <= csl * 1.005
            bounce = i > 0 and low[i] > low[i - 1]
            if dropped and near_sl and bounce:
                entry_price = close[i]
                position = balance * size_pct / entry_price
                entry_bar = i
        elif downtrend and position == 0:
            climbed = close[i] > csl * 1.005
            near_sh = close[i] >= csh * 0.995
            reject = i > 0 and high[i] < high[i - 1]
            if climbed and near_sh and reject:
                entry_price = close[i]
                position = -balance * size_pct / entry_price
                entry_bar = i

    if position != 0:
        pnl = position * (close[-1] - entry_price)
        fee_cost = abs(position) * (entry_price + close[-1]) * fee
        balance += pnl - fee_cost
        trades.append(pnl - fee_cost)

    return balance, trades


def run_atr_trail(close, high, low, params, initial_capital=1000):
    n = len(close)
    ema_trend = int(params.get("ema_trend", 200))
    swing_window = int(params.get("swing_window", 30))
    atr_period = int(params.get("atr_period", 14))
    atr_mult = float(params.get("atr_mult", 2.0))
    fee = float(params.get("fee", 0.0005))
    size_pct = float(params.get("size_pct", 0.95))

    ema_arr = ema(close, ema_trend)
    atr_arr = atr(high, low, close, atr_period)
    sh, sl = find_swing_levels(high, low, swing_window)

    balance = initial_capital
    position = 0.0
    entry_price = 0.0
    stop_price = 0.0
    bars_since_entry = 999
    csh, csl = 0.0, 0.0
    trades = []
    warmup = swing_window * 2

    for i in range(warmup, n):
        bars_since_entry += 1
        if np.isnan(ema_arr[i]):
            continue
        if not np.isnan(sh[i]):
            csh = sh[i]
        if not np.isnan(sl[i]):
            csl = sl[i]
        if csh == 0 or csl == 0:
            continue

        if position > 0:
            stop_price = max(stop_price, close[i] - atr_arr[i] * atr_mult)
            if close[i] < stop_price:
                pnl = position * (close[i] - entry_price)
                fee_cost = abs(position) * (entry_price + close[i]) * fee
                balance += pnl - fee_cost
                trades.append(pnl - fee_cost)
                position = 0
                bars_since_entry = 0
                continue
        elif position < 0:
            stop_price = min(stop_price, close[i] + atr_arr[i] * atr_mult)
            if close[i] > stop_price:
                pnl = position * (close[i] - entry_price)
                fee_cost = abs(position) * (entry_price + close[i]) * fee
                balance += pnl - fee_cost
                trades.append(pnl - fee_cost)
                position = 0
                bars_since_entry = 0
                continue

        if bars_since_entry < 3:
            continue

        uptrend = close[i] > ema_arr[i]

        if uptrend and position == 0:
            if not np.isnan(sl[i]):
                prev = close[i - 1] if i > 0 else close[i]
                if close[i] > prev and close[i] > ema_arr[i]:
                    entry_price = close[i]
                    position = balance * size_pct / entry_price
                    stop_price = entry_price - atr_arr[i] * atr_mult
                    bars_since_entry = 0
        elif not uptrend and position == 0:
            if not np.isnan(sh[i]):
                prev = close[i - 1] if i > 0 else close[i]
                if close[i] < prev and close[i] < ema_arr[i]:
                    entry_price = close[i]
                    position = -balance * size_pct / entry_price
                    stop_price = entry_price + atr_arr[i] * atr_mult
                    bars_since_entry = 0

    if position != 0:
        pnl = position * (close[-1] - entry_price)
        fee_cost = abs(position) * (entry_price + close[-1]) * fee
        balance += pnl - fee_cost
        trades.append(pnl - fee_cost)

    return balance, trades


def compute_stats(balance, initial_capital, trades):
    if not trades:
        return {"return": 0, "trades": 0, "wr": 0, "pf": 0}
    ret = (balance / initial_capital - 1) * 100
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    wr = len(wins) / len(trades) * 100
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0.001
    pf = gross_profit / gross_loss
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean([abs(t) for t in losses]) if losses else 0
    return {
        "return": ret, "trades": len(trades), "wr": wr,
        "pf": pf, "avg_win": avg_win, "avg_loss": avg_loss
    }


async def main():
    from app.services.data_cache import _load_cache

    # Load 5m data
    cache_5m = _load_cache("BTC-USDT", "5m")
    if not cache_5m:
        print("No 5m cache. Run backtests first.")
        return

    # Load 1m data
    cache_1m = _load_cache("BTC-USDT", "1m")
    if not cache_1m:
        print("No 1m cache. Run fetch_1m.py first.")
        return

    for tf, cache_data in [("5m", cache_5m), ("1m", cache_1m)]:
        print(f"\n{'='*60}")
        print(f" WALK-FORWARD BACKTEST — {tf} (NO LOOKAHEAD)")
        print(f"{'='*60}")
        print(f"Candles: {len(cache_data)}")

        arr = np.array(cache_data, dtype=object)
        close = arr[:, 4].astype(float)
        high = arr[:, 2].astype(float)
        low = arr[:, 3].astype(float)

        strategies = [
            ("LevX Pro", run_levx_pro,
             {"ema_trend": 200, "swing_window": 40, "pullback_pct": 0.993,
              "near_sl_pct": 1.003, "rsi_exit_hi": 80, "rsi_exit_lo": 20,
              "size_pct": 0.95, "fee": 0.0005, "leverage": 1, "trend_buffer_pct": 0.003}),
            ("Trend Bounce Pro", run_trend_bounce_pro,
             {"ema_trend": 200, "swing_window": 40, "size_pct": 0.95,
              "fee": 0.0005, "rsi_exit": 80}),
            ("Momentum ATR Trail", run_atr_trail,
             {"ema_trend": 200, "swing_window": 30, "atr_period": 14,
              "atr_mult": 2.0, "size_pct": 0.95, "fee": 0.0005}),
        ]

        for name, func, params in strategies:
            bal, trades = func(close, high, low, params, 1000)
            s = compute_stats(bal, 1000, trades)
            print(f"\n{name:30s} {s['trades']:4d} trades  "
                  f"{s['return']:+7.1f}%  WR {s['wr']:5.1f}%  "
                  f"PF {s['pf']:5.2f}  "
                  f"avgW {s['avg_win']:.2f}  avgL {s['avg_loss']:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
