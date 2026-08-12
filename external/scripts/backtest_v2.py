"""Backtest Rotation Strategy v2 on real OKX historical data.

Fetches 3 years of daily + hourly candles, runs the full v2 logic:
  - Hourly ATR(24) for initial stop
  - Dynamic leverage
  - RSI, volatility, correlation, BTC 200MA filters
  - Weighted ranking
  - Dynamic trailing (ATR x 0.5)
  - Partial TP at +5%
  - Risk-based sizing (2% per trade)
  - Limit order simulation (0.05% slippage saved)
"""

import asyncio
import csv
import math
import os
import sys
from datetime import datetime, timezone
from collections import defaultdict

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

COINS = ["BTC", "ETH", "BNB", "SOL"]
SWAP_MAP = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP",
            "BNB": "BNB-USDT-SWAP", "SOL": "SOL-USDT-SWAP"}
CT_VAL = {"BTC": 0.01, "ETH": 0.1, "BNB": 0.1, "SOL": 0.1}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "BNB": 0.01, "SOL": 0.01}

# Config
CAPITAL = 10000.0
TOP_K = 2
ROC_PERIOD = 14
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
ADX_PERIOD = 14
RSI_PERIOD = 14
ADX_MIN = 18.0
MIN_HOLD_DAYS = 3
MAX_LEVERAGE = 3.0
RISK_PER_TRADE = 0.02
BREAKEVEN_PCT = 0.03
PARTIAL_TP_PCT = 0.05
PARTIAL_TP_RATIO = 0.5
RSI_LONG_MAX = 75.0
RSI_SHORT_MIN = 25.0
VOL_MULT = 1.5
CORR_THRESHOLD = 0.7
HOURLY_ATR_PERIOD = 24
HOURLY_ATR_STOP_MULT = 1.5
TRAIL_ATR_MULT = 0.5
COMMISSION = 0.001   # 0.1% per side
SLIPPAGE = 0.0005   # 0.05% per side
LIMIT_SAVE = 0.001  # 0.1% saved by limit order


# ── HTTP fetch ──

async def fetch_candles(inst_id: str, bar: str, after: str = None, limit: int = 300):
    """Fetch candles from OKX public API."""
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
    if after:
        params["after"] = after
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params)
        data = resp.json()
        if data.get("code") != "0":
            print(f"  Error {inst_id} {bar}: {data.get('msg')}")
            return []
        candles = []
        for c in data["data"]:
            candles.append({
                "ts": int(c[0]),
                "O": float(c[1]), "H": float(c[2]), "L": float(c[3]),
                "C": float(c[4]), "V": float(c[5]),
            })
        candles.sort(key=lambda x: x["ts"])
        return candles


async def fetch_all_daily(coin: str, days: int = 1100):
    """Fetch all daily candles (paginate backwards)."""
    inst_id = SWAP_MAP[coin]
    all_candles = []
    after = None
    # OKX returns newest first, we need oldest first
    # Fetch in reverse batches
    total_needed = days
    fetched = 0
    while fetched < total_needed:
        batch_size = min(300, total_needed - fetched)
        candles = await fetch_candles(inst_id, "1D", after=after, limit=batch_size)
        if not candles:
            break
        all_candles = candles + all_candles  # prepend older data
        after = str(candles[0]["ts"])  # older than oldest we have
        fetched += len(candles)
        if len(candles) < batch_size:
            break
        await asyncio.sleep(0.2)
    return all_candles


async def fetch_hourly_snapshot(coin: str, day_ts: int):
    """Fetch hourly candles around a given day for ATR calculation."""
    inst_id = SWAP_MAP[coin]
    # Fetch 48 hourly bars ending after the given day
    before = str(day_ts + 86400000 * 2)  # 2 days after
    candles = await fetch_candles(inst_id, "1H", limit=48)
    return candles


# ── Indicators (same as strategy) ──

