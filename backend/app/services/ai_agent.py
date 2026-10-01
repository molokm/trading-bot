"""LLM decision layer for AI Discretionary bot.

Providers (env AI_LLM_PROVIDER):
  - mock   : rule-based heuristic (no API key) — default for dry-run
  - groq   : free-tier friendly OpenAI-compatible API (GROQ_API_KEY)
  - openai : OpenAI or any compatible base URL (OPENAI_API_KEY, OPENAI_BASE_URL)

Always returns a validated dict decision; invalid/unsafe → hold.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("ai_agent")

# ── provider rotation & cooldown ──────────────────────────────
_llm_cooldowns: dict[str, float] = {}  # provider -> expiry timestamp (time.time())
_last_provider_used: str | None = None  # last successfully used provider
PROVIDER_ROTATION_ORDER = ["groq", "openrouter", "openai"]
COOLDOWN_ON_RATE_LIMIT = 600  # 10 min cooldown on 429/rate-limit
COOLDOWN_ON_ERROR = 120       # 2 min cooldown on other errors
COOLDOWN_ON_NO_CREDIT = 3600  # 1h if provider has no balance

def is_provider_available(name: str) -> bool:
    """Check if a provider is not on cooldown."""
    import time
    exp = _llm_cooldowns.get(name, 0)
    return exp <= time.time()

def mark_provider_cooldown(name: str, seconds: float):
    """Put a provider on cooldown."""
    import time
    _llm_cooldowns[name] = time.time() + seconds
    log.warning("LLM provider %s on cooldown for %ds", name, int(seconds))

def clear_provider_cooldown(name: str):
    """Clear cooldown for a provider."""
    _llm_cooldowns.pop(name, None)

def get_provider_status() -> dict:
    """Return cooldown status of all known providers."""
    import time
    now = time.time()
    return {
        name: {
            "available": is_provider_available(name),
            "cooldown_remaining": max(0, round(_llm_cooldowns.get(name, 0) - now)),
        }
        for name in PROVIDER_ROTATION_ORDER
        if os.getenv(f"{name.upper()}_API_KEY", "").strip() or name == "mock"
    }

def next_available_provider() -> str | None:
    """Pick the next available provider in rotation order.
    If all are on cooldown, return the one with the shortest remaining cooldown."""
    import time
    now = time.time()
    best = None
    best_wait = float("inf")
    for name in PROVIDER_ROTATION_ORDER:
        exp = _llm_cooldowns.get(name, 0)
        if exp <= now:
            # Check if the provider has an API key configured
            if name == "mock":
                return name
            key_env = f"{name.upper()}_API_KEY"
            if os.getenv(key_env, "").strip():
                return name
        else:
            wait = exp - now
            if wait < best_wait:
                best_wait = wait
                best = name
    return best  # least-cooled-down (or None if no keys)

_ACTIVE_SYSTEM_PROMPT = None  # set per call_llm

ALLOWED_ACTIONS = ("open", "close", "hold", "reduce", "add")
ALLOWED_SIDES = ("long", "short")
ALLOWED_SYMBOLS = ("BTC", "ETH", "SOL", "XRP")  # v1.10 majors only

_DEPRECATED_GROQ_MODELS = {
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "mixtral-8x7b-32768",
}
# 120b hits free-tier TPD/RPM hard — pin to 20b unless AI_ALLOW_LARGE_MODEL=1
_GROQ_DEFAULT_MODEL = "openai/gpt-oss-20b"  # Groq native; was product default before Qwen pin
_GROQ_LARGE_MODELS = {
    "openai/gpt-oss-120b",
    "gpt-oss-120b",
}


def _resolve_groq_model(model: str | None) -> str:
    m = (model or "").strip() or _GROQ_DEFAULT_MODEL
    if (
        m in _DEPRECATED_GROQ_MODELS
        or "llama-3.1-8b" in m
        or m.startswith("llama-3.1-8b")
    ):
        return _GROQ_DEFAULT_MODEL
    allow_large = os.getenv("AI_ALLOW_LARGE_MODEL", "").strip().lower() in ("1", "true", "yes", "on")
    if m in _GROQ_LARGE_MODELS or m.endswith("gpt-oss-120b"):
        if not allow_large:
            return _GROQ_DEFAULT_MODEL
    # Prefer classic Groq gpt-oss-20b if env still points at Qwen pin (unless forced)
    force = os.getenv("AI_FORCE_MODEL", "").strip().lower() in ("1", "true", "yes", "on")
    if not force and (m.startswith("qwen/") or m in ("qwen3.8-27b", "qwen3.6-27b")):
        return _GROQ_DEFAULT_MODEL
    return m




SYSTEM_PROMPT_MANAGE = """You MANAGE an open OKX USDT-SWAP position (do not open a new coin).
Think like a desk: structure → momentum → risk → action.
Reply with ONE JSON object only (no markdown). Write thesis/reason FIRST — reason
through structure/momentum/risk before committing to action, not after:
{"thesis":"<=180 chars optional: structure+momentum+risk read, in that order",
"reason":"<=200 chars",
"confidence":0-1,
"action":"close|hold|reduce|add","symbol":"BTC|ETH|SOL|XRP","side":"long|short|null",
"size_pct":0.25-0.75}

