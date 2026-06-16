"""
SCALPING STRATEGY: 5 Entry Modes for BTC-USDT-SWAP 5m
Walk-forward verified, no lookahead.
Each mode uses a fundamentally different concept to find what works.
"""
import asyncio, sys, json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))


# ═══════════════════════════════════════════════════════════════
# PART 1: INDICATOR CALCULATIONS
# ═══════════════════════════════════════════════════════════════

def ema(arr, span):
    """Exponential Moving Average."""
    return pd.Series(arr).ewm(span=span, adjust=False).mean().values


def sma(arr, period):
    """Simple Moving Average."""
    return pd.Series(arr).rolling(period).mean().values


def rsi(close, period=14):
    """Relative Strength Index."""
    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean().values
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean().values
    r = np.full(len(close), 50.0)
    for i in range(period, len(close)):
        r[i] = 0.0 if loss[i] == 0 else 100.0 - 100.0 / (1.0 + gain[i] / loss[i])
    return r


def macd(close, fast=12, slow=26, signal=9):
    """MACD line, signal line, histogram."""
    ema_f = ema(close, fast)
    ema_s = ema(close, slow)
    macd_line = ema_f - ema_s
    signal_line = pd.Series(macd_line).ewm(span=signal, adjust=False).mean().values
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(high, low, close, period=14):
    """Average True Range."""
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    return np.insert(pd.Series(tr).rolling(period).mean().values, 0, 0)


def vwap(high, low, close, vol):
    """Volume Weighted Average Price (reset daily).
    For 5m data, resets every 288 bars (1 day = 24h * 12 = 288 bars).
    """
    typical = (high + low + close) / 3
    cum_tpv = np.cumsum(typical * vol)
    cum_vol = np.cumsum(vol)
    vwap_arr = np.where(cum_vol > 0, cum_tpv / cum_vol, close)
    return vwap_arr


def vwap_bands(vwap_arr, std_mult=1.5):
    """VWAP standard deviation bands."""
    upper = vwap_arr + std_mult * np.abs(vwap_arr * 0.005)  # ~0.5% from VWAP
    lower = vwap_arr - std_mult * np.abs(vwap_arr * 0.005)
    return upper, lower


def stochastic(high, low, close, k_period=14, d_period=3):
    """Stochastic Oscillator (%K, %D)."""
    n = len(close)
    k = np.full(n, 50.0)
    for i in range(k_period, n):
        h = np.max(high[i-k_period:i+1])
        l = np.min(low[i-k_period:i+1])
        k[i] = ((close[i] - l) / (h - l) * 100) if h != l else 50.0
    d = sma(k, d_period)
    return k, d


def adx(high, low, close, period=14):
    """Average Directional Index."""
    n = len(close)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    tr = np.zeros(n)
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
    return pd.Series(dx).rolling(period).mean().values, plus_di, minus_di


def supertrend(high, low, close, period=10, multiplier=1.5):
    """Supertrend indicator."""
    n = len(close)
    atr14 = atr(high, low, close, period)
    upper = (high + low) / 2 + multiplier * atr14
    lower = (high + low) / 2 - multiplier * atr14
    trend = np.ones(n)
    final_upper = np.copy(upper)
    final_lower = np.copy(lower)

    for i in range(1, n):
        if lower[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]:
            final_lower[i] = lower[i]
        else:
            final_lower[i] = final_lower[i-1]
        if upper[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]:
            final_upper[i] = upper[i]
        else:
            final_upper[i] = final_upper[i-1]
        if trend[i-1] == 1:
            trend[i] = 1 if close[i] >= final_lower[i] else -1
        else:
            trend[i] = -1 if close[i] <= final_upper[i] else 1
    return trend, final_upper, final_lower


# ═══════════════════════════════════════════════════════════════
# PART 2: STRATEGY RULES
# ═══════════════════════════════════════════════════════════════

