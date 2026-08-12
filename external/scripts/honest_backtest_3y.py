#!/usr/bin/env python3
"""Honest 3-year backtest for Momentum Rotation v2 and Alpha.

Rules (no look-ahead, no fantasy fills):
  1. Signal on bar T CLOSE using only data up to T
  2. Enter / rotate at bar T+1 OPEN
  3. Stops checked against day's HIGH/LOW first (pessimistic), then peak update
  4. Commission 0.10% + slippage 0.05% per side (no "limit save")
  5. Real Binance daily OHLCV (~3y) as proxy for OKX SWAP prices
  6. Same sizing / filters as live RotationConfig / AlphaConfig
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
BINANCE_MAP = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT", "SOL": "SOLUSDT"}
CT_VAL = {"BTC": 0.01, "ETH": 0.1, "BNB": 0.1, "SOL": 0.1}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "BNB": 0.01, "SOL": 0.01}

COMMISSION = 0.001    # 0.10% taker
SLIPPAGE = 0.0005     # 0.05%
DAYS_BACK = 1100      # ~3 years + warmup
CACHE_PATH = os.path.join(os.path.dirname(__file__), "honest_3y_cache.json")
OUT_PATH = os.path.join(os.path.dirname(__file__), "honest_3y_results.json")
FUNDING_CACHE_PATH = os.path.join(os.path.dirname(__file__), "honest_3y_funding_cache.json")

# Perp funding: OKX charges every 8h (3x/day). Longs pay shorts when rate>0.
FUNDING_INTERVALS_PER_DAY = 3


@dataclass
class StratConfig:
    name: str
    capital: float = 10000.0
    top_k: int = 2
    roc_period: int = 14              # single-ROC mode (dual mode if roc_fast_period>0)
    roc_fast_period: int = 0          # >0 → dual-ROC: rank by fast, filter by slow sign
    roc_slow_period: int = 0
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    adx_min: float = 18.0
    min_roc: float = 0.0              # 0 = disabled; |roc| below this is skipped
    min_hold_days: int = 3
    max_leverage: float = 3.0
    risk_per_trade: float = 0.02
    trail_atr_mult: float = 0.5
    breakeven_pct: float = 0.03
    partial_tp_pct: float = 0.05
    partial_tp_ratio: float = 0.5
    rsi_long_max: float = 75.0
    rsi_short_min: float = 25.0
    vol_mult: float = 1.5
    corr_threshold: float = 0.7
    atr_stop_mult: float = 1.5
    max_margin_pct: float = 0.40
    allow_short: bool = True


MOMENTUM = StratConfig(
    name="Momentum Rotation v2",
    adx_min=18.0, min_hold_days=3, risk_per_trade=0.02,
    trail_atr_mult=0.5, breakeven_pct=0.03,
    partial_tp_pct=0.05, partial_tp_ratio=0.5,
)

ALPHA = StratConfig(
    name="Alpha Rotation",
    adx_min=22.0, min_hold_days=5, risk_per_trade=0.03,
    trail_atr_mult=0.8, breakeven_pct=0.02,
    partial_tp_pct=0.07, partial_tp_ratio=0.4,
)

V3_LIVE = StratConfig(
    name="Momentum Rotation v3 (live sweep)",
    adx_min=22.0, min_hold_days=3, risk_per_trade=0.05,
    max_leverage=2.0, atr_stop_mult=3.5, trail_atr_mult=0.1,
    breakeven_pct=0.02, partial_tp_pct=0.10, partial_tp_ratio=0.5,
    max_margin_pct=2.0, min_roc=2.0,
)

V3_PROPOSED = StratConfig(
    name="Momentum Rotation v4 (proposed)",
    top_k=3, roc_fast_period=20, roc_slow_period=50,
    ema_fast=15, ema_slow=70, adx_min=25.0, min_roc=3.0,
    min_hold_days=3, max_leverage=2.0, risk_per_trade=0.02,
    atr_stop_mult=2.5, trail_atr_mult=1.5,
    breakeven_pct=0.02, partial_tp_pct=0.10, partial_tp_ratio=0.5,
    max_margin_pct=2.0, allow_short=False,
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

async def fetch_candles_okx(inst_id: str, after: str | None = None, limit: int = 300):
    url = "https://www.okx.com/api/v5/market/history-candles"
    params = {"instId": inst_id, "bar": "1D", "limit": str(limit)}
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
                        "date": datetime.fromtimestamp(int(c[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                        "O": float(c[1]), "H": float(c[2]), "L": float(c[3]), "C": float(c[4]),
                        "V": float(c[5]),
                    })
                out.sort(key=lambda x: x["ts"])
                return out
            except Exception as e:
                print(f"  retry {inst_id}: {e}", flush=True)
                await asyncio.sleep(1.2 * (attempt + 1))
    return []


async def fetch_daily(coin: str, days_back: int = DAYS_BACK):
    inst_id = f"{coin}-USDT-SWAP"
    all_candles = []
    after = None
    while len(all_candles) < days_back:
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

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if all_candles and all_candles[-1]["date"] == today:
        all_candles = all_candles[:-1]
    # keep last days_back
    if len(all_candles) > days_back:
        all_candles = all_candles[-days_back:]
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


async def fetch_funding_daily(coin: str, days_back: int = DAYS_BACK):
    inst_id = f"{coin}-USDT-SWAP"
    all_rates = []
    after = None
    while True:
        batch, ok = await fetch_funding_okx(inst_id, after=after, limit=100)
        if not batch:
            break
        all_rates = batch + all_rates
        after = str(batch[0]["ts"])
        if len(batch) < 100:
            break
        await asyncio.sleep(0.2)
    # aggregate to daily funding sum (3 intervals/day)
    daily = {}
    for r in all_rates:
        d = datetime.fromtimestamp(r["ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        daily[d] = daily.get(d, 0.0) + r["rate"]
    return daily


async def load_funding(force_refresh: bool = False):
    if not force_refresh and os.path.exists(FUNDING_CACHE_PATH):
        with open(FUNDING_CACHE_PATH) as f:
            cached = json.load(f)
        age_h = (time.time() - cached.get("fetched_at", 0)) / 3600
        if age_h < 48 and all(c in cached.get("data", {}) for c in COINS):
            print(f"  Using funding cache ({age_h:.1f}h old)", flush=True)
            return cached["data"]
    print("  Fetching OKX SWAP funding history (~3y)...", flush=True)
    funding = {}
    for coin in COINS:
        daily = await fetch_funding_daily(coin)
        if not daily:
            daily = {}
            print(f"    WARN: no funding data for {coin}", flush=True)
        funding[coin] = daily
        print(f"    {coin}: {len(daily)} days funding", flush=True)
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
            if all(len(cached["data"][c]) > 200 for c in COINS):
                print(f"  Using cache ({age_h:.1f}h old)", flush=True)
                return cached["data"]

    print("  Fetching OKX SWAP daily candles (~3y)...", flush=True)
    data = {}
    for coin in COINS:
        bars = await fetch_daily(coin)
        if not bars:
            raise RuntimeError(f"Failed to fetch candles for {coin}")
        data[coin] = bars
        print(f"    {coin}: {len(bars)} bars  {bars[0]['date']} → {bars[-1]['date']}", flush=True)
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
        "ema_f": ema_series(closes, cfg.ema_fast),
        "ema_s": ema_series(closes, cfg.ema_slow),
        "atr": atr_series(highs, lows, closes, cfg.atr_period),
        "adx": adx_series(highs, lows, closes, 14),
        "rsi": rsi_series(closes, 14),
        "sma200": sma_series(closes, 200),
    }
    if cfg.roc_fast_period > 0:
        frame["roc"] = roc_series(closes, cfg.roc_fast_period)
        frame["roc_fast"] = frame["roc"]
        frame["roc_slow"] = roc_series(closes, cfg.roc_slow_period)
    else:
        frame["roc"] = roc_series(closes, cfg.roc_period)
    return frame


def run_strategy(daily_data: dict, cfg: StratConfig, funding: dict | None = None,
                 window: tuple[str, str] | None = None,
                 return_trades: bool = False) -> dict:
    """Run strategy. If window=(start_date, end_date) given, restrict to that
    date range (used for walk-forward train/test). Indicators are computed on
    the full history (causal), so warmup values are valid inside any window.
    """
    coin_data = {}
    for coin in COINS:
        bars = daily_data.get(coin, [])
        if len(bars) < 250:
            raise RuntimeError(f"{coin}: not enough bars ({len(bars)})")
        coin_data[coin] = build_coin_frame(bars, cfg)

    # Align on common dates
    common = None
    for coin, cd in coin_data.items():
        dates = {c["date"] for c in cd["candles"]}
        common = dates if common is None else (common & dates)
    all_dates = sorted(common)

    date_idx = {
        coin: {c["date"]: i for i, c in enumerate(cd["candles"])}
        for coin, cd in coin_data.items()
    }

    equity = cfg.capital
    positions = {}
    trades = []
    equity_curve = []
    filters_hit = defaultdict(int)
    last_rotate = -10**9
    start_i = max(cfg.ema_slow + 20, 210)  # need SMA200
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

        # Mark-to-market prices for equity curve (today close)
        mtm = {}
        for coin in COINS:
            ci = date_idx[coin][date]
            mtm[coin] = coin_data[coin]["candles"][ci]["C"]

        # ── 0. Funding accrual for open positions (before stop/peak logic) ──
        if funding:
            for coin in list(positions.keys()):
                pos = positions[coin]
                ci = date_idx[coin][date]
                bar = coin_data[coin]["candles"][ci]
                ct = CT_VAL[coin]
                rate_day = funding.get(coin, {}).get(date, 0.0)
                if rate_day != 0.0:
                    notional = pos["size"] * ct * bar["C"]
                    if pos["side"] == "long":
                        fpnl = -notional * rate_day
                    else:
                        # shorts receive when rate>0, pay when rate<0
                        fpnl = notional * rate_day
                    equity += fpnl
                    total_funding += fpnl
                    pos["funding"] = pos.get("funding", 0.0) + fpnl

        # ── 1. Manage open positions on TODAY's bar (H/L first) ──
        for coin in list(positions.keys()):
            pos = positions[coin]
            ci = date_idx[coin][date]
            bar = coin_data[coin]["candles"][ci]
            ct = CT_VAL[coin]
            trail = pos["atr"] * cfg.trail_atr_mult
            if trail <= 0:
                trail = pos["entry"] * 0.02

            hit_stop = False
            hit_partial = False
            exit_raw = None
            reason = "trail_stop"

            if pos["side"] == "long":
                # Pessimistic: stop vs LOW before peak update
                if bar["L"] <= pos["stop"]:
                    hit_stop = True
                    exit_raw = pos["stop"]
                if not hit_stop:
                    if bar["H"] > pos["peak"]:
                        pos["peak"] = bar["H"]
                        new_stop = pos["peak"] - trail
                        if new_stop > pos["stop"]:
                            pos["stop"] = new_stop
                    if (not pos["breakeven"]
                            and bar["C"] >= pos["entry"] * (1 + cfg.breakeven_pct)):
                        pos["stop"] = max(pos["stop"], pos["entry"] * 0.999)
                        pos["breakeven"] = True
                    if (not pos["partial"]
                            and bar["H"] >= pos["entry"] * (1 + cfg.partial_tp_pct)):
                        hit_partial = True
                        exit_raw = pos["entry"] * (1 + cfg.partial_tp_pct)
            else:
                if bar["H"] >= pos["stop"]:
                    hit_stop = True
                    exit_raw = pos["stop"]
                if not hit_stop:
                    if bar["L"] < pos["peak"]:
                        pos["peak"] = bar["L"]
                        new_stop = pos["peak"] + trail
                        if new_stop < pos["stop"]:
                            pos["stop"] = new_stop
                    if (not pos["breakeven"]
                            and bar["C"] <= pos["entry"] * (1 - cfg.breakeven_pct)):
                        pos["stop"] = min(pos["stop"], pos["entry"] * 1.001)
                        pos["breakeven"] = True
                    if (not pos["partial"]
                            and bar["L"] <= pos["entry"] * (1 - cfg.partial_tp_pct)):
                        hit_partial = True
                        exit_raw = pos["entry"] * (1 - cfg.partial_tp_pct)

            if hit_partial and not hit_stop:
                close_sz = math.floor(pos["size"] * cfg.partial_tp_ratio / LOT_SZ[coin] + 1e-12) * LOT_SZ[coin]
                if close_sz > 0 and close_sz < pos["size"]:
                    if pos["side"] == "long":
                        fill = exit_raw * (1 - SLIPPAGE)
                        pnl = close_sz * ct * (fill - pos["entry"]) - close_sz * ct * fill * COMMISSION
                    else:
                        fill = exit_raw * (1 + SLIPPAGE)
                        pnl = close_sz * ct * (pos["entry"] - fill) - close_sz * ct * fill * COMMISSION
                    equity += pnl
                    trades.append({
                        "date": date, "coin": coin, "side": pos["side"], "pnl": round(pnl, 2),
                        "reason": "partial_tp", "entry": pos["entry"], "exit": round(fill, 4),
                        "size": close_sz, "closed": False,
                    })
                    pos["size"] -= close_sz
                    pos["partial"] = True

            if hit_stop:
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
                    "size": pos["size"], "closed": True, "hold_days": i - pos["entry_i"],
                })
                del positions[coin]

        # ── 2. Rotation using YESTERDAY signal only ──
        if i - last_rotate < cfg.min_hold_days and positions:
            unreal = _unrealized(positions, mtm)
            equity_curve.append({"date": date, "equity": equity + unreal})
            continue

        # BTC 200MA filter from signal bar
        btc_si = date_idx["BTC"][sig_date]
        btc_cd = coin_data["BTC"]
        btc_above = True
        if btc_cd["sma200"][btc_si] > 0:
            btc_above = btc_cd["closes"][btc_si] > btc_cd["sma200"][btc_si]

        ranked = []
        for coin, cd in coin_data.items():
            si = date_idx[coin][sig_date]
            atr = cd["atr"][si]
            if atr <= 0:
                continue

            # avg ATR 30d ending at signal bar
            atr_slice = [cd["atr"][j] for j in range(max(0, si - 29), si + 1) if cd["atr"][j] > 0]
            avg_atr = sum(atr_slice) / len(atr_slice) if atr_slice else 0.0
            if avg_atr > 0 and atr > avg_atr * cfg.vol_mult:
                filters_hit["volatility"] += 1
                continue

            ema_trend = cd["ema_f"][si] > cd["ema_s"][si]
            rsi = cd["rsi"][si]
            roc = cd["roc"][si]
            roc_slow = cd.get("roc_slow", [0.0])[si] if cfg.roc_fast_period > 0 else roc
            adx = cd["adx"][si]

            if cfg.min_roc > 0 and abs(roc) < cfg.min_roc:
                filters_hit["min_roc"] += 1
                continue

            if rsi > cfg.rsi_long_max and ema_trend:
                filters_hit["rsi_overbought"] += 1
                continue
            if rsi < cfg.rsi_short_min and not ema_trend:
                filters_hit["rsi_oversold"] += 1
                continue
            if not btc_above and roc > 0 and ema_trend:
                filters_hit["bear_long"] += 1
                continue

            trend_val = 0.0
            if cd["ema_s"][si] > 0:
                trend_val = (cd["ema_f"][si] - cd["ema_s"][si]) / cd["ema_s"][si] * 100
            score = roc * 0.5 + trend_val * 0.3 + (adx / 50) * 0.2

            # daily returns for correlation (signal bar)
            rets = []
            for j in range(max(1, si - 29), si + 1):
                if cd["closes"][j - 1] > 0:
                    rets.append(cd["closes"][j] / cd["closes"][j - 1] - 1)

            ranked.append({
                "coin": coin, "score": score, "roc": roc, "roc_slow": roc_slow,
                "ema_trend": ema_trend, "adx": adx, "atr": atr, "rets": rets,
            })

        ranked.sort(key=lambda x: x["score"], reverse=True)

        targets = []
        for row in ranked:
            if len(targets) >= cfg.top_k:
                break
            slow_ok = cfg.roc_fast_period <= 0 or row["roc_slow"] > 0
            if row["roc"] > 0 and row["ema_trend"] and slow_ok and row["adx"] >= cfg.adx_min:
                side = "long"
            elif (cfg.allow_short and row["roc"] < 0 and not row["ema_trend"]
                  and (cfg.roc_fast_period <= 0 or row["roc_slow"] < 0)
                  and row["adx"] >= cfg.adx_min):
                side = "short"
            else:
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
            targets.append({"coin": row["coin"], "side": side, "atr": row["atr"], "rets": row["rets"]})

        target_set = {(t["coin"], t["side"]) for t in targets}

        # Close rotated-out at TODAY OPEN
        for coin in list(positions.keys()):
            pos = positions[coin]
            if (coin, pos["side"]) in target_set:
                continue
            ci = date_idx[coin][date]
            exit_raw = coin_data[coin]["candles"][ci]["O"]
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
                "reason": "rotation_exit", "entry": pos["entry"], "exit": round(fill, 4),
                "size": pos["size"], "closed": True, "hold_days": i - pos["entry_i"],
            })
            del positions[coin]

        # Open new at TODAY OPEN, ATR/stop from SIGNAL bar
        for t in targets:
            coin, side = t["coin"], t["side"]
            if coin in positions:
                continue
            ci = date_idx[coin][date]
            entry_raw = coin_data[coin]["candles"][ci]["O"]
            atr = t["atr"]
            lev = dynamic_leverage(atr, entry_raw, cfg.max_leverage)
            stop_dist = atr * cfg.atr_stop_mult
            if side == "long":
                fill = entry_raw * (1 + SLIPPAGE)
                stop = fill - stop_dist
            else:
                fill = entry_raw * (1 - SLIPPAGE)
                stop = fill + stop_dist

            sz = calc_size(equity, coin, fill, stop_dist, lev, cfg.risk_per_trade, cfg.max_margin_pct)
            fee = sz * CT_VAL[coin] * fill * COMMISSION
            equity -= fee
            positions[coin] = {
                "side": side, "size": sz, "entry": fill, "stop": stop,
                "peak": fill, "atr": atr, "lev": lev, "breakeven": False,
                "partial": False, "entry_i": i, "rets": t["rets"],
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

    return {
        "strategy": cfg.name,
        "config": asdict(cfg),
        "period": f"{equity_curve[0]['date'] if equity_curve else ''} → {equity_curve[-1]['date'] if equity_curve else ''}",
        "years": round(max(len(equity_curve) / 365.25, 1e-9), 2),
        "capital": capital,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity / capital - 1) * 100, 1),
        "cagr_pct": round(((final_equity / capital) ** (1 / max(len(equity_curve) / 365.25, 1e-9)) - 1) * 100, 1),
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
        "equity_curve": equity_curve[::7],  # weekly samples for file size
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
    # Align to same start as strategies (~ after SMA200 warmup)
    start = 210
    if len(bars) <= start + 10:
        start = 50
    entry = bars[start]["C"] * (1 + SLIPPAGE)
    fee0 = capital * COMMISSION
    units = (capital - fee0) / entry
    curve = []
    for b in bars[start:]:
        eq = units * b["C"]
        curve.append({"date": b["date"], "equity": eq})
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
        "strategy": "BTC Buy & Hold",
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
    print("HONEST 3Y BACKTEST — no look-ahead, real OHLCV, realistic costs")
    print("Rules: signal@T close → entry@T+1 open | stop vs H/L | fee 0.10% + slip 0.05%")
    print("=" * 72)

    data = await load_data(force_refresh=force)
    funding = await load_funding(force_refresh=force)

    print("\n[run] Momentum Rotation v2 ...", flush=True)
    mom = run_strategy(data, MOMENTUM, funding)
    print_report(mom)

    print("\n[run] Alpha Rotation ...", flush=True)
    alpha = run_strategy(data, ALPHA, funding)
    print_report(alpha)

    print("\n[run] Momentum Rotation v3 (live) ...", flush=True)
    v3 = run_strategy(data, V3_LIVE, funding)
    print_report(v3)

    print("\n[run] Momentum Rotation v4 (proposed) ...", flush=True)
    v4 = run_strategy(data, V3_PROPOSED, funding)
    print_report(v4)

    print("\n[run] BTC Buy & Hold benchmark ...", flush=True)
    bnh = buy_and_hold_btc(data)
    print_report(bnh)

    # Comparison table
    print("\n" + "=" * 72)
    print("COMPARISON")
    print("=" * 72)
    rows = [mom, alpha, v3, v4, bnh]
    print(f"  {'Strategy':28s} {'Return':>8} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7} {'Trades':>7}")
    for r in rows:
        print(f"  {r['strategy']:28s} {r['total_return_pct']:+7.1f}% {r['cagr_pct']:6.1f}% "
              f"{r['max_drawdown_pct']:6.1f}% {r['sharpe']:6.2f} {r.get('closed_trades', 0):7d}")

    # Verdict
    print("\nVERDICT")
    best = max(rows, key=lambda r: r["sharpe"] if r["max_drawdown_pct"] < 60 else -999)
    vs_btc = v3["total_return_pct"] - bnh["total_return_pct"]
    print(f"  Best risk-adjusted (Sharpe, DD<60%): {best['strategy']}")
    print(f"  V3 live vs BTC buy&hold: {vs_btc:+.1f} pp total return")
    vs_v4 = v4["total_return_pct"] - v3["total_return_pct"]
    print(f"  V4 proposed vs V3 live: {vs_v4:+.1f} pp total return, "
          f"Sharpe {v4['sharpe']:.2f} vs {v3['sharpe']:.2f}, "
          f"MaxDD {v4['max_drawdown_pct']}% vs {v3['max_drawdown_pct']}%")
    if v3["sharpe"] < 0.5 or v3["total_return_pct"] < bnh["total_return_pct"]:
        print("  → Current live strategies are NOT clearly better than holding BTC on this window.")
    elif v3["max_drawdown_pct"] > 35:
        print("  → Returns exist but drawdowns are heavy — size/risk params need tightening.")
    else:
        print("  → Momentum shows usable edge vs buy&hold under honest assumptions.")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rules": {
            "signal": "bar T close (causal indicators)",
            "entry": "bar T+1 open",
            "stops": "pessimistic H/L first",
            "costs": f"commission {COMMISSION*100:.2f}% + slippage {SLIPPAGE*100:.2f}% per side",
            "data": "OKX SWAP daily candles (BTC/ETH/BNB/SOL-USDT-SWAP)",
            "look_ahead": False,
            "note": "Trading window ~2.4y after SMA200 warmup on ~3y OKX history (2023-07 → 2026-08). Old scripts/backtest_v2_results.json (+322%, Sharpe 3.09) is INVALID: same-bar entry + limit-save fantasy fills.",
        },
        "momentum": {k: v for k, v in mom.items() if k not in ("equity_curve", "recent_trades", "config")},
        "alpha": {k: v for k, v in alpha.items() if k not in ("equity_curve", "recent_trades", "config")},
        "v3_live": {k: v for k, v in v3.items() if k not in ("equity_curve", "recent_trades", "config")},
        "v4_proposed": {k: v for k, v in v4.items() if k not in ("equity_curve", "recent_trades", "config")},
        "btc_buy_hold": bnh,
        "momentum_full": mom,
        "alpha_full": alpha,
        "v3_full": v3,
        "v4_full": v4,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved → {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
