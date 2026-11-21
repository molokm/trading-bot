"""v1.44.5 — 51016 dedup: never lose an entry, never double a position.

Live 14 Nov 05:02: a BTC entry died with "Client order ID already exists"
while the exchange held the order. On the duplicate error we first look the
clOrdId up (adopt), and only retry with a fresh id when it is really gone.
"""
import pytest

from app.bots.ai_discretionary.strategy import AIConfig, AIStrategy


class _Client:
    def __init__(self, order_rows):
        self.order_rows = order_rows
        self.looked_up = []

    async def get_order(self, inst, ord_id=None, cl_ord_id=None):
        self.looked_up.append(cl_ord_id)
        return {"data": self.order_rows}


def _bot():
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot.events = []
    bot._record_exec = lambda event, **kw: bot.events.append((event, kw))
    bot.placed = []

    async def _place_postonly(client, inst, side, sz, pos_side, cl_ord_id=None):
        bot.placed.append(cl_ord_id)
        return {"data": [{"ordId": "new1", "avgPx": "100"}]}

    bot._place_postonly = _place_postonly
    return bot


@pytest.mark.asyncio
async def test_existing_order_is_adopted_not_replayed():
    bot = _bot()
    client = _Client([{"ordId": "old1", "state": "live"}])
    resp, cl = await bot._resolve_dup_clordid(
        client, "BTC-USDT-SWAP", "aidabc", coin="BTC", side="short",
        order_side="sell", sz=1.0, pos_side="short",
    )
    assert resp["data"][0]["ordId"] == "old1"
    assert cl == "aidabc"                       # same id for later confirmation
    assert bot.placed == []                     # nothing re-sent → no doubling
    assert bot.events[0][0] == "open_dedup"


@pytest.mark.asyncio
async def test_missing_order_retries_once_with_a_fresh_id():
    bot = _bot()
    client = _Client([])
    resp, cl = await bot._resolve_dup_clordid(
        client, "BTC-USDT-SWAP", "aidabc", coin="BTC", side="short",
        order_side="sell", sz=1.0, pos_side="short",
    )
    assert client.looked_up == ["aidabc"]
    assert len(bot.placed) == 1
    assert cl != "aidabc" and cl.startswith("aid")
    assert resp["data"][0]["ordId"] == "new1"


@pytest.mark.asyncio
async def test_lookup_exception_falls_back_to_a_retry():
    class _Boom(_Client):
        async def get_order(self, inst, ord_id=None, cl_ord_id=None):
            raise RuntimeError("network")

    bot = _bot()
    resp, cl = await bot._resolve_dup_clordid(
        _Boom([]), "BTC-USDT-SWAP", "aidabc", coin="BTC", side="short",
        order_side="sell", sz=1.0, pos_side="short",
    )
    assert len(bot.placed) == 1 and cl != "aidabc"
    assert not resp.get("error")
