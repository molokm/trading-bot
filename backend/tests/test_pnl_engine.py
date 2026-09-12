"""Regression tests for pnl_engine — run: python -m pytest backend/tests/test_pnl_engine.py -q"""
from datetime import datetime, timezone
from app.services.pnl_engine import (
    aggregate_rows, label_from_clord, resolve_bot, epoch_ms, PNL_EPOCH_ISO,
)

def test_clord_prefix_order():
    assert label_from_clord("ais999") == "AI Scale-In 1H"
    assert label_from_clord("ai999") == "AI Discretionary 1H"

def test_sum_both_bots():
    ts = int(datetime.fromisoformat("2026-09-05T12:00:00+00:00").timestamp() * 1000)
    rows = [
        {"pnl": 100, "close_ts": ts, "cl_ord_id": "ai1", "fee": 0},
        {"pnl": 50, "close_ts": ts, "cl_ord_id": "ais1", "fee": 0},
    ]
    r = aggregate_rows(rows, ai_only=True)
    assert abs(r["total"] - 150) < 0.01
    assert abs(r["per_bot"]["AI Discretionary 1H"] - 100) < 0.01
    assert abs(r["per_bot"]["AI Scale-In 1H"] - 50) < 0.01

def test_untagged_ai_only_counts():
    ts = int(datetime.fromisoformat("2026-09-05T12:00:00+00:00").timestamp() * 1000)
    rows = [{"pnl": 42, "close_ts": ts, "cl_ord_id": "", "bot_label": "", "fee": 0}]
    r = aggregate_rows(rows, ai_only=True)
    assert abs(r["total"] - 42) < 0.01

def test_pre_epoch_skipped():
    ts = int(datetime.fromisoformat("2026-08-01T12:00:00+00:00").timestamp() * 1000)
    rows = [{"pnl": 99, "close_ts": ts, "cl_ord_id": "ai1", "fee": 0}]
    r = aggregate_rows(rows, ai_only=True)
    assert abs(r["total"]) < 0.01
    assert r["skipped_before_epoch"] >= 1

def test_epoch_constant():
    assert PNL_EPOCH_ISO.startswith("2026-09-12")
    assert epoch_ms() > 0
