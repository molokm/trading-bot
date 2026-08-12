#!/usr/bin/env python3
"""Honest 15m scalp backtest for a Momentum Rotation variant.

Same causal rules as honest_backtest_3y, but on 15-minute OKX SWAP candles:
  1. Signal on bar T CLOSE using only data up to T
  2. Enter / rotate at bar T+1 OPEN
  3. Stops checked against bar HIGH/LOW first (pessimistic), then peak update
  4. Commission 0.10% + slippage 0.05% per side
  5. Funding applied at 8h boundaries (00:00 / 08:00 / 16:00 UTC) on open positions
  6. BTC 200-MA regime filter optional (off by default for scalp)
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
BAR = "15m"
BAR_MS = 15 * 60 * 1000
SCALP_DAYS = 400      # ~13 months for first run
CACHE_PATH = os.path.join(os.path.dirname(__file__), "scalp_15m_cache.json")
OUT_PATH = os.path.join(os.path.dirname(__file__), "scalp_15m_results.json")
FUNDING_CACHE_PATH = os.path.join(os.path.dirname(__file__), "scalp_15m_funding_cache.json")

# Perp funding: OKX charges every 8h (00:00 / 08:00 / 16:00 UTC)
FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000


@dataclass
class StratConfig:
    name: str
    capital: float = 10000.0
    top_k: int = 3
    roc_period: int = 14              # single-ROC mode (dual mode if roc_fast_period>0)
    roc_fast_period: int = 0          # >0 → dual-ROC: rank by fast, filter by slow sign
    roc_slow_period: int = 0
    ema_fast: int = 15
    ema_slow: int = 70
    atr_period: int = 14
    sma_long: int = 200             # BTC regime MA (off unless use_btc_200ma)
    adx_min: float = 25.0
    min_roc: float = 0.0              # 0 = disabled; |roc| below this is skipped
    min_hold_bars: int = 4            # 4 × 15m = 1 hour cooldown
    max_leverage: float = 2.0
    risk_per_trade: float = 0.01
    trail_atr_mult: float = 0.5
    breakeven_pct: float = 0.0015     # 0.15% → breakeven
    partial_tp_pct: float = 0.004     # 0.40% → close partial
    partial_tp_ratio: float = 0.5
    rsi_long_max: float = 75.0
    rsi_short_min: float = 25.0
    vol_mult: float = 1.5
    corr_threshold: float = 0.7
    atr_stop_mult: float = 2.5
    max_margin_pct: float = 2.0
    allow_short: bool = True
    use_btc_200ma: bool = False       # 200-MA regime filter off by default for scalp


SCALP_BASE = StratConfig(
    name="Scalp Momentum 15m (proposed)",
    top_k=3, adx_min=25.0, min_roc=0.4,
    ema_fast=15, ema_slow=70, min_hold_bars=4,
    max_leverage=2.0, risk_per_trade=0.01,
    atr_stop_mult=2.5, trail_atr_mult=0.5,
    breakeven_pct=0.0015, partial_tp_pct=0.004, partial_tp_ratio=0.5,
    max_margin_pct=2.0, allow_short=True, use_btc_200ma=False,
)

SCALP_WIDE = StratConfig(
    name="Scalp Momentum 15m (wider trail)",
    top_k=3, adx_min=25.0, min_roc=0.4,
    ema_fast=15, ema_slow=70, min_hold_bars=4,
    max_leverage=2.0, risk_per_trade=0.01,
    atr_stop_mult=2.5, trail_atr_mult=1.0,
    breakeven_pct=0.0015, partial_tp_pct=0.004, partial_tp_ratio=0.5,
    max_margin_pct=2.0, allow_short=True, use_btc_200ma=False,
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
        "ema_f": ema_series(closes, cfg.ema_fast),
        "ema_s": ema_series(closes, cfg.ema_slow),
        "atr": atr_series(highs, lows, closes, cfg.atr_period),
        "adx": adx_series(highs, lows, closes, 14),
        "rsi": rsi_series(closes, 14),
        "sma200": sma_series(closes, cfg.sma_long),
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
    start_i = max(cfg.ema_slow + 20, cfg.sma_long + 10)  # need SMA long warmup
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
                        # shorts receive when rate>0, pay when rate<0
                        fpnl = notional * rate
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
                    "size": pos["size"], "closed": True, "hold_bars": i - pos["entry_i"],
                })
                del positions[coin]

        # ── 2. Rotation using PREVIOUS bar signal only ──
        if i - last_rotate < cfg.min_hold_bars and positions:
            unreal = _unrealized(positions, mtm)
            equity_curve.append({"date": date, "equity": equity + unreal})
            continue

        # BTC long MA regime filter from signal bar (optional for scalp)
        btc_si = date_idx["BTC"][sig_date]
        btc_cd = coin_data["BTC"]
        btc_above = True
        if cfg.use_btc_200ma and btc_cd["sma200"][btc_si] > 0:
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
                "size": pos["size"], "closed": True, "hold_bars": i - pos["entry_i"],
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


BARS_PER_DAY = 24 * 60 // 15  # 96 for 15m

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
    print(f"HONEST 15m SCALP BACKTEST — no look-ahead, real OHLCV, realistic costs")
    print(f"Rules: signal@T close → entry@T+1 open | stop vs H/L | fee 0.10% + slip 0.05% | funding @8h")
    print("=" * 72)

    data = await load_data(force_refresh=force)
    funding = await load_funding(force_refresh=force)

    print("\n[run] Scalp Momentum 15m (base) ...", flush=True)
    base = run_strategy(data, SCALP_BASE, funding)
    print_report(base)

    print("\n[run] Scalp Momentum 15m (wider trail) ...", flush=True)
    wide = run_strategy(data, SCALP_WIDE, funding)
    print_report(wide)

    print("\n[run] BTC Buy & Hold (15m bars) benchmark ...", flush=True)
    bnh = buy_and_hold_btc(data)
    print_report(bnh)

    # Comparison table
    print("\n" + "=" * 72)
    print("COMPARISON")
    print("=" * 72)
    rows = [base, wide, bnh]
    print(f"  {'Strategy':36s} {'Return':>8} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7} {'Trades':>7}")
    for r in rows:
        print(f"  {r['strategy']:36s} {r['total_return_pct']:+7.1f}% {r['cagr_pct']:6.1f}% "
              f"{r['max_drawdown_pct']:6.1f}% {r['sharpe']:6.2f} {r.get('closed_trades', 0):7d}")

    print("\nVERDICT")
    best = max(rows, key=lambda r: r["sharpe"] if r["max_drawdown_pct"] < 60 else -999)
    print(f"  Best risk-adjusted (Sharpe, DD<60%): {best['strategy']}")
    if base["sharpe"] < 0.5 or base["total_return_pct"] < bnh["total_return_pct"]:
        print("  → 15m scalp is NOT clearly better than holding BTC on this window.")
    elif base["max_drawdown_pct"] > 35:
        print("  → Returns exist but drawdowns are heavy — size/risk params need tightening.")
    else:
        print("  → 15m scalp shows usable edge vs buy&hold under honest assumptions.")

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
            "note": "First honest 15m run on ~13 months of OKX 15m history.",
        },
        "scalp_base": {k: v for k, v in base.items() if k not in ("equity_curve", "recent_trades", "config")},
        "scalp_wide": {k: v for k, v in wide.items() if k not in ("equity_curve", "recent_trades", "config")},
        "btc_buy_hold": bnh,
        "scalp_base_full": base,
        "scalp_wide_full": wide,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved → {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
