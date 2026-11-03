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
