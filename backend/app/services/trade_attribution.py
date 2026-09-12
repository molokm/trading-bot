"""Single source of truth for trade → strategy bot attribution.

Rules (priority high → low):
1. Explicit forced overrides (DB setting pnl_bot_overrides + built-ins)
2. Entry fill clOrdId prefix (rot/imp/ai/val/…) keyed by (inst_id, side)
3. Row bot_id / existing bot label if already a known strategy
4. Close clOrdId prefix (weaker — closer may differ from opener)

Also exposes helpers for active-bot filtering and JSON-safe maps.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

# Human labels used on dashboard /api/pnl
CLORD_PREFIX_TO_BOT = {
    "rot": "Momentum",
    "imp": "Impulse 1D",
    "ais": "AI Discretionary 1H",  # Scale-In retired
    "ai": "AI Discretionary 1H",
    "val": "MACD+Donchian Validation",
    "scl": "Order Book Scalp",
    "scalp": "Order Book Scalp",
    "vwap": "VWAP Mean Reversion",
    "sm": "Умные деньги",
}

BOT_ID_TO_LABEL = {
    "rotation_strategy": "Momentum",
    "momentum_strategy": "Momentum",
    "impulse_strategy": "Impulse 1D",
    "validation_strategy": "MACD+Donchian Validation",
    "ai_strategy": "AI Discretionary 1H",
    "ai_scale_strategy": "AI Discretionary 1H",  # retired
    "smart_money": "Умные деньги",
    "smart_money_mirror": "Умные деньги",
}

STRICT_BOTS = set(CLORD_PREFIX_TO_BOT.values()) | {
    "Momentum",
    "Impulse 1D",
    "MACD+Donchian Validation",
    "AI Discretionary 1H",
    "Order Book Scalp",
    "VWAP Mean Reversion",
    "Умные деньги",
}

# Scale-In retired — no forced overrides to Scale
BUILTIN_OVERRIDES: List[dict] = []


def pnl_timezone() -> ZoneInfo:
    name = (os.getenv("PNL_TZ") or "Europe/Moscow").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def is_scale_bot(label_or_id: str) -> bool:
    """Scale-In retired — always False for ownership logic."""
    return False


def is_discretionary_bot(label_or_id: str) -> bool:
    s = (label_or_id or "").strip().lower()
    return (
        "discretionary" in s
        or "scale" in s  # legacy Scale-In treated as AI
        or s in ("ai", "ai_strategy", "ai discretionary 1h", "ai discretionary", "ais", "ai_scale_strategy")
    )


def prefer_owner_label(a: str, b: str) -> str:
    return a or b or "AI Discretionary 1H"


def bot_from_clord(cl_ord_id: str) -> str:
    cid = (cl_ord_id or "").strip().lower()
    if not cid:
        return ""
    # Longest prefix first — "ais" must win over "ai"
    for prefix, label in sorted(CLORD_PREFIX_TO_BOT.items(), key=lambda x: -len(x[0])):
        if cid.startswith(prefix):
            return label
    return ""


def label_from_bot_id(bot_id: str) -> str:
    bid = (bot_id or "").strip()
    if not bid:
        return ""
    if bid in BOT_ID_TO_LABEL:
        return BOT_ID_TO_LABEL[bid]
    # already a human label?
    if bid in STRICT_BOTS:
        return bid
    return ""


def normalize_side(side: str, pos_side: str = "") -> str:
    ps = (pos_side or "").strip().lower()
    if ps in ("long", "short"):
        return ps
    s = (side or "").strip().lower()
    if s in ("sell", "short"):
        return "short"
    if s in ("buy", "long"):
        return "long"
    return "long"


def build_entry_owner_map(fills: Iterable[dict]) -> Dict[Tuple[str, str], str]:
    """Newest entry fill (subType 3/4) wins per (inst_id, side)."""
    out: Dict[Tuple[str, str], str] = {}
    rows = list(fills or [])
    for f in reversed(rows):
        sub = str(f.get("subType") or "")
        if sub not in ("3", "4"):
            continue
        cid = str(f.get("clOrdId") or "").strip()
        inst = f.get("instId") or f.get("inst_id") or ""
        if not inst or not cid:
            continue
        label = bot_from_clord(cid)
        if not label:
            continue
        fside = str(f.get("side") or "").lower()
        entry_side = "short" if fside == "sell" else ("long" if fside == "buy" else "")
        if not entry_side:
            continue
        key = (str(inst), entry_side)
        if key not in out:
            out[key] = label
    return out


def _trade_exit_time(t: dict) -> str:
    return str(t.get("exit_time") or t.get("time") or t.get("timestamp") or "")


def match_override(rule: dict, t: dict) -> bool:
    inst = str(rule.get("inst_id") or rule.get("inst") or "").strip()
    ti = str(t.get("inst_id") or t.get("symbol") or "").strip()
    if inst and ti:
        if ti != inst and inst not in ti and ti not in inst:
            coin_r = inst.split("-")[0].upper()
            coin_t = ti.split("-")[0].upper()
            if coin_r != coin_t:
                return False
    et = _trade_exit_time(t)
    pfx = str(rule.get("exit_time_prefix") or "")
    if pfx and pfx not in et:
        return False
    exit_date = str(rule.get("exit_date") or rule.get("date") or "")
    if exit_date and not pfx:
        # Accept ISO date in exit_time OR MSK display formats 11.09.26 / 11.09.2026
        ok_date = exit_date in et
        if not ok_date and len(exit_date) >= 10:
            y, m, d = exit_date[:10].split("-")
            alts = (f"{d}.{m}.{y}", f"{d}.{m}.{y[2:]}", f"{y}-{m}-{d}")
            ok_date = any(a in et for a in alts)
        if not ok_date:
            return False
    from_bots = rule.get("from_bots")
    if from_bots is not None:
        cur = str(t.get("bot") or t.get("bot_label") or "").strip()
        allowed = {str(x).strip() for x in from_bots}
        # empty current bot also allowed if "" in from_bots
        if cur not in allowed and not (cur == "" and "" in allowed):
            # Also retag if currently Discretionary when Scale target day-rule
            if cur and cur not in allowed:
                return False
    pside = str(rule.get("pos_side") or rule.get("side") or "").strip().lower()
    if pside:
        tps = normalize_side(str(t.get("side") or ""), str(t.get("pos_side") or ""))
        # Allow close-of-short (side=buy) when rule says short
        if tps and tps != pside:
            ts = str(t.get("side") or "").lower()
            if not (pside == "short" and ts in ("buy", "sell", "short")):
                if not (pside == "long" and ts in ("buy", "sell", "long")):
                    return False
    pnl_near = rule.get("pnl_near")
    if pnl_near is not None:
        try:
            if abs(float(t.get("pnl") or 0) - float(pnl_near)) > 45.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


async def load_overrides(db) -> List[dict]:
    rules = list(BUILTIN_OVERRIDES)
    if not db:
        return rules
    try:
        raw = await db.get_setting("pnl_bot_overrides")
        if not raw:
            return rules
        extra = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(extra, list):
            rules.extend(extra)
    except Exception:
        pass
    return rules


def apply_attribution(
    trades: List[dict],
    *,
    entry_owner: Optional[Dict[Tuple[str, str], str]] = None,
    overrides: Optional[List[dict]] = None,
) -> List[dict]:
    """Mutate/return trades with corrected bot labels."""
    entry_owner = entry_owner or {}
    overrides = overrides or []

    for t in trades:
        reason = str(t.get("reason") or "").lower()
        inst = str(t.get("inst_id") or t.get("symbol") or "").strip()
        pside = normalize_side(str(t.get("side") or ""), str(t.get("pos_side") or ""))
        t["pos_side"] = t.get("pos_side") or pside

        # 0) forced overrides FIRST (ops corrections always win)
        forced = False
        if reason in ("closed", "close", "partial", "filled", ""):
            for rule in overrides:
                if not match_override(rule, t):
                    continue
                to_bot = str(rule.get("to_bot") or "").strip()
                if to_bot:
                    prev = t.get("bot")
                    t["bot"] = to_bot
                    if "Scale" in to_bot:
                        t["bot_id"] = "ai_strategy"
                    elif "Discretionary" in to_bot:
                        t["bot_id"] = "ai_strategy"
                    t["_attr"] = "forced"
                    forced = True
                    if prev != to_bot:
                        print(
                            f"[attr] forced {prev!r}→{to_bot!r} {inst} pnl={t.get('pnl')} "
                            f"time={_trade_exit_time(t)[:19]}",
                            flush=True,
                        )
                break
        if forced:
            continue

        # Hard safety: entire 2026-09-11 session → Scale-In (Telegram opens by SCL)
        et = _trade_exit_time(t)
        if (
            "2026-09-11" in et
            or "11.09.26" in et
            or "11.09.2026" in et
        ) and reason in ("closed", "close", "partial", "filled", ""):
            if str(t.get("bot") or "") != "AI Discretionary 1H":
                t["bot"] = "AI Discretionary 1H"
                t["bot_id"] = "ai_strategy"
                t["_attr"] = "hard_day_2026_09_11"
                continue

        # Hard safety: ETH ≈ -414 always Scale-In
        try:
            pnl = float(t.get("pnl") if t.get("pnl") is not None else 1e18)
        except (TypeError, ValueError):
            pnl = 1e18
        if "ETH" in inst.upper() and abs(pnl - (-414.06)) < 12.0:
            t["bot"] = "AI Discretionary 1H"
            t["bot_id"] = "ai_strategy"
            t["_attr"] = "hard_eth_414"
            continue

        # 1) entry owner
        opener = entry_owner.get((inst, pside), "") or entry_owner.get(inst, "")
        if opener:
            cur = str(t.get("bot") or "")
            if not cur:
                t["bot"] = opener
                t["_attr"] = "entry_owner"

        # 2) close/row clOrdId only if still empty
        if not str(t.get("bot") or "").strip():
            cid = str(t.get("clOrdId") or t.get("cl_ord_id") or "")
            lab = bot_from_clord(cid)
            if lab:
                t["bot"] = lab
                t["_attr"] = "clord"

        # 3) bot_id
        if not str(t.get("bot") or "").strip():
            lab = label_from_bot_id(str(t.get("bot_id") or ""))
            if lab:
                t["bot"] = lab
                t["_attr"] = "bot_id"

    return trades


def is_calendar_today(ts: str, tz: Optional[ZoneInfo] = None) -> bool:
    """True if timestamp falls on calendar today in tz (default PNL_TZ)."""
    tz = tz or pnl_timezone()
    if not ts:
        return False
    try:
        if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
            ms = int(ts)
            if ms > 10_000_000_000:
                ms //= 1000
            dt = datetime.fromtimestamp(ms, tz=timezone.utc).astimezone(tz)
        else:
            raw = str(ts).replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(tz)
        now = datetime.now(tz)
        return dt.date() == now.date()
    except Exception:
        return False


def json_safe_keys(d: dict) -> dict:
    if not isinstance(d, dict):
        return {}
    out = {}
    for k, v in d.items():
        if isinstance(k, (list, tuple)):
            key = "|".join(str(x) for x in k)
        else:
            key = k if isinstance(k, (str, int, float, bool)) or k is None else str(k)
        out[key if key is not None else "null"] = v
    return out