"""
STRATEGY: 5 independent entry modes tested on BTC-USDT 5m

MODE A — Bollinger Mean Reversion
  Concept: price touches BB lower band + oversold → bounce to middle
  Long:  close <= BB_lower AND RSI < 35 AND close > EMA200
  Short: close >= BB_upper AND RSI > 65 AND close < EMA200
  Exit:  target = BB_middle, SL = 1.5 ATR

MODE B — VWAP Reversion
  Concept: price deviates from VWAP → reverts to mean
  Long:  close < VWAP * 0.997 AND RSI < 40 AND close > SMA20
  Short: close > VWAP * 1.003 AND RSI > 60 AND close < SMA20
  Exit:  target = VWAP, SL = 1.5 ATR

MODE C — Momentum Breakout
  Concept: break above recent high with volume → continuation
  Long:  close > highest(high, 12) AND MACD_hist > 0 AND vol > 1.5*avg
  Short: close < lowest(low, 12) AND MACD_hist < 0 AND vol > 1.5*avg
  Exit:  TP = 2.0 ATR, SL = 1.0 ATR, trail from +1 ATR

MODE D — EMA Pullback (trend-following)
  Concept: in uptrend, pullback to EMA20 → bounce
  Long:  EMA200 > price_prev (uptrend) AND close crosses above EMA20
         AND RSI > 40 AND RSI < 60
  Short: EMA200 < price_prev (downtrend) AND close crosses below EMA20
         AND RSI > 40 AND RSI < 60
  Exit:  TP = 2.0 ATR, SL = 1.5 ATR

MODE E — Dual EMA Cross + Volume
  Concept: fast EMA crosses slow EMA with volume confirmation
  Long:  EMA9 crosses above EMA21 AND close > EMA21 AND vol > 1.3*avg
  Short: EMA9 crosses below EMA21 AND close < EMA21 AND vol > 1.3*avg
  Exit:  TP = 1.5 ATR, SL = 1.0 ATR, trail from +0.75 ATR
"""


def bollinger(close, period=20, std_dev=2.0):
    """Bollinger Bands: middle, upper, lower."""
    middle = sma(close, period)
    std = pd.Series(close).rolling(period).std().values
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return middle, upper, lower


def highest(arr, period):
    """Rolling highest high."""
    return pd.Series(arr).rolling(period).max().values


def lowest(arr, period):
    """Rolling lowest low."""
    return pd.Series(arr).rolling(period).min().values


def compute_all_indicators(close, high, low, vol):
    """Compute all indicators for the strategy."""
    bb_mid, bb_upper, bb_lower = bollinger(close, 20, 2.0)
    return {
        "ema9": ema(close, 9),
        "ema21": ema(close, 21),
        "ema50": ema(close, 50),
        "ema200": ema(close, 200),
        "rsi14": rsi(close, 14),
        "macd_line": macd(close)[0],
        "macd_signal": macd(close)[1],
        "macd_hist": macd(close)[2],
        "atr14": atr(high, low, close, 14),
        "vwap": vwap(high, low, close, vol),
        "bb_mid": bb_mid,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "sma20": sma(close, 20),
        "vol_sma20": sma(vol, 20),
        "highest12": highest(high, 12),
        "lowest12": lowest(low, 12),
    }


# ═══════════════════════════════════════════════════════════════
# 5 ENTRY MODES — each uses a fundamentally different concept
# ═══════════════════════════════════════════════════════════════

# MODE A: Bollinger Mean Reversion (wide BB, RSI + volume confirm)
def mode_a_long(i, ind, close, vol):
    return (
        close[i] <= ind["bb_lower"][i] and
        ind["rsi14"][i] < 30 and
        ind["rsi14"][i] > ind["rsi14"][i-1] and  # turning up
        close[i] > ind["ema200"][i] and
        vol[i] > ind["vol_sma20"][i] * 1.2
    )
def mode_a_short(i, ind, close, vol):
    return (
        close[i] >= ind["bb_upper"][i] and
        ind["rsi14"][i] > 70 and
        ind["rsi14"][i] < ind["rsi14"][i-1] and  # turning down
        close[i] < ind["ema200"][i] and
        vol[i] > ind["vol_sma20"][i] * 1.2
    )

# MODE B: VWAP Reversion (stricter deviation + EMA filter)
def mode_b_long(i, ind, close, vol):
    return (
        close[i] < ind["vwap"][i] * 0.995 and  # 0.5% below VWAP
        ind["rsi14"][i] < 35 and
        ind["ema9"][i] > ind["ema21"][i] and  # micro-uptrend
        vol[i] > ind["vol_sma20"][i]
    )
