import gc
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
BAR_MS = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000,
          "30m": 1800000, "1H": 3600000, "2H": 7200000, "4H": 14400000,
          "6H": 21600000, "12H": 43200000, "1D": 86400000}

_KUCOIN_INTERVAL = {
    "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min",
    "30m": "30min", "1H": "1hour", "2H": "2hour", "4H": "4hour",
    "6H": "6hour", "8H": "8hour", "12H": "12hour", "1D": "1day",
}

KUCOIN_PAGE = 1500


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


async def _fetch_kucoin(client: httpx.AsyncClient, symbol: str, interval: str,
                        start_sec: int, end_sec: int) -> list:
    try:
        r = await client.get("https://api.kucoin.com/api/v1/market/candles",
            params={"symbol": symbol, "type": interval,
                    "startAt": str(start_sec), "endAt": str(end_sec)},
            timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        if data.get("code") != "200000":
            return []
        raw = data.get("data", [])
        if not isinstance(raw, list):
            return []
        return [[str(int(k[0]) * 1000), k[1], k[2], k[3], k[4],
                 k[5], k[6] if len(k) > 6 else "0", "0", "0"] for k in raw]
    except Exception:
        return []


async def ensure_candles(symbol: str, timeframe: str,
                         start_date: str = None, end_date: str = None,
                         force_refresh: bool = False,
                         live_limit: int = 0,
                         max_candles: int = 80000) -> list:
    now_ms = int(datetime.now().timestamp() * 1000)

    if start_date:
        start_ms = int(datetime.fromisoformat(start_date).timestamp() * 1000)
    else:
        start_ms = now_ms - 365 * 24 * 3600 * 1000

    if end_date:
        end_ms = int(datetime.fromisoformat(end_date).timestamp() * 1000)
    else:
        end_ms = now_ms

    LOG(f"start={start_date or 'auto'} end={end_date or 'auto'} "
        f"range_days={(end_ms-start_ms)/86400000:.1f} "
        f"force={force_refresh} live={live_limit} max={max_candles} {symbol} {timeframe}")

    if live_limit > 0:
        async with httpx.AsyncClient(timeout=15.0) as client:
            batch = await _fetch_okx(client, symbol, timeframe)
            if not batch:
                return []
            batch.sort(key=lambda c: int(c[0]))
            return batch[-live_limit:]

    if not force_refresh:
        cached = _load_cache(symbol, timeframe)
        if cached and int(cached[0][0]) <= start_ms and int(cached[-1][0]) >= end_ms:
            LOG(f"Cache hit: {len(cached)}")
            return [c for c in cached if start_ms <= int(c[0]) <= end_ms]

    kucoin_tf = _KUCOIN_INTERVAL.get(timeframe, "5min")
    kucoin_symbol = symbol.replace("-SWAP", "").replace("-USD-SWAP", "").split("-DELIVERY")[0]

    async with httpx.AsyncClient(timeout=30.0) as kc_client:

        async def _fetch_page(page: int, bar_ms: int, sem) -> list:
            async with sem:
                cursor = start_ms + page * KUCOIN_PAGE * bar_ms
                if cursor >= end_ms:
                    return []
                end = min(cursor + KUCOIN_PAGE * bar_ms, end_ms)
                return await _fetch_kucoin(kc_client, kucoin_symbol, kucoin_tf, cursor // 1000, end // 1000) or []

        bar_ms = BAR_MS.get(timeframe, 300000)
        total_pages = (end_ms - start_ms + KUCOIN_PAGE * bar_ms - 1) // (KUCOIN_PAGE * bar_ms)
        max_pages = (max_candles // KUCOIN_PAGE) + 1
        if total_pages > max_pages:
            LOG(f"Clipping {total_pages} pages to {max_pages} (max_candles)")
            total_pages = max_pages

        LOG(f"KuCoin: {total_pages} pages of {KUCOIN_PAGE} candles")

        sem = asyncio.Semaphore(5)
        tasks = [_fetch_page(p, bar_ms, sem) for p in range(total_pages)]
        kc_results = await asyncio.gather(*tasks)
        candles = [c for batch in kc_results for c in batch if batch]

    if not candles:
        LOG("No candles from KuCoin")
        async with httpx.AsyncClient(timeout=15.0) as client:
            batch = await _fetch_okx(client, symbol, timeframe)
            if not batch:
                return []
            batch.sort(key=lambda c: int(c[0]))
            result = [c for c in batch if start_ms <= int(c[0]) <= end_ms]
            _save_cache(symbol, timeframe, result, start_ms, end_ms)
            return result

    candles.sort(key=lambda c: int(c[0]))

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            okx_batch = await _fetch_okx(client, symbol, timeframe)
            if okx_batch:
                LOG(f"OKX overlap: {len(okx_batch)} candles")
                by_ts = {int(c[0]): c for c in candles}
                for c in okx_batch:
                    by_ts[int(c[0])] = c
                candles = sorted(by_ts.values(), key=lambda c: int(c[0]))
                del by_ts
    except Exception:
        pass

    if len(candles) > max_candles:
        candles = candles[-max_candles:]
        LOG(f"Clipped to {max_candles}")

    gc.collect()
    _save_cache(symbol, timeframe, candles, start_ms, end_ms)
    result = [c for c in candles if start_ms <= int(c[0]) <= end_ms]
    LOG(f"Final: {len(result)} candles")
    return result
