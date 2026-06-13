import json
import os
import sys
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional
import httpx

LOG = lambda *a: print(f"[data_cache] {' '.join(str(x) for x in a)}", flush=True)

CACHE_DIR = Path(__file__).parent.parent.parent / "backtests_data" / "candles"

OKX_BASE = "https://www.okx.com"
BINANCE_BASE = "https://api.binance.com"

_TIMEFRAMES = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1H": "1h", "2H": "2h", "4H": "4h",
    "6H": "6h", "12H": "12h", "1D": "1d", "1W": "1w", "1M": "1M",
}


def _cache_path(symbol: str, timeframe: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{symbol.replace('-', '_').upper()}_{timeframe}.json"


def _load_cache(symbol: str, timeframe: str) -> Optional[list]:
    p = _cache_path(symbol, timeframe)
    if p.exists():
        return json.loads(p.read_text("utf-8")).get("candles", [])
    return None


def _save_cache(symbol: str, timeframe: str, candles: list, start_ms: int, end_ms: int):
    p = _cache_path(symbol, timeframe)
    p.write_text(json.dumps({
        "symbol": symbol, "timeframe": timeframe,
        "start_ms": start_ms, "end_ms": end_ms,
        "count": len(candles), "candles": candles,
    }, ensure_ascii=False, default=str))


async def _fetch_okx(client: httpx.AsyncClient, inst_id: str, bar: str, after: str = None) -> list:
    params = {"instId": inst_id, "bar": bar, "limit": "300"}
    if after:
        params["after"] = after
    headers = {}
    if os.getenv("OKX_DEMO", "").lower() in ("1", "true"):
        headers["x-simulated-trading"] = "1"
    try:
        r = await client.get(f"{OKX_BASE}/api/v5/market/candles", params=params,
                              headers=headers, timeout=30)
        if r.status_code != 200:
            return []
        d = r.json()
        if d.get("code") != "0":
            return []
        return d.get("data", [])
    except Exception:
        return []


async def _fetch_binance(client: httpx.AsyncClient, symbol: str, interval: str,
                         start_time: int, limit: int = 1000) -> list:
    parts = symbol.split("-")
    bn_symbol = (parts[0] + parts[1]).upper()
    params = {
        "symbol": bn_symbol,
        "interval": interval,
        "startTime": str(start_time),
        "limit": str(limit),
    }
    try:
        r = await client.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=5)
        if r.status_code != 200:
            LOG(f"Binance {bn_symbol} status={r.status_code} body={r.text[:200]}")
            return []
        raw = r.json()
        if not isinstance(raw, list):
            LOG(f"Binance {bn_symbol} not a list: {raw}")
            return []
        return [[str(int(k[0])), str(k[1]), str(k[2]), str(k[3]), str(k[4]),
                 str(k[5]), str(k[7]), "0", "0"] for k in raw]
    except Exception as e:
        LOG(f"Binance {bn_symbol} exception: {type(e).__name__}: {e}")
        return []


async def ensure_candles(symbol: str, timeframe: str,
                         start_date: str = None, end_date: str = None,
                         force_refresh: bool = False,
                         live_limit: int = 0) -> list:
    now_ms = int(datetime.now().timestamp() * 1000)

    if start_date:
        start_ms = int(datetime.fromisoformat(start_date).timestamp() * 1000)
    else:
        start_ms = now_ms - 365 * 24 * 3600 * 1000

    if end_date:
        end_ms = int(datetime.fromisoformat(end_date).timestamp() * 1000)
    else:
        end_ms = now_ms

    LOG(f"Request start={start_date or 'auto'} end={end_date or 'auto'} "
        f"start_ms={start_ms} end_ms={end_ms} range_days={(end_ms-start_ms)/86400000:.1f} "
        f"force_refresh={force_refresh} live_limit={live_limit} symbol={symbol} tf={timeframe}")

    if live_limit > 0:
        async with httpx.AsyncClient(timeout=15.0) as client:
            batch = await _fetch_okx(client, symbol, timeframe)
            if not batch:
                return []
            batch.sort(key=lambda c: int(c[0]))
            return batch[-live_limit:]

    if not force_refresh:
        cached = _load_cache(symbol, timeframe)
        if cached:
            c_old = int(cached[0][0])
            c_new = int(cached[-1][0])
            LOG(f"Cache hit: {len(cached)} candles, range={c_old}..{c_new}")
            if c_old <= start_ms and c_new >= end_ms:
                LOG("Cache covers full range, returning cached")
                return [c for c in cached if start_ms <= int(c[0]) <= end_ms]
            else:
                LOG("Cache does NOT cover full range, fetching fresh")

    async with httpx.AsyncClient(timeout=60.0) as client:
        okx_candles = []
        after = None
        okx_batches = 0
        while True:
            batch = await _fetch_okx(client, symbol, timeframe, after)
            if not batch:
                break
            okx_candles.extend(batch)
            okx_batches += 1
            oldest = int(batch[-1][0])
            if oldest <= start_ms:
                break
            after = str(oldest)

        LOG(f"OKX: {okx_batches} batches, {len(okx_candles)} candles total")

        if okx_candles:
            oldest_okx = int(okx_candles[-1][0])
            LOG(f"OKX oldest={oldest_okx} start_ms={start_ms} covers_all={oldest_okx <= start_ms}")
            if oldest_okx <= start_ms:
                okx_candles.sort(key=lambda c: int(c[0]))
                _save_cache(symbol, timeframe, okx_candles, start_ms, end_ms)
                return [c for c in okx_candles if start_ms <= int(c[0]) <= end_ms]

        binance_interval = _TIMEFRAMES.get(timeframe, "1h")
        LOG(f"Falling through to Binance interval={binance_interval}")

        bar_ms = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000,
                  "30m": 1800000, "1H": 3600000, "2H": 7200000, "4H": 14400000,
                  "6H": 21600000, "12H": 43200000, "1D": 86400000}.get(timeframe, 3600000)
        page_ms = 1000 * bar_ms
        total_pages = (end_ms - start_ms + page_ms - 1) // page_ms
        LOG(f"Binance pages: {total_pages} (page_ms={page_ms})")

        sem = asyncio.Semaphore(5)

        async def _fetch_bn_page(page: int) -> list:
            cursor = start_ms + page * page_ms
            if cursor >= end_ms:
                return []
            async with sem:
                return await _fetch_binance(client, symbol, binance_interval, cursor) or []

        tasks = []
        for p in range(total_pages):
            tasks.append(_fetch_bn_page(p))
            await asyncio.sleep(0.03)
        bn_results = await asyncio.gather(*tasks)
        bn_candles = [c for batch in bn_results for c in batch if batch]

        LOG(f"Binance total candles: {len(bn_candles)}")

        if okx_candles:
            by_ts = {int(c[0]): c for c in bn_candles}
            for c in okx_candles:
                by_ts[int(c[0])] = c
            merged = sorted(by_ts.values(), key=lambda c: int(c[0]))
            LOG(f"Merged: {len(merged)} candles (OKX overlapped)")
        elif bn_candles:
            merged = sorted(bn_candles, key=lambda c: int(c[0]))
            LOG(f"Merged: {len(merged)} candles (Binance only)")
        else:
            LOG("No candles from OKX or Binance!")
            return []

        _save_cache(symbol, timeframe, merged, start_ms, end_ms)
        result = [c for c in merged if start_ms <= int(c[0]) <= end_ms]
        LOG(f"Final candles: {len(result)}")
        return result
