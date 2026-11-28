"""Guards for the /api/me/dashboard cache.

Two regressions lived here:
  * the guest warmer awaited an infinite loop, so its seeding code was dead and
    every guest request paid a full blocking rebuild;
  * the stale-while-revalidate block was a stub — it never rebuilt and left
    ``me_dashboard._rebuild_lock`` set forever.
"""
import asyncio
import time

import app.main as mm


class _Bot:
    def get_status(self):
        return {
            "running": True,
            "lifetime_pnl": 12.5,
            "session_pnl": 1.5,
            "equity": 1000.0,
            "capital": 10000.0,
            "lifetime_trades": 7,
            "win_rate": 50.0,
            "open_positions": [],
            "pulse": "p",
        }


def _patch_pnl(monkeypatch, total=99.0):
    async def _pnl(mode="demo"):
        return {"total": total, "1d": 5.0, "trades_counted": 9}

    monkeypatch.setattr(mm, "_get_pnl_cached", _pnl)


def test_seed_skips_when_no_previous_trades(monkeypatch):
    """A "fresh" entry with an empty «Сделки» list would hide trades for a TTL."""
    mm._ME_DASH_CACHE.clear()
    monkeypatch.setattr(mm, "ai_bot", _Bot())
    _patch_pnl(monkeypatch)

    assert asyncio.run(mm._seed_dashboard_cache("guest:shared")) is False
    assert "guest:shared" not in mm._ME_DASH_CACHE


def test_seed_refreshes_guest_entry(monkeypatch):
    mm._ME_DASH_CACHE.clear()
    mm._ME_DASH_CACHE["guest:shared"] = {
        "ts": time.time() - 90,
        "data": {
            "demo": {},
            "live": {"equity": 5.0},
            "trades": [{"pnl": 1.0}],
            "pnl": {},
            "role": "guest",
        },
    }
    monkeypatch.setattr(mm, "ai_bot", _Bot())
    _patch_pnl(monkeypatch)

    assert asyncio.run(mm._seed_dashboard_cache("guest:shared")) is True
    entry = mm._ME_DASH_CACHE["guest:shared"]
    assert entry["data"]["warm"] is True
    assert entry["data"]["trades"] == [{"pnl": 1.0}]   # preserved
    assert entry["data"]["live"] == {"equity": 5.0}    # preserved
    assert entry["data"]["demo"]["pnl"] == 99.0        # refreshed
    assert time.time() - entry["ts"] < 5


def test_seed_ignores_authenticated_keys(monkeypatch):
    """A per-user payload needs its LIVE block — only a real request can build it."""
    mm._ME_DASH_CACHE.clear()
    monkeypatch.setattr(mm, "ai_bot", _Bot())

    assert asyncio.run(mm._seed_dashboard_cache("admin:42")) is False


def test_cache_is_per_key_so_warmer_does_not_thrash_users(monkeypatch):
    """A single slot made every authenticated poll miss after the guest warmer ran."""
    mm._ME_DASH_CACHE.clear()
    monkeypatch.setattr(mm, "ai_bot", _Bot())
    _patch_pnl(monkeypatch)
    mm._ME_DASH_CACHE["guest:shared"] = {
        "ts": time.time(),
        "data": {"trades": [{"pnl": 1.0}], "demo": {}, "live": {}, "pnl": {}},
    }
    asyncio.run(mm._seed_dashboard_cache("guest:shared"))

    # The authenticated entry survives alongside the guest one.
    mm._ME_DASH_CACHE["admin:42"] = {"ts": time.time(), "data": {"trades": []}}
    assert set(mm._ME_DASH_CACHE) == {"guest:shared", "admin:42"}


def test_stale_entry_is_served_and_rebuild_releases_lock(monkeypatch):
    mm._ME_DASH_CACHE.clear()
    mm.me_dashboard._rebuild_lock = False
    stale = {"demo": {"pnl": 1}, "trades": [{"pnl": 1.0}], "role": "guest"}
    mm._ME_DASH_CACHE["guest:shared"] = {"ts": time.time() - 60, "data": stale}

    async def _ctx(request):
        return "guest", None, None

    monkeypatch.setattr(mm, "_me_ctx", _ctx)

    seeded = []

    async def _seed(key):
        seeded.append(key)
        return True

    monkeypatch.setattr(mm, "_seed_dashboard_cache", _seed)

    async def scenario():
        out = await mm.me_dashboard(None)
        assert out is stale
        await asyncio.sleep(0.05)
        assert seeded == ["guest:shared"]
        # The old stub left this True forever.
        assert getattr(mm.me_dashboard, "_rebuild_lock", False) is False

    asyncio.run(scenario())


def test_history_stats_can_skip_exchange_sync(monkeypatch):
    """/api/ai/status cancels at 1.5s — a sync started there never finishes."""
    seen = {}

    async def _stats(*, allow_sync=True):
        seen["allow_sync"] = allow_sync
        return {}

    async def _pnl(mode="demo"):
        return {"total": 1.0, "1d": 0.0, "per_bot": {}}

    monkeypatch.setattr(mm, "_bot_history_stats", _stats)
    monkeypatch.setattr(mm, "_get_pnl_cached", _pnl)

    status = asyncio.run(
        mm._apply_history_kpi({"x": 1}, "AI Discretionary 1H", allow_sync=False))
    assert seen["allow_sync"] is False
    assert status["kpi_from_history"] is True
    assert status["total_pnl"] == 1.0
