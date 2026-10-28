"""P1 «Учёт и риск»: partial booking, risk-gate inputs, scale-in/2nd slot.

Audit findings these lock down:
  1. `_scale_out_partial` shrank `pos.size` but never booked the realized
     PnL → adaptive/reflection, equity and the daily-loss limit under-counted
     every trade that took a partial.
  2. `_open` called `assert_can_open` without `notional_usd`/`leverage`, so
     RISK_MAX_POSITION_USD and RISK_MAX_LEVERAGE were silently disabled.
  3. The blanket `already_open` skip ran BEFORE the scale-in branch, making
     scale-in unreachable; LLM `action="add"` was never handled.
"""
import inspect
from types import SimpleNamespace

import pytest

from app.bots.ai_discretionary.strategy import AIConfig, AIStrategy


class _FakeDB:
    def __init__(self):
        self.saved = []
        self.close_rows = []

    async def save_trade(self, **kw):
        self.saved.append(kw)

    async def upsert_exchange_close_trades(self, rows):
        self.close_rows.extend(rows)


def _bot(db=None, indicators=None):
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot._positions = {}
    bot._trade_log = []
    bot._latest_indicators = indicators or {"BTC": {"close": 102.0}}
    bot._equity = 10000.0
    bot._daily_realized = 0.0
    bot._weekly_realized = 0.0
    bot._session_pnl = 0.0
    bot._lifetime_pnl = 0.0
    bot._lifetime_trades = 0
    bot._lifetime_wins = 0
    bot._lifetime_fees = 0.0
    bot._persist = lambda *a, **k: None
    bot._refresh_adaptive = lambda *a, **k: {}
    bot._account_mode_tag = lambda: ("demo", "")
    bot.db = db
    return bot


def _pos(coin="BTC", side="long", entry=100.0, size=70.0):
    return SimpleNamespace(
        coin=coin, inst_id=f"{coin}-USDT-SWAP", side=side,
        entry_price=entry, size=size, signal_id=123, stop_price=95.0,
        take_price=110.0, peak_price=entry, partial_taken=True,
    )


# ── 1. partial booking ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_partial_books_realized_pnl_everywhere():
    db = _FakeDB()
    bot = _bot(db=db)
    pos = _pos(size=70.0)
    filler = {"data": [{"ordId": "o1", "avgPx": "102.0", "fee": "-0.1",
                        "fillSz": "30", "clOrdId": "aid123"}]}

    # long, 30 contracts, ctVal 0.01, 100 -> 102 => 0.60 gross - 0.10 fee
    booking = await bot._book_partial(None, "BTC", pos, 30.0, filler, "partial_take")

    assert booking["pnl"] == pytest.approx(0.50, abs=1e-6)
    assert bot._equity == pytest.approx(10000.50)
    assert bot._daily_realized == pytest.approx(0.50)
    assert bot._weekly_realized == pytest.approx(0.50)
    assert bot._session_pnl == pytest.approx(0.50)
    assert bot._lifetime_pnl == pytest.approx(0.50)
    assert bot._lifetime_trades == 1
    assert bot._lifetime_wins == 1
    assert bot._lifetime_fees == pytest.approx(0.10)

    row = bot._trade_log[-1]
    assert row["reason"] == "partial"
    assert row["pnl"] == pytest.approx(0.50)
    assert row["size"] == 30.0
    assert row["account_mode"] == "demo"

    assert db.saved and db.saved[-1]["state"] == "closed"
    assert db.saved[-1]["pnl"] == pytest.approx(0.50)
    assert db.close_rows and db.close_rows[-1]["ord_id"] == "o1"
    assert db.close_rows[-1]["bot_label"] == AIStrategy.BOT_NAME


@pytest.mark.asyncio
async def test_partial_short_pnl_is_signed_correctly():
    bot = _bot()
    pos = _pos(side="short", size=70.0)
    filler = {"data": [{"ordId": "o2", "avgPx": "98.0", "fee": "-0.1",
                        "fillSz": "30"}]}
    booking = await bot._book_partial(None, "BTC", pos, 30.0, filler, "partial")
    # short 30 ct, 100 -> 98 => +0.60 gross - 0.10 fee
    assert booking["pnl"] == pytest.approx(0.50, abs=1e-6)


@pytest.mark.asyncio
async def test_partial_without_fill_still_books_at_mark():
    bot = _bot(indicators={"BTC": {"close": 101.0}})
    pos = _pos(size=70.0)
    booking = await bot._book_partial(None, "BTC", pos, 30.0, {"data": []}, "partial")
    # fallback mark 101 => 30*0.01*1 = 0.30, no fee known
    assert booking["pnl"] == pytest.approx(0.30, abs=1e-6)
    assert booking["ord_id"] == ""