Analysis checklist (use fields in snapshot):
- Structure: price vs EMA21/50/200, tf_4h.trend_up, BB position
- Momentum: ROC, MACD hist sign/change, RSI zone
- Trend strength: ADX, regime (bull/bear/chop)
- Risk: distance to stop/take in %, unrealized if present, funding_rate
- Context: reflection + daily_lessons + journal_tail (recent outcomes)

Rules:
1) Prefer close if trend flipped (EMA/ROC against side), ADX collapsed, or structure broke.
2) reduce if in profit but momentum fading; lock gains.
3) add ONLY if open_positions.can_add is true AND adverse move is moderate AND regime is not chop.
4) hold if trend intact, ADX supportive, stop not threatened.
5) In regime=chop prefer reduce/close over add; do not average into noise.
6) Cite >=2 concrete metrics in reason. Put deeper logic in thesis. DEFAULT=hold if unsure.
"""

SYSTEM_PROMPT = """You are an OKX USDT-SWAP discretionary desk. Analyze first, trade second.
Prefer candidates_allowed; avoid candidates_blocked. You receive a full quant snapshot — use it.

Reply with ONE JSON object only (no markdown). Write the analysis fields FIRST —
they are your scratchpad, use them to reason step by step BEFORE committing to
action/side/sizing. Do not decide the action mentally first and backfill thesis
to match it.
{"thesis":"<=220 chars: structure+momentum+strength+risk, in that order",
"reason":"<=200 chars: the >=2 concrete metrics that decided it",
"regime":"bull|bear|chop|unknown",
"confidence":0-1,
"action":"open|close|hold|reduce|add","symbol":"BTC|ETH|SOL|XRP|null","side":"long|short|null",
"size_pct_equity":0.03-0.12,"stop_pct":0.015-0.04,"take_pct":0.04-0.10}

How to analyze (write this into thesis/reason, in order):
A) Market structure per coin: close vs EMA21/50/200, BB location, tf_4h trend alignment
B) Momentum: ROC, MACD histogram sign, RSI (avoid extremes against the trade)
C) Strength: ADX, regime, vol_ratio (participation)
D) Risk context: ATR-based stop room, funding_rate bias, max_positions, adaptive preset
E) Self-reflection: journal_tail + daily_lessons + reflection — skip patterns that recently lost

