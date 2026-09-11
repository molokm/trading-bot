"""Single source of truth for dashboard / bot-card PnL.

STABILITY LOCK — do not reintroduce:
- lifetime_pnl / bot counters as dashboard source
- frontend ai_status_seed overwriting /api/pnl
- SQL filters that drop untagged or close_ts=0 rows before Python
- attributing untagged closes only when AI_ONLY (resolve_bot fallback)


Rules:
1. Realized PnL from exchange_close_trades (OKX close bills), optionally
   supplemented from DB trades when exchange is empty after epoch.
2. Epoch: 2026-09-01 00:00:00 UTC — closes before this are ignored.
3. Label priority: clOrdId (ais/ai/…) → stored bot_label → AI_ONLY fallback.
4. Calendar periods in Europe/Moscow.
5. All UI cards must use /api/pnl from this engine only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

PNL_EPOCH_ISO = "2026-09-01T00:00:00+00:00"
PNL_TZ = ZoneInfo("Europe/Moscow")

_CLORD_MAP = (
    ("ais", "AI Scale-In 1H"),
    ("ai", "AI Discretionary 1H"),
    ("rot", "Momentum"),
    ("momentum", "Momentum"),
    ("imp", "Impulse 1D"),
    ("val", "MACD+Donchian Validation"),
)

AI_ONLY_LABELS = ("AI Discretionary 1H", "AI Scale-In 1H")

_BOT_ID_MAP = {
    "ai_strategy": "AI Discretionary 1H",
    "ai_scale_strategy": "AI Scale-In 1H",
    "ai_discretionary": "AI Discretionary 1H",
}


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


def normalize_bot_label(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if s in AI_ONLY_LABELS:
        return s
    low = s.lower()
    if "scale" in low:
        return "AI Scale-In 1H"
    if "discretionary" in low or s in ("AI", "ai_strategy"):
        return "AI Discretionary 1H"
    if s in _BOT_ID_MAP:
        return _BOT_ID_MAP[s]
    return s


def _parse_ts_ms(raw: Any) -> int:
    try:
        ts = int(float(raw or 0))
    except (TypeError, ValueError):
        return 0
    if ts <= 0:
        return 0
    if ts < 10_000_000_000:
        ts *= 1000
    return ts


def _to_msk_date(ts_ms: int):
    t = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone(PNL_TZ)
    return t.date()


def resolve_bot(row: dict, *, ai_only: bool) -> str:
    """clOrdId → stored label → AI_ONLY fallback to Discretionary."""
    cl = str(row.get("cl_ord_id") or row.get("clOrdId") or "")
    tagged = label_from_clord(cl)
    if tagged:
        return tagged
    stored = normalize_bot_label(row.get("bot_label") or row.get("bot") or "")
    if stored in AI_ONLY_LABELS:
        return stored
    if stored and not ai_only:
        return stored
    if ai_only:
        # Showcase account: only AI bots trade — untagged closes still count
        return "AI Discretionary 1H"
    return ""


def aggregate_rows(
    rows: list[dict],
    *,
    ai_only: bool = True,
    now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    now_msk = now.astimezone(PNL_TZ)
    today = now_msk.date()
    week_start = (now_msk - timedelta(days=now_msk.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start_d = week_start.date()
    ep = epoch_ms()

    per_bot: dict[str, float] = {k: 0.0 for k in AI_ONLY_LABELS} if ai_only else {}
    realized_1d = realized_week = realized_7d = realized_30d = 0.0
    total_fees = account_all = 0.0
    counted = skipped_before_epoch = skipped_other = 0
    used_fallback_label = 0

    for r in rows or []:
        try:
            pnl = float(r.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
        if abs(pnl) < 1e-12:
            continue

        ts_ms = _parse_ts_ms(r.get("close_ts") or r.get("ts") or r.get("timestamp"))
        # Epoch filter only when we have a timestamp; ts=0 kept (legacy rows)
        if ts_ms and ts_ms < ep:
            skipped_before_epoch += 1
            continue

        bot = resolve_bot(r, ai_only=ai_only)
        if not label_from_clord(str(r.get("cl_ord_id") or "")) and bot:
            used_fallback_label += 1

        account_all += pnl
        try:
            total_fees += abs(float(r.get("fee") or 0))
        except (TypeError, ValueError):
            pass

        if not bot:
            skipped_other += 1
            continue
        if ai_only and bot not in AI_ONLY_LABELS:
            skipped_other += 1
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

    if ai_only:
        for k in AI_ONLY_LABELS:
            per_bot.setdefault(k, 0.0)
        total = sum(per_bot[k] for k in AI_ONLY_LABELS)
        active = list(AI_ONLY_LABELS)
    else:
        total = sum(per_bot.values())
        active = sorted(per_bot.keys())

    result = {
        "total": round(total, 2),
        "account_total": round(account_all, 2),
        "1d": round(realized_1d, 2),
        "7d": round(realized_7d, 2),
        "30d": round(realized_30d, 2),
        "week": round(realized_week, 2),
        "week_basis": "calendar_week_msk_monday",
        "week_start": week_start.isoformat(),
        "7d_rolling": round(realized_7d, 2),
        "unrealized": 0.0,
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
        "skipped_before_epoch": skipped_before_epoch,
        "skipped_other_bot": skipped_other,
        "used_fallback_label": used_fallback_label,
        "trades_counted": counted,
        "pnl_epoch": PNL_EPOCH_ISO,
        "engine": "pnl_engine_v2",
    }
    if ai_only:
        _s = sum(float(result["per_bot"].get(k, 0) or 0) for k in AI_ONLY_LABELS)
        if abs(_s - float(result["total"])) > 0.05:
            result["total"] = round(_s, 2)
            result["strategy_realized"] = round(_s, 2)
    return result



async def ensure_epoch(db) -> str:
    try:
        await db.set_setting("pnl_epoch", PNL_EPOCH_ISO)
        await db.set_setting("pnl_epoch_marker", "manual_2026_09_01")
    except Exception as e:
        print(f"[pnl_engine] ensure_epoch: {e}", flush=True)
    return PNL_EPOCH_ISO


async def _rows_from_db_trades(db, ai_only: bool = True) -> list[dict]:
    """Secondary source: closed rows in trades table (bot_id + pnl) since epoch."""
    out = []
    ep = epoch_ms()
    bot_ids = list(_BOT_ID_MAP.keys()) if ai_only else None
    try:
        if bot_ids and hasattr(db, "get_trades_multi_bot"):
            rows = await db.get_trades_multi_bot(bot_ids, limit=5000)
        elif bot_ids:
            rows = []
            for bid in bot_ids:
                rows.extend(await db.get_trades(bot_id=bid, limit=2000) or [])
        else:
            rows = await db.get_trades(limit=5000) or []
    except Exception as e:
        print(f"[pnl_engine] db trades: {e}", flush=True)
        return []

    for r in rows or []:
        try:
            pnl = float(r.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
        if abs(pnl) < 1e-12:
            continue
        # skip pure opens without realized pnl already handled
        state = str(r.get("state") or r.get("reason") or "").lower()
        if state in ("open", "add", "live"):
            continue
        ts_raw = r.get("timestamp") or r.get("created_at") or r.get("time") or 0
        ts_ms = _parse_ts_ms(ts_raw)
        if not ts_ms and isinstance(ts_raw, str) and ts_raw:
            try:
                ts_ms = int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                ts_ms = 0
        if ts_ms and ts_ms < ep:
            continue
        bid = str(r.get("bot_id") or "")
        label = _BOT_ID_MAP.get(bid) or normalize_bot_label(bid)
        if ai_only and label not in AI_ONLY_LABELS:
            continue
        out.append({
            "ord_id": str(r.get("ord_id") or r.get("id") or f"db-{bid}-{ts_ms}"),
            "inst_id": r.get("inst_id") or "",
            "cl_ord_id": r.get("cl_ord_id") or "",
            "bot_label": label,
            "pnl": pnl,
            "fee": abs(float(r.get("fee") or 0)),
            "close_ts": ts_ms,
        })
    return out


async def compute(
    db,
    *,
    account_mode: str = "demo",
    ai_only: bool = True,
    sync_fn: Optional[Callable] = None,
    reclassify_fn: Optional[Callable] = None,
) -> dict:
    await ensure_epoch(db)

    # Force a fresh exchange sync when possible
    if sync_fn:
        try:
            await sync_fn()
        except Exception as e:
            print(f"[pnl_engine] sync: {e}", flush=True)
    try:
        if reclassify_fn:
            await reclassify_fn()
        elif hasattr(db, "reclassify_exchange_bot_labels"):
            await db.reclassify_exchange_bot_labels()
    except Exception as e:
        print(f"[pnl_engine] reclassify: {e}", flush=True)

    # Load ALL rows (no SQL epoch / mode filter) — filter in Python
    rows = []
    try:
        rows = await db.get_exchange_pnl_timebucket(
            bot_label=None, account_mode=None, epoch_ms=0,
        ) or []
    except Exception as e:
        print(f"[pnl_engine] load exchange rows: {e}", flush=True)

    # Strict demo/live isolation — never mix modes on the dashboard
    mode = (account_mode or "").lower()
    if mode == "live":
        rows = [r for r in rows if str(r.get("account_mode") or "").lower() == "live"]
    elif mode == "demo":
        # legacy rows without account_mode count as demo
        rows = [
            r for r in rows
            if str(r.get("account_mode") or "").lower() in ("", "demo")
        ]

    out = aggregate_rows(rows, ai_only=ai_only)

    # If exchange produced nothing, fall back to DB trade log
    if out["trades_counted"] == 0 or abs(out["total"]) < 1e-9:
        db_rows = await _rows_from_db_trades(db, ai_only=ai_only)
        if db_rows:
            out2 = aggregate_rows(db_rows, ai_only=ai_only)
            out2["source"] = "db_trades_fallback"
            out2["engine"] = "pnl_engine_v2"
            print(
                f"[pnl_engine] FALLBACK db_trades total={out2['total']} "
                f"per_bot={out2['per_bot']} n={out2['trades_counted']}",
                flush=True,
            )
            out = out2
        else:
            print(
                f"[pnl_engine] EMPTY exchange_rows={len(rows)} total=0 "
                f"skip_pre={out.get('skipped_before_epoch')} "
                f"skip_other={out.get('skipped_other_bot')}",
                flush=True,
            )
    else:
        print(
            f"[pnl_engine] mode={account_mode} total={out['total']} 1d={out['1d']} "
            f"week={out['week']} per_bot={out['per_bot']} trades={out['trades_counted']} "
            f"fallback_labels={out.get('used_fallback_label')} "
            f"skip_pre={out.get('skipped_before_epoch')} rows_in={len(rows)}",
            flush=True,
        )

    out["account_mode"] = account_mode
    return out
