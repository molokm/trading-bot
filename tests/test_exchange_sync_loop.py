"""Periodic exchange-close sync: freshness, single-flight, cache invalidation.

Before this the sync ran once at startup and afterwards only when a page asked
for PnL, so a close made while nobody watched could stay invisible for hours.
_ME_DASH_CACHE then kept the old payload for another 45 s, so the card lagged
behind the «Сделки» list even once the row was in the DB.
"""
import pytest

import app.main as m


def _bill(oid, pnl="1.5", mode=None):
    b = {
        "ordId": oid, "instId": "BTC-USDT-SWAP", "subType": "5",
        "pnl": pnl, "fee": "-0.1", "sz": "1", "avgPx": "100",
        "ts": "1793200000000", "clOrdId": "aidabc123",
    }
    if mode:
        b["account_mode"] = mode
    return b


class _DB:
    def __init__(self):
        self.rows = []

    async def upsert_exchange_close_trades(self, rows):
        self.rows.append(list(rows))
        return len(rows)


@pytest.mark.asyncio
async def test_force_bypasses_ttl(monkeypatch):
    db = _DB()

    async def _bills(limit_per_page=100, mode=None, recent_only=False):
        return [_bill(f"ord-{mode}")]

    monkeypatch.setattr(m, "db", db)
    monkeypatch.setattr(m, "_fetch_all_trade_bills", _bills)
    monkeypatch.setattr(m, "_exchange_sync_ts", {})
    monkeypatch.setattr(m, "_exchange_close_seen", {})

    assert await m.sync_exchange_close_trades(mode="demo") == 1
    assert await m.sync_exchange_close_trades(mode="demo") == 0  # TTL
    assert await m.sync_exchange_close_trades(mode="demo", force=True) == 1


@pytest.mark.asyncio
async def test_recent_only_is_forwarded_to_the_bills_fetch(monkeypatch):
    seen = []

    async def _bills(limit_per_page=100, mode=None, recent_only=False):
        seen.append(recent_only)
        return [_bill(f"ord-{mode}")]

    monkeypatch.setattr(m, "db", _DB())
    monkeypatch.setattr(m, "_fetch_all_trade_bills", _bills)
    monkeypatch.setattr(m, "_exchange_sync_ts", {})
    monkeypatch.setattr(m, "_exchange_close_seen", {})

    await m.sync_exchange_close_trades(mode="demo", force=True, recent_only=True)
    assert seen == [True]


@pytest.mark.asyncio
async def test_concurrent_sync_of_one_mode_is_single_flight(monkeypatch):
    db = _DB()
    monkeypatch.setattr(m, "db", db)
    monkeypatch.setattr(m, "_exchange_sync_ts", {})
    monkeypatch.setattr(m, "_exchange_close_seen", {})
    monkeypatch.setattr(m, "_exchange_sync_inflight", {"demo": m._time.time()})

    async def _bills(limit_per_page=100, mode=None, recent_only=False):
        return [_bill("ord-demo")]

    monkeypatch.setattr(m, "_fetch_all_trade_bills", _bills)
    assert await m.sync_exchange_close_trades(mode="demo", force=True) == 0
    assert db.rows == []


@pytest.mark.asyncio
async def test_rows_are_tagged_with_the_synced_mode_not_the_current_one(monkeypatch):
    """Syncing LIVE while the UI is on DEMO used to write account_key=showcase."""
    db = _DB()

    async def _bills(limit_per_page=100, mode=None, recent_only=False):
        # No account_mode tag on the bill — the sync must use its own mode.
        return [_bill("ord-live")]

    monkeypatch.setattr(m, "db", db)
    monkeypatch.setattr(m, "_account_mode", lambda: "demo")
    monkeypatch.setattr(m, "_fetch_all_trade_bills", _bills)
    monkeypatch.setattr(m, "_exchange_sync_ts", {})
    monkeypatch.setattr(m, "_exchange_close_seen", {})

    assert await m.sync_exchange_close_trades(mode="live") == 1
    row = db.rows[0][0]
    assert row["account_mode"] == "live"
    assert row["account_key"] == "live"


@pytest.mark.asyncio
async def test_new_close_drops_cached_dashboard_payloads(monkeypatch):
    db = _DB()
    cache = {
        "admin:7": {"ts": 1.0, "data": {"trades": ["old"]}},
        "guest:shared": {"ts": m._time.time(), "data": {"trades": ["old"]}},
    }
    monkeypatch.setattr(m, "db", db)
    monkeypatch.setattr(m, "_ME_DASH_CACHE", cache)
    monkeypatch.setattr(m, "_exchange_sync_ts", {})
    monkeypatch.setattr(m, "_exchange_close_seen", {"demo": 1793200000000})
    monkeypatch.setattr(m, "_exchange_sync_inflight", {})

    async def _bills(limit_per_page=100, mode=None, recent_only=False):
        old = _bill("ord-1")
        new = _bill("ord-2")
        new["ts"] = "1793200600000"  # newer close than anything seen before
        return [old, new]

    monkeypatch.setattr(m, "_fetch_all_trade_bills", _bills)

    assert await m.sync_exchange_close_trades(mode="demo", force=True) == 2
    assert "admin:7" not in cache            # authenticated → rebuilt on request
    assert "guest:shared" in cache           # kept, but marked stale
    assert m._time.time() - cache["guest:shared"]["ts"] >= m._ME_DASH_TTL


