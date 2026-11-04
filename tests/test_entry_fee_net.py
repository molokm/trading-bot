"""Round-trip PnL: the entry commission is part of the result.

An OKX bill carries ONE leg's commission: the close bill's `fee` is the exit
fee, the entry commission sits on the OPEN bill. Only the exit leg used to be
subtracted, so every net number on the main page was short by ~half the
round-trip cost. Real NEAR trade (03.11): gross −15.06, close fee 2.63, entry
fee 2.62 — the card showed −17.68 instead of −20.30.

Close rows are now the true round trip: gross − entry fee − exit fee. The entry
fee is allocated by position lifecycle (instrument + time), because our close
orders reach OKX with an EMPTY clOrdId and an id-based match finds nothing.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import app.main as m
from app.services.pnl_engine import entry_fee_by_close, rows_from_bills


def _ts(minutes_ago=0):
    return int((datetime.now(timezone.utc).timestamp() - minutes_ago * 60) * 1000)


def _open(oid="o1", fee="-2.62", sz="130.3", ts=None, inst="NEAR-USDT-SWAP",
          cl="aidc9d8897c44b2"):
    return {
        "ordId": oid, "instId": inst, "subType": "4",
        "pnl": "0", "fee": fee, "sz": sz, "px": "4.018",
        "ts": str(ts if ts is not None else _ts(30)), "clOrdId": cl,
    }


def _close(oid="c1", pnl="-15.056", fee="-2.62", sz="130.3", ts=None,
           inst="NEAR-USDT-SWAP", cl=""):
    # Real closes carry no clOrdId — that is why pairing cannot rely on it.
    return {
        "ordId": oid, "instId": inst, "subType": "6",
        "pnl": pnl, "fee": fee, "sz": sz, "px": "4.0297",
        "ts": str(ts if ts is not None else _ts()), "clOrdId": cl,
    }


# ── allocation ──────────────────────────────────────────────────────────

def test_entry_fee_is_matched_to_its_close_by_time():
    fees = entry_fee_by_close([_open(fee="-2.62", sz="100"),
                               _close(oid="c1", sz="100")])
    assert fees["c1"] == pytest.approx(2.62)


def test_partial_closes_split_the_entry_fee():
    fees = entry_fee_by_close([
        _open(fee="-5.0", sz="100"),
        _close(oid="p1", sz="50", ts=_ts(20)),
        _close(oid="p2", sz="50", ts=_ts(10)),
    ])
    assert fees["p1"] == pytest.approx(2.5)
    assert fees["p2"] == pytest.approx(2.5)


def test_entry_fee_is_never_paid_twice():
    fees = entry_fee_by_close([
        _open(fee="-3.0", sz="100"),
        _close(oid="c1", sz="100", ts=_ts(20)),
        _close(oid="c2", sz="100", ts=_ts(10)),
    ])
    assert fees["c1"] == pytest.approx(3.0)
    assert "c2" not in fees


def test_new_position_does_not_inherit_the_previous_fee():
    fees = entry_fee_by_close([
        _open(oid="o1", fee="-3.0", sz="100", ts=_ts(60)),
        _close(oid="c1", sz="100", ts=_ts(50)),
        _open(oid="o2", fee="-1.0", sz="100", ts=_ts(40)),
        _close(oid="c2", sz="100", ts=_ts(10)),
    ])
    assert fees["c1"] == pytest.approx(3.0)
    assert fees["c2"] == pytest.approx(1.0)


def test_other_instruments_are_isolated():
    fees = entry_fee_by_close([
        _open(oid="o1", fee="-9.0", sz="100", inst="BTC-USDT-SWAP"),
        _close(oid="c1", sz="100", inst="NEAR-USDT-SWAP"),
    ])
    assert "c1" not in fees


def test_close_without_any_entry_bill_owes_nothing():
    assert entry_fee_by_close([_close()]) == {}


# ── engine rows ─────────────────────────────────────────────────────────

def test_bill_row_is_net_of_both_legs():
    rows = rows_from_bills([_open(), _close()], allow_untagged_ai=True)
    assert len(rows) == 1
    row = rows[0]
    assert row["pnl"] == pytest.approx(-15.056 - 2.62 - 2.62)
    assert row["fee"] == pytest.approx(5.24)
    assert row["entry_fee"] == pytest.approx(2.62)


def test_multi_fill_close_is_aggregated():
    rows = rows_from_bills([
        _open(fee="-2.0", sz="100"),
        _close(oid="c1", pnl="-6.0", fee="-1.0", sz="50", ts=_ts(20)),
        _close(oid="c1", pnl="-4.0", fee="-1.0", sz="50", ts=_ts(19)),
    ], allow_untagged_ai=True)
    assert len(rows) == 1
    assert rows[0]["gross_pnl"] == pytest.approx(-10.0)
    assert rows[0]["fee"] == pytest.approx(4.0)
    assert rows[0]["pnl"] == pytest.approx(-14.0)


def test_close_without_entry_bill_keeps_old_semantics():
    rows = rows_from_bills([_close()], allow_untagged_ai=True)
    assert rows[0]["pnl"] == pytest.approx(-15.056 - 2.62)


# ── exchange sync ───────────────────────────────────────────────────────

class _DB:
    def __init__(self):
        self.rows = []

    async def upsert_exchange_close_trades(self, rows):
        self.rows.extend(rows)
        return len(rows)


async def _run(monkeypatch, bills, recent_only):
    db = _DB()

    async def _bills(limit_per_page=100, mode=None, recent_only=False):
        return bills

    monkeypatch.setattr(m, "db", db)
    monkeypatch.setattr(m, "_fetch_all_trade_bills", _bills)
    monkeypatch.setattr(m, "_exchange_sync_ts", {})
    monkeypatch.setattr(m, "_exchange_close_seen", {})
    n = await m._sync_exchange_close_trades_impl("demo", recent_only=recent_only)
    return n, db.rows


@pytest.mark.asyncio
async def test_sync_stores_round_trip_net(monkeypatch):
    n, rows = await _run(monkeypatch, [_open(), _close()], recent_only=False)
    assert n == 1
    assert rows[-1]["pnl"] == pytest.approx(-20.296, abs=1e-5)
    assert rows[-1]["fee"] == pytest.approx(5.24)


@pytest.mark.asyncio
async def test_recent_sync_never_drops_a_close_without_its_entry(monkeypatch):
    """A window that cannot see the entry bill still writes the close.

    Dropping it would hide a real trade from the card until the full pass;
    a slightly understated fee for <=30 min is the lesser evil.
    """
    n, rows = await _run(monkeypatch, [_close()], recent_only=True)
    assert n == 1
    assert rows[-1]["pnl"] == pytest.approx(-15.056 - 2.62)


# ── entry fee recovered from the exchange at close ──────────────────────
# A post-only entry often returns no fee, so pos.entry_fee stays 0 and the
# close message (Telegram, DB row) silently drops the entry leg while the main
# page — built from exchange bills — subtracts it. BTC 04.11: TG +3.31, card
# +3.14. These lock the recovery order: stored → order → fills.

class _Ex:
    def __init__(self, order_fee=None, fills=None):
        self.order_fee = order_fee
        self.fills = fills or []
        self.order_calls = 0
        self.fill_calls = 0

    async def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        self.order_calls += 1
        if self.order_fee is None:
            return {"data": []}
        return {"data": [{"fee": str(self.order_fee)}]}

    async def get_fills(self, inst_id=None, limit=20, **kw):
        self.fill_calls += 1
        return {"data": [{"fee": str(f)} for f in self.fills]}


def _fee_bot():
    from app.bots.ai_discretionary.strategy import AIStrategy
    bot = object.__new__(AIStrategy)
    return bot


def _fee_pos(entry_fee=0.0, tid="c9d8897c44b2"):
    return SimpleNamespace(
        inst_id="BTC-USDT-SWAP", entry_fee=entry_fee, demo_trade_id=tid,
    )


@pytest.mark.asyncio
async def test_recover_entry_fee_prefers_the_stored_value():
    bot = _fee_bot()
    ex = _Ex(order_fee="-9.99")
    fee = await bot._recover_entry_fee(ex, _fee_pos(entry_fee=0.17), live=True)
    assert fee == pytest.approx(0.17)
    assert ex.order_calls == 0


@pytest.mark.asyncio
async def test_recover_entry_fee_reads_the_entry_order():
    bot = _fee_bot()
    ex = _Ex(order_fee="-0.1696")
    fee = await bot._recover_entry_fee(ex, _fee_pos(), live=True)
    assert fee == pytest.approx(0.1696)
    assert ex.fill_calls == 0


@pytest.mark.asyncio
async def test_recover_entry_fee_falls_back_to_fills():
    bot = _fee_bot()
    ex = _Ex(order_fee=None, fills=["-0.10", "-0.07"])
    fee = await bot._recover_entry_fee(ex, _fee_pos(), live=True)
    assert fee == pytest.approx(0.17)
    assert ex.order_calls == 1 and ex.fill_calls == 1


@pytest.mark.asyncio
async def test_recover_entry_fee_without_trade_id_is_zero():
    bot = _fee_bot()
    ex = _Ex(order_fee="-1.0")
    assert await bot._recover_entry_fee(ex, _fee_pos(tid=""), live=True) == 0.0
    assert ex.order_calls == 0


# ── the entry fee is booked exactly once ────────────────────────────────
# _close() added entry_fee to fee_c (so the round trip is complete) and ALSO
# did `_lifetime_fees += _entry_fee`, double-counting the entry leg. The card
# masked it by overlaying engine_stats, but the process counter was wrong.

class _CloseClient:
    demo = True

    async def get_positions(self, inst_id=None):
        return {"data": [{"pos": "1", "instId": inst_id}]}

    async def get_order(self, *a, **k):
        return {"data": []}


@pytest.mark.asyncio
async def test_close_books_the_entry_fee_once(monkeypatch):
    import app.bots.ai_discretionary.strategy as S
    from app.bots.ai_discretionary.strategy import AIConfig, AIPosition, AIStrategy

    async def _noop(*a, **k):
        return None

    async def _false():
        return False

    async def _place(*a, **k):
        return {"data": [{
            "ordId": "c1", "avgPx": "110", "fillSz": "1",
            "fee": "-0.06", "fillPnl": "10", "clOrdId": "adc1",
        }]}

    monkeypatch.setattr(S, "release_open", _noop)

    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot.db = None
    bot.client_manager = None
    bot.analysis = None
    bot._execute_enabled = lambda: True
    bot._cancel_exchange_sl_tp = _noop
    bot._place = _place
    bot._persist = lambda: None
    bot._refresh_adaptive = lambda: None
    bot._account_mode_tag = lambda: ("demo", "showcase")
    bot._notify_close_tg = _noop
    bot._remember_close = _noop
    bot._settle_close_memory = lambda *a, **k: None
    bot._mirror_enabled = _false
    bot._exec_stat = lambda *a, **k: None
    bot._lifetime_pnl = 0.0
    bot._lifetime_trades = 0
    bot._lifetime_wins = 0
    bot._lifetime_fees = 0.0
    bot._session_pnl = 0.0
    bot._daily_realized = 0.0
    bot._weekly_realized = 0.0
    bot._equity = 10000.0
    bot._flow_day = ""
    bot._daily_opens = 0
    bot._daily_fees = 0.0
    bot._engine_stats_ts = 0.0
    bot._trade_log = []
    bot._pending_live_closes = set()
    bot._latest_indicators = {"ETH": {"close": 110.0}}
    bot._positions = {"ETH": AIPosition(
        coin="ETH", inst_id="ETH-USDT-SWAP", side="long", size=1.0,
        entry_price=100.0, stop_price=98.0, take_price=110.0, leverage=3,
        opened_at="2026-11-04T00:00:00+00:00", entry_fee=0.05,
    )}

    await bot._close(_CloseClient(), "ETH", "take_profit")

    # exit 0.06 + entry 0.05 — not 0.06 + 0.05 + 0.05
    assert bot._lifetime_fees == pytest.approx(0.11)
    # open fee was already counted at open, so the close only adds the exit leg
    assert bot._daily_fees == pytest.approx(0.06)


@pytest.mark.asyncio
async def test_close_recovered_entry_fee_hits_the_daily_budget(monkeypatch):
    """A fee recovered at close never passed note_open_flow — count it now."""
    import app.bots.ai_discretionary.strategy as S
    from app.bots.ai_discretionary.strategy import AIConfig, AIPosition, AIStrategy

    async def _noop(*a, **k):
        return None

    async def _false():
        return False

    async def _place(*a, **k):
        return {"data": [{
            "ordId": "c2", "avgPx": "110", "fillSz": "1",
            "fee": "-0.06", "fillPnl": "10", "clOrdId": "adc2",
        }]}

    async def _recover(self, client, pos, *, live):
        return 0.04

    monkeypatch.setattr(S, "release_open", _noop)

    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot.db = None
    bot.client_manager = None
    bot.analysis = None
    bot._execute_enabled = lambda: True
    bot._cancel_exchange_sl_tp = _noop
    bot._place = _place
    bot._persist = lambda: None
    bot._refresh_adaptive = lambda: None
    bot._account_mode_tag = lambda: ("demo", "showcase")
    bot._notify_close_tg = _noop
    bot._remember_close = _noop
    bot._settle_close_memory = lambda *a, **k: None
    bot._mirror_enabled = _false
    bot._exec_stat = lambda *a, **k: None
    bot._recover_entry_fee = _recover.__get__(bot)
    bot._lifetime_pnl = 0.0
    bot._lifetime_trades = 0
    bot._lifetime_wins = 0
    bot._lifetime_fees = 0.0
    bot._session_pnl = 0.0
    bot._daily_realized = 0.0
    bot._weekly_realized = 0.0
    bot._equity = 10000.0
    bot._flow_day = ""
    bot._daily_opens = 0
    bot._daily_fees = 0.0
    bot._engine_stats_ts = 0.0
    bot._trade_log = []
    bot._pending_live_closes = set()
    bot._latest_indicators = {"ETH": {"close": 110.0}}
    bot._positions = {"ETH": AIPosition(
        coin="ETH", inst_id="ETH-USDT-SWAP", side="long", size=1.0,
        entry_price=100.0, stop_price=98.0, take_price=110.0, leverage=3,
        opened_at="2026-11-04T00:00:00+00:00", entry_fee=0.0,
    )}

    await bot._close(_CloseClient(), "ETH", "take_profit")

    assert bot._lifetime_fees == pytest.approx(0.10)
    assert bot._daily_fees == pytest.approx(0.10)   # exit 0.06 + recovered 0.04
