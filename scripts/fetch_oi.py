#!/usr/bin/env python3
"""Fetch OKX SWAP open-interest (OI) history for BTC/ETH/BNB/SOL, cached.

Data source: OKX /api/v5/aigc/mcp/oi-history (returns ~900 daily bars from
2023-12-31). Fields per bar:
  ts         - bar timestamp (ms)
  oiCcy      - open interest in base currency
  oiCont     - open interest in contracts
  oiDeltaPct - bar-over-bar OI change (%)
  oiDeltaUsd - bar-over-bar OI change (USD)
  oiUsd      - open interest (USD)

Usage:
  python scripts/fetch_oi.py            # use cache if fresh (<24h), else fetch
  python scripts/fetch_oi.py --refresh  # force refetch
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

COINS = ["BTC", "ETH", "BNB", "SOL"]
CACHE_PATH = os.path.join(os.path.dirname(__file__), "oi_cache.json")
CACHE_FRESH_H = 24
ENDPOINT = "https://www.okx.com/api/v5/aigc/mcp/oi-history"
MAX_PER_CALL = 500


def fetch_oi(inst_id: str, end_ts: int) -> list[dict]:
    """Fetch OI daily bars back to earliest availability, paging by ts.

    The endpoint returns at most 720 bars on a single backward scan, but
    re-querying from the oldest obtained ts keeps going further back, so
    we iterate until no new (older) rows arrive.
    """
    out = {}
    after = end_ts
    while True:
        resp = httpx.post(ENDPOINT, json={
            "instId": inst_id, "bar": "1D", "ts": str(after), "limit": str(MAX_PER_CALL),
        }, timeout=30)
        data = resp.json()
        if data.get("code") != "0":
            print(f"  WARN {inst_id}: {data.get('msg')}", flush=True)
            break
        rows = data.get("data", [{}])[0].get("rows", []) if data.get("data") else []
        if not rows:
            break
        new_rows = [r for r in rows if int(r["ts"]) not in out]
        if not new_rows:
            # nothing new even from the oldest point → history exhausted
            break
        for r in new_rows:
            out[int(r["ts"])] = r
        oldest_this = min(int(r["ts"]) for r in rows)
        after = oldest_this - 1
        time.sleep(0.2)
    bars = [out[k] for k in sorted(out)]
    print(f"  {inst_id}: {len(bars)} bars  "
          f"{time.strftime('%Y-%m-%d', time.gmtime(bars[0]['ts']/1000))} → "
          f"{time.strftime('%Y-%m-%d', time.gmtime(bars[-1]['ts']/1000))}", flush=True)
    return bars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    if not args.refresh and os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cached = json.load(f)
        age_h = (time.time() - cached.get("fetched_at", 0)) / 3600
        if age_h < CACHE_FRESH_H and all(c in cached.get("data", {}) for c in COINS):
            print(f"  Using cache ({age_h:.1f}h old)", flush=True)
            return cached["data"]

    print("  Fetching OKX SWAP OI history...", flush=True)
    data = {}
    for coin in COINS:
        inst = f"{coin}-USDT-SWAP"
        bars = fetch_oi(inst, int(time.time() * 1000))
        data[coin] = bars
        time.sleep(0.3)

    with open(CACHE_PATH, "w") as f:
        json.dump({"fetched_at": time.time(), "data": data}, f)
    print(f"  Saved → {CACHE_PATH}", flush=True)
    return data


if __name__ == "__main__":
    main()