Hard rules:
1) DEFAULT action is hold. Open only with clear multi-factor edge.
2) Never open if open_positions is non-empty (close/reduce/add existing first).
3) Prefer align_score >= 0.6 and regime matching side (bull→long, bear→short).
4) regime=chop → hold unless exceptional align (>=0.75) AND 4H agrees AND catalyst in thesis.
5) Require RR take_pct/stop_pct >= 1.6 and confidence >= adaptive.min_confidence (or 0.68).
6) If block_open true → hold for that coin.
7) reason must cite >=2 metrics; thesis may expand structure/momentum/risk narrative.
8) Respect adaptive.size_cap and daily_lessons RULE*.
9) Prefer BTC/ETH leadership consistent with the chosen side when trading alts.
10) Size down (near size_pct floor) when ADX is only moderate or regime mixed.

Worked examples (follow this reasoning pattern, not these exact numbers):

Example A — good open:
thesis: "BTC>EMA21>50>200, 4H confirms. ROC+1.8%, MACD hist rising 3 bars, RSI 61 (room
to 70). ADX 27 regime=bull. Funding flat, stop room 1.4xATR."
reason: "ADX27 trend-confirmed, align_long 0.74, MACD hist rising"
confidence: 0.74 -> action: open, side: long, size_pct_equity: 0.07

Example B — correct hold despite a tempting setup:
thesis: "ETH ROC +2.1% looks strong but 1H ADX only 14 (chop), 4H trend flat, RSI
already 68 near extreme against fresh longs. BB mid, no clear structure break."
reason: "ADX14<min, regime=chop, RSI stretched — no multi-factor edge"
confidence: 0.41 -> action: hold (DEFAULT wins: single-factor momentum is not enough)

