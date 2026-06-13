import json
import os
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional
import httpx

LOG = lambda *a: print(f"[data_cache] {' '.join(str(x) for x in a)}", flush=True)

CACHE_DIR = Path(__file__).parent.parent.parent / "backtests_data" / "candles"

OKX_BASE = "https://www.okx.com"

_BYBIT_INTERVALS = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15",
    "30m": "30", "1H": "60", "2H": "120", "4H": "240",
    "6H": "360", "12H": "720", "1D": "D",
}

BAR_MS = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000,
          "30m": 1800000, "1H": 3600000, "2H": 7200000, "4H": 14400000,
          "6H": 21600000, "12H": 43200000, "1D": 86400000}


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


async def _fetch_bybit(client: httpx.AsyncClient, symbol: str, interval: str,
                       start_time: int, limit: int = 1000) -> list:
    parts = symbol.split("-")
    bb_symbol = (parts[0] + parts[1]).upper()
    params = {
        "symbol": bb_symbol,
        "interval": interval,
        "from": str(start_time // 1000),
        "limit": str(limit),
    }
    try:
        r = await client.get("https://api.bybit.com/v5/market/kline",
                              params=params, timeout=5)
        if r.status_code != 200:
            LOG(f"Bybit {bb_symbol} status={r.status_code} body={r.text[:200]}")
            return []
        data = r.json()
        if data.get("retCode") != 0:
            LOG(f"Bybit {bb_symbol} retCode={data.get('retCode')} msg={data.get('retMsg','')}")
            return []
        raw = data.get("result", {}).get("list", [])
        if not isinstance(raw, list):
            return []
        result = []
        for k in raw:
            result.append([str(int(k[0])), k[1], k[2], k[3], k[4], k[5], k[6] if len(k) > 6 else "0",
                           "0", "0"])
        return result
    except Exception as e:
        LOG(f"Bybit exception: {type(e).__name__}: {e}")
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

        bybit_interval = _BYBIT_INTERVALS.get(timeframe, "60")
        LOG(f"Falling through to Bybit interval={bybit_interval}")

        page_ms = 1000 * BAR_MS.get(timeframe, 3600000)
        total_pages = (end_ms - start_ms + page_ms - 1) // page_ms
        LOG(f"Bybit pages: {total_pages} (page_ms={page_ms})")

        sem = asyncio.Semaphore(5)

        async def _fetch_bb_page(page: int) -> list:
            cursor = start_ms + page * page_ms
            if cursor >= end_ms:
                return []
            async with sem:
                return await _fetch_bybit(client, symbol, bybit_interval, cursor) or []

        tasks = []
        for p in range(total_pages):
            tasks.append(_fetch_bb_page(p))
            await asyncio.sleep(0.03)
        bb_results = await asyncio.gather(*tasks)
        bb_candles = [c for batch in bb_results for c in batch if batch]

        LOG(f"Bybit total candles: {len(bb_candles)}")

        if okx_candles:
            by_ts = {int(c[0]): c for c in bb_candles}
            for c in okx_candles:
                by_ts[int(c[0])] = c
            merged = sorted(by_ts.values(), key=lambda c: int(c[0]))
            LOG(f"Merged: {len(merged)} candles")
        elif bb_candles:
            merged = sorted(bb_candles, key=lambda c: int(c[0]))
            LOG(f"Merged: {len(merged)} candles (Bybit only)")
        else:
            LOG("No candles from OKX or Bybit!")
            return []

        _save_cache(symbol, timeframe, merged, start_ms, end_ms)
        result = [c for c in merged if start_ms <= int(c[0]) <= end_ms]
        LOG(f"Final candles: {len(result)}")
        return result
