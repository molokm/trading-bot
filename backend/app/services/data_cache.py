import json
import os
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional
import httpx

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
    params = {
        "symbol": symbol.replace("-", "").upper(),
        "interval": interval,
        "startTime": str(start_time),
        "limit": str(limit),
    }
    try:
        r = await client.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=30)
        if r.status_code != 200:
            return []
        raw = r.json()
        if not isinstance(raw, list):
            return []
        return [[str(int(k[0])), str(k[1]), str(k[2]), str(k[3]), str(k[4]),
                 str(k[5]), str(k[7]), "0", "0"] for k in raw]
    except Exception:
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
            if c_old <= start_ms and c_new >= end_ms:
                return [c for c in cached if start_ms <= int(c[0]) <= end_ms]

    async with httpx.AsyncClient(timeout=60.0) as client:
        okx_candles = []
        after = None
        while True:
            batch = await _fetch_okx(client, symbol, timeframe, after)
            if not batch:
                break
            okx_candles.extend(batch)
            oldest = int(batch[-1][0])
            if oldest <= start_ms:
                break
            after = str(oldest)

        if okx_candles:
            oldest_okx = int(okx_candles[-1][0])
            if oldest_okx <= start_ms:
                okx_candles.sort(key=lambda c: int(c[0]))
                _save_cache(symbol, timeframe, okx_candles, start_ms, end_ms)
                return [c for c in okx_candles if start_ms <= int(c[0]) <= end_ms]

        binance_interval = _TIMEFRAMES.get(timeframe, "1h")

        bar_ms = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000,
                  "30m": 1800000, "1H": 3600000, "2H": 7200000, "4H": 14400000,
                  "6H": 21600000, "12H": 43200000, "1D": 86400000}.get(timeframe, 3600000)
        page_ms = 1000 * bar_ms
        total_pages = (end_ms - start_ms + page_ms - 1) // page_ms

        sem = asyncio.Semaphore(10)

        async def _fetch_bn_page(page: int) -> list:
            cursor = start_ms + page * page_ms
            if cursor >= end_ms:
                return []
            async with sem:
                return await _fetch_binance(client, symbol, binance_interval, cursor) or []

        bn_results = await asyncio.gather(*[_fetch_bn_page(p) for p in range(total_pages)])
        bn_candles = [c for batch in bn_results for c in batch]

        if okx_candles:
            by_ts = {int(c[0]): c for c in bn_candles}
            for c in okx_candles:
                by_ts[int(c[0])] = c
            merged = sorted(by_ts.values(), key=lambda c: int(c[0]))
        elif bn_candles:
            merged = sorted(bn_candles, key=lambda c: int(c[0]))
        else:
            return []

        _save_cache(symbol, timeframe, merged, start_ms, end_ms)
        return [c for c in merged if start_ms <= int(c[0]) <= end_ms]
