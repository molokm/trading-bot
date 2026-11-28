"""v1.43.1 — post-only (maker) entry.

Market orders pay the spread on both legs; a resting limit earns it. The maker
order must never block a trade: rejected / unfilled orders fall back to the
same market order the bot used before.
"""
import pytest

from app.bots.ai_discretionary.strategy import (
    AIConfig, AIStrategy, _best_entry_px, _round_entry_px, _tick_size,
)


class _Client:
    demo = True

    def __init__(self, place_resp, order_resp=None, error_on_place=False):
        self.place_resp = place_resp
        self.order_resp = order_resp or {"data": []}
        self.error_on_place = error_on_place
        self.placed = []
        self.cancelled = []

    async def place_order(self, **kw):
        self.placed.append(kw)
        if self.error_on_place:
            raise RuntimeError("boom")
        return self.place_resp

    async def get_order(self, inst, ord_id=None, cl_ord_id=None):
        return self.order_resp

    async def cancel_order(self, inst, ord_id):
        self.cancelled.append(ord_id)
        return {"code": "0"}


def _bot():
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot.config.maker_wait_sec = 0
    bot._latest_indicators = {"ETH": {"close": 2500.0}}
    bot._latest_books = {}
    bot._mirror_call_active = False
    bot._positions = {}
    bot._live_positions = {}
    bot._fmt_sz = lambda coin, sz: str(sz)
    bot._market_calls = []

    async def _market(client, inst_id, side, sz, pos_side, is_close=False, cl_ord_id=None):
        bot._market_calls.append((inst_id, side, sz))
        return {"data": [{"ordId": "mkt", "avgPx": "2500", "fillSz": str(sz)}]}

    bot._place = _market
    return bot


# ── price helpers ────────────────────────────────────────────────────────

def test_tick_size_known_and_fallback():
    assert _tick_size("BTC") == 0.01
    assert _tick_size("XRP") == 0.001
    assert _tick_size("NOPE") == 0.01


def test_entry_price_snaps_to_grid_in_trade_direction():
    assert _round_entry_px(100.019, "ETH", "long") == 100.01
    assert _round_entry_px(100.011, "ETH", "short") == 100.02


def test_best_entry_px_is_passive():
    assert _best_entry_px(100.0, "ETH", "long") < 100.0
    assert _best_entry_px(100.0, "ETH", "short") > 100.0


# ── post-only execution ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_immediate_maker_fill_returns_fill_without_market():
    bot = _bot()
    node = {"data": [{"ordId": "mk1", "avgPx": "2499.9", "fillSz": "1"}]}
    client = _Client(node)
    resp = await bot._place_postonly(client, "ETH-USDT-SWAP", "buy", 1,
                                     "long", cl_ord_id="c1")
    assert resp["data"][0]["avgPx"] == "2499.9"
    assert bot._market_calls == []
    assert client.placed[0]["ord_type"] == "post_only"
    assert client.placed[0]["px"]  # limit price always set


@pytest.mark.asyncio
async def test_s_code_rejection_falls_back_to_market():
    bot = _bot()
    node = {"data": [{"ordId": "mk1", "sCode": "51121",
                      "sMsg": "post only would fill immediately"}]}
    client = _Client(node)
    resp = await bot._place_postonly(client, "ETH-USDT-SWAP", "buy", 1,
                                     "long", cl_ord_id="c1")
    assert resp["data"][0]["ordId"] == "mkt"
    assert len(bot._market_calls) == 1


@pytest.mark.asyncio
async def test_resting_order_filled_on_confirm_is_kept():
    bot = _bot()
    node = {"data": [{"ordId": "mk1"}]}          # resting ack, no avgPx
    bot_wait = _Client(node, order_resp={
        "data": [{"state": "filled", "avgPx": "2499.5", "accFillSz": "1"}],
    })
    resp = await bot._place_postonly(bot_wait, "ETH-USDT-SWAP", "buy", 1,
                                     "long", cl_ord_id="c1")
    assert resp["data"][0]["avgPx"] == "2499.5"
    assert bot._market_calls == []
    assert bot_wait.cancelled == []


@pytest.mark.asyncio
async def test_resting_order_unfilled_is_cancelled_then_marketed():
    bot = _bot()
    node = {"data": [{"ordId": "mk1"}]}
    client = _Client(node, order_resp={"data": [{"state": "live"}]})
    resp = await bot._place_postonly(client, "ETH-USDT-SWAP", "buy", 1,
                                     "long", cl_ord_id="c1")
    assert client.cancelled == ["mk1"]
    assert resp["data"][0]["ordId"] == "mkt"
    assert len(bot._market_calls) == 1


@pytest.mark.asyncio
async def test_place_exception_falls_back_to_market():
    bot = _bot()
    client = _Client({}, error_on_place=True)
    resp = await bot._place_postonly(client, "ETH-USDT-SWAP", "buy", 1,
                                     "long", cl_ord_id="c1")
    assert resp["data"][0]["ordId"] == "mkt"
