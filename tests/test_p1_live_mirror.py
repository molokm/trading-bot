"""P1 «LIVE-зеркало»: paired close tag + mirror audit classification.

Audit finding this locks down: `_close_live` passed `cl_ord_id=None`, so
`_place` generated `ai{ms}` for every LIVE close. The mirror audit therefore
reported ``mirror=0, ai_other=N`` — mirror closes were indistinguishable from
untagged AI orders and could not be paired with their DEMO twin. DEMO closes had
the same problem (`ai{ms}` instead of the open's trade id).
"""
import inspect

import pytest

from app.bots.ai_discretionary.strategy import AIStrategy
from app.services.pnl_engine import label_from_clord


TID = "a1b2c3d4e5f6"


# ── tag format + round-trip ─────────────────────────────────────────────

def test_live_close_tag_is_a_mirror_tag():
    cl = AIStrategy._clord_live_close(TID)
    assert cl == f"ail{TID}c"
    assert label_from_clord(cl) == "AI Discretionary 1H"


def test_demo_close_tag_is_an_ai_tag():
    cl = AIStrategy._clord_demo_close(TID)
    assert cl == f"aid{TID}c"
    assert label_from_clord(cl) == "AI Discretionary 1H"


def test_close_tags_round_trip_to_the_same_trade_id():
    # This is what makes a DEMO close pairable with its LIVE twin.
    assert AIStrategy._trade_id_from_clord(AIStrategy._clord_demo_close(TID)) == TID
    assert AIStrategy._trade_id_from_clord(AIStrategy._clord_live_close(TID)) == TID
    # And both still match the open tags of the same slot.
    assert AIStrategy._trade_id_from_clord(AIStrategy._clord_demo(TID)) == TID
    assert AIStrategy._trade_id_from_clord(AIStrategy._clord_live(TID)) == TID


def test_close_tag_differs_from_open_tag():
    # OKX wants clOrdId unique among pending orders → close must not reuse open.
    assert AIStrategy._clord_live_close(TID) != AIStrategy._clord_live(TID)


def test_unknown_trade_id_keeps_fallback():
    # No id → empty string, so `_place` keeps its ai{ms} fallback instead of
    # fabricating a link to a trade that never existed.
    assert AIStrategy._clord_live_close("") == ""
    assert AIStrategy._clord_demo_close("") == ""
    assert AIStrategy._clord_live_close(None) == ""
    assert AIStrategy._clord_demo_close(None) == ""


# ── wiring: the tags actually reach the orders / DB ─────────────────────

def test_close_live_sends_the_mirror_tag():
    src = inspect.getsource(AIStrategy._close_live)
    assert "_clord_live_close" in src
    assert "cl_ord_id=(_cl_close or None)" in src


def test_close_live_stores_the_same_tag_in_db():
    src = inspect.getsource(AIStrategy._close_live)
    assert "cl = _cl_close or" in src
    assert 'cl = f"ai{int(time.time() * 1000)}"' not in src


def test_demo_close_sends_the_demo_tag():
    src = inspect.getsource(AIStrategy._close)
    assert "_clord_demo_close" in src


def test_mirror_audit_classifies_ail_as_mirror():
    import app.main as m

    src = inspect.getsource(m.ai_status)
    assert "startswith('ail')" in src
    assert "startswith('ai')" in src


def test_live_sync_runs_from_status_using_the_fresh_client():
    # The module-global live_manager is stale: /api/live/connect can bind a
    # manager straight onto ai_bot. Status must resolve the freshest client.
    import app.main as m

    src = inspect.getsource(m.ai_status)
    assert "_live_read_client()" in src
    assert "sync_exchange_close_trades(mode='live')" in src


# ── root cause: per-mode sync stamp ─────────────────────────────────────

def _bill(oid, clord, pnl="1.5"):
    return {
        "ordId": oid, "instId": "BTC-USDT-SWAP", "subType": "5",
        "pnl": pnl, "fee": "-0.1", "sz": "1", "avgPx": "100",
        "ts": "1793200000000", "clOrdId": clord,
    }


@pytest.mark.asyncio
async def test_exchange_sync_stamp_is_per_mode_not_a_float(monkeypatch):
    """Regression: the stamp used to replace the dict with a float.

    `_exchange_sync_ts = _time.time()` made the next `.get()` raise
    AttributeError, so after the first (demo) sync of each process every later
    sync died — live bills never reached exchange_close_trades (14 vs 49).
    """
    import app.main as m

    class _DB:
        async def upsert_exchange_close_trades(self, rows):
            return len(rows)

    async def _bills(limit_per_page=100, mode=None):
        return [_bill(f"ord-{mode}", "aidabc123")]

    monkeypatch.setattr(m, "db", _DB())
    monkeypatch.setattr(m, "_fetch_all_trade_bills", _bills)
    monkeypatch.setattr(m, "_exchange_sync_ts", {})
    monkeypatch.setattr(m, "_sync_diag", {})

    assert await m.sync_exchange_close_trades(mode="demo") == 1
    assert isinstance(m._exchange_sync_ts, dict)
    assert "demo" in m._exchange_sync_ts

    # A second mode must still run — the old float broke this line.
    assert await m.sync_exchange_close_trades(mode="live") == 1
    assert "live" in m._exchange_sync_ts
    assert {"demo", "live"} <= set(m._sync_diag)


@pytest.mark.asyncio
async def test_sync_ttl_is_tracked_separately_for_demo_and_live(monkeypatch):
    import app.main as m

    class _DB:
        calls = 0

        async def upsert_exchange_close_trades(self, rows):
            _DB.calls += 1
            return len(rows)

    async def _bills(limit_per_page=100, mode=None):
        return [_bill(f"ord-{mode}", "aidabc123")]

    monkeypatch.setattr(m, "db", _DB())
    monkeypatch.setattr(m, "_fetch_all_trade_bills", _bills)
    monkeypatch.setattr(m, "_exchange_sync_ts", {})
    monkeypatch.setattr(m, "_sync_diag", {})

    await m.sync_exchange_close_trades(mode="demo")
    assert await m.sync_exchange_close_trades(mode="demo") == 0  # TTL
    assert await m.sync_exchange_close_trades(mode="live") == 1  # not blocked


# ── never rebind the shared client_manager to LIVE keys ─────────────────

@pytest.mark.asyncio
async def test_live_reads_prefer_the_dedicated_live_manager(monkeypatch):
    import app.main as m

    class _Client:
        demo = False

        async def ping(self):
            return "live"

    class _Mgr:
        def __init__(self, client):
            self._client = client

        def get_client(self):
            return self._client

    class _Shared:
        def __init__(self):
            self.inited = False

        def get_client(self):
            return None

        async def init_client(self, *a, **k):
            self.inited = True

    async def _noop():
        return None

    shared = _Shared()
    monkeypatch.setattr(m, "live_manager", _Mgr(_Client()))
    monkeypatch.setattr(m, "client_manager", shared)
    monkeypatch.setattr(m, "_load_live_creds_from_db", _noop)

    assert await m._okx_call_account(lambda c: c.ping(), mode="live") == "live"
    assert shared.inited is False, "shared client_manager must stay demo"
    assert m._live_read_client() is not None