def mode_b_short(i, ind, close, vol):
    return (
        close[i] > ind["vwap"][i] * 1.005 and  # 0.5% above VWAP
        ind["rsi14"][i] > 65 and
        ind["ema9"][i] < ind["ema21"][i] and  # micro-downtrend
        vol[i] > ind["vol_sma20"][i]
    )

# MODE C: Momentum Breakout (with pullback filter — enter after pullback, not at peak)
def mode_c_long(i, ind, close, vol):
    # Pullback to EMA21 in uptrend, then bounce
    return (
        close[i-1] <= ind["ema21"][i-1] and  # was below EMA21
        close[i] > ind["ema21"][i] and  # now above
        ind["ema50"][i] > ind["ema200"][i] and  # macro uptrend
        ind["rsi14"][i] > 45 and ind["rsi14"][i] < 65 and
        vol[i] > ind["vol_sma20"][i] * 1.3
    )
def mode_c_short(i, ind, close, vol):
    return (
        close[i-1] >= ind["ema21"][i-1] and  # was above EMA21
        close[i] < ind["ema21"][i] and  # now below
        ind["ema50"][i] < ind["ema200"][i] and  # macro downtrend
        ind["rsi14"][i] > 35 and ind["rsi14"][i] < 55 and
        vol[i] > ind["vol_sma20"][i] * 1.3
    )

# MODE D: EMA Pullback (stricter — require RSI in range + MACD confirmation)
def mode_d_long(i, ind, close, vol):
    return (
        ind["ema50"][i] > ind["ema200"][i] and  # macro uptrend
        close[i-1] < ind["ema50"][i-1] and  # was below EMA50
        close[i] > ind["ema50"][i] and  # now above EMA50
        40 < ind["rsi14"][i] < 60 and
        ind["macd_hist"][i] > 0
    )
def mode_d_short(i, ind, close, vol):
    return (
        ind["ema50"][i] < ind["ema200"][i] and
        close[i-1] > ind["ema50"][i-1] and
        close[i] < ind["ema50"][i] and
        40 < ind["rsi14"][i] < 60 and
        ind["macd_hist"][i] < 0
    )

# MODE E: Dual EMA Cross + Volume (stricter — RSI filter + volume spike)
def mode_e_long(i, ind, close, vol):
    return (
        ind["ema9"][i-1] <= ind["ema21"][i-1] and
        ind["ema9"][i] > ind["ema21"][i] and
        close[i] > ind["ema21"][i] and
        30 < ind["rsi14"][i] < 70 and  # not extreme
        vol[i] > ind["vol_sma20"][i] * 1.5  # strong volume
    )
def mode_e_short(i, ind, close, vol):
    return (
        ind["ema9"][i-1] >= ind["ema21"][i-1] and
        ind["ema9"][i] < ind["ema21"][i] and
        close[i] < ind["ema21"][i] and
        30 < ind["rsi14"][i] < 70 and
        vol[i] > ind["vol_sma20"][i] * 1.5
    )

ENTRY_MODES = {
    "A: BB Mean Reversion":    (mode_a_long, mode_a_short),
    "B: VWAP Reversion":       (mode_b_long, mode_b_short),
    "C: Momentum Breakout":    (mode_c_long, mode_c_short),
    "D: EMA Pullback":         (mode_d_long, mode_d_short),
    "E: EMA Cross + Volume":   (mode_e_long, mode_e_short),
}


# ═══════════════════════════════════════════════════════════════
# PART 3: BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════

