#!/usr/bin/env python3
"""Fetch spot + perp daily candles and 8h funding history for a multi-coin
universe (~3y), cached to JSON. Data source: OKX public endpoints.
Used by honest funding-carry backtest (long spot + short perp, collect funding).

No credentials required. Caches to fetch_carry_data_cache.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import httpx

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_carry_data_cache.json")

# Expandable universe: top perp coins on OKX. Add/remove freely.
COINS = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE",
    "AVAX", "LINK", "LTC", "DOT", "TRX",
]

DAYS_BACK = 1100          # ~3 years

CLIENT_TOTAL_TIMEOUT = 60


async def fetch_candles(client, inst_id: str, after: str | None = None, limit: int = 300):
    """1D candles. history-candles paginates back via `after`."""
    url = "https://www.okx.com/api/v5/market/history-candles"
    params = {"instId": inst_id, "bar": "1D", "limit": str(limit)}
    if after:
        params["after"] = after
    try:
        resp = await client.get(url, params=params)
        data = resp.json()
        if data.get("code") != "0":
            return []
        out = []
        for c in data.get("data", []):
            out.append({
                "ts": int(c[0]),
                "O": float(c[1]), "H": float(c[2]), "L": float(c[3]), "C": float(c[4]),
                "V": float(c[5]),
            })
        out.sort(key=lambda x: x["ts"])
        return out
    except Exception:
        return []


async def fetch_daily(client, inst_id: str, days_back: int = DAYS_BACK):
    all_candles = []
    after = None
    while len(all_candles) < days_back:
        batch = await fetch_candles(client, inst_id, after=after, limit=300)
        if not batch:
            break
        all_candles = batch + all_candles
        after = str(batch[0]["ts"])
        if len(batch) < 100:
            break
        await asyncio.sleep(0.15)
    uniq = {}
    for c in all_candles:
        uniq[c["ts"]] = c
    all_candles = [uniq[k] for k in sorted(uniq)]
    if len(all_candles) > days_back:
        all_candles = all_candles[-days_back:]
    return all_candles


async def fetch_funding_okx(client, inst_id: str, after: str | None = None, limit: int = 100):
    url = "https://www.okx.com/api/v5/public/funding-rate-history"
    params = {"instId": inst_id, "limit": str(limit)}
    if after:
        params["after"] = after
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


async def fetch_funding_raw(client, inst_id: str):
    """Fetch full 8h funding history (point in time, not aggregated)."""
    all_rates = []
    after = None
    while True:
        batch, ok = await fetch_funding_okx(client, inst_id, after=after, limit=100)
        if not batch:
            break
        all_rates = batch + all_rates
        after = str(batch[0]["ts"])
        if len(batch) < 100:
            break
        await asyncio.sleep(0.15)
    return all_rates


async def fetch_coin(client, coin: str):
    spot = await fetch_daily(client, f"{coin}-USDT")
    perp = await fetch_daily(client, f"{coin}-USDT-SWAP")
    funding = await fetch_funding_raw(client, f"{coin}-USDT-SWAP")
    return {"spot": spot, "perp": perp, "funding": funding}


async def main():
    start = time.time()
    if os.path.exists(CACHE_PATH):
        cached = json.load(open(CACHE_PATH))
        age_h = (time.time() - cached.get("fetched_at", 0)) / 3600
        if age_h < 24 and all(c in cached.get("data", {}) for c in COINS):
            print(f"Cache fresh ({age_h:.1f}h old, {len(COINS)} coins). Use force to refresh.", flush=True)
            return
    print(f"Fetching spot+perp+funding for {len(COINS)} coins (~{DAYS_BACK}d)...", flush=True)
    async with httpx.AsyncClient(timeout=CLIENT_TOTAL_TIMEOUT) as client:
        out = {}
        for coin in COINS:
            d = await fetch_coin(client, coin)
            out[coin] = d
            print(f"  {coin}: spot={len(d['spot'])} perp={len(d['perp'])} funding={len(d['funding'])}", flush=True)
            await asyncio.sleep(0.2)
    payload = {"fetched_at": time.time(), "data": out}
    with open(CACHE_PATH, "w") as f:
        json.dump(payload, f)
    print(f"Done in {time.time()-start:.0f}s -> {CACHE_PATH}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
