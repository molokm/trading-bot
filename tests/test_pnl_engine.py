"""Regression tests for the PnL contract of pnl_engine.

These pin the RULES the product relies on (state 2026-10). They used to live in
``backend/tests/`` and had rotted: CI never ran pytest, so the assertions had
silently drifted from the code (old epoch 2026-09-12, ``ais*`` counted as
AI Scale-In, untagged closes counted). Run:

    pytest tests/test_pnl_engine.py -q

Rules under test
----------------
1. Only closes from ``PNL_EPOCH`` onwards count (product start); rows without a
   real timestamp are skipped (legacy ``ts=0`` rows inflated totals).
2. Only AI Discretionary closes count in AI_ONLY mode:
   ``ai*`` → AI Discretionary; ``ais*`` (retired Scale-In) → never;
   untagged closes are NOT silently attributed.
3. A stored bot_label / bot_id is trusted for closes without clOrdId (that is
   how LIVE mirror closes are recovered), including ``ai_strategy_live``.
"""
from datetime import datetime

import pytest

from app.services.pnl_engine import (
    AI_ONLY_LABELS,
    BUG_EXCLUDED_ORD_IDS,
    PNL_EPOCH_ISO,
    aggregate_rows,
    epoch_ms,
    filter_pnl_rows,
    is_bug_excluded,
    label_from_clord,
    pnl_eligible,
    resolve_bot,
)

AI_LABEL = "AI Discretionary 1H"


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


# After the epoch, used as the "normal" timestamp
TS_OK = _ts("2026-09-05T12:00:00+00:00")


# ── 1. clOrdId → label mapping ────────────────────────────────────────────

@pytest.mark.parametrize(
    "cl_ord_id,expected",
    [
        ("ai999", AI_LABEL),
        ("ai_ok", AI_LABEL),
        ("AI123", AI_LABEL),          # case-insensitive
        ("ais999", ""),               # retired Scale-In must NOT match "ai"
        ("rot1788251921995", "Momentum"),
        ("imp42", "Impulse 1D"),
        ("val7", "MACD+Donchian Validation"),
        ("", ""),
        ("manual_close", ""),
    ],
)
def test_label_from_clord(cl_ord_id, expected):
    assert label_from_clord(cl_ord_id) == expected


def test_scale_in_never_attributed_to_ai():
    """``startswith('ai')`` would false-positive on ``ais`` — guard it."""
    assert label_from_clord("ais-whatever") == ""
    assert resolve_bot({"cl_ord_id": "ais1"}, ai_only=True) == ""


# ── 2. attribution fallbacks ──────────────────────────────────────────────

def test_resolve_bot_trusts_stored_label_without_clordid():
    row = {"cl_ord_id": "", "bot_label": AI_LABEL}
    assert resolve_bot(row, ai_only=True) == AI_LABEL


def test_resolve_bot_maps_live_mirror_bot_id():
    """LIVE mirror engine writes closes under ai_strategy_live."""
    assert resolve_bot({"bot_id": "ai_strategy_live"}, ai_only=True) == AI_LABEL


def test_resolve_bot_skips_retired_scale_bot_id():
    assert resolve_bot({"bot_id": "ai_scale_strategy"}, ai_only=True) == ""


def test_resolve_bot_ignores_untagged():
    assert resolve_bot({"cl_ord_id": "", "bot_label": "", "bot_id": ""}, ai_only=True) == ""


# ── 3. aggregation ────────────────────────────────────────────────────────

def test_demo_close_counts():
    rows = [{"pnl": 100, "close_ts": TS_OK, "cl_ord_id": "ai1", "fee": 0}]
    r = aggregate_rows(rows, ai_only=True)
    assert r["total"] == pytest.approx(100)
    assert r["per_bot"][AI_LABEL] == pytest.approx(100)
    assert r["account_total"] == pytest.approx(100)


def test_account_total_keeps_retired_scale_closes():
    """Account total keeps every close; AI total keeps only AI closes."""
    rows = [
        {"pnl": 100, "close_ts": TS_OK, "cl_ord_id": "ai1", "fee": 0},
        {"pnl": 50, "close_ts": TS_OK, "cl_ord_id": "ais1", "fee": 0},
    ]
    r = aggregate_rows(rows, ai_only=True)
    assert r["total"] == pytest.approx(100)          # ais* excluded
    assert r["per_bot"][AI_LABEL] == pytest.approx(100)
    assert "AI Scale-In 1H" not in r["per_bot"]      # retired label gone
    assert r["account_total"] == pytest.approx(150)  # but still on the account
    assert AI_ONLY_LABELS == (AI_LABEL,)