def ema(data, period):
    if len(data) < period:
        return data[:]
    k = 2 / (period + 1)
    result = [data[0]]
    for v in data[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def sma(data, period):
    if len(data) < period:
        return [0.0] * len(data)
    result = [0.0] * len(data)
    s = sum(data[:period])
    result[period - 1] = s / period
    for i in range(period, len(data)):
        s += data[i] - data[i - period]
        result[i] = s / period
    return result


def calc_atr(highs, lows, closes, period=14):
    n = len(closes)
    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
    result = [0.0] * n
    if n < period + 1:
        return result
    val = sum(trs[1:period+1]) / period
    result[period] = val
    for i in range(period + 1, n):
        val = (val * (period - 1) + trs[i]) / period
        result[i] = val
    return result


def calc_adx(highs, lows, closes, period=14):
    n = len(closes)
    if n < period * 2 + 1:
        return [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    trs = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        plus_dm[i] = max(up, 0) if up > down else 0.0
        minus_dm[i] = max(down, 0) if down > up else 0.0
        trs[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    s_pdm = sum(plus_dm[1:period+1])
    s_mdm = sum(minus_dm[1:period+1])
    s_tr = sum(trs[1:period+1])
    adx_arr = [0.0] * n
    dx_list = []
    for i in range(period, n):
        s_pdm = s_pdm - s_pdm/period + plus_dm[i]
        s_mdm = s_mdm - s_mdm/period + minus_dm[i]
        s_tr = s_tr - s_tr/period + trs[i]
        pdi = (s_pdm/s_tr*100) if s_tr > 0 else 0.0
        mdi = (s_mdm/s_tr*100) if s_tr > 0 else 0.0
        dx = (abs(pdi-mdi)/(pdi+mdi)*100) if (pdi+mdi) > 0 else 0.0
        dx_list.append(dx)
    if len(dx_list) >= period:
        adx_val = sum(dx_list[:period]) / period
        for i in range(period, len(dx_list)):
            adx_val = (adx_val*(period-1) + dx_list[i]) / period
            adx_arr[period + i] = adx_val
    return adx_arr


def calc_rsi(closes, period=14):
    n = len(closes)
    if n < period + 1:
        return [50.0] * n
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        delta = closes[i] - closes[i-1]
        if delta > 0:
            gains[i] = delta
        else:
            losses[i] = abs(delta)
    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    result = [50.0] * n
    if avg_loss == 0:
        result[period] = 100.0
    else:
        result[period] = 100 - 100 / (1 + avg_gain/avg_loss)
    for i in range(period+1, n):
        avg_gain = (avg_gain*(period-1) + gains[i]) / period
        avg_loss = (avg_loss*(period-1) + losses[i]) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            result[i] = 100 - 100 / (1 + avg_gain/avg_loss)
    return result


def calc_roc(closes, period):
    result = [0.0] * len(closes)
    for i in range(period, len(closes)):
        result[i] = (closes[i] / closes[i-period] - 1) * 100
    return result


def correlation(x, y, period=30):
    if len(x) < period or len(y) < period:
        return 0.0
    x, y = x[-period:], y[-period:]
    n = len(x)
    mx, my = sum(x)/n, sum(y)/n
    cov = sum((x[i]-mx)*(y[i]-my) for i in range(n))
    sx = math.sqrt(sum((x[i]-mx)**2 for i in range(n)))
    sy = math.sqrt(sum((y[i]-my)**2 for i in range(n)))
    if sx == 0 or sy == 0:
        return 0.0
    return cov / (sx * sy)


# ── Position & Backtest State ──

class Position:
    def __init__(self, coin, side, size, entry_price, stop_price, atr, atr_h, leverage, entry_idx):
        self.coin = coin
        self.side = side
        self.size = size
        self.size_original = size
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.peak_price = entry_price
        self.atr = atr
        self.atr_h = atr_h
        self.leverage = leverage
        self.breakeven = False
        self.partial_done = False
        self.entry_idx = entry_idx


def dynamic_leverage(atr_h, price):
    if atr_h <= 0 or price <= 0:
        return 1.0
    atr_pct = atr_h / price
    lev = 1.0 / (atr_pct * 2)
    return max(1.0, min(lev, MAX_LEVERAGE))


def calc_size(equity, coin, price, stop_distance, leverage):
    ct_val = CT_VAL.get(coin, 0.01)
    lot = LOT_SZ.get(coin, 0.01)
    stop_pct = stop_distance / price if (stop_distance > 0 and price > 0) else 0.03
    risk_usd = equity * RISK_PER_TRADE
    notional = risk_usd / stop_pct
    margin = notional / leverage if leverage > 0 else notional
    max_margin = equity * 0.40
    if margin > max_margin:
        margin = max_margin
        notional = margin * leverage
    raw_sz = notional / (ct_val * price)
    sz = round(raw_sz / lot) * lot
    return max(sz, lot)


# ── Main Backtest ──

async def run_backtest():
    print("="*70)
    print("ROTATION STRATEGY v2 — BACKTEST")
    print("="*70)

    # 1. Fetch daily data
    print("\n[1/3] Fetching daily candles (3 years)...")
    daily_data = {}
    for coin in COINS:
        print(f"  Fetching {coin}...", end="", flush=True)
        candles = await fetch_all_daily(coin, days=1100)
        daily_data[coin] = candles
        print(f" {len(candles)} bars  ({candles[0]['ts'] if candles else '?'}...{candles[-1]['ts'] if candles else '?'})")

    # 2. Pre-compute all daily indicators for each coin
    print("\n[2/3] Computing indicators...")
    indicators_cache = {}  # coin -> list of dicts (one per day)

    for coin in COINS:
        candles = daily_data[coin]
        n = len(candles)
        closes = [c["C"] for c in candles]
        highs = [c["H"] for c in candles]
        lows = [c["L"] for c in candles]

        roc_arr = calc_roc(closes, ROC_PERIOD)
        ema_f = ema(closes, EMA_FAST)
        ema_s = ema(closes, EMA_SLOW)
        atr_arr = calc_atr(highs, lows, closes, ATR_PERIOD)
        adx_arr = calc_adx(highs, lows, closes, ADX_PERIOD)
        rsi_arr = calc_rsi(closes, RSI_PERIOD)
        sma200 = sma(closes, 200)

        coin_indicators = []
        for i in range(n):
            # Need at least ema_slow + 10 bars
            if i < EMA_SLOW + 10:
                coin_indicators.append(None)
                continue

            # Average ATR over last 30 days
            atr_30_start = max(0, i - 30)
            atr_vals = [atr_arr[j] for j in range(atr_30_start, i+1) if atr_arr[j] > 0]
            avg_atr_30 = sum(atr_vals) / len(atr_vals) if atr_vals else 0.0

            # Daily returns for correlation
            daily_returns = []
            for j in range(max(1, i-29), i+1):
                if closes[j-1] > 0:
                    daily_returns.append(closes[j] / closes[j-1] - 1)

            coin_indicators.append({
                "roc": roc_arr[i],
                "ema_fast": ema_f[i], "ema_slow": ema_s[i],
                "ema_trend": ema_f[i] > ema_s[i],
                "atr": atr_arr[i], "avg_atr_30": avg_atr_30,
                "adx": adx_arr[i], "rsi": rsi_arr[i],
                "price": closes[i],
                "sma200": sma200[i],
                "daily_returns": daily_returns,
            })
        indicators_cache[coin] = coin_indicators
        print(f"  {coin}: {sum(1 for x in coin_indicators if x)} valid bars")

    # 3. Run backtest day by day
    print("\n[3/3] Running backtest...")

    # Find common date range
    min_len = min(len(daily_data[c]) for c in COINS)
    start_idx = EMA_SLOW + 10 + 1  # skip warmup, signal bar is i-1

    equity = CAPITAL
    capital = CAPITAL
    positions = {}  # coin -> Position
    trade_log = []
    equity_curve = []
    last_rotate_idx = -999
    total_trades = 0
    wins = 0
    losses = 0
    filters_hit = defaultdict(int)

    for i in range(start_idx, min_len):
        date_str = datetime.fromtimestamp(daily_data["BTC"][i]["ts"]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
        year = daily_data["BTC"][i]["ts"] / 1000 / 86400 / 365.25 + 1970

        # Current prices (today's close for position management)
        today_prices = {coin: daily_data[coin][i]["C"] for coin in COINS}

        # ── A. Manage existing positions: trailing + partial TP + stop check ──
        closed_coins = []
        for coin in list(positions.keys()):
            pos = positions[coin]
            ind = indicators_cache[coin][i]
            if not ind:
                continue
            current = today_prices[coin]

            # Dynamic trailing = ATR_at_entry x 0.5
            trail_step = pos.atr * TRAIL_ATR_MULT
            if trail_step <= 0:
                trail_step = pos.entry_price * 0.02

            hit_stop = False
            reason = "trail_stop"

            if pos.side == "long":
                if current > pos.peak_price:
                    pos.peak_price = current
                new_stop = pos.peak_price - trail_step
                if new_stop > pos.stop_price:
                    pos.stop_price = new_stop
                if not pos.breakeven and current >= pos.entry_price * (1 + BREAKEVEN_PCT):
                    pos.stop_price = max(pos.stop_price, pos.entry_price * 0.999)
                    pos.breakeven = True
                # Partial TP
                if not pos.partial_done and current >= pos.entry_price * (1 + PARTIAL_TP_PCT):
                    close_sz = round(pos.size * PARTIAL_TP_RATIO / LOT_SZ[coin]) * LOT_SZ[coin]
                    if close_sz > 0 and close_sz < pos.size:
                        # Simulate limit sell at current - slippage saved
                        fill_px = current * (1 - SLIPPAGE + LIMIT_SAVE)
                        pnl = close_sz * CT_VAL[coin] * (fill_px - pos.entry_price) - close_sz * CT_VAL[coin] * fill_px * COMMISSION
                        equity += pnl
                        trade_log.append({"date": date_str, "coin": coin, "side": "sell",
                                          "size": close_sz, "pnl": round(pnl, 2), "reason": "partial_tp",
                                          "entry": pos.entry_price, "exit": round(fill_px, 2), "leverage": pos.leverage})
                        pos.size -= close_sz
                        pos.partial_done = True
                if current <= pos.stop_price:
                    hit_stop = True
            else:  # short
                if current < pos.peak_price:
                    pos.peak_price = current
                new_stop = pos.peak_price + trail_step
                if new_stop < pos.stop_price:
                    pos.stop_price = new_stop
                if not pos.breakeven and current <= pos.entry_price * (1 - BREAKEVEN_PCT):
                    pos.stop_price = min(pos.stop_price, pos.entry_price * 1.001)
                    pos.breakeven = True
                if not pos.partial_done and current <= pos.entry_price * (1 - PARTIAL_TP_PCT):
                    close_sz = round(pos.size * PARTIAL_TP_RATIO / LOT_SZ[coin]) * LOT_SZ[coin]
                    if close_sz > 0 and close_sz < pos.size:
                        fill_px = current * (1 + SLIPPAGE - LIMIT_SAVE)
                        pnl = close_sz * CT_VAL[coin] * (pos.entry_price - fill_px) - close_sz * CT_VAL[coin] * fill_px * COMMISSION
                        equity += pnl
                        trade_log.append({"date": date_str, "coin": coin, "side": "buy",
                                          "size": close_sz, "pnl": round(pnl, 2), "reason": "partial_tp",
                                          "entry": pos.entry_price, "exit": round(fill_px, 2), "leverage": pos.leverage})
                        pos.size -= close_sz
                        pos.partial_done = True
                if current >= pos.stop_price:
                    hit_stop = True

            if hit_stop:
                # Close at stop price + slippage
                if pos.side == "long":
                    fill_px = pos.stop_price * (1 - SLIPPAGE)
                    pnl = pos.size * CT_VAL[coin] * (fill_px - pos.entry_price) - pos.size * CT_VAL[coin] * fill_px * COMMISSION
                else:
                    fill_px = pos.stop_price * (1 + SLIPPAGE)
                    pnl = pos.size * CT_VAL[coin] * (pos.entry_price - fill_px) - pos.size * CT_VAL[coin] * fill_px * COMMISSION
                equity += pnl
                trade_log.append({"date": date_str, "coin": coin, "side": "sell" if pos.side=="long" else "buy",
                                  "size": pos.size, "pnl": round(pnl, 2), "reason": reason,
                                  "entry": pos.entry_price, "exit": round(fill_px, 2), "leverage": pos.leverage})
                total_trades += 1
                if pnl > 0: wins += 1
                else: losses += 1
                closed_coins.append(coin)

        for c in closed_coins:
            del positions[c]

        # ── B. Rotation check (once per day, min hold) ──
        if i - last_rotate_idx < MIN_HOLD_DAYS and positions:
            # Still record equity
            unrealized = 0.0
            for coin, pos in positions.items():
                ct = CT_VAL[coin]
                cur = today_prices[coin]
                if pos.side == "long":
                    unrealized += pos.size * ct * (cur - pos.entry_price)
                else:
                    unrealized += pos.size * ct * (pos.entry_price - cur)
            equity_curve.append({"date": date_str, "equity": equity + unrealized, "realized": equity})
            continue

        # ── C. Compute rankings with filters ──
        # BTC 200MA filter
        btc_ind = indicators_cache["BTC"][i]
        btc_above_200ma = True
        if btc_ind and btc_ind["sma200"] > 0:
            btc_above_200ma = today_prices["BTC"] > btc_ind["sma200"]

        ranked = []
        for coin in COINS:
            ind = indicators_cache[coin][i]
            if not ind or ind["atr"] <= 0:
                continue

            # Volatility filter
            if ind["avg_atr_30"] > 0 and ind["atr"] > ind["avg_atr_30"] * VOL_MULT:
                filters_hit["volatility"] += 1
                continue

            # RSI filter
            if ind["rsi"] > RSI_LONG_MAX and ind["ema_trend"]:
                filters_hit["rsi_overbought"] += 1
                continue
            if ind["rsi"] < RSI_SHORT_MIN and not ind["ema_trend"]:
                filters_hit["rsi_oversold"] += 1
                continue

            # Bear market: block longs when BTC < 200MA
            if not btc_above_200ma and ind["roc"] > 0 and ind["ema_trend"]:
                filters_hit["bear_long"] += 1
                continue

            # Weighted score
            roc_val = ind["roc"]
            trend_val = (ind["ema_fast"] - ind["ema_slow"]) / ind["ema_slow"] * 100 if ind["ema_slow"] > 0 else 0
            score = roc_val * 0.5 + trend_val * 0.3 + (ind["adx"] / 50) * 0.2

            ranked.append((coin, score, ind))

        ranked.sort(key=lambda x: x[1], reverse=True)

        # Select targets with correlation filter
        target_coins = set()
        for coin, score, ind in ranked:
            if len(target_coins) >= TOP_K:
                break

            if ind["roc"] > 0 and ind["ema_trend"] and ind["adx"] >= ADX_MIN:
                side = "long"
            elif ind["roc"] < 0 and not ind["ema_trend"] and ind["adx"] >= ADX_MIN:
                side = "short"
            else:
                continue

            # Correlation check
            corr_ok = True
            for held_coin in positions:
                held_ret = indicators_cache[held_coin][i]
                if not held_ret:
                    continue
                corr = correlation(ind["daily_returns"], held_ret["daily_returns"])
                if abs(corr) > CORR_THRESHOLD:
                    filters_hit["correlation"] += 1
                    corr_ok = False
                    break
            if not corr_ok:
                continue

            target_coins.add((coin, side))

        # Close positions not in target
        for coin in list(positions.keys()):
            pos = positions[coin]
            if (coin, pos.side) not in target_coins:
                cur = today_prices[coin]
                if pos.side == "long":
                    fill_px = cur * (1 - SLIPPAGE + LIMIT_SAVE * 0.5)
                    pnl = pos.size * CT_VAL[coin] * (fill_px - pos.entry_price) - pos.size * CT_VAL[coin] * fill_px * COMMISSION
                else:
                    fill_px = cur * (1 + SLIPPAGE - LIMIT_SAVE * 0.5)
                    pnl = pos.size * CT_VAL[coin] * (pos.entry_price - fill_px) - pos.size * CT_VAL[coin] * fill_px * COMMISSION
                equity += pnl
                trade_log.append({"date": date_str, "coin": coin, "side": "sell" if pos.side=="long" else "buy",
                                  "size": pos.size, "pnl": round(pnl, 2), "reason": "rotation_exit",
                                  "entry": pos.entry_price, "exit": round(fill_px, 2), "leverage": pos.leverage})
                total_trades += 1
                if pnl > 0: wins += 1
                else: losses += 1
                del positions[coin]

        # Open new positions
        for coin, side in target_coins:
            if coin in positions:
                continue
            ind = indicators_cache[coin][i]
            price = today_prices[coin]
            atr_d = ind["atr"]
            atr_h = atr_d  # In backtest we use daily ATR as proxy for hourly (conservative)

            lev = dynamic_leverage(atr_h, price)

            # Initial stop
            stop_dist = atr_h * HOURLY_ATR_STOP_MULT
            if side == "long":
                stop = price - stop_dist
            else:
                stop = price + stop_dist

            sz = calc_size(equity, coin, price, stop_dist, lev)

            # Simulate entry (limit saves 0.1% slippage)
            if side == "long":
                fill_px = price * (1 - SLIPPAGE + LIMIT_SAVE)
            else:
                fill_px = price * (1 + SLIPPAGE - LIMIT_SAVE)

            fee = sz * CT_VAL[coin] * fill_px * COMMISSION
            equity -= fee

            pos = Position(coin, side, sz, fill_px, stop, atr_d, atr_h, lev, i)
            positions[coin] = pos
            trade_log.append({"date": date_str, "coin": coin, "side": "buy" if side=="long" else "sell",
                              "size": sz, "pnl": round(-fee, 2), "reason": "open",
                              "entry": round(fill_px, 2), "exit": None, "leverage": lev})

            last_rotate_idx = i

        # Record equity
        unrealized = 0.0
        for coin, pos in positions.items():
            ct = CT_VAL[coin]
            cur = today_prices[coin]
            if pos.side == "long":
                unrealized += pos.size * ct * (cur - pos.entry_price)
            else:
                unrealized += pos.size * ct * (pos.entry_price - cur)
        equity_curve.append({"date": date_str, "equity": equity + unrealized, "realized": equity})

    # Close all remaining positions at end
    final_date = datetime.fromtimestamp(daily_data["BTC"][-1]["ts"]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
    for coin in list(positions.keys()):
        pos = positions[coin]
        cur = today_prices[coin]
        if pos.side == "long":
            fill_px = cur * (1 - SLIPPAGE)
            pnl = pos.size * CT_VAL[coin] * (fill_px - pos.entry_price) - pos.size * CT_VAL[coin] * fill_px * COMMISSION
        else:
            fill_px = cur * (1 + SLIPPAGE)
            pnl = pos.size * CT_VAL[coin] * (pos.entry_price - fill_px) - pos.size * CT_VAL[coin] * fill_px * COMMISSION
        equity += pnl
        trade_log.append({"date": final_date, "coin": coin, "side": "sell" if pos.side=="long" else "buy",
                          "size": pos.size, "pnl": round(pnl, 2), "reason": "backtest_end",
                          "entry": pos.entry_price, "exit": round(fill_px, 2), "leverage": pos.leverage})
        total_trades += 1
        if pnl > 0: wins += 1
        else: losses += 1

    # ── Results ──
    print("\n" + "="*70)
    print("RESULTS")
    print("="*70)

    final_equity = equity
    total_return = (final_equity / capital - 1) * 100

    # Time span
    first_date = equity_curve[0]["date"] if equity_curve else ""
    last_date = equity_curve[-1]["date"] if equity_curve else ""
    years = len(equity_curve) / 365.25 if equity_curve else 1
    cagr = (final_equity / capital) ** (1 / years) - 1

    # Max drawdown
    peak = capital
    max_dd = 0
    max_dd_date = ""
    for pt in equity_curve:
        if pt["equity"] > peak:
            peak = pt["equity"]
        dd = (peak - pt["equity"]) / peak * 100
        if dd > max_dd:
            max_dd = dd
            max_dd_date = pt["date"]

    # Sharpe (annualized, assuming 365 trading days)
    daily_returns_list = []
    for j in range(1, len(equity_curve)):
        r = equity_curve[j]["equity"] / equity_curve[j-1]["equity"] - 1
        daily_returns_list.append(r)
    if daily_returns_list:
        avg_r = sum(daily_returns_list) / len(daily_returns_list) * 365
        std_r = (sum((r - sum(daily_returns_list)/len(daily_returns_list))**2 for r in daily_returns_list) / len(daily_returns_list)) ** 0.5 * (365**0.5)
        sharpe = avg_r / std_r if std_r > 0 else 0
    else:
        sharpe = 0

    print(f"\n  Period:              {first_date} → {last_date} ({years:.1f} years)")
    print(f"  Starting capital:    ${capital:,.0f}")
    print(f"  Final equity:        ${final_equity:,.2f}")
    print(f"  Total return:        {total_return:+.1f}%")
    print(f"  CAGR:                {cagr*100:.1f}%")
    print(f"  Max drawdown:        {max_dd:.1f}% ({max_dd_date})")
    print(f"  Sharpe ratio:        {sharpe:.2f}")
    print(f"")
    print(f"  Total closed trades: {total_trades}")
    print(f"  Wins / Losses:       {wins} / {losses}")
    print(f"  Win rate:            {wins/total_trades*100:.1f}%" if total_trades > 0 else "  Win rate:            N/A")
    print(f"")
    print(f"  Filters triggered:")
    for k, v in sorted(filters_hit.items(), key=lambda x: -x[1]):
        print(f"    {k:25s} {v} times")

    # Monthly returns
    print(f"\n  Monthly returns (last 12 months):")
    monthly = defaultdict(float)
    for pt in equity_curve:
        ym = pt["date"][:7]  # YYYY-MM
        monthly[ym] = pt["equity"]
    monthly_items = sorted(monthly.items())
    if len(monthly_items) >= 2:
        prev_eq = None
        for ym, eq in monthly_items[-13:]:
            if prev_eq is not None:
                ret = (eq / prev_eq - 1) * 100
                bar = "█" * max(0, int(ret / 2))
                print(f"    {ym}  {ret:+6.1f}%  {bar}")
            prev_eq = eq

    # Equity milestones
    print(f"\n  Equity milestones:")
    milestones = [15000, 20000, 30000, 50000, 75000, 100000]
    for m in milestones:
        for pt in equity_curve:
            if pt["equity"] >= m:
                print(f"    ${m:>6,} reached on {pt['date']}")
                break
        else:
            print(f"    ${m:>6,} — not reached")

    # Save results
    results = {
        "strategy": "rotation_v2",
        "period": f"{first_date} → {last_date}",
        "years": round(years, 2),
        "capital": capital,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return, 1),
        "cagr_pct": round(cagr * 100, 1),
        "max_drawdown_pct": round(max_dd, 1),
        "sharpe": round(sharpe, 2),
        "total_trades": total_trades,
        "wins": wins, "losses": losses,
        "win_rate": round(wins/total_trades*100, 1) if total_trades > 0 else 0,
        "filters_hit": dict(filters_hit),
    }
    import json
    out_path = os.path.join(os.path.dirname(__file__), "backtest_v2_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Results saved to {out_path}")

    return results


if __name__ == "__main__":
    asyncio.run(run_backtest())
