"""Runtime health guard for the AI engine.

Two responsibilities:

1. Beat registry — every critical stage of the engine stamps the moment it
   last succeeded. A silent stall then becomes measurable instead of guessed.
2. Evaluation — turn beats + open positions into concrete issues with a
   severity, so callers (watchdog, /api/health/selftest, preflight) can act.

The module is intentionally dependency-free (no DB, no client) so it is
trivially testable and safe to import from anywhere.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

_BEATS: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------- beat registry
def beat(name: str, **meta: Any) -> None:
    """Record that `name` just succeeded."""
    rec: Dict[str, Any] = {"ts": time.time()}
    if meta:
        rec.update(meta)
    _BEATS[name] = rec


def get_beat(name: str) -> Optional[Dict[str, Any]]:
    return _BEATS.get(name)


def beat_age_sec(name: str, now: Optional[float] = None) -> Optional[float]:
    rec = _BEATS.get(name)
    if not rec:
        return None
    return max(0.0, (now if now is not None else time.time()) - float(rec.get("ts") or 0))


def snapshot(now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    now = now if now is not None else time.time()
    out: Dict[str, Dict[str, Any]] = {}
    for name, rec in _BEATS.items():
        item = dict(rec)
        item["age_sec"] = round(max(0.0, now - float(rec.get("ts") or 0)), 1)
        item["ts_iso"] = datetime.fromtimestamp(float(rec.get("ts") or 0), timezone.utc).isoformat()
        item.pop("ts", None)
        out[name] = item
    return out


def reset() -> None:
    """Test helper."""
    _BEATS.clear()


# --------------------------------------------------------------------- issues
@dataclass
class Issue:
    code: str
    severity: str  # "warn" | "crit"
    detail: str
    coin: str = ""
    action: str = ""  # suggested remedy: "flatten" | "alert" | "restart"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _pos_get(pos: Any, key: str, default: Any = None) -> Any:
    if isinstance(pos, dict):
        return pos.get(key, default)
    return getattr(pos, key, default)


def flatten_decision(
    *,
    upl: float,
    pos_age_sec: Optional[float],
    manage_age_sec: Optional[float],
    flatten_after_sec: float,
    min_pos_age_sec: float,
    confirm_sec: float,
    bleeding_since: Optional[float],
    now: float,
) -> tuple[bool, str]:
    """Decide whether the in-trade guard may market-close ONE position.

    History: the guard used to flatten *every* losing position the moment the
    global manage-heartbeat was stale. On 28 Oct that market-closed NEAR (only
    9 minutes old, -557.72) together with ADA/BTC because the *shared* manage
    beat was old — not because those trades were unmanageable.

    A restart or a brief stall is not a reason to dump a healthy position, so
    flattening now requires all of:
      * the engine is unmanaged past ``flatten_after_sec``;
      * the position is older than ``min_pos_age_sec`` (a fresh entry has not
        had a chance to be managed yet);
      * the position has been bleeding (upl < 0) continuously for
        ``confirm_sec``.
    Returns (should_flatten, reason) where reason explains a hold.
    """
    if manage_age_sec is None or manage_age_sec <= flatten_after_sec:
        return False, "managed"
    if upl >= 0:
        return False, "not_losing"
    if pos_age_sec is not None and pos_age_sec < min_pos_age_sec:
        return False, "young"
    if bleeding_since is None or (now - bleeding_since) < confirm_sec:
        return False, "confirm"
    return True, "flatten"


def evaluate(
    *,
    positions: Iterable[Any],
    manage_age_sec: Optional[float],
    tick_age_sec: Optional[float],
    max_manage_age_sec: float = 300.0,
    max_tick_age_sec: float = 180.0,
    max_hold_hours: float = 18.0,
    hold_slack_sec: float = 900.0,
    now: Optional[float] = None,
) -> List[Issue]:
    """Detect situations where the engine is 'up' but not actually managing."""
    now = now if now is not None else time.time()
    positions = list(positions or [])
    issues: List[Issue] = []

    if positions:
        if tick_age_sec is None:
            issues.append(Issue("engine_tick_never", "crit",
                                "engine has not completed a single tick", action="restart"))
        elif tick_age_sec > max_tick_age_sec:
            issues.append(Issue("engine_tick_stale", "crit",
                                f"last tick {tick_age_sec:.0f}s ago (max {max_tick_age_sec:.0f}s)",
                                action="restart"))
        if manage_age_sec is None:
            issues.append(Issue("manage_never", "crit",
                                "positions are open but position management never ran",
                                action="flatten"))
        elif manage_age_sec > max_manage_age_sec:
            issues.append(Issue("manage_stale", "crit",
                                f"position management stalled for {manage_age_sec:.0f}s "
                                f"(max {max_manage_age_sec:.0f}s)", action="flatten"))

    for p in positions:
        coin = str(_pos_get(p, "coin", "") or "")
        side = str(_pos_get(p, "side", "") or "").lower()
        size = float(_pos_get(p, "size", 0) or 0)
        stop = float(_pos_get(p, "stop_price", 0) or 0)
        mark = float(_pos_get(p, "mark_px", 0) or 0)
        if size <= 0:
            continue
        if stop > 0 and mark > 0:
            breached = (side == "short" and stop < mark) or (side == "long" and stop > mark)
            if breached:
                issues.append(Issue(
                    "stop_wrong_side", "crit",
                    f"{side} stop {stop} already breached by mark {mark} — stop is inert",
                    coin=coin, action="flatten",
                ))
        opened_raw = _pos_get(p, "opened_at", None)
        if opened_raw:
            try:
                if isinstance(opened_raw, str):
                    opened = datetime.fromisoformat(opened_raw.replace("Z", "+00:00"))
                elif isinstance(opened_raw, (int, float)):
                    opened = datetime.fromtimestamp(float(opened_raw), timezone.utc)
                else:
                    opened = opened_raw
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                held_h = (now - opened.timestamp()) / 3600.0
                if held_h > max_hold_hours + hold_slack_sec / 3600.0:
                    issues.append(Issue(
                        "hold_overrun", "crit",
                        f"{side} held {held_h:.1f}h > max {max_hold_hours:.1f}h — "
                        f"time exit did not fire",
                        coin=coin, action="flatten",
                    ))
            except Exception:
                pass
    return issues


# ------------------------------------------------------------------- preflight
def preflight(
    *,
    has_demo_client: bool,
    live_as_primary: bool = False,
    indicators_ok: bool = True,
    llm_rate_limited: bool = False,
    llm_error: bool = False,
    db_ok: bool = True,
    tick_age_sec: Optional[float] = None,
    max_tick_age_sec: float = 180.0,
    open_positions: int = 0,
    max_positions: int = 1,
) -> List[str]:
    """Return blocking reasons for a NEW entry (empty list == clear to trade)."""
    reasons: List[str] = []
    if not has_demo_client:
        reasons.append("no_demo_client")
    if live_as_primary:
        reasons.append("live_client_as_primary")
    if not indicators_ok:
        reasons.append("indicators_missing")
    if llm_rate_limited:
        reasons.append("llm_rate_limited")
    if llm_error:
        reasons.append("llm_error")
    if not db_ok:
        reasons.append("db_unavailable")
    if tick_age_sec is not None and tick_age_sec > max_tick_age_sec:
        reasons.append("engine_tick_stale")
    if open_positions >= max_positions:
        reasons.append("max_positions")
    return reasons


def summarize(issues: Iterable[Issue]) -> Dict[str, Any]:
    items = [i.to_dict() for i in issues]
    return {
        "ok": not any(i["severity"] == "crit" for i in items),
        "crit": sum(1 for i in items if i["severity"] == "crit"),
        "warn": sum(1 for i in items if i["severity"] == "warn"),
        "issues": items,
    }