Example C — avoid counter-trend chase:
thesis: "SOL ROC -3% but BTC regime=bull and btc_roc +0.9% (leadership against this
short). Align_short only 0.48. Funding -0.09% also against the short."
reason: "fighting BTC leadership + weak align_short — high false-signal risk"
confidence: 0.38 -> action: hold
"""

def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def validate_decision(raw: Any, open_symbols: Optional[list] = None) -> dict:
    """Normalize model output into a safe decision dict."""
    open_symbols = open_symbols or []
    if not isinstance(raw, dict):
        return {"action": "hold", "symbol": None, "side": None,
                "size_pct_equity": 0.0, "stop_pct": 0.03, "take_pct": 0.06,
                "confidence": 0.0, "reason": "invalid_json"}

    action = str(raw.get("action") or "hold").lower().strip()
    if action not in ALLOWED_ACTIONS:
        action = "hold"

    symbol = raw.get("symbol")
    if symbol is not None:
        symbol = str(symbol).upper().replace("-USDT-SWAP", "").replace("USDT", "")
        if symbol not in ALLOWED_SYMBOLS:
            symbol = None

    side = raw.get("side")
    if side is not None:
        side = str(side).lower()
        if side not in ALLOWED_SIDES:
            side = None

    try:
        size_pct = float(raw.get("size_pct_equity") or 0)
    except (TypeError, ValueError):
        size_pct = 0.0
    size_pct = _clip(size_pct, 0.0, 0.12)

    try:
        stop_pct = float(raw.get("stop_pct") or 0.03)
    except (TypeError, ValueError):
        stop_pct = 0.03
    stop_pct = _clip(stop_pct, 0.015, 0.05)

    try:
        take_pct = float(raw.get("take_pct") or 0.06)
    except (TypeError, ValueError):
        take_pct = 0.06
    take_pct = _clip(take_pct, 0.02, 0.12)

    try:
        conf = float(raw.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = _clip(conf, 0.0, 1.0)

    reason = str(raw.get("reason") or "")[:240]
    thesis = str(raw.get("thesis") or raw.get("analysis") or "")[:280]

    # Policy clamps
    if action == "open":
        if not symbol or not side or conf < 0.52 or size_pct < 0.02:
            action = "hold"
            reason = (reason + " | policy: open rejected").strip(" |")
    if action in ("close", "reduce", "add") and symbol and symbol not in open_symbols:
        action = "hold"
        reason = (reason + " | policy: no open pos").strip(" |")
    # add only allowed when snapshot enables scale-in
    if action == "add" and not (isinstance(raw, dict) and (raw.get("_scale_ok") or False)):
        # caller may pass scale via reason tag; allow if size_pct present and open
        if symbol not in open_symbols:
            action = "hold"
    if action == "hold":
        symbol = symbol if symbol in open_symbols else None
        side = None
        size_pct = 0.0

    return {
        "action": action,
        "symbol": symbol,
        "side": side,
        "size_pct_equity": round(size_pct, 4),
        "stop_pct": round(stop_pct, 4),
        "take_pct": round(take_pct, 4),
        "confidence": round(conf, 3),
        "reason": reason,
        "thesis": thesis,
    }


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    # strip ```json fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def mock_decide(snapshot: dict) -> dict:
    """Deterministic free heuristic: trade only clear 1H momentum + ADX."""
    open_pos = snapshot.get("open_positions") or []
    open_syms = [p.get("coin") for p in open_pos]

    # Manage open first: exit if ROC flipped hard against
    for p in open_pos:
        coin = p.get("coin")
        side = p.get("side")
        ind = (snapshot.get("indicators") or {}).get(coin) or {}
        roc = float(ind.get("roc_3") or 0)
        if side == "long" and roc < -1.5:
            return validate_decision({
                "action": "close", "symbol": coin, "side": side,
                "size_pct_equity": 0, "stop_pct": 0.03, "take_pct": 0.06,
                "confidence": 0.7, "reason": "mock: long invalidated by negative ROC3",
            }, open_syms)
        if side == "short" and roc > 1.5:
            return validate_decision({
                "action": "close", "symbol": coin, "side": side,
                "size_pct_equity": 0, "stop_pct": 0.03, "take_pct": 0.06,
                "confidence": 0.7, "reason": "mock: short invalidated by positive ROC3",
            }, open_syms)

    if len(open_pos) >= int(snapshot.get("max_positions") or 1):
        return validate_decision({
            "action": "hold", "confidence": 0.6,
            "reason": "mock: max positions reached",
        }, open_syms)

    # Rank candidates by |roc_3| with trend filter
    best = None
    best_score = 0.0
    for coin, ind in (snapshot.get("indicators") or {}).items():
        if coin in open_syms:
            continue
        roc = float(ind.get("roc_3") or 0)
        adx = float(ind.get("adx") or 0)
        ema_fast = float(ind.get("ema_fast") or 0)
        ema_slow = float(ind.get("ema_slow") or 0)
        close = float(ind.get("close") or 0)
        if adx < 18 or close <= 0:
            continue
        if roc > 1.2 and ema_fast >= ema_slow:
            score = roc * (adx / 25)
            if score > best_score:
                best_score = score
                best = (coin, "long", roc, adx)
        elif roc < -1.2 and ema_fast <= ema_slow:
            score = (-roc) * (adx / 25)
            if score > best_score:
                best_score = score
                best = (coin, "short", roc, adx)

    if best and best_score >= 1.0:
        coin, side, roc, adx = best
        return validate_decision({
            "action": "open", "symbol": coin, "side": side,
            "size_pct_equity": 0.08, "stop_pct": 0.03, "take_pct": 0.06,
            "confidence": min(0.85, 0.55 + best_score / 10),
            "reason": f"mock: {side} {coin} roc3={roc:.2f}% adx={adx:.1f}",
        }, open_syms)

    return validate_decision({
        "action": "hold", "confidence": 0.5,
        "reason": "mock: no clear 1H setup",
    }, open_syms)


async def _call_llm_once(snapshot: dict, provider: Optional[str] = None) -> dict:
    """Ask LLM (or mock) for a single decision given market snapshot.

    Internal — call_llm() wraps this with a self-consistency re-check for
    new-position opens (the highest-stakes decision point).
    """
    provider = (provider or os.getenv("AI_LLM_PROVIDER") or "").strip().lower()
    if not provider:
        # Auto: Groq first, then openrouter, else mock (BAI removed)
        if os.getenv("GROQ_API_KEY", "").strip():
            provider = "groq"
        elif os.getenv("OPENROUTER_API_KEY", "").strip():
            provider = "openrouter"
        else:
            provider = "mock"
    open_syms = [p.get("coin") for p in (snapshot.get("open_positions") or [])]
    global _ACTIVE_SYSTEM_PROMPT
    mode = (snapshot.get("decision_mode") or ("manage" if open_syms else "entry")).lower()
    _ACTIVE_SYSTEM_PROMPT = SYSTEM_PROMPT_MANAGE if (mode == "manage" and open_syms) else SYSTEM_PROMPT

    def _compact_ind(ind):
        if not isinstance(ind, dict):
            return {}
        keys = (
            "close", "ema21", "ema50", "ema200", "roc_3", "adx", "rsi",
            "macd_hist", "atr", "bb_mid", "bb_upper", "bb_lower", "vol_ratio",
            "regime", "align_long", "align_short", "funding_rate", "tf_4h",
            "bar_closes", "ema_slope",
        )
        return {k: ind.get(k) for k in keys if ind.get(k) is not None}

    inds_raw = snapshot.get("indicators") or {}
    inds_compact = {c: _compact_ind(v) for c, v in inds_raw.items()}

    user_payload = {
        "decision_mode": mode,
        "decision_trigger": snapshot.get("decision_trigger"),
        "fresh_bars": snapshot.get("fresh_bars"),
        "equity": snapshot.get("equity"),
        "capital": snapshot.get("capital"),
        "max_leverage": snapshot.get("max_leverage"),
        "max_positions": snapshot.get("max_positions"),
        "open_positions": snapshot.get("open_positions"),
        "quant": snapshot.get("quant"),
        "candidates_allowed": snapshot.get("candidates_allowed") or [],
        "candidates_blocked": snapshot.get("candidates_blocked") or [],
        "indicators": inds_compact,
        "analysis_hints": snapshot.get("analysis_hints") or [],
        "server_time": snapshot.get("server_time"),
        "policy": {
            "prefer": "trade_allowed_candidates",
            "min_confidence_open": (snapshot.get("adaptive") or {}).get(
                "min_confidence", 0.62),
            "min_rr": 1.6,
            "max_size_pct": (snapshot.get("adaptive") or {}).get("size_cap", 0.15),
            "adapt_preset": (snapshot.get("adaptive") or {}).get("preset"),
            "hint": snapshot.get("policy_hint") or "",
            "risk_note": "Prefer smaller size in chop/mixed; respect stop distance vs ATR",
        },
        "reflection": snapshot.get("reflection") or "",
        "daily_lessons": snapshot.get("daily_lessons") or [],
        "journal_tail": snapshot.get("journal_tail") or [],
        "adaptive": snapshot.get("adaptive"),
    }
    raw_json = json.dumps(user_payload, ensure_ascii=False)
    max_chars = int(os.getenv("AI_LLM_PAYLOAD_CHARS", "6000") or 6000)
    user_msg = (
        "Full quant snapshot for discretionary analysis. "
        "Follow checklist in system prompt; then decide.\n"
        + raw_json[:max_chars]
    )

    if provider == "mock" or not provider:
        return mock_decide(snapshot)

    # Provider rotation: start from requested, but skip cooldown providers
    preferred = provider
    chain = _provider_chain(provider)
    # Reorder: preferred first, then rotation order, skipping cooldown
    available = [p for p in chain if is_provider_available(p)]
    if not available:
        # All on cooldown — use the one with shortest remaining wait
        fallback = next_available_provider()
        available = [fallback] if fallback else chain[:1]
    # Ensure preferred is first if available
    if preferred in available:
        available = [preferred] + [p for p in available if p != preferred]
    chain = available

    errors = []
    raw = None
    used = None
    for prov in chain:
        try:
            raw = await _call_provider(prov, user_msg)
            used = prov
            clear_provider_cooldown(prov)
            break
        except Exception as e:
            msg = str(e)
            log.warning("LLM provider %s failed: %s", prov, msg)
            errors.append(f"{prov}:{msg[:120]}")
            # Rate limit / credit errors → long cooldown; other errors → short cooldown
            is_rate = "429" in msg or "rate" in msg.lower() or "limit" in msg.lower()
            is_credit = "balance" in msg.lower() or "credit" in msg.lower() or "insufficient" in msg.lower()
            mark_provider_cooldown(prov, COOLDOWN_ON_RATE_LIMIT if (is_rate or is_credit) else COOLDOWN_ON_ERROR)
            continue

    if raw is None:
        d = mock_decide(snapshot)
        d["reason"] = (
            "llm_error:" + (" | ".join(errors)[:240])
            + f"; fallback: {d.get('reason')}"
        )
        return d

    parsed = _extract_json(raw) if isinstance(raw, str) else raw
    if not parsed:
        d = mock_decide(snapshot)
        d["reason"] = f"parse_fail via {used}; fallback: {d.get('reason')}"
        return d
    dec = validate_decision(parsed, open_syms)
    if used:
        global _last_provider_used
        _last_provider_used = used
        dec["provider_used"] = used
        if used != provider:
            dec["reason"] = f"via_{used}: {dec.get('reason')}"
    return dec


async def call_llm(snapshot: dict, provider: Optional[str] = None) -> dict:
    """Ask LLM for a decision, with a self-consistency re-check on new opens.

    Free-tier budget: we only spend a second call at the highest-stakes moment
    (opening a brand-new position). Manage-mode decisions (hold/close/reduce/
    add on an existing position) and holds stay single-call, since they're
    already re-evaluated every llm_manage_interval_sec anyway — a second call
    there would burn quota for little benefit.
    """
    dec1 = await _call_llm_once(snapshot, provider)

    open_syms = [p.get("coin") for p in (snapshot.get("open_positions") or [])]
    mode = (snapshot.get("decision_mode") or ("manage" if open_syms else "entry")).lower()
    if mode != "entry" or dec1.get("action") != "open":
        return dec1

    dec2 = await _call_llm_once(snapshot, provider)

    agree = (
        dec2.get("action") == "open"
        and dec2.get("symbol") == dec1.get("symbol")
        and dec2.get("side") == dec1.get("side")
    )
    if not agree:
        dec1["action"] = "hold"
        dec1["size_pct_equity"] = 0.0
        dec1["reason"] = (
            f"self_consistency_fail: run1={dec1.get('symbol')}/{dec1.get('side')} "
            f"vs run2={dec2.get('symbol')}/{dec2.get('side')}/{dec2.get('action')}"
        )[:240]
        return dec1

    # Agreement: merge conservatively — average size/stop/take, take the LOWER
    # confidence of the two (don't let one lucky high-confidence run dominate).
    for k in ("size_pct_equity", "stop_pct", "take_pct"):
        try:
            v1, v2 = float(dec1.get(k) or 0), float(dec2.get(k) or 0)
            if v1 > 0 and v2 > 0:
                dec1[k] = round((v1 + v2) / 2, 5)
        except (TypeError, ValueError):
            pass
    try:
        dec1["confidence"] = round(min(float(dec1.get("confidence") or 0),
                                        float(dec2.get("confidence") or 0)), 3)
    except (TypeError, ValueError):
        pass
    dec1["reason"] = f"consensus(2/2): {dec1.get('reason', '')}"[:240]
    return dec1


def _provider_chain(primary: str) -> list[str]:
    """Primary + free/configured fallbacks when rate-limited or down."""
    primary = (primary or "mock").lower()
    # Prefer openrouter before plain openai (openai free models often 404)
    env_fb = [
        x.strip().lower()
        for x in (os.getenv("AI_LLM_FALLBACKS") or "openrouter,openai").split(",")
        if x.strip()
    ]
    chain = [primary]
    for p in env_fb:
        if p not in chain and p != "mock":
            chain.append(p)
    out = []
    for p in chain:
        if p == "groq" and os.getenv("GROQ_API_KEY", "").strip():
            out.append(p)
        elif p == "openrouter" and (
            os.getenv("OPENROUTER_API_KEY", "").strip()
            or (os.getenv("OPENAI_API_KEY", "").strip()
                and "openrouter" in (os.getenv("OPENAI_BASE_URL") or "").lower())
        ):
            out.append(p)
        elif p == "openai" and os.getenv("OPENAI_API_KEY", "").strip():
            # Skip openai if base is openrouter (handled above) or key is openrouter-only
            base = (os.getenv("OPENAI_BASE_URL") or "").lower()
            if "openrouter" in base:
                if "openrouter" not in out:
                    out.append("openrouter")
                continue
            out.append(p)
        elif p == primary and p not in out:
            out.append(p)
    if not out:
        out = ["mock"]
    return out


async def _call_provider(provider: str, user_msg: str) -> str:
    import time as _time
    provider = (provider or "").lower()
    if provider == "groq":
        if _time.time() < _rate_limit_until:
            wait_left = int(_rate_limit_until - _time.time())
            raise RuntimeError(f"rate-limit cooldown {wait_left}s")
        return await _openai_compatible(
            api_key=os.getenv("GROQ_API_KEY", ""),
            base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            model=_resolve_groq_model(os.getenv("AI_LLM_MODEL")),
            system=(_ACTIVE_SYSTEM_PROMPT or SYSTEM_PROMPT),
            user=user_msg,
        )
    if provider == "openai":
        # Never pass Groq-only model ids to OpenAI
        om = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        if "gpt-oss" in om or om.startswith("qwen/") or om.startswith("llama"):
            om = "gpt-4o-mini"
        return await _openai_compatible(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=om,
            system=(_ACTIVE_SYSTEM_PROMPT or SYSTEM_PROMPT),
            user=user_msg,
        )
    if provider == "openrouter":
        key = (
            os.getenv("OPENROUTER_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
        )
        # Prefer free-tier OpenRouter models
        om = (
            os.getenv("OPENROUTER_MODEL")
            or os.getenv("AI_OPENROUTER_MODEL")
            or "openrouter/free"
        )
        return await _openai_compatible(
            api_key=key,
            base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            model=om,
            system=(_ACTIVE_SYSTEM_PROMPT or SYSTEM_PROMPT),
            user=user_msg,
        )
    if provider == "bai":
        raise RuntimeError("BAI provider removed — use groq/openrouter/openai")
    raise RuntimeError(f"unknown or unconfigured provider {provider}")


# Fallback chain when a Groq model id is deprecated / not on the account
_GROQ_MODEL_FALLBACKS = (
    "openai/gpt-oss-20b",  # primary Groq model
    "qwen/qwen3.8-27b",
    "qwen/qwen3.6-27b",
    # never auto-chain 120b — burns org quota
)


_rate_limit_until: float = 0.0  # unix time; skip Groq until then after hard 429


async def _openai_compatible(api_key: str, base_url: str, model: str,
                             system: str, user: str,
                             json_mode: bool = True) -> str:
    if not api_key:
        raise RuntimeError("missing API key")
    global _rate_limit_until
    import time as _time
    if "groq.com" in base_url and _time.time() < _rate_limit_until:
        wait_left = int(_rate_limit_until - _time.time())
        raise RuntimeError(f"LLM rate-limit cooldown {wait_left}s — using fewer tokens")
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if "groq.com" in base_url:
        model = _resolve_groq_model(model)
    candidates = [model]
    if "groq.com" in base_url:
        for m in _GROQ_MODEL_FALLBACKS:
            m = _resolve_groq_model(m)
            if m not in candidates:
                candidates.append(m)
    last_err = None
    tpd_hit = False
    async with httpx.AsyncClient(timeout=45.0) as client:
        for i, mid in enumerate(candidates):
            body = {
                "model": mid,
                "temperature": 0.2,
                "max_tokens": 900,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            r = await client.post(url, headers=headers, json=body)
            if r.status_code == 200:
                data = r.json()
                if mid != model:
                    log.warning("LLM model fallback: %s -> %s", model, mid)
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                # Some reasoning models return the answer in reasoning_content
                # and an EMPTY content. Use it as fallback so the JSON decision
                # is still parseable.
                text = (msg.get("content") or "").strip()
                if not text:
                    text = (msg.get("reasoning_content") or "").strip()
                if not text:
                    # defensive: re-read raw message dump
                    text = str(msg)
                return text
            last_err = f"LLM HTTP {r.status_code}: {r.text[:300]}"
            txt = (r.text or "").lower()
            if r.status_code == 429:
                wait_s = 120.0
                try:
                    m = re.search(
                        r"try again in\s*(?:(\d+)m)?\s*([0-9.]+)s",
                        r.text or "",
                        re.I,
                    )
                    if m:
                        mins = int(m.group(1) or 0)
                        secs = float(m.group(2) or 0)
                        wait_s = max(45.0, mins * 60 + secs + 15)
                except Exception:
                    pass
                if "tokens per day" in txt or "tpd" in txt:
                    tpd_hit = True
                    wait_s = max(wait_s, 180.0)
                    # TPD is often per-model — try next model before org-wide cool
                    log.warning("LLM 429 TPD on %s — try next model", mid)
                    continue
                # RPM: short cool then try next model
                if i < len(candidates) - 1:
                    log.warning("LLM 429 RPM on %s — try next model", mid)
                    await asyncio.sleep(min(8.0, wait_s))
                    continue
                _rate_limit_until = _time.time() + min(wait_s, 600.0)
                log.warning("LLM 429 hard — cooldown %.0fs", min(wait_s, 600.0))
            if r.status_code == 404 or "model_not_found" in txt or "does not exist" in txt \
                    or "unavailable for free" in txt:
                log.warning("LLM model unavailable %s — next", mid)
                continue
            # other errors: stop this provider
            break
    if tpd_hit:
        # All models exhausted TPD — cool so we don't spin
        _rate_limit_until = _time.time() + 600.0
        log.warning("LLM all Groq models TPD — cooldown 600s")
    raise RuntimeError(last_err or "LLM request failed")


def llm_status() -> dict:
    """Public-safe LLM config for /api/ai/status (no secrets)."""
    import time as _time
    key = os.getenv("GROQ_API_KEY", "").strip()
    provider = (os.getenv("AI_LLM_PROVIDER") or "").strip().lower()
    if not provider:
        provider = "groq" if key else "mock"
    cool = max(0, int(_rate_limit_until - _time.time()))
    return {
        "provider": provider,
        "model": (
            _resolve_groq_model(os.getenv("AI_LLM_MODEL"))
            if provider == "groq"
            else (os.getenv("AI_LLM_MODEL") or (
                "gpt-4o-mini" if provider == "openai" else "mock-heuristic"
            ))
        ),
        "groq_key_configured": bool(key),
        "execute": os.getenv("AI_EXECUTE", "0").strip().lower() in ("1", "true", "yes", "on"),
        "rate_limit_cooldown_sec": cool,
        "rate_limited": cool > 0,
        "fallbacks": _provider_chain(provider),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
    }
