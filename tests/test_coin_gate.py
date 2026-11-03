"""v1.43.1 — per-coin performance gate.

The 80-trade sample was not uniform: a handful of coins carried almost all of
the loss while others stayed green. Five straight losses on one coin must raise
that coin's confidence bar for 48h — not ban it, and not slow the whole book.
"""
from datetime import datetime, timedelta, timezone

from app.bots.ai_discretionary.strategy import AIConfig, AIStrategy


def _bot(cap=10_000.0):
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot.config.min_confidence = 0.72
    bot._trade_log = []
    bot._adapt = {}
    bot._capital = cap
    return bot


def _close(coin, pnl, age_h=0.0, account="demo"):
    ts = datetime.now(timezone.utc) - timedelta(hours=age_h)
    return {
        "symbol": f"{coin}-USDT-SWAP",
        "pnl": pnl,
        "reason": "stop" if pnl < 0 else "take",
        "time": ts.isoformat(),
        "account_mode": account,
    }


def test_no_gate_with_fewer_than_streak():
    bot = _bot()
    bot._trade_log = [_close("LTC", -10) for _ in range(4)]
    assert bot._coin_gate_add("LTC") == 0.0
    assert bot._effective_min_confidence("LTC") == 0.72


def test_five_straight_losses_raise_bar():
    bot = _bot()
    bot._trade_log = [_close("LTC", -10) for _ in range(5)]
    assert bot._coin_gate_add("LTC") == 0.10
    assert abs(bot._effective_min_confidence("LTC") - 0.82) < 1e-9


def test_win_breaks_the_streak():
    bot = _bot()
    bot._trade_log = [_close("LTC", -10) for _ in range(4)] + [_close("LTC", 5)]
    assert bot._coin_gate_add("LTC") == 0.0


def test_streak_only_counts_that_coin():
    bot = _bot()
    for _ in range(5):
        bot._trade_log.append(_close("LTC", -10))
        bot._trade_log.append(_close("ETH", 5))
    assert bot._coin_gate_add("LTC") == 0.10
    assert bot._coin_gate_add("ETH") == 0.0


def test_gate_expires_after_ttl():
    bot = _bot()
    bot._trade_log = [_close("LTC", -10, age_h=49) for _ in range(5)]
    assert bot._coin_gate_add("LTC") == 0.0


def test_live_closes_do_not_trigger_demo_gate():
    bot = _bot()
    bot._trade_log = [_close("LTC", -10, account="live") for _ in range(5)]
    assert bot._coin_gate_add("LTC") == 0.0


def test_global_effective_confidence_unchanged_without_coin():
    bot = _bot()
    bot._trade_log = [_close("LTC", -10) for _ in range(5)]
    assert bot._effective_min_confidence() == 0.72
