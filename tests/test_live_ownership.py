"""LIVE safety: the mirror may only touch positions it opened itself."""
import pytest

from app.bots.ai_discretionary.strategy import AIConfig, AIStrategy


class FakeLive:
    demo = False

    def __init__(self, *, positions=None, fills=None, algos=None):
        self.positions = positions or []
        self.fills = fills or []
        self.algos = algos or []
        self.orders = []
        self.cancelled = []
        self.get_positions_calls = 0

    async def get_positions(self, inst_id=None, inst_type=None):
        self.get_positions_calls += 1
        return {"data": self.positions}

    async def get_fills(self, inst_id=None, limit=20):
        return {"data": self.fills}

    async def get_algo_orders(self, **kwargs):
        return {"data": self.algos}

    async def cancel_algo_order(self, inst, algo):
        self.cancelled.append(algo)
        return {"data": [{"algoId": algo}]}

    async def place_order(self, *a, **k):
        self.orders.append((a, k))
        return {"data": [{"ordId": "1", "fillPx": "100", "fillSz": "1"}]}

    async def get_ticker(self, inst):
        return {"data": [{"last": "100"}]}


def _bot():
    return AIStrategy(config=AIConfig())


@pytest.mark.asyncio
async def test_open_live_requires_demo_twin():
    bot = _bot()
    lc = FakeLive()

    async def _enabled():
        return True

    bot._mirror_enabled = _enabled
    bot._live_client = lambda: lc
    bot._positions = {}
    ok = await bot._open_live("BTC", "long", 0.02, 0.04, reason="test")
    assert ok is False
    assert lc.orders == []


@pytest.mark.asyncio
async def test_ownership_classifies_fills():
    bot = _bot()
    bot._positions = {}
    bot._live_positions = {}
    ours = FakeLive(fills=[{"clOrdId": "ail123abc", "subType": "3"}])
    assert await bot._live_ownership(ours, "BTC-USDT-SWAP") == "ai"

    manual = FakeLive(fills=[{"clOrdId": "web_20260101", "subType": "1"}])
    assert await bot._live_ownership(manual, "BTC-USDT-SWAP") == "manual"

    unknown = FakeLive(fills=[{"clOrdId": "", "subType": "1"}])
    assert await bot._live_ownership(unknown, "BTC-USDT-SWAP") == "unknown"

    err = FakeLive()
    err.get_fills = lambda **k: (_ for _ in ()).throw(RuntimeError("boom"))
    assert await bot._live_ownership(err, "BTC-USDT-SWAP") == "unknown"


@pytest.mark.asyncio
async def test_purge_algos_only_cancels_ours():
    bot = _bot()
    lc = FakeLive(algos=[
        {"instId": "BTC-USDT-SWAP", "algoId": "a1", "clOrdId": "ai123"},
        {"instId": "BTC-USDT-SWAP", "algoId": "a2", "clOrdId": ""},
        {"instId": "ETH-USDT-SWAP", "algoId": "a3", "clOrdId": "user_web"},
    ])
    n = await bot._purge_live_algos(lc)
    assert n >= 1
    assert set(lc.cancelled) == {"a1"}


@pytest.mark.asyncio
async def test_close_live_memory_miss_never_flattens_manual():
    bot = _bot()

    async def _enabled():
        return True

    bot._mirror_enabled = _enabled
    lc = FakeLive(
        positions=[{"instId": "BTC-USDT-SWAP", "pos": "1", "posSide": "long"}],
        fills=[{"clOrdId": "web_1", "subType": "1"}],
    )
    bot._live_client = lambda: lc
    bot._live_positions = {}
    ok = await bot._close_live("BTC", reason="demo_close")
    assert ok is False
    assert lc.orders == []


@pytest.mark.asyncio
async def test_close_live_closes_own_mirror():
    bot = _bot()

    async def _enabled():
        return True

    bot._mirror_enabled = _enabled
    lc = FakeLive(
        positions=[{"instId": "BTC-USDT-SWAP", "pos": "1", "posSide": "long"}],
        fills=[{"clOrdId": "ail123abc", "subType": "3"}],
    )
    bot._live_client = lambda: lc
    bot._live_positions = {}
    ok = await bot._close_live("BTC", reason="demo_close")
    assert ok is True
