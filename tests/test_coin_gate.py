"""v1.44.2 — per-coin performance gate.

The 80-trade sample was not uniform: a handful of coins carried almost all of
the loss while others stayed green. Three straight losses on one coin must raise
that coin's confidence bar for 72h — not ban it, and not slow the whole book.
(LINK printed 6 straight losses and LTC 4 under the old 5-loss/48h bar.)
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
    bot._trade_log = [_close("LTC", -10) for _ in range(2)]
    assert bot._coin_gate_add("LTC") == 0.0
    assert bot._effective_min_confidence("LTC") == 0.72


def test_three_straight_losses_raise_bar():
    bot = _bot()
    bot._trade_log = [_close("LTC", -10) for _ in range(3)]
    assert bot._coin_gate_add("LTC") == 0.15
    assert abs(bot._effective_min_confidence("LTC") - 0.87) < 1e-9


def test_win_breaks_the_streak():
    bot = _bot()
    bot._trade_log = [_close("LTC", -10) for _ in range(4)] + [_close("LTC", 5)]
    assert bot._coin_gate_add("LTC") == 0.0


def test_streak_only_counts_that_coin():
    bot = _bot()
    for _ in range(3):
        bot._trade_log.append(_close("LTC", -10))
        bot._trade_log.append(_close("ETH", 5))
    assert bot._coin_gate_add("LTC") == 0.15
    assert bot._coin_gate_add("ETH") == 0.0


def test_gate_expires_after_ttl():
    bot = _bot()
    bot._trade_log = [_close("LTC", -10, age_h=73) for _ in range(3)]
    assert bot._coin_gate_add("LTC") == 0.0


def test_live_closes_do_not_trigger_demo_gate():
    bot = _bot()
    bot._trade_log = [_close("LTC", -10, account="live") for _ in range(3)]
    assert bot._coin_gate_add("LTC") == 0.0


def test_global_effective_confidence_unchanged_without_coin():
    bot = _bot()
    bot._trade_log = [_close("LTC", -10) for _ in range(3)]
    assert bot._effective_min_confidence() == 0.72


def test_gate_is_capped_so_it_never_becomes_a_ban():
    # adaptive base 0.78 + 0.15 = 0.93 -> nothing passes; cap keeps it at 0.88
    bot = _bot()
    bot._adapt = {"min_confidence": 0.78}
    bot._trade_log = [_close("LTC", -10) for _ in range(3)]
    assert bot._coin_gate_add("LTC") == 0.15
    assert abs(bot._effective_min_confidence("LTC") - 0.88) < 1e-9
    # below the cap the raw penalty applies
    assert abs(bot._gated_min_confidence(0.72, "LTC") - 0.87) < 1e-9
