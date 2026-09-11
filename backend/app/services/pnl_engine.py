"""Single source of truth for dashboard / bot-card PnL.

Rules (locked):
1. Realized PnL comes ONLY from exchange_close_trades (OKX bills type=2, subType 5/6).
2. Epoch is fixed: 2026-09-01 00:00:00 UTC — nothing before counts.
3. Bot label from clOrdId only: ais* → Scale-In, ai* → Discretionary (longest prefix first).
   Untagged closes are excluded from strategy totals (not guessed).
4. Calendar periods use Europe/Moscow (day = calendar date, week = Mon 00:00 → now).
5. Unrealized = sum of OKX position upl for positions tagged to active AI bots (optional; 0 if unknown).
6. All cards (total / today / week / per_bot / bot panels) MUST use this result — no lifetime counters.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

PNL_EPOCH_ISO = "2026-09-01T00:00:00+00:00"
PNL_TZ = ZoneInfo("Europe/Moscow")

# Longest-prefix first
_CLORD_MAP = (
    ("ais", "AI Scale-In 1H"),
    ("ai", "AI Discretionary 1H"),
    ("rot", "Momentum"),
    ("momentum", "Momentum"),
    ("imp", "Impulse 1D"),
    ("val", "MACD+Donchian Validation"),
)

AI_ONLY_LABELS = ("AI Discretionary 1H", "AI Scale-In 1H")


def epoch_ms() -> int:
    return int(datetime.fromisoformat(PNL_EPOCH_ISO).timestamp() * 1000)


def label_from_clord(cl_ord_id: str) -> str:
    cl = (cl_ord_id or "").strip().lower()
    if not cl:
        return ""
    for pfx, label in _CLORD_MAP:
        if cl.startswith(pfx):
            return label
    return ""


def _parse_ts_ms(raw: Any) -> int:
    try:
        ts = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    if ts <= 0:
        return 0
    if ts < 10_000_000_000:  # seconds → ms
        ts *= 1000
    return ts


def _to_msk_date(ts_ms: int):
    t = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone(PNL_TZ)
    return t.date()


def aggregate_rows(
    rows: list[dict],
    *,
    ai_only: bool = True,
    now: Optional[datetime] = None,
) -> dict:
    """Aggregate exchange_close_trades rows into dashboard payload."""
    now = now or datetime.now(timezone.utc)
    now_msk = now.astimezone(PNL_TZ)
    today = now_msk.date()
    week_start = (now_msk - timedelta(days=now_msk.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start_d = week_start.date()
    ep = epoch_ms()

    per_bot: dict[str, float] = {k: 0.0 for k in AI_ONLY_LABELS} if ai_only else {}
    realized_1d = 0.0
    realized_week = 0.0
    realized_7d = 0.0
    realized_30d = 0.0
    total_fees = 0.0
    account_all = 0.0
    counted = 0
    skipped_before_epoch = 0
    skipped_untagged = 0
    skipped_other_bot = 0

    for r in rows or []:
        try:
            pnl = float(r.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
        ts_ms = _parse_ts_ms(r.get("close_ts"))
        if ts_ms and ts_ms < ep:
            skipped_before_epoch += 1
            continue

        cl = str(r.get("cl_ord_id") or "")
        bot = label_from_clord(cl) or (r.get("bot_label") or "").strip()
        # Prefer clOrdId retag over stored label
        if cl:
            tagged = label_from_clord(cl)
            if tagged:
                bot = tagged

        account_all += pnl
        try:
            total_fees += abs(float(r.get("fee") or 0))
        except (TypeError, ValueError):
            pass

        if not bot:
            skipped_untagged += 1
            continue

        if ai_only and bot not in AI_ONLY_LABELS:
            skipped_other_bot += 1
            continue

        per_bot[bot] = per_bot.get(bot, 0.0) + pnl
        counted += 1

        if not ts_ms:
            continue
        try:
            d = _to_msk_date(ts_ms)
        except (ValueError, OSError):
            continue
        if d == today:
            realized_1d += pnl
        if d >= week_start_d:
            realized_week += pnl
        age = (now - datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)).total_seconds()
        if age <= 604800:
            realized_7d += pnl
        if age <= 2592000:
            realized_30d += pnl

    # Ensure both AI keys always present for UI
    if ai_only:
        for k in AI_ONLY_LABELS:
            per_bot.setdefault(k, 0.0)
        total = sum(per_bot[k] for k in AI_ONLY_LABELS)
        active = list(AI_ONLY_LABELS)
    else:
        total = sum(per_bot.values())
        active = sorted(per_bot.keys())

    return {
        "total": round(total, 2),
        "account_total": round(account_all, 2),
        "1d": round(realized_1d, 2),
        "7d": round(realized_7d, 2),
        "30d": round(realized_30d, 2),
        "week": round(realized_week, 2),
        "week_basis": "calendar_week_msk_monday",
        "week_start": week_start.isoformat(),
        "7d_rolling": round(realized_7d, 2),
        "unrealized": 0.0,  # filled by caller
        "funding": 0.0,
        "funding_source": "none",
        "funding_bills": 0,
        "funding_scope": "account",
        "economic_approx": round(total, 2),
        "strategy_realized": round(total, 2),
        "source": "exchange_close_trades_v2",
        "pnl_tz": "Europe/Moscow",
        "fees": round(total_fees, 2),
        "fees_informational": True,
        "pnl_includes_fee": True,
        "fees_note": "OKX bill pnl is net of trading fees for closes.",
        "per_bot": {k: round(v, 2) for k, v in sorted(per_bot.items())},
        "per_bot_all": {k: round(v, 2) for k, v in sorted(per_bot.items())},
        "active_bots": active,
        "skipped_untagged": skipped_untagged,
        "skipped_other_bot": skipped_other_bot,
        "skipped_before_epoch": skipped_before_epoch,
        "trades_counted": counted,
        "pnl_epoch": PNL_EPOCH_ISO,
        "engine": "pnl_engine_v1",
    }


async def ensure_epoch(db) -> str:
    """Force epoch to 2026-09-01 (user-mandated recount start)."""
    try:
        await db.set_setting("pnl_epoch", PNL_EPOCH_ISO)
        await db.set_setting("pnl_epoch_marker", "manual_2026_09_01")
    except Exception as e:
        print(f"[pnl_engine] ensure_epoch: {e}", flush=True)
    return PNL_EPOCH_ISO


async def compute(
    db,
    *,
    account_mode: str = "demo",
    ai_only: bool = True,
    sync_fn: Optional[Callable] = None,
    reclassify_fn: Optional[Callable] = None,
) -> dict:
    """Full pipeline: optional sync → reclassify → aggregate since epoch."""
    await ensure_epoch(db)
    if sync_fn:
        try:
            await sync_fn()
        except Exception as e:
            print(f"[pnl_engine] sync: {e}", flush=True)
    if reclassify_fn:
        try:
            await reclassify_fn()
        except Exception as e:
            print(f"[pnl_engine] reclassify: {e}", flush=True)
    elif hasattr(db, "reclassify_exchange_bot_labels"):
        try:
            await db.reclassify_exchange_bot_labels()
        except Exception as e:
            print(f"[pnl_engine] reclassify: {e}", flush=True)

    ep = epoch_ms()
    rows = await db.get_exchange_pnl_timebucket(
        bot_label=None, account_mode=account_mode, epoch_ms=ep,
    )
    if not rows:
        rows = await db.get_exchange_pnl_timebucket(
            bot_label=None, account_mode=None, epoch_ms=ep,
        )
    out = aggregate_rows(rows or [], ai_only=ai_only)
    out["account_mode"] = account_mode
    print(
        f"[pnl_engine] mode={account_mode} total={out['total']} 1d={out['1d']} "
        f"week={out['week']} per_bot={out['per_bot']} trades={out['trades_counted']} "
        f"skip_untagged={out['skipped_untagged']} skip_pre_epoch={out['skipped_before_epoch']}",
        flush=True,
    )
    return out
