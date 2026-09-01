"""LLM decision layer for AI Discretionary bot.

Providers (env AI_LLM_PROVIDER):
  - mock   : rule-based heuristic (no API key) — default for dry-run
  - groq   : free-tier friendly OpenAI-compatible API (GROQ_API_KEY)
  - openai : OpenAI or any compatible base URL (OPENAI_API_KEY, OPENAI_BASE_URL)
  - gemini : Google Gemini (GEMINI_API_KEY)
  - bai    : api.b.ai (deepseek-v4-flash) — BAI_API_KEY + BAI_MODEL (default deepseek-v4-flash)

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

ALLOWED_ACTIONS = ("open", "close", "hold", "reduce")
ALLOWED_SIDES = ("long", "short")
ALLOWED_SYMBOLS = ("BTC", "ETH", "SOL", "XRP")

_DEPRECATED_GROQ_MODELS = {
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "mixtral-8x7b-32768",
}
# 120b hits free-tier TPD/RPM hard — pin to 20b unless AI_ALLOW_LARGE_MODEL=1
_GROQ_DEFAULT_MODEL = "qwen/qwen3.8-27b"  # free tier ~2M TPD vs gpt-oss ~200k
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
    # Prefer Qwen when env still points at low-TPD gpt-oss-20b unless forced
    force = os.getenv("AI_FORCE_MODEL", "").strip().lower() in ("1", "true", "yes", "on")
    if not force and m in ("openai/gpt-oss-20b", "gpt-oss-20b"):
        return _GROQ_DEFAULT_MODEL
    return m




SYSTEM_PROMPT = """You are an OKX USDT-SWAP discretionary desk (balanced-aggressive). Prefer trading candidates_allowed when align is solid; avoid candidates_blocked.
Reply with ONE JSON object only (no markdown):
{"action":"open|close|hold|reduce","symbol":"BTC|ETH|SOL|XRP|null","side":"long|short|null",
"size_pct_equity":0.03-0.12,"stop_pct":0.015-0.04,"take_pct":0.04-0.10,
"confidence":0-1,"regime":"bull|bear|chop|unknown","reason":"<=120 chars"}

Hard rules:
1) DEFAULT action is hold. Open only with clear edge.
2) Never open if open_positions is non-empty (close/reduce first).
3) Prefer setups where quant.align_score >= 0.6 and regime is bull (long) or bear (short).
4) In regime=chop → hold unless one side has strong quant alignment (>=0.55) and a clear catalyst in reason.
5) Require RR take_pct/stop_pct >= 1.8 and confidence >= 0.75 to open.
6) Use precomputed indicators (EMA21/50/200, RSI, MACD, ADX, ATR, BB, vol_ratio, tf_4h).
7) Short reason must cite 2+ concrete metrics (e.g. adx, ema200, rsi).
8) If quant.block_open is true → hold.
9) Respect adaptive.min_confidence and adaptive.size_cap; read reflection (recent trade outcomes) before opening.
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

    # Policy clamps
    if action == "open":
        if not symbol or not side or conf < 0.52 or size_pct < 0.02:
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
        # Auto: prefer BAI (api.b.ai deepseek) when key present, else Groq, else mock
        if os.getenv("BAI_API_KEY", "").strip():
            provider = "bai"
        else:
            provider = "groq" if os.getenv("GROQ_API_KEY", "").strip() else "mock"
    open_syms = [p.get("coin") for p in (snapshot.get("open_positions") or [])]

    user_payload = {
        "equity": snapshot.get("equity"),
        "capital": snapshot.get("capital"),
        "max_leverage": snapshot.get("max_leverage"),
        "max_positions": snapshot.get("max_positions"),
        "open_positions": snapshot.get("open_positions"),
        "quant": snapshot.get("quant"),
        "candidates_allowed": snapshot.get("candidates_allowed") or [],
        "candidates_blocked": snapshot.get("candidates_blocked") or [],
        "indicators": snapshot.get("indicators"),
        "server_time": snapshot.get("server_time"),
        "policy": {
            "prefer": "trade_allowed_candidates",
            "min_confidence_open": (snapshot.get("adaptive") or {}).get(
                "min_confidence", 0.62),
            "min_rr": 1.6,
            "max_size_pct": (snapshot.get("adaptive") or {}).get("size_cap", 0.15),
            "adapt_preset": (snapshot.get("adaptive") or {}).get("preset"),
            "hint": snapshot.get("policy_hint") or "",
        },
        "reflection": snapshot.get("reflection") or "",
        "adaptive": snapshot.get("adaptive"),
    }
    user_msg = (
        "Quant-preprocessed market snapshot + self-reflection. Decide next action.\n"
        + json.dumps(user_payload, ensure_ascii=False)[:2200]
    )

    if provider == "mock" or not provider:
        return mock_decide(snapshot)

    chain = _provider_chain(provider)
    errors = []
    raw = None
    used = None
    for prov in chain:
        try:
            raw = await _call_provider(prov, user_msg)
            used = prov
            break
        except Exception as e:
            msg = str(e)
            log.warning("LLM provider %s failed: %s", prov, msg)
            errors.append(f"{prov}:{msg[:120]}")
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
    if used and used != provider:
        dec["reason"] = f"via_{used}: {dec.get('reason')}"
        dec["provider_used"] = used
    return dec


def _provider_chain(primary: str) -> list[str]:
    """Primary + free/configured fallbacks when rate-limited or down."""
    primary = (primary or "mock").lower()
    # Prefer openrouter before plain openai (openai free models often 404)
    env_fb = [
        x.strip().lower()
        for x in (os.getenv("AI_LLM_FALLBACKS") or "openrouter,gemini,openai,bai").split(",")
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
        elif p == "gemini" and os.getenv("GEMINI_API_KEY", "").strip():
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
        elif p == "bai" and os.getenv("BAI_API_KEY", "").strip():
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
            system=SYSTEM_PROMPT,
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
            system=SYSTEM_PROMPT,
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
            system=SYSTEM_PROMPT,
            user=user_msg,
        )
    if provider == "gemini":
        return await _gemini(
            api_key=os.getenv("GEMINI_API_KEY", ""),
            model=os.getenv("GEMINI_MODEL") or "gemini-2.0-flash",
            system=SYSTEM_PROMPT,
            user=user_msg,
        )
    if provider == "bai":
        return await _openai_compatible(
            api_key=os.getenv("BAI_API_KEY", ""),
            base_url=os.getenv("BAI_BASE_URL", "https://api.b.ai/v1"),
            model=os.getenv("BAI_MODEL", "deepseek-v4-flash"),
            system=SYSTEM_PROMPT,
            user=user_msg,
        )
    raise RuntimeError(f"unknown or unconfigured provider {provider}")


# Fallback chain when a Groq model id is deprecated / not on the account
_GROQ_MODEL_FALLBACKS = (
    "qwen/qwen3.8-27b",   # highest free TPD on Groq (~2M)
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",  # last resort — low TPD (~200k)
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
                "max_tokens": 280,
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
                return data["choices"][0]["message"]["content"]
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
                "gpt-4o-mini" if provider == "openai" else
                "gemini-2.0-flash" if provider == "gemini" else "mock-heuristic"
            ))
        ),
        "groq_key_configured": bool(key),
        "execute": os.getenv("AI_EXECUTE", "0").strip().lower() in ("1", "true", "yes", "on"),
        "rate_limit_cooldown_sec": cool,
        "rate_limited": cool > 0,
        "fallbacks": _provider_chain(provider),
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
    }