@pytest.mark.asyncio
async def test_reupsert_of_the_same_closes_keeps_the_dashboard_cache(monkeypatch):
    """The periodic loop re-upserts every minute; that must not churn the card."""
    cache = {"admin:7": {"ts": 1.0, "data": {"trades": ["old"]}}}
    monkeypatch.setattr(m, "db", _DB())
    monkeypatch.setattr(m, "_ME_DASH_CACHE", cache)
    monkeypatch.setattr(m, "_exchange_sync_ts", {})
    monkeypatch.setattr(m, "_exchange_close_seen", {"demo": 1793200000000})
    monkeypatch.setattr(m, "_exchange_sync_inflight", {})

    async def _bills(limit_per_page=100, mode=None, recent_only=False):
        return [_bill("ord-1")]

    monkeypatch.setattr(m, "_fetch_all_trade_bills", _bills)

    assert await m.sync_exchange_close_trades(mode="demo", force=True) == 1
    assert cache["admin:7"]["ts"] == 1.0


def test_startup_schedules_the_periodic_sync():
    """Regression: the sync used to be one-shot and depended on page views."""
    import inspect

    src = inspect.getsource(m.startup)
    assert "_exchange_sync_loop" in src
    assert "recent_only=not _full" in src


@pytest.mark.asyncio
async def test_recent_pass_skips_orders_at_the_window_edge(monkeypatch):
    """A close whose fills straddle the oldest page must not be upserted partial.

    The freshness pass pages only the newest bills; storing a half-seen order
    overwrote its correct PnL with a smaller one (card drifted by ~3 USDT).
    """
    db = _DB()
    monkeypatch.setattr(m, "db", db)
    monkeypatch.setattr(m, "_exchange_sync_ts", {})
    monkeypatch.setattr(m, "_exchange_close_seen", {})
    monkeypatch.setattr(m, "_exchange_sync_inflight", {})
    # The fetch stopped on a full 5th page → older bills exist but were not read.
    monkeypatch.setattr(m, "_bills_diag",
                        {"demo": {"endpoints": {"bills": {"truncated": True}}}})

    async def _bills(limit_per_page=100, mode=None, recent_only=False):
        edge = _bill("ord-edge", pnl="0.10")
        edge["ts"] = "1793200000000"          # oldest row of the window
        inner = _bill("ord-inner", pnl="5.0")
        inner["ts"] = "1793203600000"         # hours newer
        return [edge, inner]

    monkeypatch.setattr(m, "_fetch_all_trade_bills", _bills)

    assert await m.sync_exchange_close_trades(mode="demo", force=True, recent_only=True) == 1
    assert [r["ord_id"] for r in db.rows[0]] == ["ord-inner"]


@pytest.mark.asyncio
async def test_complete_recent_pass_keeps_every_order(monkeypatch):
    """If the whole window was read (no truncation) nothing needs dropping."""
    db = _DB()
    monkeypatch.setattr(m, "db", db)
    monkeypatch.setattr(m, "_exchange_sync_ts", {})
    monkeypatch.setattr(m, "_exchange_close_seen", {})
    monkeypatch.setattr(m, "_exchange_sync_inflight", {})
    monkeypatch.setattr(m, "_bills_diag",
                        {"demo": {"endpoints": {"bills": {"truncated": False}}}})

    async def _bills(limit_per_page=100, mode=None, recent_only=False):
        return [_bill("ord-only")]

    monkeypatch.setattr(m, "_fetch_all_trade_bills", _bills)

    assert await m.sync_exchange_close_trades(mode="demo", force=True, recent_only=True) == 1
    assert db.rows[0][0]["ord_id"] == "ord-only"


@pytest.mark.asyncio
async def test_recent_pass_aborts_when_a_bill_page_failed(monkeypatch):
    db = _DB()
    monkeypatch.setattr(m, "db", db)
    monkeypatch.setattr(m, "_exchange_sync_ts", {})
    monkeypatch.setattr(m, "_exchange_close_seen", {})
    monkeypatch.setattr(m, "_exchange_sync_inflight", {})
    monkeypatch.setattr(m, "_bills_diag", {"demo": {"endpoints": {"bills": {"error": "429"}}}})

    async def _bills(limit_per_page=100, mode=None, recent_only=False):
        return [_bill("ord-1")]

    monkeypatch.setattr(m, "_fetch_all_trade_bills", _bills)

    assert await m.sync_exchange_close_trades(mode="demo", force=True, recent_only=True) == 0
    assert db.rows == []
