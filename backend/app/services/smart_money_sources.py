"""Multi-source smart-money discovery (OKX + open leaderboards + optional social).

Sources
-------
okx          — OKX Copy Trading public leaderboard (copyable on OKX)
hyperliquid  — Hyperliquid public stats leaderboard (observe / research)
social       — curated / env list + optional X handles (unverified ROI)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("smart_money_sources")

HL_LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

# Small curated open profiles (research / follow — not auto-verified exchange ROI)
_CURATED_SOCIAL: List[Dict[str, Any]] = [
    {
        "unique_code": "social:crypto_wizard",
        "alias": "Crypto Wizard (X)",
        "source": "social",
        "profile_url": "https://x.com/Wizard_Crypto",
        "roi_pct": 0.0,
        "pnl_usd": 0.0,
        "win_rate": 0.0,
        "max_drawdown": 0.0,
        "copy_traders": 0,
        "verified": False,
        "verify_score": 0,
        "verify_failures": ["ROI с соцсетей не подтверждён биржей"],
        "copyable": False,
        "note": "Публичный аналитик; для автокопирования нужен OKX lead-код",
    },
]


def _norm_profile(
    *,
    code: str,
    alias: str,
    source: str,
    roi_pct: float = 0.0,
    pnl_usd: float = 0.0,
    win_rate: float = 0.0,
    max_drawdown: float = 0.0,
    copy_traders: int = 0,
    aum: float = 0.0,
    verified: bool = False,
    verify_score: float = 0.0,
    verify_failures: Optional[List[str]] = None,
    profile_url: str = "",
    copyable: bool = False,
    note: str = "",
    extra: Optional[Dict] = None,
) -> Dict[str, Any]:
    out = {
        "unique_code": code,
        "alias": alias,
        "source": source,
        "inst_type": "SWAP" if source == "okx" else source.upper(),
        "roi_pct": round(float(roi_pct), 2),
        "pnl_usd": round(float(pnl_usd), 2),
        "win_rate": float(win_rate),
        "max_drawdown": float(max_drawdown),
        "copy_traders": int(copy_traders),
        "aum": round(float(aum), 2),
        "verified": bool(verified),
        "verify_score": float(verify_score),
        "verify_failures": list(verify_failures or []),
        "profile_url": profile_url,
        "copyable": bool(copyable),
        "note": note,
        "tracked": False,
        "current_positions": [],
    }
    if extra:
        out.update(extra)
    return out


async def fetch_hyperliquid(limit: int = 30, min_account: float = 50_000,
                            window: str = "month") -> List[Dict]:
    """Top Hyperliquid traders by ROI for window day|week|month|allTime."""
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.get(HL_LEADERBOARD_URL)
            r.raise_for_status()
            data = r.json()
        rows = data.get("leaderboardRows") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return []

        scored: List[tuple] = []
        for row in rows:
            try:
                av = float(row.get("accountValue") or 0)
                if av < min_account:
                    continue
                perfs = {
                    w[0]: w[1]
                    for w in (row.get("windowPerformances") or [])
                    if isinstance(w, (list, tuple)) and len(w) >= 2
                }
                block = perfs.get(window) or perfs.get("month") or perfs.get("allTime") or {}
                roi = float(block.get("roi") or 0) * 100.0  # fraction → %
                pnl = float(block.get("pnl") or 0)
                addr = (row.get("ethAddress") or "").lower()
                if not addr:
                    continue
                name = (row.get("displayName") or "").strip() or f"{addr[:6]}…{addr[-4:]}"
                scored.append((roi, pnl, av, addr, name, block))
            except Exception:
                continue

        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for roi, pnl, av, addr, name, block in scored[: max(1, int(limit))]:
            # Soft verify: positive ROI + meaningful account
            failures = []
            score = 40.0
            if roi < 5:
                failures.append(f"ROI {roi:.1f}% низкий за окно {window}")
            else:
                score += min(roi / 5.0, 3) * 15
            if av >= 100_000:
                score += 15
            verified = roi >= 10 and av >= 50_000 and not failures
            vlm = float(block.get("vlm") or 0)
            out.append(
                _norm_profile(
                    code=f"hl:{addr}",
                    alias=name,
                    source="hyperliquid",
                    roi_pct=roi,
                    pnl_usd=pnl,
                    aum=av,
                    verified=verified,
                    verify_score=min(100.0, score),
                    verify_failures=failures,
                    profile_url=f"https://app.hyperliquid.xyz/explorer/address/{addr}",
                    copyable=False,
                    note=f"Hyperliquid · окно {window} · on-chain PnL/ROI",
                    extra={
                        "eth_address": addr,
                        "window": window,
                        "volume_usd": round(vlm, 2),
                        "metric_label_wr": "Объём",
                        "metric_value_wr": vlm,
                        "metric_label_dd": "Депозит",
                        "metric_value_dd": av,
                        "metric_label_followers": "Окно",
                        "metric_value_followers": window,
                    },
                )
            )
        log.info("Hyperliquid: %d traders", len(out))
        return out
    except Exception as e:
        log.warning("Hyperliquid fetch failed: %s", e)
        return []


async def fetch_social() -> List[Dict]:
    """Curated social + optional JSON from SMART_MONEY_SOCIAL_JSON env."""
    out = [dict(x) for x in _CURATED_SOCIAL]
    raw = (os.getenv("SMART_MONEY_SOCIAL_JSON") or "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, list):
                for item in extra:
                    if not isinstance(item, dict):
                        continue
                    code = item.get("unique_code") or f"social:{(item.get('alias') or 'x')}"
                    out.append(
                        _norm_profile(
                            code=str(code),
                            alias=str(item.get("alias") or code),
                            source="social",
                            roi_pct=float(item.get("roi_pct") or 0),
                            pnl_usd=float(item.get("pnl_usd") or 0),
                            win_rate=float(item.get("win_rate") or 0),
                            max_drawdown=float(item.get("max_drawdown") or 0),
                            verified=False,
                            verify_score=float(item.get("verify_score") or 0),
                            verify_failures=["Соц/ручной источник — ROI не с биржи"],
                            profile_url=str(item.get("profile_url") or item.get("url") or ""),
                            copyable=False,
                            note=str(item.get("note") or "Социальный профиль"),
                        )
                    )
        except Exception as e:
            log.warning("SMART_MONEY_SOCIAL_JSON parse: %s", e)
    return out


async def fetch_all_external(
    limit_per_source: int = 25,
    sources: Optional[List[str]] = None,
) -> List[Dict]:
    """Fetch non-OKX sources in parallel."""
    want = set(s.lower() for s in (sources or ["hyperliquid", "social"]))
    tasks = []
    if "hyperliquid" in want or "hl" in want:
        tasks.append(fetch_hyperliquid(limit=limit_per_source))
    else:
        tasks.append(asyncio.sleep(0, result=[]))
    if "social" in want or "twitter" in want or "x" in want:
        tasks.append(fetch_social())
    else:
        tasks.append(asyncio.sleep(0, result=[]))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    merged: List[Dict] = []
    for r in results:
        if isinstance(r, list):
            merged.extend(r)
        elif isinstance(r, Exception):
            log.warning("source error: %s", r)
    return merged


# Simple TTL cache for heavy HL payload
_cache: Dict[str, Any] = {"ts": 0.0, "data": []}
_CACHE_TTL = 600.0


async def fetch_hyperliquid_cached(limit: int = 30, **kw) -> List[Dict]:
    now = time.time()
    if _cache["data"] and now - float(_cache["ts"]) < _CACHE_TTL:
        return list(_cache["data"])[:limit]
    data = await fetch_hyperliquid(limit=max(limit, 40), **kw)
    _cache["ts"] = now
    _cache["data"] = data
    return data[:limit]
