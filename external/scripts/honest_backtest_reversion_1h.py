#!/usr/bin/env python3
"""Honest 1H mean-reversion backtest (contrarian — opposite of momentum).

Rules (causal, no look-ahead):
  1. Signal on bar T CLOSE using only data up to T
  2. Entry at bar T+1 OPEN (long when oversold, short when overbought)
  3. Stops checked against bar HIGH/LOW first (pessimistic)
  4. Commission 0.10% + slippage 0.05% per side
  5. Funding applied at 8h boundaries on open positions
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import httpx

COINS = ["BTC", "ETH", "BNB", "SOL"]
CT_VAL = {"BTC": 0.01, "ETH": 0.1, "BNB": 0.1, "SOL": 0.1}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "BNB": 0.01, "SOL": 0.01}

COMMISSION = 0.001    # 0.10% taker
SLIPPAGE = 0.0005     # 0.05%
BAR = "1H"
BAR_MS = 60 * 60 * 1000
BARS_PER_DAY = 24
SCALP_DAYS = 400      # ~13 months for first run
CACHE_PATH = os.path.join(os.path.dirname(__file__), "reversion_1h_cache.json")
OUT_PATH = os.path.join(os.path.dirname(__file__), "reversion_1h_results.json")
FUNDING_CACHE_PATH = os.path.join(os.path.dirname(__file__), "reversion_1h_funding_cache.json")

# Perp funding: OKX charges every 8h (00:00 / 08:00 / 16:00 UTC)
FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000


@dataclass
class StratConfig:
    name: str
    capital: float = 10000.0
    top_k: int = 2                   # max concurrent positions
    rsi_period: int = 14
    rsi_oversold: float = 30.0       # long when RSI below this
    rsi_overbought: float = 70.0     # short when RSI above this
    bb_period: int = 20              # Bollinger lookback
    bb_std: float = 2.0              # Bollinger std multiplier
    ema_trend: int = 200             # trend filter EMA (off if 0)
    min_hold_bars: int = 2           # cooldown between rotations
    max_hold_bars: int = 24          # time exit = 24h
    max_leverage: float = 1.0
    risk_per_trade: float = 0.01
    tp_atr_mult: float = 2.0         # take-profit = ATR * this
    sl_atr_mult: float = 1.5         # stop-loss = ATR * this
    vol_mult: float = 1.5
    corr_threshold: float = 0.7
    max_margin_pct: float = 0.5
    allow_short: bool = True
    tp_bb_mid: bool = False          # exit at Bollinger mid (mean) instead of ATR TP


REV_BASE = StratConfig(
    name="Mean-Reversion 1H (base)",
    top_k=2, rsi_oversold=30.0, rsi_overbought=70.0,
    bb_period=20, bb_std=2.0, ema_trend=0,
    min_hold_bars=2, max_hold_bars=24,
    max_leverage=1.0, risk_per_trade=0.01,
    tp_atr_mult=2.0, sl_atr_mult=1.5,
    max_margin_pct=0.5, allow_short=True,
)

REV_WIDE = StratConfig(
    name="Mean-Reversion 1H (trend filter + wider TP)",
    top_k=2, rsi_oversold=30.0, rsi_overbought=70.0,
    bb_period=20, bb_std=2.0, ema_trend=200,
    min_hold_bars=2, max_hold_bars=36,
    max_leverage=1.0, risk_per_trade=0.01,
    tp_atr_mult=3.0, sl_atr_mult=2.0,
    max_margin_pct=0.5, allow_short=True,
)

REV_MEAN = StratConfig(
    name="Mean-Reversion 1H (exit at Bollinger mid)",
    top_k=2, rsi_oversold=30.0, rsi_overbought=70.0,
    bb_period=20, bb_std=2.0, ema_trend=0,
    min_hold_bars=2, max_hold_bars=24,
    max_leverage=1.0, risk_per_trade=0.01,
    tp_atr_mult=0.0, sl_atr_mult=1.5,
    max_margin_pct=0.5, allow_short=True, tp_bb_mid=True,
)


# ── Indicators (causal: value at i uses only data[:i+1]) ──

def ema_series(data, period):
    if not data:
        return []
    k = 2 / (period + 1)
    out = [data[0]]
    for v in data[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr_series(highs, lows, closes, period=14):
    n = len(closes)
    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    out = [0.0] * n
    if n < period + 1:
        return out
    val = sum(trs[1:period + 1]) / period
    out[period] = val
    for i in range(period + 1, n):
        val = (val * (period - 1) + trs[i]) / period
        out[i] = val
    return out


def adx_series(highs, lows, closes, period=14):
    n = len(closes)
    out = [0.0] * n
    if n < period * 2 + 1:
        return out
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    trs = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = max(up, 0) if up > down else 0.0
        minus_dm[i] = max(down, 0) if down > up else 0.0
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    s_pdm = sum(plus_dm[1:period + 1])
    s_mdm = sum(minus_dm[1:period + 1])
    s_tr = sum(trs[1:period + 1])
    dx_list = []
    for i in range(period, n):
        s_pdm = s_pdm - s_pdm / period + plus_dm[i]
        s_mdm = s_mdm - s_mdm / period + minus_dm[i]
        s_tr = s_tr - s_tr / period + trs[i]
        pdi = (s_pdm / s_tr * 100) if s_tr > 0 else 0.0
        mdi = (s_mdm / s_tr * 100) if s_tr > 0 else 0.0
        dx = (abs(pdi - mdi) / (pdi + mdi) * 100) if (pdi + mdi) > 0 else 0.0
        dx_list.append(dx)
    if len(dx_list) >= period:
        adx_val = sum(dx_list[:period]) / period
        for i in range(period, len(dx_list)):
            adx_val = (adx_val * (period - 1) + dx_list[i]) / period
            out[period + i] = adx_val
    return out


def rsi_series(closes, period=14):
    n = len(closes)
    out = [50.0] * n
    if n < period + 1:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains[i] = d
        else:
            losses[i] = abs(d)
    avg_g = sum(gains[1:period + 1]) / period
    avg_l = sum(losses[1:period + 1]) / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, n):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def roc_series(closes, period):
    out = [0.0] * len(closes)
    for i in range(period, len(closes)):
        if closes[i - period] > 0:
            out[i] = (closes[i] / closes[i - period] - 1) * 100
    return out


def sma_series(data, period):
    n = len(data)
    out = [0.0] * n
    if n < period:
        return out
    s = sum(data[:period])
    out[period - 1] = s / period
    for i in range(period, n):
        s += data[i] - data[i - period]
        out[i] = s / period
    return out


def correlation(x, y, period=30):
    if len(x) < period or len(y) < period:
        return 0.0
    x, y = x[-period:], y[-period:]
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    sx = math.sqrt(sum((x[i] - mx) ** 2 for i in range(n)))
    sy = math.sqrt(sum((y[i] - my) ** 2 for i in range(n)))
    if sx == 0 or sy == 0:
        return 0.0
    return cov / (sx * sy)


# ── Data (OKX public API — same exchange as live bots) ──

async def fetch_candles_okx(inst_id: str, after: str | None = None, limit: int = 300, bar: str = BAR):
    url = "https://www.okx.com/api/v5/market/history-candles"
    params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
    if after:
        params["after"] = after
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(4):
            try:
                resp = await client.get(url, params=params)
                data = resp.json()
                if data.get("code") != "0":
                    # fallback to recent candles endpoint
                    url2 = "https://www.okx.com/api/v5/market/candles"
                    resp = await client.get(url2, params=params)
                    data = resp.json()
                if data.get("code") != "0":
                    print(f"  OKX error {inst_id}: {data.get('msg')}", flush=True)
                    return []
                out = []
                for c in data.get("data", []):
                    out.append({
                        "ts": int(c[0]),
                        "dt": datetime.fromtimestamp(int(c[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        "O": float(c[1]), "H": float(c[2]), "L": float(c[3]), "C": float(c[4]),
                        "V": float(c[5]),
                    })
                out.sort(key=lambda x: x["ts"])
                return out
            except Exception as e:
                print(f"  retry {inst_id}: {e}", flush=True)
                await asyncio.sleep(1.2 * (attempt + 1))
    return []


async def fetch_bars(coin: str, days_back: int = SCALP_DAYS):
    inst_id = f"{coin}-USDT-SWAP"
    all_candles = []
    after = None
    target = days_back * 96  # 15m bars per day = 96
    while len(all_candles) < target:
        batch = await fetch_candles_okx(inst_id, after=after, limit=300)
        if not batch:
            break
        # history-candles returns newest-first batches via `after` = older than ts
        all_candles = batch + all_candles
        after = str(batch[0]["ts"])
        if len(batch) < 100:
            break
        await asyncio.sleep(0.2)

    # de-dupe by ts
    uniq = {}
    for c in all_candles:
        uniq[c["ts"]] = c
    all_candles = [uniq[k] for k in sorted(uniq)]

    # drop the last (possibly unclosed) bar if it isn't on a bar boundary
    if all_candles and all_candles[-1]["ts"] % BAR_MS != 0:
        all_candles = all_candles[:-1]
    # keep last target
    if len(all_candles) > target:
        all_candles = all_candles[-target:]
    return all_candles


async def fetch_funding_okx(inst_id: str, after: str | None = None, limit: int = 100):
    url = "https://www.okx.com/api/v5/public/funding-rate-history"
    params = {"instId": inst_id, "limit": str(limit)}
    if after:
        params["after"] = after
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(url, params=params)
            data = resp.json()
            if data.get("code") != "0":
                return [], False
            out = []
            for c in data.get("data", []):
                out.append({
                    "ts": int(c["fundingTime"]),
                    "rate": float(c.get("fundingRate", 0.0)) or 0.0,
                })
            out.sort(key=lambda x: x["ts"])
            return out, True
        except Exception:
            return [], False


async def fetch_funding_all(coin: str, days_back: int = SCALP_DAYS):
    inst_id = f"{coin}-USDT-SWAP"
    all_rates = []
    after = None
    limit_ts = int((datetime.now(timezone.utc).timestamp() - days_back * 86400) * 1000)
    while True:
        batch, ok = await fetch_funding_okx(inst_id, after=after, limit=100)
        if not batch or not ok:
            break
        all_rates = batch + all_rates
        after = str(batch[0]["ts"])
        if batch[0]["ts"] <= limit_ts or len(batch) < 100:
            break
        await asyncio.sleep(0.2)

    uniq = {}
    for r in all_rates:
        uniq[r["ts"]] = r["rate"]
    return {ts: rate for ts, rate in sorted(uniq.items())}


async def load_funding(force_refresh: bool = False):
    if not force_refresh and os.path.exists(FUNDING_CACHE_PATH):
        with open(FUNDING_CACHE_PATH) as f:
            cached = json.load(f)
        age_h = (time.time() - cached.get("fetched_at", 0)) / 3600
        if age_h < 48 and all(c in cached.get("data", {}) for c in COINS):
            print(f"  Using funding cache ({age_h:.1f}h old)", flush=True)
            return cached["data"]
    print(f"  Fetching OKX SWAP funding history (~{SCALP_DAYS}d)...", flush=True)
    funding = {}
    for coin in COINS:
        rates = await fetch_funding_all(coin)
        if not rates:
            rates = {}
            print(f"    WARN: no funding data for {coin}", flush=True)
        funding[coin] = rates
        print(f"    {coin}: {len(rates)} funding records", flush=True)
        await asyncio.sleep(0.25)
    with open(FUNDING_CACHE_PATH, "w") as f:
        json.dump({"fetched_at": time.time(), "data": funding}, f)
    return funding


async def load_data(force_refresh: bool = False):
    if not force_refresh and os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cached = json.load(f)
        age_h = (time.time() - cached.get("fetched_at", 0)) / 3600
        if age_h < 24 and all(c in cached.get("data", {}) for c in COINS):
            # sanity: each coin must have bars
            if all(len(cached["data"][c]) > 500 for c in COINS):
                print(f"  Using cache ({age_h:.1f}h old)", flush=True)
                return cached["data"]

    print(f"  Fetching OKX SWAP {BAR} candles (~{SCALP_DAYS}d)...", flush=True)
    data = {}
    for coin in COINS:
        bars = await fetch_bars(coin)
        if not bars:
            raise RuntimeError(f"Failed to fetch candles for {coin}")
        data[coin] = bars
        print(f"    {coin}: {len(bars)} bars  {bars[0]['dt']} → {bars[-1]['dt']}", flush=True)
        await asyncio.sleep(0.25)

    with open(CACHE_PATH, "w") as f:
        json.dump({"fetched_at": time.time(), "data": data}, f)
    return data


# ── Engine ──

def dynamic_leverage(atr, price, max_lev):
    if atr <= 0 or price <= 0:
        return 1.0
    lev = 1.0 / ((atr / price) * 2)
    return max(1.0, min(lev, max_lev))


def calc_size(equity, coin, price, stop_dist, leverage, risk_pct, max_margin_pct):
    ct = CT_VAL[coin]
    lot = LOT_SZ[coin]
    stop_pct = stop_dist / price if stop_dist > 0 and price > 0 else 0.03
    risk_usd = equity * risk_pct
    notional = risk_usd / stop_pct
    margin = notional / leverage if leverage > 0 else notional
    max_margin = equity * max_margin_pct
    if margin > max_margin:
        margin = max_margin
        notional = margin * leverage
    raw = notional / (ct * price)
    sz = math.floor(raw / lot + 1e-12) * lot
    return max(sz, lot)


def build_coin_frame(candles, cfg: StratConfig):
    closes = [c["C"] for c in candles]
    highs = [c["H"] for c in candles]
    lows = [c["L"] for c in candles]
    frame = {
        "candles": candles,
        "closes": closes,
        "atr": atr_series(highs, lows, closes, 14),
        "rsi": rsi_series(closes, cfg.rsi_period),
        "bb_mid": sma_series(closes, cfg.bb_period),
        "bb_up": None,
        "bb_lo": None,
    }
    # Bollinger bands (causal: std of last bb_period closes ending at i)
    up = [0.0] * len(closes)
    lo = [0.0] * len(closes)
    for i in range(cfg.bb_period - 1, len(closes)):
        window = closes[i - cfg.bb_period + 1:i + 1]
        m = sum(window) / cfg.bb_period
        var = sum((v - m) ** 2 for v in window) / cfg.bb_period
        sd = math.sqrt(var)
        up[i] = m + cfg.bb_std * sd
        lo[i] = m - cfg.bb_std * sd
    frame["bb_up"] = up
    frame["bb_lo"] = lo
    if cfg.ema_trend > 0:
        frame["ema_t"] = ema_series(closes, cfg.ema_trend)
    else:
        frame["ema_t"] = None
    return frame


def run_strategy(bar_data: dict, cfg: StratConfig, funding: dict | None = None,
                 window: tuple[str, str] | None = None,
                 return_trades: bool = False) -> dict:
    """Run mean-reversion. Signal on bar T close → entry at T+1 open.
    Exit: TP / SL (ATR multiples) / time stop (max_hold_bars).
    """
    coin_data = {}
    for coin in COINS:
        bars = bar_data.get(coin, [])
        if len(bars) < 250:
            raise RuntimeError(f"{coin}: not enough bars ({len(bars)})")
        coin_data[coin] = build_coin_frame(bars, cfg)

    # Align on common bars
    common = None
    for coin, cd in coin_data.items():
        bars = {c["dt"] for c in cd["candles"]}
        common = bars if common is None else (common & bars)
    all_dates = sorted(common)

    date_idx = {
        coin: {c["dt"]: i for i, c in enumerate(cd["candles"])}
        for coin, cd in coin_data.items()
    }

    equity = cfg.capital
    positions = {}
    trades = []
    equity_curve = []
    filters_hit = defaultdict(int)
    last_rotate = -10**9
    start_i = max(cfg.bb_period + 10, cfg.rsi_period + 10, cfg.ema_trend + 10 if cfg.ema_trend > 0 else 0)
    total_funding = 0.0

    if window is not None:
        w_start, w_end = window
        idxs = [j for j, d in enumerate(all_dates) if w_start <= d <= w_end]
        if len(idxs) < 30:
            raise RuntimeError(f"window {w_start}→{w_end}: too few common dates ({len(idxs)})")
        i_lo, i_hi = idxs[0], idxs[-1]
    else:
        i_lo, i_hi = start_i, len(all_dates) - 1
    if i_lo < start_i:
        i_lo = start_i

    for i in range(i_lo, i_hi + 1):
        date = all_dates[i]
        sig_date = all_dates[i - 1]

        # Mark-to-market prices for equity curve (this bar close)
        mtm = {}
        for coin in COINS:
            ci = date_idx[coin][date]
            mtm[coin] = coin_data[coin]["candles"][ci]["C"]

        # ── 0. Funding accrual at 8h boundaries (00:00/08:00/16:00 UTC) ──
        if funding:
            for coin in list(positions.keys()):
                pos = positions[coin]
                ci = date_idx[coin][date]
                bar = coin_data[coin]["candles"][ci]
                if bar["ts"] % FUNDING_INTERVAL_MS != 0:
                    continue
                ct = CT_VAL[coin]
                rate = funding.get(coin, {}).get(str(bar["ts"]),
                          funding.get(coin, {}).get(bar["ts"], 0.0))
                if rate != 0.0:
                    notional = pos["size"] * ct * bar["C"]
                    if pos["side"] == "long":
                        fpnl = -notional * rate
                    else:
                        fpnl = notional * rate
                    equity += fpnl
                    total_funding += fpnl
                    pos["funding"] = pos.get("funding", 0.0) + fpnl

        # ── 1. Manage open positions on THIS bar (H/L first, pessimistic) ──
        for coin in list(positions.keys()):
            pos = positions[coin]
            ci = date_idx[coin][date]
            bar = coin_data[coin]["candles"][ci]
            ct = CT_VAL[coin]
            tp = pos["tp"]
            sl = pos["sl"]

            hit_tp = False
            hit_sl = False
            exit_raw = None
            reason = None

            if pos["side"] == "long":
                if bar["L"] <= sl:
                    hit_sl = True
                    exit_raw = sl
                    reason = "stop_loss"
                elif bar["H"] >= tp:
                    hit_tp = True
                    exit_raw = tp
                    reason = "take_profit"
            else:
                if bar["H"] >= sl:
                    hit_sl = True
                    exit_raw = sl
                    reason = "stop_loss"
                elif bar["L"] <= tp:
                    hit_tp = True
                    exit_raw = tp
                    reason = "take_profit"

            if hit_sl or hit_tp:
                if pos["side"] == "long":
                    fill = exit_raw * (1 - SLIPPAGE)
                    pnl = pos["size"] * ct * (fill - pos["entry"]) - pos["size"] * ct * fill * COMMISSION
                else:
                    fill = exit_raw * (1 + SLIPPAGE)
                    pnl = pos["size"] * ct * (pos["entry"] - fill) - pos["size"] * ct * fill * COMMISSION
                equity += pnl
                trades.append({
                    "date": date, "coin": coin, "side": pos["side"], "pnl": round(pnl, 2),
                    "funding_pnl": round(pos.get("funding", 0.0), 2),
                    "reason": reason, "entry": pos["entry"], "exit": round(fill, 4),
                    "size": pos["size"], "closed": True, "hold_bars": i - pos["entry_i"],
                })
                del positions[coin]
                continue

            # Time exit
            if i - pos["entry_i"] >= cfg.max_hold_bars:
                ci0 = date_idx[coin][date]
                exit_raw = coin_data[coin]["candles"][ci0]["C"]
                if pos["side"] == "long":
                    fill = exit_raw * (1 - SLIPPAGE)
                    pnl = pos["size"] * ct * (fill - pos["entry"]) - pos["size"] * ct * fill * COMMISSION
                else:
                    fill = exit_raw * (1 + SLIPPAGE)
                    pnl = pos["size"] * ct * (pos["entry"] - fill) - pos["size"] * ct * fill * COMMISSION
                equity += pnl
                trades.append({
                    "date": date, "coin": coin, "side": pos["side"], "pnl": round(pnl, 2),
                    "funding_pnl": round(pos.get("funding", 0.0), 2),
                    "reason": "time_exit", "entry": pos["entry"], "exit": round(fill, 4),
                    "size": pos["size"], "closed": True, "hold_bars": i - pos["entry_i"],
                })
                del positions[coin]

        # ── 2. Rotation: rank contrarian candidates on SIGNAL bar ──
        if i - last_rotate < cfg.min_hold_bars and positions:
            unreal = _unrealized(positions, mtm)
            equity_curve.append({"date": date, "equity": equity + unreal})
            continue

        ranked = []
        for coin, cd in coin_data.items():
            si = date_idx[coin][sig_date]
            atr = cd["atr"][si]
            if atr <= 0:
                continue

            # volatility filter
            atr_slice = [cd["atr"][j] for j in range(max(0, si - 29), si + 1) if cd["atr"][j] > 0]
            avg_atr = sum(atr_slice) / len(atr_slice) if atr_slice else 0.0
            if avg_atr > 0 and atr > avg_atr * cfg.vol_mult:
                filters_hit["volatility"] += 1
                continue

            rsi = cd["rsi"][si]
            close = cd["closes"][si]

            # trend filter (if enabled): long only above EMA, short only below
            trend_up = None
            if cd["ema_t"] is not None and cd["ema_t"][si] > 0:
                trend_up = close > cd["ema_t"][si]

            long_sig = False
            short_sig = False
            if rsi < cfg.rsi_oversold:
                if trend_up is None or trend_up:
                    long_sig = True
                else:
                    filters_hit["trend_long"] += 1
            if rsi > cfg.rsi_overbought:
                if trend_up is None or not trend_up:
                    short_sig = True
                else:
                    filters_hit["trend_short"] += 1

            if not long_sig and not short_sig:
                continue

            # score = extremity: distance from RSI 50
            score = abs(rsi - 50)

            # returns for correlation
            rets = []
            for j in range(max(1, si - 29), si + 1):
                if cd["closes"][j - 1] > 0:
                    rets.append(cd["closes"][j] / cd["closes"][j - 1] - 1)

            ranked.append({
                "coin": coin, "score": score, "rsi": rsi, "atr": atr,
                "bb": cd["bb_mid"][si] if cd["bb_mid"][si] > 0 else None,
                "long_sig": long_sig, "short_sig": short_sig, "rets": rets,
            })

        ranked.sort(key=lambda x: x["score"], reverse=True)

        targets = []
        for row in ranked:
            if len(targets) >= cfg.top_k:
                break
            side = None
            if row["long_sig"]:
                side = "long"
            elif row["short_sig"] and cfg.allow_short:
                side = "short"
            if side is None:
                continue

            # correlation vs already selected / held
            corr_ok = True
            check_against = [positions[c]["rets"] for c in positions] + [t["rets"] for t in targets]
            for held_rets in check_against:
                if abs(correlation(row["rets"], held_rets)) > cfg.corr_threshold:
                    filters_hit["correlation"] += 1
                    corr_ok = False
                    break
            if not corr_ok:
                continue
            targets.append({"coin": row["coin"], "side": side, "atr": row["atr"], "rets": row["rets"], "bb": row["bb"]})

        # Open new at THIS bar OPEN (T+1), ATR/stop from SIGNAL bar
        for t in targets:
            coin, side = t["coin"], t["side"]
            if coin in positions:
                continue
            ci = date_idx[coin][date]
            entry_raw = coin_data[coin]["candles"][ci]["O"]
            atr = t["atr"]
            lev = dynamic_leverage(atr, entry_raw, cfg.max_leverage)
            tp_dist = atr * cfg.tp_atr_mult
            sl_dist = atr * cfg.sl_atr_mult
            if side == "long":
                fill = entry_raw * (1 + SLIPPAGE)
                if cfg.tp_bb_mid and t["bb"] and t["bb"] > fill:
                    tp = t["bb"]
                else:
                    tp = fill + tp_dist
                sl = fill - sl_dist
            else:
                fill = entry_raw * (1 - SLIPPAGE)
                if cfg.tp_bb_mid and t["bb"] and t["bb"] < fill:
                    tp = t["bb"]
                else:
                    tp = fill - tp_dist
                sl = fill + sl_dist

            sz = calc_size(equity, coin, fill, sl_dist, lev, cfg.risk_per_trade, cfg.max_margin_pct)
            fee = sz * CT_VAL[coin] * fill * COMMISSION
            equity -= fee
            positions[coin] = {
                "side": side, "size": sz, "entry": fill, "tp": tp, "sl": sl,
                "atr": atr, "lev": lev, "entry_i": i, "rets": t["rets"],
            }
            trades.append({
                "date": date, "coin": coin, "side": side, "pnl": round(-fee, 2),
                "reason": "open", "entry": round(fill, 4), "exit": None,
                "size": sz, "closed": False, "leverage": round(lev, 2),
            })
            last_rotate = i

        unreal = _unrealized(positions, mtm)
        equity_curve.append({"date": date, "equity": equity + unreal})

    # Force-close at last close
    if positions and all_dates:
        date = all_dates[i_hi]
        for coin in list(positions.keys()):
            pos = positions[coin]
            ci = date_idx[coin][date]
            exit_raw = coin_data[coin]["candles"][ci]["C"]
            ct = CT_VAL[coin]
            if pos["side"] == "long":
                fill = exit_raw * (1 - SLIPPAGE)
                pnl = pos["size"] * ct * (fill - pos["entry"]) - pos["size"] * ct * fill * COMMISSION
            else:
                fill = exit_raw * (1 + SLIPPAGE)
                pnl = pos["size"] * ct * (pos["entry"] - fill) - pos["size"] * ct * fill * COMMISSION
            equity += pnl
            trades.append({
                "date": date, "coin": coin, "side": pos["side"], "pnl": round(pnl, 2),
                "funding_pnl": round(pos.get("funding", 0.0), 2),
                "reason": "backtest_end", "entry": pos["entry"], "exit": round(fill, 4),
                "size": pos["size"], "closed": True,
            })
        if equity_curve:
            equity_curve[-1]["equity"] = equity

    return summarize(cfg, equity_curve, trades, filters_hit, equity, total_funding,
                     return_trades=return_trades)


def _unrealized(positions, mtm):
    u = 0.0
    for coin, pos in positions.items():
        ct = CT_VAL[coin]
        cur = mtm[coin]
        if pos["side"] == "long":
            u += pos["size"] * ct * (cur - pos["entry"])
        else:
            u += pos["size"] * ct * (pos["entry"] - cur)
    return u


BARS_PER_DAY = 24  # 1H bars per day

def summarize(cfg, equity_curve, trades, filters_hit, final_equity, total_funding=0.0,
              return_trades=False):
    capital = cfg.capital
    closed = [t for t in trades if t.get("closed")]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]

    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = sum(t["pnl"] for t in losses)

    reason_counts = {}
    for t in closed:
        r = t.get("reason", "unknown")
        reason_counts[r] = reason_counts.get(r, 0) + 1

    mc = monte_carlo(closed, capital, n=500, block=5)

    n_bars = len(equity_curve)
    years = max(n_bars / (365.25 * BARS_PER_DAY), 1e-9)

    _cagr = 0.0
    if final_equity > 0 and capital > 0:
        ratio = final_equity / capital
        if ratio > 0:
            _cagr = (ratio ** (1 / years) - 1) * 100
        else:
            _cagr = -100.0

    return {
        "strategy": cfg.name,
        "config": asdict(cfg),
        "period": f"{equity_curve[0]['date'] if equity_curve else ''} → {equity_curve[-1]['date'] if equity_curve else ''}",
        "years": round(years, 2),
        "bars": n_bars,
        "capital": capital,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity / capital - 1) * 100, 1),
        "cagr_pct": round(_cagr, 1),
        "max_drawdown_pct": round(_max_dd(equity_curve, capital)["dd"], 1),
        "max_drawdown_date": _max_dd(equity_curve, capital)["date"],
        "sharpe": round(_sharpe(equity_curve), 2),
        "closed_trades": len(closed),
        "exit_reasons": reason_counts,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0.0,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(
            (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)))
            if losses and sum(t["pnl"] for t in losses) != 0 else 0.0, 2
        ),
        "total_funding_pnl": round(total_funding, 2),
        "funding_pct_of_capital": round(total_funding / capital * 100, 2) if capital else 0.0,
        "monte_carlo": mc,
        "filters_hit": dict(filters_hit),
        "yearly": _yearly(equity_curve, capital),
        "equity_curve": equity_curve[::24],  # ~6/hour samples for file size
        "recent_trades": closed[-15:],
        "all_trades": trades if return_trades else [],
    }


def _max_dd(equity_curve, capital):
    peak = capital
    max_dd = 0.0
    date = ""
    for pt in equity_curve:
        peak = max(peak, pt["equity"])
        dd = (peak - pt["equity"]) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            date = pt["date"]
    return {"dd": max_dd, "date": date}


def _yearly(equity_curve, capital):
    by_year = {}
    for pt in equity_curve:
        y = pt["date"][:4]
        by_year[y] = pt["equity"]
    yearly = []
    prev = capital
    for y in sorted(by_year):
        eq = by_year[y]
        yearly.append({"year": y, "equity": round(eq, 2), "return_pct": round((eq / prev - 1) * 100, 1)})
        prev = eq
    return yearly


def _sharpe(equity_curve):
    rets = []
    for j in range(1, len(equity_curve)):
        prev = equity_curve[j - 1]["equity"]
        if prev > 0:
            rets.append(equity_curve[j]["equity"] / prev - 1)
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = math.sqrt(var)
    return (mean * 365) / (std * math.sqrt(365)) if std > 0 else 0.0


def monte_carlo(closed_trades, capital, n=500, block=5):
    """Block-bootstrap trade sequence to estimate distribution of outcomes.
    Resamples trades in contiguous blocks (preserving autocorrelation from
    correlated same-regime trades) and re-runs the equity path.
    """
    if not closed_trades:
        return {"n": 0}
    import random
    rng = random.Random(42)
    final = []
    maxdd = []
    for _ in range(n):
        boot = _block_bootstrap(closed_trades, block, rng)
        eq = capital
        peak = capital
        dd = 0.0
        for t in boot:
            eq += t["pnl"]
            if eq <= 0:
                eq = 0
                break
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
        final.append(eq)
        maxdd.append(dd)
    final.sort()
    maxdd.sort()
    def pct(arr, p):
        idx = min(len(arr) - 1, int(p / 100 * len(arr)))
        return arr[idx]
    return {
        "n": n,
        "block_size": block,
        "profit_pct": round(sum(1 for f in final if f > capital) / n * 100, 1),
        "final_equity_p05": round(pct(final, 5), 0),
        "final_equity_median": round(pct(final, 50), 0),
        "final_equity_p95": round(pct(final, 95), 0),
        "maxdd_p05": round(pct(maxdd, 5), 1),   # worst 5% drawdown
        "maxdd_median": round(pct(maxdd, 50), 1),
        "maxdd_p95": round(pct(maxdd, 95), 1),
    }


def _block_bootstrap(closed, block, rng):
    n = len(closed)
    if n <= block:
        return list(closed)
    out = []
    while len(out) < n:
        start = rng.randrange(0, n - block + 1)
        out.extend(closed[start:start + block])
    return out[:n]


def buy_and_hold_btc(daily_data: dict, capital: float = 10000.0) -> dict:
    bars = daily_data["BTC"]
    # Align to same start as strategies (~ after SMA warmup ~210 bars)
    start = 210
    if len(bars) <= start + 10:
        start = 50
    entry = bars[start]["C"] * (1 + SLIPPAGE)
    fee0 = capital * COMMISSION
    units = (capital - fee0) / entry
    curve = []
    for b in bars[start:]:
        eq = units * b["C"]
        curve.append({"date": b["dt"], "equity": eq})
    exit_px = bars[-1]["C"] * (1 - SLIPPAGE)
    final = units * exit_px * (1 - COMMISSION)
    years = max(len(curve) / 365.25, 1e-9)
    peak = capital
    max_dd = 0.0
    for pt in curve:
        peak = max(peak, pt["equity"])
        dd = (peak - pt["equity"]) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    rets = []
    for j in range(1, len(curve)):
        if curve[j - 1]["equity"] > 0:
            rets.append(curve[j]["equity"] / curve[j - 1]["equity"] - 1)
    mean = sum(rets) / len(rets) if rets else 0
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) if rets else 0
    sharpe = (mean * 365) / (std * math.sqrt(365)) if std > 0 else 0
    return {
        "strategy": "BTC Buy & Hold (15m)",
        "period": f"{curve[0]['date']} → {curve[-1]['date']}",
        "years": round(years, 2),
        "capital": capital,
        "final_equity": round(final, 2),
        "total_return_pct": round((final / capital - 1) * 100, 1),
        "cagr_pct": round(((final / capital) ** (1 / years) - 1) * 100, 1),
        "max_drawdown_pct": round(max_dd, 1),
        "sharpe": round(sharpe, 2),
        "closed_trades": 1,
        "win_rate": 100.0 if final > capital else 0.0,
    }


def print_report(r: dict):
    print("\n" + "=" * 72)
    print(f"  {r['strategy']}")
    print("=" * 72)
    print(f"  Period:          {r['period']}  ({r['years']}y)")
    print(f"  Final equity:    ${r['final_equity']:,.2f}   (start ${r['capital']:,.0f})")
    print(f"  Total return:    {r['total_return_pct']:+.1f}%")
    print(f"  CAGR:            {r['cagr_pct']:.1f}%")
    print(f"  Max drawdown:    {r['max_drawdown_pct']:.1f}%"
          + (f"  ({r.get('max_drawdown_date','')})" if r.get('max_drawdown_date') else ""))
    print(f"  Sharpe:          {r['sharpe']:.2f}")
    if "closed_trades" in r and r["strategy"] != "BTC Buy & Hold":
        print(f"  Trades:          {r['closed_trades']}  |  WR {r['win_rate']}%  |  "
              f"PF {r.get('profit_factor', 0)}  |  avgW ${r.get('avg_win', 0)} / avgL ${r.get('avg_loss', 0)}")
        if r.get("yearly"):
            print("  Yearly:")
            for y in r["yearly"]:
                print(f"    {y['year']}: {y['return_pct']:+6.1f}%   eq=${y['equity']:,.0f}")
        if r.get("filters_hit"):
            print("  Filters:")
            for k, v in sorted(r["filters_hit"].items(), key=lambda x: -x[1]):
                print(f"    {k:18s} {v}")
        if "total_funding_pnl" in r and r["strategy"] != "BTC Buy & Hold":
            print(f"  Funding PnL:    ${r['total_funding_pnl']:,.2f}  "
                  f"({r['funding_pct_of_capital']:+.2f}% of capital)")
        mc = r.get("monte_carlo")
        if mc and mc.get("n"):
            print("  Monte Carlo (block bootstrap):")
            print(f"    P(profit)          {mc['profit_pct']}%")
            print(f"    Final equity p05/med/p95: ${mc['final_equity_p05']:,.0f} / "
                  f"${mc['final_equity_median']:,.0f} / ${mc['final_equity_p95']:,.0f}")
            print(f"    MaxDD p05/med/p95:   {mc['maxdd_p05']}% / {mc['maxdd_median']}% / {mc['maxdd_p95']}%")


async def main():
    force = "--refresh" in sys.argv
    print("=" * 72)
    print("HONEST 1H MEAN-REVERSION BACKTEST — no look-ahead, realistic costs")
    print("Rules: signal@T close → entry@T+1 open | TP/SL vs H/L | fee 0.10% + slip 0.05% | funding @8h")
    print("=" * 72)

    data = await load_data(force_refresh=force)
    funding = await load_funding(force_refresh=force)

    print("\n[run] Mean-Reversion 1H (base) ...", flush=True)
    base = run_strategy(data, REV_BASE, funding)
    print_report(base)

    print("\n[run] Mean-Reversion 1H (exit at Bollinger mid) ...", flush=True)
    mean = run_strategy(data, REV_MEAN, funding)
    print_report(mean)

    print("\n[run] Mean-Reversion 1H (trend filter + wider TP) ...", flush=True)
    wide = run_strategy(data, REV_WIDE, funding)
    print_report(wide)

    print("\n[run] BTC Buy & Hold (1H bars) benchmark ...", flush=True)
    bnh = buy_and_hold_btc(data)
    print_report(bnh)

    # Comparison table
    print("\n" + "=" * 72)
    print("COMPARISON")
    print("=" * 72)
    rows = [base, mean, wide, bnh]
    print(f"  {'Strategy':44s} {'Return':>8} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7} {'Trades':>7}")
    for r in rows:
        print(f"  {r['strategy']:44s} {r['total_return_pct']:+7.1f}% {r['cagr_pct']:6.1f}% "
              f"{r['max_drawdown_pct']:6.1f}% {r['sharpe']:6.2f} {r.get('closed_trades', 0):7d}")

    print("\nVERDICT")
    best = max(rows, key=lambda r: r["sharpe"] if r["max_drawdown_pct"] < 60 else -999)
    print(f"  Best risk-adjusted (Sharpe, DD<60%): {best['strategy']}")
    if base["sharpe"] < 0.5 or base["total_return_pct"] < bnh["total_return_pct"]:
        print("  → 1H mean-reversion is NOT clearly better than holding BTC on this window.")
    elif base["max_drawdown_pct"] > 35:
        print("  → Returns exist but drawdowns are heavy — size/risk params need tightening.")
    else:
        print("  → 1H mean-reversion shows usable edge vs buy&hold under honest assumptions.")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rules": {
            "signal": "bar T close (causal indicators)",
            "entry": "bar T+1 open",
            "stops": "pessimistic H/L first",
            "costs": f"commission {COMMISSION*100:.2f}% + slippage {SLIPPAGE*100:.2f}% per side",
            "data": f"OKX SWAP {BAR} candles (BTC/ETH/BNB/SOL-USDT-SWAP)",
            "look_ahead": False,
            "funding": "8h boundaries on open positions",
            "note": "First honest 1H mean-reversion run on ~13 months of OKX 1H history.",
        },
        "rev_base": {k: v for k, v in base.items() if k not in ("equity_curve", "recent_trades", "config")},
        "rev_mean": {k: v for k, v in mean.items() if k not in ("equity_curve", "recent_trades", "config")},
        "rev_wide": {k: v for k, v in wide.items() if k not in ("equity_curve", "recent_trades", "config")},
        "btc_buy_hold": bnh,
        "rev_base_full": base,
        "rev_mean_full": mean,
        "rev_wide_full": wide,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved → {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
