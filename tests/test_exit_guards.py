"""v1.43.1 — exit guards.

D: thesis_break only fires on a DEAD trend (ADX below the floor); a 2-of-3
   soft flip inside a live trend is noise and used to cut trades that resumed.
E: a failed trade takes half off at −1×ATR instead of letting the full stop
   take the whole position. One-shot, and the remainder keeps its stop.
"""
import pytest

from app.bots.ai_discretionary.strategy import AIConfig, AIPosition, AIStrategy


def _bot():
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot.config.min_hold_minutes = 120.0
    bot._positions = {}
    bot._live_positions = {}
    bot._mirror_call_active = False
    return bot


def _pos(side="long", size=2.0, entry=100.0, stop=98.0):
    return AIPosition(
        coin="ETH", inst_id="ETH-USDT-SWAP", side=side, size=size,
        entry_price=entry, stop_price=stop, take_price=110.0, leverage=3,
        opened_at="2026-11-02T00:00:00+00:00",
    )


# ── D: thesis break only on a dead trend ────────────────────────────────

def _broken_ind(adx):
    return {
        "close": 98.0,        # below ema21*0.9985 for a long
        "ema21": 100.0,
        "macd_hist": -1.0,    # flipped down
        "roc_3": -1.0,        # flipped down
        "adx": adx,
    }


def test_thesis_break_fires_when_trend_is_dead():
    bot = _bot()
    pos = _pos()
    reason = bot._thesis_break_reason(pos, _broken_ind(10.0), upl=-0.5, held_min=180)
    assert reason and reason.startswith("thesis_break:")


def test_thesis_break_skipped_while_trend_is_alive():
    bot = _bot()
    pos = _pos()
    assert bot._thesis_break_reason(pos, _broken_ind(25.0), upl=-0.5, held_min=180) is None
    # exact floor is alive too
    assert bot._thesis_break_reason(pos, _broken_ind(18.0), upl=-0.5, held_min=180) is None


def test_thesis_break_can_be_restored_to_old_behaviour():
    bot = _bot()
    bot.config.thesis_break_require_dead_trend = False
    pos = _pos()
    assert bot._thesis_break_reason(pos, _broken_ind(25.0), upl=-0.5, held_min=180)


def test_thesis_break_still_respects_min_hold():
    bot = _bot()
    pos = _pos()
    assert bot._thesis_break_reason(pos, _broken_ind(10.0), upl=-0.5, held_min=60) is None


# ── E: −1×ATR loss partial ──────────────────────────────────────────────

class _Client:
    demo = True

    async def place_order(self, **kw):
        return {"data": [{"ordId": "x", "avgPx": "99", "fillSz": str(kw.get("sz"))}]}


async def _noop(*a, **k):
    return None


def _loss_bot():
    bot = _bot()
    pos = _pos()
    bot._positions = {"ETH": pos}
    bot._execute_enabled = lambda: True
    bot._place = lambda *a, **k: _fake_place(*a, **k)
    bot._book_partial = _noop
    bot._mirror_enabled = _false
    bot._persist_peaks = _noop
    return bot, pos


async def _fake_place(*a, **k):
    return {"data": [{"ordId": "c1", "avgPx": "99", "fillSz": "1"}]}


async def _false():
    return False


@pytest.mark.asyncio
async def test_loss_partial_takes_half_and_is_one_shot():
    bot, pos = _loss_bot()
    ok = await bot._scale_out_partial(_Client(), "ETH", 0.5, "loss_partial_atr", kind="loss")
    assert ok
    assert pos.size == 1.0
    assert pos.loss_partial_done is True
    assert pos.partial_taken is False
    # second call is a no-op
    assert not await bot._scale_out_partial(_Client(), "ETH", 0.5, "again", kind="loss")


@pytest.mark.asyncio
async def test_profit_partial_flag_does_not_block_loss_partial():
    bot, pos = _loss_bot()
    pos.partial_taken = True
    ok = await bot._scale_out_partial(_Client(), "ETH", 0.5, "loss", kind="loss")
    assert ok
    assert pos.loss_partial_done is True


@pytest.mark.asyncio
async def test_profit_partial_still_one_shot():
    bot, pos = _loss_bot()
    pos.size = 2.0
    ok = await bot._scale_out_partial(_Client(), "ETH", 0.3, "partial", kind="profit")
    assert ok
    assert pos.partial_taken is True
    assert not await bot._scale_out_partial(_Client(), "ETH", 0.3, "again", kind="profit")
