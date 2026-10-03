"""Single source of truth for dashboard / bot-card PnL.

STABILITY LOCK — do not reintroduce:
- lifetime_pnl / bot counters as dashboard source
- frontend ai_status_seed overwriting /api/pnl
- SQL filters that drop untagged or close_ts=0 rows before Python
- attributing untagged closes only when AI_ONLY (resolve_bot fallback)


Rules (unified filter — one set T for cards, history, bot KPI, TG):
1. Realized closes only (non-zero pnl), from app DB (exchange_close_trades + trades).
2. Epoch: 2026-09-01 00:00:00 UTC — closes before this are ignored.
3. Bot = AI Discretionary only (clOrdId ai* not ais*, or stored label).
4. account_mode matches context (demo XOR live).
5. Calendar periods in Europe/Moscow.
6. All UI surfaces must use the same eligible set T.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

PNL_EPOCH_ISO = "2026-09-01T00:00:00+00:00"  # product start — include full AI demo history
PNL_TZ = ZoneInfo("Europe/Moscow")

_CLORD_MAP = (
    # NOTE: "ais" (retired Scale-In) must NOT match via startswith("ai")
    # — handled explicitly in label_from_clord.
    ("ai", "AI Discretionary 1H"),
    ("rot", "Momentum"),
    ("momentum", "Momentum"),
    ("imp", "Impulse 1D"),
    ("val", "MACD+Donchian Validation"),
)

AI_ONLY_LABELS = ("AI Discretionary 1H",)

_BOT_ID_MAP = {
    "ai_strategy": "AI Discretionary 1H",
    "ai_scale_strategy": "",  # retired — do not attribute to AI
    "ai_discretionary": "AI Discretionary 1H",
    # LIVE mirror engine writes closes under <BOT_ID>_live (see ai_strategy._live_bot_id)
    "ai_strategy_live": "AI Discretionary 1H",
}


def epoch_ms() -> int:
    return int(datetime.fromisoformat(PNL_EPOCH_ISO).timestamp() * 1000)


def label_from_clord(cl_ord_id: str) -> str:
    """Map OKX clOrdId prefix → bot label.

    Critical: ``ais…`` (retired Scale-In) must NOT be attributed to AI.
    Plain ``startswith('ai')`` would false-positive on ``ais``.
    """
    cl = (cl_ord_id or "").strip().lower()
    if not cl:
        return ""
    # Retired Scale-In — exclude from AI Discretionary totals
    if cl.startswith("ais"):
        return ""
    # Longest known prefix first
    for pfx, label in sorted(_CLORD_MAP, key=lambda x: -len(x[0])):
        if cl.startswith(pfx):
            # Boundary: next char should not extend another token oddly;
            # ai123 / ai_ok / ai-… all ok.
            return label
    return ""


def normalize_bot_label(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if s in AI_ONLY_LABELS:
        return s
    low = s.lower()
    if "scale" in low or s in ("AI Scale-In 1H", "ai_scale_strategy"):
        return ""  # retired — do not fold into AI totals
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
    """Attribute close → bot.

    Rules (AI_ONLY):
      - clOrdId ``ais*`` → never count (retired Scale-In)
      - clOrdId ``ai*``  → AI Discretionary
      - else trust stored bot_label / bot_id when it is AI Discretionary
        (covers closes without clOrdId and recovered labels)
    """
    cl = str(row.get("cl_ord_id") or row.get("clOrdId") or "").strip().lower()
    if cl.startswith("ais"):
        return ""
    tagged = label_from_clord(cl)
    if tagged:
        return tagged
    stored = normalize_bot_label(row.get("bot_label") or row.get("bot") or "")
    if stored in AI_ONLY_LABELS:
        return stored
    if stored and not ai_only:
        return stored
    bid = str(row.get("bot_id") or "")
    mapped = _BOT_ID_MAP.get(bid) or ""
    if mapped in AI_ONLY_LABELS:
        return mapped
    if mapped and not ai_only:
        return mapped
    return ""



def normalize_close_row(r: dict) -> Optional[dict]:
    """Normalize a DB/API close row into the canonical shape used by aggregate_rows.

    Returns None if the row has no usable pnl.
    """
    if not isinstance(r, dict):
        return None
    try:
        pnl = float(
            r.get("realized_pnl")
            if r.get("realized_pnl") not in (None, "")
            else (r.get("pnl") if r.get("pnl") not in (None, "") else 0)
        )
    except (TypeError, ValueError):
        return None
    if abs(pnl) < 1e-12:
        return None
    ts_ms = _parse_ts_ms(
        r.get("close_time_ms")
        or r.get("close_ts")
        or r.get("ts")
        or r.get("timestamp")
        or r.get("time")
    )
    if not ts_ms:
        for k in ("closed_at", "time", "timestamp"):
            v = r.get(k)
            if not v or isinstance(v, (int, float)):
                continue
            try:
                ts_ms = int(
                    datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() * 1000
                )
                break
            except Exception:
                continue
    mode = str(r.get("account_mode") or r.get("mode") or "demo").strip().lower()
    if mode not in ("demo", "live"):
        mode = "demo"
    inst = str(r.get("inst_id") or r.get("instId") or r.get("symbol") or r.get("inst") or r.get("coin") or "")
    side = str(r.get("side") or r.get("posSide") or r.get("pos_side") or "").lower()
    return {
        "pnl": pnl,
        "fee": abs(float(r.get("fee") or 0) or 0),
        "close_ts": ts_ms,
        "ts": ts_ms,
        "cl_ord_id": str(r.get("cl_ord_id") or r.get("clOrdId") or "").strip(),
        "bot_label": str(r.get("bot_label") or r.get("bot") or r.get("strategy_name") or "").strip(),
        "bot_id": str(r.get("bot_id") or "").strip(),
        "inst_id": inst,
        "ord_id": str(r.get("ord_id") or r.get("ordId") or "").strip(),
        "account_mode": mode,
        "side": side,
    }


def pnl_eligible(
    row: dict,
    *,
    mode: str = "demo",
    ai_only: bool = True,
    require_ts: bool = True,
) -> tuple[bool, str]:
    """Unified membership test for set T (cards + history + KPI).

    Returns (ok, reason). reason is empty when ok.
    """
    if not isinstance(row, dict):
        return False, "not_dict"
    try:
        pnl = float(row.get("pnl") or 0)
    except (TypeError, ValueError):
        return False, "bad_pnl"
    if abs(pnl) < 1e-12:
        return False, "zero_pnl"
    ts_ms = _parse_ts_ms(row.get("close_ts") or row.get("ts") or row.get("timestamp"))
    ep = epoch_ms()
    if require_ts and (not ts_ms or ts_ms < ep):
        return False, "before_epoch_or_no_ts"
    row_mode = str(row.get("account_mode") or "demo").strip().lower()
    if row_mode not in ("demo", "live"):
        row_mode = "demo"
    want = (mode or "demo").strip().lower()
    if want in ("demo", "live") and row_mode != want:
        return False, f"mode:{row_mode}!={want}"
    bot = resolve_bot(row, ai_only=ai_only)
    if ai_only and bot not in AI_ONLY_LABELS:
        return False, "not_ai"
    if not bot and ai_only:
        return False, "no_bot"
    return True, ""


def filter_pnl_rows(
    rows: list[dict],
    *,
    mode: str = "demo",
    ai_only: bool = True,
) -> tuple[list[dict], dict]:
    """Normalize + filter to set T. Returns (eligible_rows, diagnostics)."""
    out: list[dict] = []
    excluded_n = 0
    excluded_pnl = 0.0
    reasons: dict[str, int] = {}
    seen_oid: set[str] = set()
    for raw in rows or []:
        norm = normalize_close_row(raw) if "close_ts" not in (raw or {}) or "pnl" not in (raw or {}) else None
        row = norm or (dict(raw) if isinstance(raw, dict) else None)
        if not row:
            excluded_n += 1
            reasons["normalize_fail"] = reasons.get("normalize_fail", 0) + 1
            continue
        # If already canonical, still re-normalize numbers
        if norm is None:
            n2 = normalize_close_row(raw)
            if n2:
                row = n2
        ok, reason = pnl_eligible(row, mode=mode, ai_only=ai_only)
        if not ok:
            excluded_n += 1
            try:
                excluded_pnl += float(row.get("pnl") or 0)
            except (TypeError, ValueError):
                pass
            reasons[reason or "other"] = reasons.get(reason or "other", 0) + 1
            continue
        oid = str(row.get("ord_id") or "")
        if oid and oid in seen_oid:
            excluded_n += 1
            reasons["dup_ord"] = reasons.get("dup_ord", 0) + 1
            continue
        if oid:
            seen_oid.add(oid)
        bot = resolve_bot(row, ai_only=ai_only) or "AI Discretionary 1H"
        row["bot"] = bot
        row["bot_label"] = bot
        out.append(row)
    diag = {
        "eligible_n": len(out),
        "excluded_n": excluded_n,
        "excluded_pnl": round(excluded_pnl, 2),
        "exclude_reasons": reasons,
        "mode": mode,
        "filter": "AI Discretionary · mode=%s · since %s" % (mode, PNL_EPOCH_ISO[:10]),
    }
    return out, diag


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
    realized_before_week = 0.0
    total_fees = account_all = 0.0
    counted = skipped_before_epoch = skipped_other = 0
    used_fallback_label = 0
    counted_week = counted_before = 0
    # Keep a few largest losers for diagnostics (absolute pnl)
    sample_losers: list[dict] = []

    for r in rows or []:
        try:
            pnl = float(r.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
        if abs(pnl) < 1e-12:
            continue

        ts_ms = _parse_ts_ms(r.get("close_ts") or r.get("ts") or r.get("timestamp"))
        # Require a real timestamp after epoch — ts=0 legacy rows inflated totals
        if not ts_ms or ts_ms < ep:
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
            counted_week += 1
        else:
            realized_before_week += pnl
            counted_before += 1
            if pnl < -1.0:
                sample_losers.append({
                    "pnl": round(pnl, 2),
                    "ts": ts_ms,
                    "date": str(d),
                    "inst": str(r.get("inst_id") or "")[:24],
                    "bot": bot,
                    "ord": str(r.get("ord_id") or "")[:16],
                    "cl": str(r.get("cl_ord_id") or "")[:20],
                })
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
        "before_week": round(realized_before_week, 2),
        "trades_week": counted_week,
        "trades_before_week": counted_before,
        "week_basis": "calendar_week_msk_monday",
        "week_start": week_start.isoformat(),
        "sample_losers_before_week": sorted(sample_losers, key=lambda x: x["pnl"])[:8],
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
        await db.set_setting("pnl_epoch_marker", "manual_2026_09_12_clean")
    except Exception as e:
        print(f"[pnl_engine] ensure_epoch: {e}", flush=True)
    return PNL_EPOCH_ISO


async def _rows_from_db_trades(db, ai_only: bool = True, account_mode: str = "demo") -> list[dict]:
    """Secondary source: closed rows in trades table (bot_id + pnl) since epoch.

    LIVE never uses this fallback for demo rows — account_mode must match.
    """
    out = []
    ep = epoch_ms()
    mode = (account_mode or "demo").lower()
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
        # Only realized closes — skip opens / adds / partial fills tagged open
        state = str(r.get("state") or r.get("reason") or "").lower()
        if state in ("open", "add", "live", "opening"):
            continue
        # Prefer explicit close markers; bare "filled" often is an open fill with fee-as-pnl
        if state not in ("closed", "close", "closing", "filled_close"):
            # legacy filled rows: keep only if |pnl| looks like a real close (>= $1)
            # and not a tiny fee marker
            try:
                _pnl_chk = abs(float(r.get("pnl") or 0))
            except (TypeError, ValueError):
                _pnl_chk = 0.0
            if state == "filled" and _pnl_chk < 1.0:
                continue
            if state and state not in ("filled", "closed", "close", ""):
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
        row_mode = str(r.get("account_mode") or "demo").lower()
        if mode == "live":
            if row_mode != "live":
                continue
        else:
            if row_mode not in ("", "demo"):
                continue
        out.append({
            "ord_id": str(r.get("ord_id") or r.get("id") or f"db-{bid}-{ts_ms}"),
            "inst_id": r.get("inst_id") or "",
            "cl_ord_id": r.get("cl_ord_id") or "",
            "bot_label": label,
            "pnl": pnl,
            "fee": abs(float(r.get("fee") or 0)),
            "close_ts": ts_ms,
            "account_mode": row_mode or mode,
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
        # LIVE: ONLY rows tagged live — never legacy/demo/empty
        before = len(rows)
        rows = [r for r in rows if str(r.get("account_mode") or "").lower() == "live"]
        print(
            f"[pnl_engine] live filter {before}→{len(rows)} "
            f"(excluded demo/legacy)",
            flush=True,
        )
    elif mode == "demo":
        # DEMO: demo + untagged legacy (pre-isolation rows)
        rows = [
            r for r in rows
            if str(r.get("account_mode") or "").lower() in ("", "demo")
        ]

    out = aggregate_rows(rows, ai_only=ai_only)

    # DB fallback only when exchange source is genuinely EMPTY (no rows at
    # all) — MUST match account_mode (live≠demo).
    # v1.17: previously also triggered on abs(total) < 1e-9, treating a
    # near-zero NET total as "source must be broken". That's wrong: wins and
    # losses legitimately cancelling out to ~0 is a normal, valid outcome —
    # especially lately (recent AI config had "negative expectancy despite
    # 63% WR", i.e. totals hovering right around zero). Whenever the running
    # total crossed zero, this flipped between two sources with different
    # filtering/dedup logic (aggregate_rows vs _rows_from_db_trades's own
    # state heuristics), producing a different number for the exact same
    # underlying data on every refresh — the dashboard PnL "jumping" bug.
    if out["trades_counted"] == 0:
        db_rows = await _rows_from_db_trades(db, ai_only=ai_only, account_mode=mode or "demo")
        if db_rows:
            out2 = aggregate_rows(db_rows, ai_only=ai_only)
            out2["source"] = "db_trades_fallback"
            out2["engine"] = "pnl_engine_v2"
            print(
                f"[pnl_engine] FALLBACK db_trades mode={mode} total={out2['total']} "
                f"per_bot={out2['per_bot']} n={out2['trades_counted']}",
                flush=True,
            )
            out = out2
        else:
            print(
                f"[pnl_engine] EMPTY mode={mode} exchange_rows={len(rows)} total=0 "
                f"(no cross-mode fallback)",
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

    out["account_mode"] = (account_mode or "demo").lower()
    # Guarantee keys for UI isolation
    if ai_only:
        out["active_bots"] = ["AI Discretionary 1H"]
    return out



def _period_bounds(period: str, now: Optional[datetime] = None, date_from: str = "", date_to: str = ""):
    """Return (start_date, end_date inclusive) in MSK for a named period."""
    now = now or datetime.now(timezone.utc)
    now_msk = now.astimezone(PNL_TZ)
    today = now_msk.date()
    period = (period or "all").strip().lower()

    if date_from or date_to:
        try:
            d0 = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else datetime.fromisoformat(PNL_EPOCH_ISO).astimezone(PNL_TZ).date()
        except Exception:
            d0 = datetime.fromisoformat(PNL_EPOCH_ISO).astimezone(PNL_TZ).date()
        try:
            d1 = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else today
        except Exception:
            d1 = today
        return d0, d1

    if period in ("today", "1d", "day"):
        return today, today
    if period in ("week", "w"):
        week_start = today - timedelta(days=today.weekday())  # Monday MSK
        return week_start, today
    if period in ("7d",):
        return today - timedelta(days=6), today
    if period in ("30d", "month"):
        return today - timedelta(days=29), today
    if period in ("90d", "quarter"):
        return today - timedelta(days=89), today
    # all / default — from epoch
    ep = datetime.fromisoformat(PNL_EPOCH_ISO).astimezone(PNL_TZ).date()
    return ep, today


def _coin_from_row(r: dict) -> str:
    inst = str(r.get("inst_id") or r.get("symbol") or r.get("coin") or "")
    coin = inst.replace("-USDT-SWAP", "").replace("-USD-SWAP", "").replace("-USDT", "")
    if not coin and r.get("coin"):
        coin = str(r.get("coin"))
    return (coin or "?").upper()


def aggregate_stats(
    rows: list[dict],
    *,
    ai_only: bool = True,
    period: str = "all",
    date_from: str = "",
    date_to: str = "",
    now: Optional[datetime] = None,
) -> dict:
    """Detailed DEMO/LIVE stats: wins/losses, by_coin, equity curve for a period."""
    now = now or datetime.now(timezone.utc)
    d0, d1 = _period_bounds(period, now=now, date_from=date_from, date_to=date_to)
    ep = epoch_ms()

    wins = losses = breakeven = 0
    sum_win = sum_loss = 0.0
    total_pnl = 0.0
    total_fees = 0.0
    by_coin: dict[str, dict] = {}
    by_side: dict[str, dict] = {"long": {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0},
                                 "short": {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}}
    daily: dict = {}  # date -> pnl
    recent: list[dict] = []

    for r in rows or []:
        try:
            pnl = float(r.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
        if abs(pnl) < 1e-12:
            continue

        ts_ms = _parse_ts_ms(r.get("close_ts") or r.get("ts") or r.get("timestamp"))
        if ts_ms and ts_ms < ep:
            continue

        bot = resolve_bot(r, ai_only=ai_only)
        if not bot:
            continue
        if ai_only and bot not in AI_ONLY_LABELS:
            continue

        if ts_ms:
            try:
                d = _to_msk_date(ts_ms)
            except (ValueError, OSError):
                d = None
        else:
            d = None
        if d is not None and (d < d0 or d > d1):
            continue

        total_pnl += pnl
        try:
            total_fees += abs(float(r.get("fee") or 0))
        except (TypeError, ValueError):
            pass

        if pnl > 1e-9:
            wins += 1
            sum_win += pnl
        elif pnl < -1e-9:
            losses += 1
            sum_loss += pnl
        else:
            breakeven += 1

        coin = _coin_from_row(r)
        bc = by_coin.setdefault(coin, {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
        bc["pnl"] += pnl
        bc["trades"] += 1
        if pnl > 1e-9:
            bc["wins"] += 1
        elif pnl < -1e-9:
            bc["losses"] += 1

        side = str(r.get("pos_side") or r.get("side") or "").lower()
        if side in ("buy", "long"):
            side = "long"
        elif side in ("sell", "short"):
            side = "short"
        else:
            side = ""
        if side in by_side:
            by_side[side]["pnl"] += pnl
            by_side[side]["trades"] += 1
            if pnl > 1e-9:
                by_side[side]["wins"] += 1
            elif pnl < -1e-9:
                by_side[side]["losses"] += 1

        if d is not None:
            daily[d] = daily.get(d, 0.0) + pnl

        recent.append({
            "coin": coin,
            "side": side or str(r.get("side") or ""),
            "pnl": round(pnl, 2),
            "close_ts": ts_ms or 0,
            "bot": bot,
            "inst_id": r.get("inst_id") or "",
        })

    decided = wins + losses
    win_rate = round(100.0 * wins / decided, 1) if decided else 0.0
    avg_win = round(sum_win / wins, 2) if wins else 0.0
    avg_loss = round(sum_loss / losses, 2) if losses else 0.0
    profit_factor = round(sum_win / abs(sum_loss), 2) if sum_loss < -1e-9 else (None if sum_win <= 0 else 99.0)

    # Equity curve sorted by date
    equity_curve = []
    cum = 0.0
    for d in sorted(daily.keys()):
        cum += daily[d]
        equity_curve.append({
            "date": d.isoformat(),
            "pnl": round(daily[d], 2),
            "cum": round(cum, 2),
        })

    by_coin_list = [
        {
            "coin": k,
            "pnl": round(v["pnl"], 2),
            "trades": v["trades"],
            "wins": v["wins"],
            "losses": v["losses"],
            "win_rate": round(100.0 * v["wins"] / (v["wins"] + v["losses"]), 1) if (v["wins"] + v["losses"]) else 0.0,
        }
        for k, v in sorted(by_coin.items(), key=lambda x: -abs(x[1]["pnl"]))
    ]

    for s in by_side.values():
        s["pnl"] = round(s["pnl"], 2)
        w, l = s["wins"], s["losses"]
        s["win_rate"] = round(100.0 * w / (w + l), 1) if (w + l) else 0.0

    recent.sort(key=lambda x: -(x.get("close_ts") or 0))
    recent = recent[:15]

    return {
        "period": period or "all",
        "from": d0.isoformat(),
        "to": d1.isoformat(),
        "realized_pnl": round(total_pnl, 2),
        "trades": wins + losses + breakeven,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "sum_wins": round(sum_win, 2),
        "sum_losses": round(sum_loss, 2),
        "fees": round(total_fees, 2),
        "by_coin": by_coin_list,
        "by_side": by_side,
        "equity_curve": equity_curve,
        "recent_trades": recent,
        "pnl_epoch": PNL_EPOCH_ISO,
        "pnl_tz": "Europe/Moscow",
        "engine": "pnl_engine_stats_v1",
    }


def _merge_close_rows(
    exchange_rows: list[dict],
    db_rows: list[dict],
) -> tuple[list[dict], int]:
    """Union of exchange closes + DB closes, deduped by ord_id (synthetic key fallback)."""
    merged = list(exchange_rows or [])
    seen: set[str] = set()
    for r in merged:
        oid = str(r.get("ord_id") or r.get("ordId") or "").strip()
        if oid:
            seen.add(oid)
    extra = 0
    for r in db_rows or []:
        oid = str(r.get("ord_id") or r.get("ordId") or "").strip()
        if oid and oid in seen:
            continue
        # synthetic key if no ord_id
        if not oid:
            oid = f"db-{r.get('bot_label') or r.get('bot_id')}-{r.get('close_ts') or r.get('pnl')}"
            if oid in seen:
                continue
        seen.add(oid)
        merged.append(r)
        extra += 1
    return merged, extra


async def compute_stats(
    db,
    *,
    account_mode: str = "demo",
    ai_only: bool = True,
    period: str = "all",
    date_from: str = "",
    date_to: str = "",
    sync_fn: Optional[Callable] = None,
) -> dict:
    """Public stats for DEMO showcase (and LIVE for owner — caller enforces)."""
    await ensure_epoch(db)
    if sync_fn:
        try:
            await sync_fn()
        except Exception as e:
            print(f"[pnl_engine] stats sync: {e}", flush=True)

    mode = (account_mode or "demo").lower()
    if mode != "live":
        mode = "demo"

    rows = []
    try:
        rows = await db.get_exchange_pnl_timebucket(
            bot_label=None,
            account_mode=("live" if mode == "live" else None),
            epoch_ms=0,
        ) or []
    except Exception as e:
        print(f"[pnl_engine] stats load: {e}", flush=True)

    if mode == "live":
        # SQL already filtered by account_mode; legacy rows without the column
        # are treated as live (they were only written while in live mode).
        rows = [r for r in rows if str(r.get("account_mode") or "live").lower() == "live"]
    else:
        rows = [r for r in rows if str(r.get("account_mode") or "demo").lower() in ("", "demo")]

    # Always merge DB closes so bot-tagged closes appear immediately
    # (before OKX bills sync), without double-counting by ord_id.
    db_rows = await _rows_from_db_trades(db, ai_only=ai_only, account_mode=mode)
    merged, extra = _merge_close_rows(rows, db_rows)

    out = aggregate_stats(
        merged, ai_only=ai_only, period=period, date_from=date_from, date_to=date_to,
    )
    if out["trades"] == 0:
        out["source"] = "empty"
    elif extra and rows:
        out["source"] = "exchange_close_trades+db"
    elif rows:
        out["source"] = "exchange_close_trades"
    else:
        out["source"] = "db_trades_fallback"

    out["account_mode"] = mode
    out["active_bots"] = list(AI_ONLY_LABELS) if ai_only else []
    out["rows_exchange"] = len(rows or [])
    out["rows_db_extra"] = extra
    return out


def summarize_close_rows(
    rows: list[dict],
    *,
    ai_only: bool = True,
) -> dict:
    """Aggregate a list of close rows into the KPI block used by the LIVE mirror card."""
    total = aggregate_stats(rows or [], ai_only=ai_only, period="all")
    day = aggregate_stats(rows or [], ai_only=ai_only, period="today")
    week = aggregate_stats(rows or [], ai_only=ai_only, period="week")
    return {
        "total": float(total.get("realized_pnl") or 0),
        "1d": float(day.get("realized_pnl") or 0),
        "week": float(week.get("realized_pnl") or 0),
        "trades": int(total.get("trades") or 0),
        "wins": int(total.get("wins") or 0),
        "losses": int(total.get("losses") or 0),
        "win_rate": float(total.get("win_rate") or 0),
        "fees": float(total.get("fees") or 0),
    }


async def mirror_stats(
    db,
    *,
    account_mode: str = "live",
    ai_only: bool = True,
) -> dict:
    """Unified PnL for the LIVE mirror card — SAME rows/filters as the Stats page.

    Sources (merged, deduped by ord_id):
      1. exchange_close_trades rows of this account_mode (SQL-filtered)
      2. closed rows in `trades` (bot_id <BOT_ID>_live …), merged without double-count

    Returns total / 1d / week / trades / wins / win_rate / fees + source/rows debug.
    Callers should treat source == "empty" as "no DB data" and fall back to OKX bills.
    """
    mode = (account_mode or "live").lower()
    if mode not in ("demo", "live"):
        mode = "live"

    await ensure_epoch(db)
    rows: list[dict] = []
    try:
        rows = await db.get_exchange_pnl_timebucket(
            bot_label=None, account_mode=mode, epoch_ms=0,
        ) or []
    except Exception as e:
        print(f"[pnl_engine] mirror load: {e}", flush=True)

    if mode == "live":
        rows = [r for r in rows if str(r.get("account_mode") or "live").lower() == "live"]
    else:
        rows = [r for r in rows if str(r.get("account_mode") or "demo").lower() in ("", "demo")]

    db_rows = await _rows_from_db_trades(db, ai_only=ai_only, account_mode=mode)
    merged, extra = _merge_close_rows(rows, db_rows)

    out = summarize_close_rows(merged, ai_only=ai_only)
    if out["trades"] == 0:
        out["source"] = "empty"
    elif extra and rows:
        out["source"] = "exchange_close_trades+db"
    elif rows:
        out["source"] = "exchange_close_trades"
    else:
        out["source"] = "db_trades_fallback"

    out["account_mode"] = mode
    out["rows_exchange"] = len(rows or [])
    out["rows_db_extra"] = extra
    out["pnl_epoch"] = PNL_EPOCH_ISO
    return out