def test_partial_rows_count_as_closed_trades():
    bot = _bot()
    bot._trade_log = [{"reason": "partial", "pnl": 0.5, "coin": "BTC",
                       "account_mode": "demo"}]
    closed = bot._closed_trades(demo_only=True)
    assert len(closed) == 1
    assert closed[0]["pnl"] == 0.5


def test_scale_out_partial_calls_book_partial():
    src = inspect.getsource(AIStrategy._scale_out_partial)
    assert "_book_partial" in src


# ── 2. risk gate inputs ─────────────────────────────────────────────────

def test_open_passes_notional_and_leverage_to_risk_guard():
    src = inspect.getsource(AIStrategy._open)
    assert "assert_can_open(" in src
    assert "notional_usd=" in src
    assert "leverage=" in src


# ── 3. scale-in / action=add ────────────────────────────────────────────

def test_scale_in_branch_runs_before_already_open_skip():
    src = inspect.getsource(AIStrategy._tick)
    scale_at = src.index("_maybe_scale_in")
    skip_at = src.index('reason="already_open"')
    assert scale_at < skip_at, "already_open skip shadows the scale-in branch again"


def test_explicit_add_action_is_handled():
    src = inspect.getsource(AIStrategy._tick)
    assert 'if action == "add" and coin:' in src
    assert "add_without_position" in src


def test_second_slot_entry_inherits_sibling_ownership_guard():
    # The guard moved into _open, so the 2nd slot (which calls _open directly)
    # cannot open a coin the sibling bot already owns.
    src = inspect.getsource(AIStrategy._open)
    assert "_other_ai_owns" in src


# ── 4. align internal stats with the PnL engine ─────────────────────────

_DB_ROWS = [
    {"state": "filled", "inst_id": "BTC-USDT-SWAP", "ord_id": "e1", "px": 100.0,
     "sz": 1.0, "timestamp": "2026-10-27T06:00:00+00:00", "account_mode": "demo",
     "side": "buy", "pnl": 0.0},
    {"state": "closed", "inst_id": "BTC-USDT-SWAP", "ord_id": "c1", "px": 102.0,
     "sz": 1.0, "timestamp": "2026-10-27T06:55:00+00:00", "account_mode": "demo",
     "side": "sell", "pnl": -83.24},
]

_ENG_ROWS = [
    {"ord_id": "c1", "inst_id": "BTC-USDT-SWAP", "pnl": -103.78,
     "close_ts": 1793084100000, "entry_px": 100.0, "exit_px": 101.5,
     "side": "sell", "account_mode": "demo", "fee": 2.0},
    {"ord_id": "c2", "inst_id": "ETH-USDT-SWAP", "pnl": 20.0,
     "close_ts": 1793084700000, "entry_px": 3000.0, "exit_px": 3010.0,
     "side": "buy", "account_mode": "demo", "fee": 1.0},
]


def test_engine_pnl_overrides_bot_close_estimate_by_ord_id():
    bot = _bot()
    log, stats = bot._rebuild_trade_log(_DB_ROWS, _ENG_ROWS)
    by_oid = {t["ord_id"]: t for t in log}
    assert by_oid["c1"]["pnl"] == pytest.approx(-103.78)
    assert by_oid["c1"]["exit_price"] == pytest.approx(101.5)


def test_engine_only_close_is_appended():
    bot = _bot()
    log, _ = bot._rebuild_trade_log(_DB_ROWS, _ENG_ROWS)
    assert any(t["ord_id"] == "c2" and t["pnl"] == pytest.approx(20.0) for t in log)


def test_entry_rows_stay_out_of_closed_stats():
    bot = _bot()
    log, _ = bot._rebuild_trade_log(_DB_ROWS, _ENG_ROWS)
    bot._trade_log = log
    closed = bot._closed_trades(demo_only=True)
    assert [t["ord_id"] for t in closed] == ["c1", "c2"]  # chronological
    assert all(t["reason"] != "open" for t in closed)


def test_engine_stats_match_deduped_set():
    bot = _bot()
    _, stats = bot._rebuild_trade_log(_DB_ROWS, _ENG_ROWS)
    assert stats["trades"] == 2
    assert stats["wins"] == 1
    assert stats["pnl"] == pytest.approx(-83.78)
    assert stats["fees"] == pytest.approx(3.0)


def test_without_engine_rows_falls_back_to_trades_table():
    bot = _bot()
    log, stats = bot._rebuild_trade_log(_DB_ROWS, [])
    assert stats is None
    assert len(log) == 2
    assert {t["reason"] for t in log} == {"open", "closed"}


def test_engine_rows_are_sorted_chronologically():
    bot = _bot()
    log, _ = bot._rebuild_trade_log(_DB_ROWS, _ENG_ROWS)
    keys = [bot._ts_sort_key(t) for t in log]
    assert keys == sorted(keys)