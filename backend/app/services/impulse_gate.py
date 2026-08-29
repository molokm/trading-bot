"""Impulse entry gate: quant multi-TF regime + optional LLM veto.

Pattern from successful AI crypto systems: mechanical signal stays primary;
gate may only BLOCK (never invent entries). Hard rules always win over LLM.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger("impulse_gate")


def quant_gate(
    side: str,
    coin: str,
    ind: dict,
    btc_ind: Optional[dict] = None,
    tf4h: Optional[dict] = None,
    *,
    require_btc_sma200: bool = True,
    btc_rsi_min: float = 50.0,
    coin_rsi_max: float = 88.0,
    require_4h_align: bool = True,
) -> tuple[bool, list[str]]:
    """Return (allow, reasons). reasons empty when allow=True."""
    reasons: list[str] = []
    side = (side or "long").lower()
    btc = btc_ind or {}
    ind = ind or {}
    tf4h = tf4h or {}

    if side == "long":
        if require_btc_sma200 and btc:
            if not bool(btc.get("above_sma200", True)):
                reasons.append("btc_below_sma200")
        if btc and btc_rsi_min > 0:
            br = float(btc.get("rsi") or 0)
            if br and br < btc_rsi_min:
                reasons.append(f"btc_rsi_low:{br:.0f}")
        # coin already extended — impulse often fails after vertical spike
        if coin_rsi_max < 100:
            cr = float(ind.get("rsi") or 0)
            if cr and cr > coin_rsi_max:
                reasons.append(f"coin_rsi_hot:{cr:.0f}")
        if require_4h_align and tf4h:
            if "ema_trend" in tf4h and not bool(tf4h.get("ema_trend")):
                reasons.append("4h_ema_bear")
            r4 = tf4h.get("rsi")
            if r4 is not None and float(r4) < 45:
                reasons.append(f"4h_rsi_weak:{float(r4):.0f}")
    elif side == "short":
        # Prefer risk-off: block shorts when BTC clearly bullish above SMA200
        if require_btc_sma200 and btc and bool(btc.get("above_sma200", False)):
            reasons.append("btc_above_sma200")
        if btc and btc_rsi_min > 0:
            br = float(btc.get("rsi") or 0)
            # mirror of long floor: shorts need soft bearish BTC RSI
            if br and br > (100.0 - float(btc_rsi_min) + 5.0):  # e.g. > 57 if min=48
                reasons.append(f"btc_rsi_high:{br:.0f}")
        cr = float(ind.get("rsi") or 0)
        # avoid shorting already-crashed RSI (exhaustion bounce risk)
        if cr and cr < 22:
            reasons.append(f"coin_rsi_washed:{cr:.0f}")
        if require_4h_align and tf4h:
            if "ema_trend" in tf4h and bool(tf4h.get("ema_trend")):
                reasons.append("4h_ema_bull")
            r4 = tf4h.get("rsi")
            if r4 is not None and float(r4) > 55:
                reasons.append(f"4h_rsi_strong:{float(r4):.0f}")

    return (len(reasons) == 0, reasons)


async def llm_veto(
    *,
    coin: str,
    side: str,
    strength: float,
    ind: dict,
    btc_ind: dict,
    quant_reasons_ok: bool,
    provider: Optional[str] = None,
) -> tuple[bool, str]:
    """Optional LLM veto. Returns (allow, reason). On error → allow (fail-open)."""
    # Shared flag: IMPULSE_LLM_VETO or MOMENTUM_LLM_VETO or LLM_VETO
    raw = (
        os.getenv("LLM_VETO")
        or os.getenv("MOMENTUM_LLM_VETO")
        or os.getenv("IMPULSE_LLM_VETO")
        or "1"
    )
    enabled = raw.strip().lower() not in ("0", "false", "no", "off")
    if not enabled:
        return True, "llm_veto_off"

    payload = {
        "task": "veto_only",
        "coin": coin,
        "side": side,
        "strength_roc": round(strength, 2),
        "coin_indicators": {
            "rsi": ind.get("rsi"),
            "roc": ind.get("roc"),
            "ema_trend": ind.get("ema_trend"),
            "vol_ratio": (
                round(float(ind["vol"]) / float(ind["avg_vol"]), 2)
                if ind.get("avg_vol") else None
            ),
        },
        "btc": {
            "rsi": btc_ind.get("rsi"),
            "above_sma200": btc_ind.get("above_sma200"),
            "roc": btc_ind.get("roc"),
        },
        "quant_passed": quant_reasons_ok,
        "rules": (
            "Daily crypto momentum/impulse. Side is given (long or short). Reply JSON only: "
            '{"allow": true|false, "reason": "short"}. '
            "BLOCK only on clear bear/chop/exhaustion; otherwise ALLOW."
        ),
    }
    try:
        from .ai_agent import _provider_chain, _call_provider, _extract_json

        prov = (provider or os.getenv("AI_LLM_PROVIDER") or "").strip().lower()
        if not prov:
            prov = "groq" if os.getenv("GROQ_API_KEY", "").strip() else "mock"
        if prov == "mock":
            # Mock: block only if BTC RSI very weak
            br = float(btc_ind.get("rsi") or 50)
            if side == "long" and br < 40:
                return False, "mock:btc_rsi_weak"
            return True, "mock:allow"

        user_msg = json.dumps(payload, ensure_ascii=False)[:2000]
        chain = _provider_chain(prov)
        raw = None
        for p in chain:
            if p == "mock":
                continue
            try:
                raw = await _call_provider(p, user_msg)
                break
            except Exception as e:
                log.warning("impulse llm_veto %s: %s", p, e)
                continue
        if not raw:
            return True, "llm_fail_open"
        parsed = _extract_json(raw) if isinstance(raw, str) else raw
        if not isinstance(parsed, dict):
            return True, "llm_parse_fail_open"
        allow = parsed.get("allow")
        if allow is None and "action" in parsed:
            allow = str(parsed.get("action")).lower() in ("allow", "open", "long", "buy")
        reason = str(parsed.get("reason") or parsed.get("thesis") or "")[:160]
        if allow is False or str(allow).lower() in ("false", "0", "block", "deny"):
            return False, reason or "llm_block"
        return True, reason or "llm_allow"
    except Exception as e:
        log.warning("impulse llm_veto error: %s", e)
        return True, f"llm_error_open:{e}"


def format_desk(
    *,
    btc_ind: dict,
    open_positions: list,
    gate_stats: dict,
    version: str = "",
) -> str:
    """Short morning/desk message for Telegram."""
    btc = btc_ind or {}
    regime = "BULL" if btc.get("above_sma200") else "BEAR/CHOP"
    lines = [
        f"📋 Impulse Desk {version}".strip(),
        f"BTC regime: {regime} | RSI {float(btc.get('rsi') or 0):.0f} | "
        f"ROC {float(btc.get('roc') or 0):+.1f}%",
        f"SMA200 filter: {'ON' if btc.get('above_sma200') is not None else 'n/a'}",
    ]
    if open_positions:
        lines.append("Open: " + ", ".join(
            f"{p.get('coin')}:{p.get('side')}" for p in open_positions[:6]
        ))
    else:
        lines.append("Open: none")
    if gate_stats:
        lines.append(
            f"Gate today: allow={gate_stats.get('allow', 0)} "
            f"block={gate_stats.get('block', 0)} "
            f"llm_block={gate_stats.get('llm_block', 0)}"
        )
    return "\n".join(lines)