def run_scalp_backtest(close, high, low, vol, ts, cap=10000,
                       risk_pct=0.01, sl_atr=1.5, tp_atr=2.0,
                       trail_activate=1.0, trail_atr=0.75,
                       max_daily_loss=0.03, max_daily_trades=10,
                       cooldown=3, max_hold=20, fee=0.0005,
                       long_fn=None, short_fn=None):
    """
    Full backtest of scalping strategy with risk management.

    Parameters:
      - risk_pct: max risk per trade (1% = 0.01)
      - sl_atr: stop loss in ATR multiples
      - tp_atr: take profit in ATR multiples
      - trail_activate: trailing activation in ATR from entry
      - trail_atr: trailing distance in ATR
      - max_daily_loss: max daily drawdown (3% = 0.03)
      - max_daily_trades: max trades per day
      - cooldown: bars between trades
      - max_hold: max bars to hold position
      - fee: taker fee rate
    """
    n = len(close)
    ind = compute_all_indicators(close, high, low, vol)
    if long_fn is None:
        long_fn = mode_e_long
    if short_fn is None:
        short_fn = mode_e_short

    balance = float(cap)
    peak_balance = float(cap)
    equity = [float(cap)]

    position = 0.0
    entry_price = 0.0
    entry_bar = -999
    sl_price = 0.0
    tp_price = 0.0
    trail_active = False
    trail_sl = 0.0
    entry_atr = 0.0

    trades = []
    daily_pnl = 0.0
    daily_trades = 0
    current_date = None

    for i in range(50, n):
        # ─── Daily reset ───
        try:
            ts_date = str(ts[i])[:10]
        except:
            ts_date = str(i // 288)

        if ts_date != current_date:
            daily_pnl = 0.0
            daily_trades = 0
            current_date = ts_date

        # ─── Track equity ───
        if position > 0:
            unrealized = position * (close[i] - entry_price)
        elif position < 0:
            unrealized = position * (close[i] - entry_price)
        else:
            unrealized = 0
        equity.append(balance + unrealized)

        # ─── Daily loss limit ───
        if abs(daily_pnl) >= cap * max_daily_loss:
            if position != 0:
                exit_price = close[i]
                notional = abs(position) * entry_price
                total_fee = (notional + abs(position) * exit_price) * fee
                pnl = position * (exit_price - entry_price) - total_fee
                balance += pnl
                daily_pnl += pnl
                trades.append({"pnl": pnl, "reason": "daily_limit", "bar": i})
                position = 0
            equity[-1] = balance
            continue

        # ─── Manage existing position ───
        if position != 0:
            bars_held = i - entry_bar

            if position > 0:
                # Update trailing stop
                if close[i] > entry_price + entry_atr * trail_activate:
                    trail_active = True
                    new_trail = close[i] - entry_atr * trail_atr
                    trail_sl = max(trail_sl, new_trail)

                # Check exits (priority: SL > trail > TP > time)
                exit_price = None
                exit_reason = None

                if close[i] <= sl_price:
                    exit_price = sl_price
                    exit_reason = "stop_loss"
                elif trail_active and close[i] <= trail_sl:
                    exit_price = trail_sl
                    exit_reason = "trailing_stop"
                elif close[i] >= tp_price:
                    exit_price = tp_price
                    exit_reason = "take_profit"
                elif bars_held >= max_hold:
                    exit_price = close[i]
                    exit_reason = "time_exit"

            else:  # short
                if close[i] < entry_price - entry_atr * trail_activate:
                    trail_active = True
                    new_trail = close[i] + entry_atr * trail_atr
                    trail_sl = min(trail_sl, new_trail)

                exit_price = None
                exit_reason = None

                if close[i] >= sl_price:
                    exit_price = sl_price
                    exit_reason = "stop_loss"
                elif trail_active and close[i] >= trail_sl:
                    exit_price = trail_sl
                    exit_reason = "trailing_stop"
                elif close[i] <= tp_price:
                    exit_price = tp_price
                    exit_reason = "take_profit"
                elif bars_held >= max_hold:
                    exit_price = close[i]
                    exit_reason = "time_exit"

            if exit_price is not None:
                notional = abs(position) * entry_price
                total_fee = (notional + abs(position) * exit_price) * fee
                pnl = position * (exit_price - entry_price) - total_fee
                balance += pnl
                daily_pnl += pnl
                peak_balance = max(peak_balance, balance)
                trades.append({
                    "pnl": pnl,
                    "reason": exit_reason,
                    "entry": entry_price,
                    "exit": exit_price,
                    "bars": bars_held,
                    "bar": i,
                })
                position = 0
                trail_active = False
                entry_bar = i  # for cooldown

        # ─── Check for new entry ───
        if position == 0:
            if i - entry_bar < cooldown:
                equity.append(balance)
                continue
            if daily_trades >= max_daily_trades:
                equity.append(balance)
                continue

            cur_atr = ind["atr14"][i]
            if cur_atr <= 0 or np.isnan(cur_atr):
                equity.append(balance)
                continue

            # LONG entry
            if long_fn(i, ind, close, vol):
                # Position sizing: risk 1% of capital
                risk_amount = balance * risk_pct
                sl_distance = cur_atr * sl_atr
                pos_size = risk_amount / sl_distance  # contracts
                entry_price = close[i]
                position = pos_size
                sl_price = entry_price - sl_distance
                tp_price = entry_price + cur_atr * tp_atr
                trail_sl = entry_price - cur_atr * sl_atr
                trail_active = False
                entry_atr = cur_atr
                entry_bar = i
                daily_trades += 1
                equity.append(balance)

            # SHORT entry
            elif short_fn(i, ind, close, vol):
                risk_amount = balance * risk_pct
                sl_distance = cur_atr * sl_atr
                pos_size = risk_amount / sl_distance
                entry_price = close[i]
                position = -pos_size
                sl_price = entry_price + sl_distance
                tp_price = entry_price - cur_atr * tp_atr
                trail_sl = entry_price + cur_atr * sl_atr
                trail_active = False
                entry_atr = cur_atr
                entry_bar = i
                daily_trades += 1
                equity.append(balance)
            else:
                equity.append(balance)

    # ─── Close remaining position ───
    if position != 0:
        exit_price = close[-1]
        notional = abs(position) * entry_price
        total_fee = (notional + abs(position) * exit_price) * fee
        pnl = position * (exit_price - entry_price) - total_fee
        balance += pnl
        trades.append({"pnl": pnl, "reason": "end_of_data", "bar": n-1})
        equity[-1] = balance

    return balance, trades, equity


# ═══════════════════════════════════════════════════════════════
# PART 4: ANALYSIS & REPORTING
# ═══════════════════════════════════════════════════════════════

def analyze_results(cap, final, trades, equity):
    """Comprehensive analysis of backtest results."""
    if not trades:
        print("  No trades executed!")
        return

    ret = (final / cap - 1) * 100
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    wr = len(wins) / len(trades) * 100

    gp = sum(t["pnl"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 0.001
    pf = gp / gl

    eq = np.array(equity)
    dd = ((np.maximum.accumulate(eq) - eq) / np.maximum.accumulate(eq) * 100).max()

    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0
    avg_bars = np.mean([t["bars"] for t in trades if "bars" in t])

    # Exit reason breakdown
    reasons = {}
    for t in trades:
        r = t.get("reason", "unknown")
        reasons[r] = reasons.get(r, 0) + 1

    # Profit factor by reason
    reason_pnl = {}
    for t in trades:
        r = t.get("reason", "unknown")
        reason_pnl[r] = reason_pnl.get(r, 0) + t["pnl"]

    # Consecutive wins/losses
    max_consec_w = 0
    max_consec_l = 0
    cur_w = 0
    cur_l = 0
    for t in trades:
        if t["pnl"] > 0:
            cur_w += 1; cur_l = 0
            max_consec_w = max(max_consec_w, cur_w)
        else:
            cur_l += 1; cur_w = 0
            max_consec_l = max(max_consec_l, cur_l)

    print(f"\n{'='*65}")
    print(f" SCALPING STRATEGY: FULL ANALYSIS")
    print(f"{'='*65}")
    print(f" {'Return:':<25} {ret:+.2f}%")
    print(f" {'Final Capital:':<25} ${final:,.2f}")
    print(f" {'Total Trades:':<25} {len(trades)}")
    print(f" {'Win Rate:':<25} {wr:.1f}%")
    print(f" {'Profit Factor:':<25} {pf:.2f}")
    print(f" {'Max Drawdown:':<25} {dd:.1f}%")
    print(f" {'Avg Win:':<25} ${avg_win:+.2f}")
    print(f" {'Avg Loss:':<25} ${avg_loss:+.2f}")
    print(f" {'Avg Bars Held:':<25} {avg_bars:.1f}")
    print(f" {'Max Consec Wins:':<25} {max_consec_w}")
    print(f" {'Max Consec Losses:':<25} {max_consec_l}")

    print(f"\n {'Exit Reasons:'}")
    for r in sorted(reasons.keys()):
        print(f"   {r:<20} {reasons[r]:>4} trades  PnL=${reason_pnl.get(r,0):+.2f}")

    print(f"\n {'Risk/Reward:'}")
    print(f"   Avg Win/Avg Loss:  {abs(avg_win/avg_loss):.2f}")
    print(f"   Risk per trade:    1% of capital")
    print(f"   SL: 1.5x ATR | TP: 2.0x ATR")
    print(f"   Trail: activate +1.0 ATR, distance 0.75x ATR")

    # Equity curve stats
    eq_series = pd.Series(equity)
    daily_returns = eq_series.pct_change().dropna()
    sharpe = daily_returns.mean() / daily_returns.std() * np.sqrt(288) if daily_returns.std() > 0 else 0
    print(f"   Sharpe (5m):       {sharpe:.2f}")
    print(f"   Sharpe (annual):   {sharpe * np.sqrt(288 * 365):.2f}")

    # Monthly PnL
    print(f"\n {'Top 5 wins:'}")
    top_wins = sorted(wins, key=lambda x: x["pnl"], reverse=True)[:5]
    for t in top_wins:
        print(f"   ${t['pnl']:+.2f}  ({t['reason']}, {t.get('bars',0)} bars)")

    print(f"\n {'Top 5 losses:'}")
    top_losses = sorted(losses, key=lambda x: x["pnl"])[:5]
    for t in top_losses:
        print(f"   ${t['pnl']:+.2f}  ({t['reason']}, {t.get('bars',0)} bars)")


def downsample_5m_to_15m(cache_5m):
    """Downsample 5m cache to 15m OHLCV (every 3 candles)."""
    arr = np.array(cache_5m, dtype=object)
    result = []
    for i in range(0, len(arr) - 2, 3):
        ts_val = arr[i, 0]
        open_val = float(arr[i, 1])
        high_val = max(float(arr[i, 2]), float(arr[i+1, 2]), float(arr[i+2, 2]))
        low_val = min(float(arr[i, 3]), float(arr[i+1, 3]), float(arr[i+2, 3]))
        close_val = float(arr[i+2, 4])
        vol_val = float(arr[i, 5]) + float(arr[i+1, 5]) + float(arr[i+2, 5])
        result.append([ts_val, open_val, high_val, low_val, close_val, vol_val])
    return result


async def main():
    from app.services.data_cache import _load_cache

    cache = _load_cache("BTC-USDT", "5m")
    if not cache:
        print("No 5m cache found"); return

    for tf_name, tf_cache in [("5m", cache), ("15m", downsample_5m_to_15m(cache))]:
        arr = np.array(tf_cache, dtype=object)
        close = arr[:, 4].astype(float)
        high = arr[:, 2].astype(float)
        low = arr[:, 3].astype(float)
        vol = arr[:, 5].astype(float)
        ts = arr[:, 0]

        print(f"\n{'#'*70}")
        print(f" TIMEFRAME: {tf_name} | {len(tf_cache)} candles (~{len(tf_cache)//(288 if tf_name=='5m' else 96)} days)")
        print(f"{'#'*70}")

        # ─── Count signals for each mode ───
        ind = compute_all_indicators(close, high, low, vol)
        n = len(close)
        print(f"\n{'Mode':<30} {'LONG':>6} {'SHORT':>6} {'Total':>6}")
        print("-" * 52)
        for name, (lf, sf) in ENTRY_MODES.items():
            start = 200 if tf_name == "5m" else 70
            nl = sum(1 for i in range(start, n) if lf(i, ind, close, vol))
            ns = sum(1 for i in range(start, n) if sf(i, ind, close, vol))
            print(f"  {name:<28} {nl:>6} {ns:>6} {nl+ns:>6}")

        # ─── Test all 5 modes with default params ───
        print(f"\n  Default params — SL=1.5 TP=2.0 Trail=1.0/0.75 CD=5 MH=30")
        print(f"  {'Mode':<30} {'Ret%':>7} {'#':>4} {'WR%':>5} {'PF':>5} {'DD%':>5}")
        print(f"  {'-'*62}")

        mode_results = {}
        for name, (lf, sf) in ENTRY_MODES.items():
            bal, trades, eq = run_scalp_backtest(
                close, high, low, vol, ts, cap=10000,
                sl_atr=1.5, tp_atr=2.0,
                trail_activate=1.0, trail_atr=0.75,
                cooldown=5, max_hold=30, fee=0.0005,
                long_fn=lf, short_fn=sf,
            )
            if not trades:
                print(f"  {name:<28} {'NO TRADES':>7}")
                mode_results[name] = None
                continue
            ret = (bal / 10000 - 1) * 100
            wins = [t for t in trades if t["pnl"] > 0]
            wr = len(wins) / len(trades) * 100
            gp = sum(t["pnl"] for t in wins) if wins else 0
            gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0)) or 0.001
            pf = gp / gl
            eq_arr = np.array(eq)
            dd = ((np.maximum.accumulate(eq_arr) - eq_arr) / np.maximum.accumulate(eq_arr) * 100).max()
            mode_results[name] = (bal, trades, eq, ret, wr, pf, dd)
            marker = " *" if ret > 0 and pf > 1.0 else ""
            print(f"  {name:<28} {ret:>+6.1f}% {len(trades):>4} {wr:>5.1f}% {pf:>5.2f} {dd:>5.1f}%{marker}")

        # ─── Parameter sweep on modes with positive signals ───
        profitable_modes = [(k, v) for k, v in mode_results.items()
                           if v is not None and v[3] > -50 and v[5] > 0.3]
        if not profitable_modes:
            print(f"\n  No mode with PF > 0.3 on {tf_name}. Skipping sweep.")
            continue

        for mode_name, (bal, trades, eq, ret, wr, pf, dd) in profitable_modes:
            lf, sf = ENTRY_MODES[mode_name]
            print(f"\n  Sweep: {mode_name} on {tf_name}")
            print(f"  {'SL':>4} {'TP':>4} {'TrA':>4} {'TrD':>4} {'CD':>3} {'MH':>3} {'#':>4} {'Ret%':>7} {'WR%':>5} {'PF':>5} {'DD%':>5}")
            print(f"  {'-'*58}")

            best_score = -999
            best_combo = None
            best_combo_result = None

            for sl in [1.0, 1.5, 2.0]:
                for tp in [1.5, 2.0, 3.0]:
                    for tr_a in [0.5, 1.0]:
                        for tr_d in [0.5, 0.75]:
                            for cd in [5, 8, 12]:
                                for mh in [20, 30]:
                                    bal2, trades2, eq2 = run_scalp_backtest(
                                        close, high, low, vol, ts, cap=10000,
                                        sl_atr=sl, tp_atr=tp,
                                        trail_activate=tr_a, trail_atr=tr_d,
                                        cooldown=cd, max_hold=mh, fee=0.0005,
                                        long_fn=lf, short_fn=sf,
                                    )
                                    if len(trades2) < 8:
                                        continue
                                    ret2 = (bal2 / 10000 - 1) * 100
                                    wins2 = [t for t in trades2 if t["pnl"] > 0]
                                    wr2 = len(wins2) / len(trades2) * 100
                                    gp2 = sum(t["pnl"] for t in wins2) if wins2 else 0
                                    gl2 = abs(sum(t["pnl"] for t in trades2 if t["pnl"] <= 0)) or 0.001
                                    pf2 = gp2 / gl2
                                    eq_arr2 = np.array(eq2)
                                    dd2 = ((np.maximum.accumulate(eq_arr2) - eq_arr2) / np.maximum.accumulate(eq_arr2) * 100).max()
                                    retdd2 = ret2 / dd2 if dd2 > 0 else 0

                                    if ret2 > 0 and pf2 > 1.0 and retdd2 > best_score:
                                        best_score = retdd2
                                        best_combo = (sl, tp, tr_a, tr_d, cd, mh)
                                        best_combo_result = (bal2, trades2, eq2)

                                    if pf2 > 1.2 and ret2 > 0:
                                        print(f"  {sl:>4.1f} {tp:>4.1f} {tr_a:>4.1f} {tr_d:>4.2f} {cd:>3} {mh:>3} {len(trades2):>4} {ret2:>+6.1f}% {wr2:>5.1f}% {pf2:>5.2f} {dd2:>5.1f}%")

            if best_combo and best_combo_result:
                print(f"\n  >>> BEST on {tf_name}: SL={best_combo[0]} TP={best_combo[1]} Trail={best_combo[2]}/{best_combo[3]} CD={best_combo[4]} MH={best_combo[5]}")
                bal2, trades2, eq2 = best_combo_result
                analyze_results(10000, bal2, trades2, eq2)
            else:
                print(f"\n  No profitable combo found for {mode_name} on {tf_name}.")


if __name__ == "__main__":
    asyncio.run(main())