def test_untagged_close_is_not_counted():
    rows = [{"pnl": 42, "close_ts": TS_OK, "cl_ord_id": "", "bot_label": "", "fee": 0}]
    r = aggregate_rows(rows, ai_only=True)
    assert r["total"] == pytest.approx(0)
    assert r["account_total"] == pytest.approx(42)


def test_live_mirror_close_counts_via_bot_id():
    rows = [{"pnl": 33, "close_ts": TS_OK, "cl_ord_id": "", "bot_id": "ai_strategy_live", "fee": 0}]
    r = aggregate_rows(rows, ai_only=True)
    assert r["total"] == pytest.approx(33)


def test_pre_epoch_skipped():
    ts = _ts("2026-08-01T12:00:00+00:00")
    rows = [{"pnl": 99, "close_ts": ts, "cl_ord_id": "ai1", "fee": 0}]
    r = aggregate_rows(rows, ai_only=True)
    assert r["total"] == pytest.approx(0)
    assert r["skipped_before_epoch"] >= 1


def test_missing_timestamp_skipped():
    """Legacy ts=0 rows used to inflate totals."""
    rows = [{"pnl": 77, "close_ts": 0, "cl_ord_id": "ai1", "fee": 0}]
    r = aggregate_rows(rows, ai_only=True)
    assert r["total"] == pytest.approx(0)
    assert r["skipped_before_epoch"] >= 1


def test_zero_pnl_rows_ignored():
    rows = [{"pnl": 0, "close_ts": TS_OK, "cl_ord_id": "ai1", "fee": 0}]
    r = aggregate_rows(rows, ai_only=True)
    assert r["total"] == pytest.approx(0)


def test_fees_accumulated():
    rows = [{"pnl": 10, "close_ts": TS_OK, "cl_ord_id": "ai1", "fee": -0.5}]
    r = aggregate_rows(rows, ai_only=True)
    assert r["total"] == pytest.approx(10)
    assert r["fees"] == pytest.approx(0.5)


# ── 4. epoch ──────────────────────────────────────────────────────────────

def test_epoch_constant():
    assert PNL_EPOCH_ISO.startswith("2026-09-01")
    assert epoch_ms() == _ts(PNL_EPOCH_ISO)
    assert epoch_ms() < TS_OK


def test_aggregate_accepts_empty_rows():
    r = aggregate_rows([], ai_only=True)
    assert r["total"] == pytest.approx(0)
    assert r["per_bot"][AI_LABEL] == pytest.approx(0)


# ── 5. defect-driven closes are excluded from set T (by ord_id) ───────────
NEAR_BUG_OID = "4050574887064129536"


def test_bug_close_is_not_eligible():
    row = {"pnl": -557.72, "close_ts": TS_OK, "cl_ord_id": "ai1",
           "ord_id": NEAR_BUG_OID, "fee": 0}
    ok, reason = pnl_eligible(row, mode="demo", ai_only=True, require_ts=False)
    assert not ok
    assert reason == "excluded:ctval_10x"


def test_bug_close_skipped_by_filter_but_row_survives():
    """Excluded from statistics, not deleted from the audit trail."""
    rows = [
        {"pnl": -557.72, "close_ts": TS_OK, "cl_ord_id": "ai1",
         "ord_id": NEAR_BUG_OID, "fee": 0},
        {"pnl": 12.5, "close_ts": TS_OK, "cl_ord_id": "ai2", "fee": 0},
    ]
    out, diag = filter_pnl_rows(rows, mode="demo", ai_only=True)
    assert [r["pnl"] for r in out] == [pytest.approx(12.5)]
    assert diag["exclude_reasons"].get("excluded:ctval_10x") == 1
    # the input is untouched — nothing is deleted from history
    assert rows[0]["ord_id"] == NEAR_BUG_OID


def test_bug_excluded_ids_cover_near_and_flat_artifacts():
    assert is_bug_excluded(NEAR_BUG_OID)
    assert len(BUG_EXCLUDED_ORD_IDS) >= 8
    assert not is_bug_excluded("")
    assert not is_bug_excluded("some-unrelated-ord-id")
