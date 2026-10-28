"""P1 «LIVE-зеркало»: disaster stop (process-death guard).

v1.19 removed tight exchange SL/TP from LIVE because an independent algo could
close LIVE while DEMO stayed open. The guard added here sits *beyond* the
strategy stop, so normal management never reaches it — it only matters if the
bot dies with a position open. These tests pin that contract down.
"""
import inspect

import pytest

from app.bots.ai_discretionary.strategy import AIConfig, AIPosition, AIStrategy


class _FakeAlgo:
    def __init__(self, error: str = ""):
        self.calls = []
        self.error = error

    async def place_algo_order(self, **kw):
        self.calls.append(kw)
        if self.error:
            return {"error": True, "message": self.error}
        return {"data": [{"algoId": "algo-42"}]}

    async def cancel_algo_order(self, inst_id, algo_id):
        self.calls.append({"inst_id": inst_id, "cancel": algo_id})
        return {"data": [{}]}


def _bot():
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot.db = None
    bot._live_audit = []
    return bot


def _pos(side="long", entry=100.0, stop=98.0):
    return AIPosition(
        coin="ETH", inst_id="ETH-USDT-SWAP", side=side, size=1.0,
        entry_price=entry, stop_price=stop,
        take_price=entry * 1.06 if side == "long" else entry * 0.94,
        leverage=3.0, opened_at="2026-10-28T00:00:00+00:00",
        demo_trade_id="a1b2c3d4e5f6",
    )


# ── price is strictly beyond the strategy stop ─────────────────────────

def test_disaster_px_is_wider_than_the_strategy_stop():
    bot = _bot()
    # strategy stop 2% below entry
    px = bot._live_disaster_px("long", 100.0, 0.02)
    assert px < 98.0                     # long guard sits further below
    assert pytest.approx(px) == 100.0 * (1 - 0.02 * 1.8)


def test_disaster_px_short_sits_above_entry():
    bot = _bot()
    px = bot._live_disaster_px("short", 100.0, 0.02)
    assert px > 102.0


def test_disaster_px_respects_the_extra_floor_and_cap():
    bot = _bot()
    bot.config.live_disaster_stop_mult = 1.0
    bot.config.live_disaster_stop_extra = 0.01
    assert pytest.approx(bot._live_disaster_px("long", 100.0, 0.02)) == 97.0

    bot.config.live_disaster_stop_mult = 10.0
    bot.config.live_disaster_stop_max = 0.15
    assert pytest.approx(bot._live_disaster_px("long", 100.0, 0.02)) == 85.0


def test_disaster_px_never_returns_zero_for_a_valid_entry():
    bot = _bot()
    assert bot._live_disaster_px("long", 0.0, 0.02) == 0.0
    # missing stop_pct still yields a guard from min_stop_pct
    assert bot._live_disaster_px("long", 100.0, 0.0) > 0


# ── order shape ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disaster_stop_is_reduce_only_and_sl_only():
    bot = _bot()
    client = _FakeAlgo()
    pos = _pos()
    await bot._place_live_disaster_stop(client, pos)

    call = client.calls[0]
    assert call["reduce_only"] is True
    assert call["cxl_on_close_pos"] is True
    assert call["sl_trigger_px"]
    assert "tp_trigger_px" not in call
    assert call["cl_ord_id"] == "ailsa1b2c3d4e5f6"
    assert pos.disaster_algo_id == "algo-42"
    assert pos.disaster_stop_px > 0


@pytest.mark.asyncio
async def test_disaster_stop_sells_a_long():
    bot = _bot()
    client = _FakeAlgo()
    pos = _pos("long")
    await bot._place_live_disaster_stop(client, pos)
    assert client.calls[0]["side"] == "sell"
    assert client.calls[0]["pos_side"] == "long"


@pytest.mark.asyncio
async def test_disaster_stop_failure_is_audited_not_raised():
    bot = _bot()
    client = _FakeAlgo(error="algo rejected")
    pos = _pos()
    await bot._place_live_disaster_stop(client, pos)
    assert pos.disaster_algo_id == ""
    assert any(a["action"] == "disaster_stop" and a["ok"] is False
               for a in bot._live_audit)


@pytest.mark.asyncio
async def test_ensure_does_not_duplicate_an_existing_guard():
    bot = _bot()
    client = _FakeAlgo()
    pos = _pos()
    await bot._ensure_live_disaster_stop(client, pos)
    await bot._ensure_live_disaster_stop(client, pos)
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_cancel_clears_the_guard():
    bot = _bot()
    client = _FakeAlgo()
    pos = _pos()
    await bot._place_live_disaster_stop(client, pos)
    await bot._cancel_live_disaster_stop(client, pos)
    assert client.calls[-1]["cancel"] == "algo-42"
    assert pos.disaster_algo_id == ""
    assert pos.disaster_stop_px == 0.0


# ── wiring ──────────────────────────────────────────────────────────────

def test_open_live_places_the_guard():
    src = inspect.getsource(AIStrategy._open_live)
    assert "_place_live_disaster_stop" in src
    assert "deliberately NOT placing exchange-level SL/TP" not in src


def test_close_live_cancels_the_guard_before_closing():
    src = inspect.getsource(AIStrategy._close_live)
    assert "_cancel_live_disaster_stop" in src


def test_adoption_paths_ensure_the_guard():
    assert "_ensure_live_disaster_stop" in inspect.getsource(
        AIStrategy._reconcile_live_from_exchange)
    assert "_ensure_live_disaster_stop" in inspect.getsource(
        AIStrategy._hydrate_from_db)


def test_status_surfaces_unprotected_live_positions():
    src = inspect.getsource(AIStrategy.get_status)
    assert "unprotected_open" in src
    assert "protected" in src