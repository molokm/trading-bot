"""Fresh-database initialisation must produce the full schema.

Regression for a bug found while smoke-testing stage 3: the SQLite schema script
created an index on ``trades(account_mode)`` before the column existed, so
``db.init()`` aborted on a *fresh* database after only 3 of 13 tables, and every
later query failed with 'no such table' (the same fallback path Render uses when
the Postgres quota is hit). Existing databases were unaffected, which is why it
went unnoticed.
"""
import sqlite3

import pytest

EXPECTED_TABLES = {
    "bots", "signals", "trades", "positions", "performance_metrics", "settings",
    "subscriptions", "users", "tg_processed_updates", "exchange_close_trades",
    "tracked_traders", "copy_trades", "audit_log",
}

ISOLATION_COLUMNS = {
    "trades": {"account_mode", "account_key"},
    "exchange_close_trades": {"account_mode", "account_key"},
}


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """A Database bound to a brand-new file, isolated from DATABASE_URL."""
    import app.database as dbmod

    path = str(tmp_path / "fresh.db")
    # Database.__init__ prefers a `file:` DATABASE_URL over the db_path argument,
    # so neutralise it for the duration of the test.
    monkeypatch.setattr(dbmod, "DATABASE_URL", "")

    def init():
        import asyncio

        db = dbmod.Database(db_path=path)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(db._init_sqlite())
        finally:
            # aiosqlite keeps a thread per connection: close both or pytest hangs
            try:
                loop.run_until_complete(db.close())
            except Exception:
                pass
            loop.close()
        return db

    return path, init


def _tables(path):
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_fresh_database_gets_full_schema(fresh_db):
    path, init = fresh_db
    init()
    missing = EXPECTED_TABLES - _tables(path)
    assert not missing, f"db.init() did not create: {sorted(missing)}"


def test_isolation_columns_exist_on_fresh_database(fresh_db):
    path, init = fresh_db
    init()
    con = sqlite3.connect(path)
    try:
        for table, cols in ISOLATION_COLUMNS.items():
            got = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            missing = cols - got
            assert not missing, f"{table} is missing {sorted(missing)} after init"
    finally:
        con.close()


def test_init_is_idempotent(fresh_db):
    """Running init twice (redeploy on an existing volume) must not raise."""
    path, init = fresh_db
    init()
    init()
    assert EXPECTED_TABLES <= _tables(path)
