"""LLM decision layer for AI Discretionary bot.

Providers (env AI_LLM_PROVIDER):
  - mock   : rule-based heuristic (no API key) — default for dry-run
  - groq   : free-tier friendly OpenAI-compatible API (GROQ_API_KEY)
  - openai : OpenAI or any compatible base URL (OPENAI_API_KEY, OPENAI_BASE_URL)
  - gemini : Google Gemini (GEMINI_API_KEY)

Always returns a validated dict decision; invalid/unsafe → hold.
"""
from __future__ import annotations

import json
import os
import re
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("ai_agent")

ALLOWED_ACTIONS = ("open", "close", "hold", "reduce")
ALLOWED_SIDES = ("long", "short")
ALLOWED_SYMBOLS = ("BTC", "ETH", "SOL")


SYSTEM_PROMPT = """You are a cautious crypto futures trading agent for OKX SWAP.
Universe: BTC, ETH, SOL only. Timeframe: 1H.
Account: ~$10,000, max leverage 3x, must avoid liquidation — stops must be tight
relative to leverage (prefer stop 2.5–5% from entry at 2–3x).
Reply with ONLY a single JSON object, no markdown:
{
  "action": "open"|"close"|"hold"|"reduce",
  "symbol": "BTC"|"ETH"|"SOL"|null,
  "side": "long"|"short"|null,
  "size_pct_equity": 0.0-0.15,
  "stop_pct": 0.015-0.05,
  "take_pct": 0.02-0.12,
  "confidence": 0.0-1.0,
  "reason": "short string"
}
Rules:
- Prefer hold when trend is unclear or confidence < 0.55.
- Never risk more than 15% of equity notional margin per new position.
- One clear thesis per decision. No multiple symbols in one reply.
- If a position is open and thesis is invalid, close or reduce.
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
    size_pct = _clip(size_pct, 0.0, 0.15)

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

    # Policy clamps
    if action == "open":
        if not symbol or not side or conf < 0.55 or size_pct < 0.02:
            action = "hold"
            reason = (reason + " | policy: open rejected").strip(" |")
    if action in ("close", "reduce") and symbol and symbol not in open_symbols:
        action = "hold"
        reason = (reason + " | policy: no open pos").strip(" |")
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


async def call_llm(snapshot: dict, provider: Optional[str] = None) -> dict:
    """Ask LLM (or mock) for a decision given market snapshot."""
    provider = (provider or os.getenv("AI_LLM_PROVIDER") or "").strip().lower()
    if not provider:
        # Auto: prefer Groq when key is present, else mock (free, no signup)
        provider = "groq" if os.getenv("GROQ_API_KEY", "").strip() else "mock"
    open_syms = [p.get("coin") for p in (snapshot.get("open_positions") or [])]

    user_payload = {
        "equity": snapshot.get("equity"),
        "capital": snapshot.get("capital"),
        "max_leverage": snapshot.get("max_leverage"),
        "max_positions": snapshot.get("max_positions"),
        "open_positions": snapshot.get("open_positions"),
        "indicators": snapshot.get("indicators"),
        "server_time": snapshot.get("server_time"),
    }
    user_msg = (
        "Market snapshot (JSON). Decide next action.\n"
        + json.dumps(user_payload, ensure_ascii=False)[:12000]
    )

    if provider == "mock" or not provider:
        return mock_decide(snapshot)

    try:
        if provider == "groq":
            raw = await _openai_compatible(
                api_key=os.getenv("GROQ_API_KEY", ""),
                base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
                model=os.getenv("AI_LLM_MODEL", "openai/gpt-oss-20b"),
                system=SYSTEM_PROMPT,
                user=user_msg,
            )
        elif provider == "openai":
            raw = await _openai_compatible(
                api_key=os.getenv("OPENAI_API_KEY", ""),
                base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                model=os.getenv("AI_LLM_MODEL", "gpt-4o-mini"),
                system=SYSTEM_PROMPT,
                user=user_msg,
            )
        elif provider == "gemini":
            raw = await _gemini(
                api_key=os.getenv("GEMINI_API_KEY", ""),
                model=os.getenv("AI_LLM_MODEL", "gemini-2.0-flash"),
                system=SYSTEM_PROMPT,
                user=user_msg,
            )
        else:
            log.warning("Unknown provider %s — mock", provider)
            return mock_decide(snapshot)
    except Exception as e:
        log.warning("LLM call failed: %s — mock fallback", e)
        d = mock_decide(snapshot)
        d["reason"] = f"llm_error:{e}; fallback: {d.get('reason')}"
        return d

    parsed = _extract_json(raw) if isinstance(raw, str) else raw
    if not parsed:
        d = mock_decide(snapshot)
        d["reason"] = f"parse_fail; fallback: {d.get('reason')}"
        return d
    return validate_decision(parsed, open_syms)


# Fallback chain when a Groq model id is deprecated / not on the account
_GROQ_MODEL_FALLBACKS = (
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
)


async def _openai_compatible(api_key: str, base_url: str, model: str,
                             system: str, user: str,
                             json_mode: bool = True) -> str:
    if not api_key:
        raise RuntimeError("missing API key")
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    candidates = [model]
    if "groq.com" in base_url:
        for m in _GROQ_MODEL_FALLBACKS:
            if m not in candidates:
                candidates.append(m)
    last_err = None
    async with httpx.AsyncClient(timeout=45.0) as client:
        for mid in candidates:
            body = {
                "model": mid,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            r = await client.post(url, headers=headers, json=body)
            if r.status_code >= 400 and json_mode and r.status_code in (400, 422):
                body.pop("response_format", None)
                r = await client.post(url, headers=headers, json=body)
            if r.status_code < 400:
                data = r.json()
                if mid != model:
                    log.warning("LLM model fallback: %s -> %s", model, mid)
                return data["choices"][0]["message"]["content"]
            last_err = f"LLM HTTP {r.status_code}: {r.text[:300]}"
            # only continue chain on model_not_found
            if "model_not_found" not in (r.text or "") and "does not exist" not in (r.text or ""):
                break
    raise RuntimeError(last_err or "LLM request failed")


async def _gemini(api_key: str, model: str, system: str, user: str) -> str:
    if not api_key:
        raise RuntimeError("missing GEMINI_API_KEY")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.2},
    }
    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.post(url, json=body)
        r.raise_for_status()
        data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def llm_status() -> dict:
    """Public-safe LLM config for /api/ai/status (no secrets)."""
    key = os.getenv("GROQ_API_KEY", "").strip()
    provider = (os.getenv("AI_LLM_PROVIDER") or "").strip().lower()
    if not provider:
        provider = "groq" if key else "mock"
    return {
        "provider": provider,
        "model": os.getenv("AI_LLM_MODEL") or (
            "openai/gpt-oss-20b" if provider == "groq" else
            "gpt-4o-mini" if provider == "openai" else
            "gemini-2.0-flash" if provider == "gemini" else "mock-heuristic"
        ),
        "groq_key_configured": bool(key),
        "execute": os.getenv("AI_EXECUTE", "0").strip().lower() in ("1", "true", "yes", "on"),
    }
