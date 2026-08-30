"""Lightweight Smart Money leaderboard — single OKX request, no enrichment."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("smart_money_light")

_CACHE: Dict[str, Any] = {"ts": 0.0, "key": "", "data": None}
_CACHE_TTL = 120.0
_LOCK: Optional[asyncio.Lock] = None


def _lock() -> asyncio.Lock:
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    return _LOCK


def _parse_ranks(resp: dict) -> List[dict]:
    if not resp:
        return []
    data = resp.get("data") or []
    if not data:
        return []
    traders = data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if "uniqueCode" not in data[0]:
            ranks = data[0].get("ranks")
            if isinstance(ranks, list):
                traders = ranks
            else:
                inner = data[0].get("data") or data[0].get("list") or []
                if isinstance(inner, list):
                    traders = inner
    out = []
    for i, t in enumerate(traders or [], 1):
        if not isinstance(t, dict):
            continue
        code = t.get("uniqueCode") or t.get("unique_code") or ""
        if not code:
            continue
        try:
            roi = float(t.get("pnlRatio") or t.get("roi") or t.get("yieldRatio") or 0)
            if abs(roi) < 1 and roi != 0:
                roi *= 100.0
        except (TypeError, ValueError):
            roi = 0.0
        try:
            pnl = float(t.get("pnl") or t.get("profit") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        try:
            wr = float(t.get("winRatio") or t.get("winRate") or 0)
            if wr > 1:
                wr = wr / 100.0
        except (TypeError, ValueError):
            wr = 0.0
        try:
            dd = float(t.get("drawdown") or t.get("maxDrawdown") or 0)
            if dd > 1:
                dd = dd / 100.0
        except (TypeError, ValueError):
            dd = 0.0
        try:
            copies = int(float(t.get("copyTraderNum") or t.get("copyTraderCount") or 0))
        except (TypeError, ValueError):
            copies = 0
        try:
            days = int(float(t.get("leadDays") or t.get("days") or 0))
        except (TypeError, ValueError):
            days = 0
        alias = t.get("nickName") or t.get("nickname") or t.get("alias") or code[:10]
        out.append({
            "rank": i,
            "unique_code": code,
            "alias": alias,
            "source": "okx",
            "roi_pct": round(roi, 2),
            "pnl_usd": round(pnl, 2),
            "win_rate": round(wr, 4),
            "max_drawdown": round(dd, 4),
            "copy_traders": copies,
            "lead_days": days,
            "period_label": f"{days}д ведения" if days else "OKX рейтинг",
            "total_trades": 0,
            "trades_label": "н/д",
            "verified": copies >= 5 and roi > 0,
            "verify_score": min(100.0, max(0.0, roi / 2 + copies)),
            "copyable": True,
            "profile_url": f"https://www.okx.com/copy-trading/account/{code}",
            "note": "OKX Copy (light)",
        })
    return out


async def discover_okx_light(
    okx_api,
    *,
    page: str = "1",
    limit: str = "20",
    sort_type: str = "pnl_ratio",
    min_roi_pct: float = 0.0,
) -> Dict[str, Any]:
    """One OKX leaderboard request. Cached. Never blocks >12s."""
    global _CACHE
    try:
        lim = max(1, min(30, int(limit)))
    except Exception:
        lim = 20
    key = f"{page}|{lim}|{sort_type}|{min_roi_pct}"
    now = time.time()
    if _CACHE.get("data") and _CACHE.get("key") == key and now - float(_CACHE.get("ts") or 0) < _CACHE_TTL:
        return _CACHE["data"]

    async with _lock():
        now = time.time()
        if _CACHE.get("data") and _CACHE.get("key") == key and now - float(_CACHE.get("ts") or 0) < _CACHE_TTL:
            return _CACHE["data"]

        traders: List[dict] = []
        err = None
        if not okx_api:
            err = "OKX API not configured"
        else:
            try:
                resp = await asyncio.wait_for(
                    okx_api.get_lead_traders(
                        sort_type=sort_type or "pnl_ratio",
                        inst_type="SWAP",
                        page=str(page or "1"),
                        limit=str(lim),
                    ),
                    timeout=12.0,
                )
                traders = _parse_ranks(resp if isinstance(resp, dict) else {})
                if min_roi_pct and min_roi_pct > 0:
                    traders = [t for t in traders if float(t.get("roi_pct") or 0) >= float(min_roi_pct)]
                for i, t in enumerate(traders, 1):
                    t["rank"] = i
            except Exception as e:
                err = str(e)
                log.warning("discover_okx_light: %s", e)
                if _CACHE.get("data"):
                    stale = dict(_CACHE["data"])
                    stale["stale"] = True
                    stale["error"] = err
                    return stale

        out = {
            "traders": traders,
            "total": len(traders),
            "sort": sort_type,
            "min_roi": float(min_roi_pct or 0),
            "sources": "okx",
            "mode": "light",
            "error": err,
        }
        if traders:
            _CACHE = {"ts": time.time(), "key": key, "data": {**out, "cached": True}}
        return out
