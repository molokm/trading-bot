"""Stage 5: durable DB invariants.

Covers the three things that silently corrupted state before:
* position claims must UPSERT atomically (no duplicate rows on re-claim),
* per-bot history needs its composite index (queries scan otherwise),
* opt-in retention must delete only old rows, and be a no-op when disabled.
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import app.database as dbmod

    monkeypatch.setattr(dbmod, "DATABASE_URL", "")
    d = dbmod.Database(db_path=str(tmp_path / "maint.db"))
    loop = asyncio.new_event_loop()
    loop.run_until_complete(d._init_sqlite())
    try:
        yield d, loop, str(tmp_path / "maint.db")
    finally:
        try:
            loop.run_until_complete(d.close())
        except Exception:
            pass
        loop.close()


def test_save_position_upserts_without_duplicates(db):
    d, loop, path = db
    loop.run_until_complete(d.ensure_bot("b1", strategy_id="s", name="n"))
    loop.run_until_complete(d.save_position("b1", "BTC-USDT-SWAP", "long", 1.0, 100.0))
    loop.run_until_complete(d.save_position("b1", "BTC-USDT-SWAP", "long", 3.0, 105.0))

    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT size, entry_price FROM positions WHERE bot_id='b1'"
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1, f"duplicate claim rows: {rows}"
    assert rows[0][0] == 3.0 and rows[0][1] == 105.0


def test_per_bot_history_index_exists(db):
    _, _, path = db
    con = sqlite3.connect(path)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trades'"
        )}
    finally:
        con.close()
    assert "idx_trades_bot_ts" in names


def test_prune_removes_only_old_rows(db):
    d, loop, path = db
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=90)).isoformat()
    fresh = (now - timedelta(days=1)).isoformat()
    old_ms = int((now - timedelta(days=90)).timestamp() * 1000)
    fresh_ms = int((now - timedelta(days=1)).timestamp() * 1000)
    loop.run_until_complete(d.ensure_bot("b1", strategy_id="s", name="n"))
    for ts in (old, fresh):
        loop.run_until_complete(d._execute(
            "INSERT INTO trades (id, bot_id, inst_id, side, timestamp) VALUES (?,?,?,?,?)",
            (f"t-{ts}", "b1", "BTC-USDT-SWAP", "buy", ts),
        ))
        loop.run_until_complete(d._execute(
            "INSERT INTO performance_metrics (bot_id, timestamp, equity) VALUES (?,?,?)",
            ("b1", ts, 1.0),
        ))
    for oid, ts in (("old", old_ms), ("fresh", fresh_ms)):
        loop.run_until_complete(d._execute(
            "INSERT INTO exchange_close_trades (ord_id, inst_id, cl_ord_id, close_ts, synced_at) "
            "VALUES (?,?,?,?,?)",
            (oid, "BTC-USDT-SWAP", "c", ts, now.isoformat()),
        ))

    res = loop.run_until_complete(d.prune_old_rows(30))
    assert res == {"trades": 1, "exchange_close_trades": 1, "performance_metrics": 1}

    con = sqlite3.connect(path)
    try:
        trades = [r[0] for r in con.execute("SELECT timestamp FROM trades")]
        closes = [r[0] for r in con.execute("SELECT ord_id FROM exchange_close_trades")]
        metrics = [r[0] for r in con.execute("SELECT timestamp FROM performance_metrics")]
    finally:
        con.close()
    assert trades == [fresh]
    assert closes == ["fresh"]
    assert metrics == [fresh]


def test_prune_disabled_is_noop(db):
    d, loop, _ = db
    loop.run_until_complete(d.ensure_bot("b1", strategy_id="s", name="n"))
    loop.run_until_complete(d._execute(
        "INSERT INTO trades (id, bot_id, inst_id, side, timestamp) VALUES (?,?,?,?,?)",
        ("t1", "b1", "BTC-USDT-SWAP", "buy", "2020-01-01T00:00:00+00:00"),
    ))
    res = loop.run_until_complete(d.prune_old_rows(0))
    assert res == {"trades": 0, "exchange_close_trades": 0, "performance_metrics": 0}
    assert loop.run_until_complete(d.get_trades(bot_id="b1"))
