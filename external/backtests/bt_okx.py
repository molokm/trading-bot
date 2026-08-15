#!/usr/bin/env python3
"""Shared OKX data loading for Backtrader backtests.

Pulls real candles from the public OKX market APIs (no credentials needed),
paginating far enough back to cover multi-year backtests, then hands them to
Backtrader as PandasData feeds with an aligned common index.

Used by: backtrader_momentum_rotation.py, backtrader_impulse.py
"""

import asyncio
from datetime import timezone

import httpx
import pandas as pd
import backtrader as bt

OKX_MARKET_URL = "https://www.okx.com/api/v5/market/candles"
OKX_HISTORY_URL = "https://www.okx.com/api/v5/market/history-candles"

BAR_MAP = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
           "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H", "1d": "1D"}
TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
              "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440}


async def _fetch_page(client, url, params):
    resp = await client.get(url, params=params)
    return resp.json()


async def fetch_candles(inst_id: str, bar: str, total: int) -> pd.DataFrame:
    """Fetch up to `total` candles of `inst_id`/`bar` from OKX (newest first).

    `market/candles` covers the most recent ~1440 bars; older history is
    paginated from `market/history-candles` (100/page).
    """
    all_candles = []
    after = ""
    async with httpx.AsyncClient(timeout=30.0) as _client:
        while len(all_candles) < total:
            params = {"instId": inst_id, "bar": bar, "limit": "300"}
            if after:
                params["after"] = after
            data = await _fetch_page(_client, OKX_MARKET_URL, params)
            if data.get("code") != "0" or not data.get("data"):
                break
            candles = data["data"]
            all_candles.extend(candles)
            after = candles[-1][0]
            if len(candles) < 300:
                break
            await asyncio.sleep(0.1)

        while len(all_candles) < total:
            params = {"instId": inst_id, "bar": bar, "limit": "100", "after": after}
            data = await _fetch_page(_client, OKX_HISTORY_URL, params)
            if data.get("code") != "0" or not data.get("data"):
                break
            candles = data["data"]
            all_candles.extend(candles)
            after = candles[-1][0]
            if len(candles) < 100:
                break
            await asyncio.sleep(0.08)

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=[
        "ts", "Open", "High", "Low", "Close", "Volume", "VolCcy", "VolCcyQuote", "Confirm"
    ])
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
    df = df[["ts", "Open", "High", "Low", "Close", "Volume"]].astype(
        {"Open": float, "High": float, "Low": float, "Close": float, "Volume": float})
    df = df.drop_duplicates(subset="ts", keep="first")
    df = df.set_index("ts").sort_index()
    df.columns = [c.lower() for c in df.columns]  # Backtrader PandasData expects lowercase
    return df


def candles_needed(days: int, timeframe: str) -> int:
    return int(days * 24 * 60 / TF_MINUTES[timeframe]) + 5


def as_bt_feed(df: pd.DataFrame, name: str = None) -> bt.feeds.PandasData:
    """Wrap an OKX DataFrame (lowercase columns, DatetimeIndex) as a BT feed."""
    return bt.feeds.PandasData(dataname=df, datetime=None, name=name)


def align(dfs: dict[str, pd.DataFrame], start=None, end=None) -> dict[str, pd.DataFrame]:
    """Slice all frames to a common range so Backtrader bars stay in lockstep."""
    if not dfs:
        return dfs
    s = start or max(d.index[0] for d in dfs.values())
    e = end or min(d.index[-1] for d in dfs.values())
    return {k: d.loc[s:e] for k, d in dfs.items() if not d.empty}


async def load_universe(inst_ids: list[str], timeframe: str, days: int) -> dict[str, pd.DataFrame]:
    """Fetch N instruments over `days` (plus warmup footprint is the frame itself)."""
    bar = BAR_MAP[timeframe]
    total = candles_needed(days, timeframe)
    result = {}
    for inst in inst_ids:
        df = await fetch_candles(inst, bar, total)
        if df.empty:
            print(f"  !! нет данных по {inst}")
            continue
        result[inst] = df
        print(f"  {inst}: {len(df)} свечей ({df.index[0].date()} -> {df.index[-1].date()})")
    return result


def run_sync(coro):
    """Run an async fetch coroutine within a sync script."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()