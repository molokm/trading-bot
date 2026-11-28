"""v1.44.4 — chase guard: do not enter after the move already ran 2×ATR.

Live 05–10 Nov: BTC shorts fired after a −7% drop and were stopped by the
bounce; ETH was signalled at a 12-bar ROC of −4.6% (≈2.3×ATR1H). The guard
rejects entries whose |12-bar ROC| exceeds ``max_roc_atr_mult`` × ATR1H.
"""
from app.bots.ai_discretionary.strategy import AIConfig, AIStrategy


def _bot(mult=2.0):
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot.config.max_roc_atr_mult = mult
    return bot


def _ind(roc, atr=1.0, close=100.0):
    """ATR 1.0 on a 100 price = 1% ATR, so |roc| in % is the ATR ratio."""
    return {
        "close": close,
        "ema21": close,
        "ema_fast": close,
        "roc": roc,
        "roc_3": roc,
        "macd_hist": -1.0,
        "rsi": 45.0,
        "atr": atr,
    }


def test_allows_entry_inside_the_move():
    bot = _bot()
    assert bot._momentum_against("short", _ind(-1.5)) is None
    assert bot._momentum_against("long", _ind(1.5)) is None


def test_rejects_late_short_after_a_big_drop():
    bot = _bot()
    reason = bot._momentum_against("short", _ind(-5.0))
    assert reason and reason.startswith("overextended_roc")


def test_rejects_late_long_after_a_big_run():
    bot = _bot()
    reason = bot._momentum_against("long", _ind(5.0))
    assert reason and reason.startswith("overextended_roc")


def test_filter_is_config_switchable():
    bot = _bot(mult=0.0)
    assert bot._momentum_against("short", _ind(-5.0)) is None


def test_missing_atr_does_not_block_entries():
    bot = _bot()
    ind = _ind(-5.0)
    ind["atr"] = 0.0
    assert bot._momentum_against("short", ind) is None
