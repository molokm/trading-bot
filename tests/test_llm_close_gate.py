"""v1.44.5 — LLM close gate: close only on evidence, not on noise.

Live 10–14 Nov: 18 of 24 exits were the model's "closed" (WR 3/18, −28.9 USD)
and the price kept moving in the trade's direction afterwards (+0.66% 6h,
+1.30% 12h, +2.04% 24h). The old gate allowed a close at upl <= −0.35% or after
90 flat minutes, which is inside normal 1H noise.
"""
from app.bots.ai_discretionary.strategy import AIConfig, AIStrategy


def _bot(adx=30.0, **cfg):
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    for k, v in cfg.items():
        setattr(bot.config, k, v)
    bot._latest_indicators = {"BTC": {"adx": adx}}
    return bot


def test_deep_loss_may_close():
    # a real loss (at/over the stop budget) is a valid reason to leave
    bot = _bot()
    assert bot._llm_close_allowed("BTC", -1.5, 20) == "deep_loss"
    assert bot._llm_close_allowed("BTC", -1.2, 20) == "deep_loss"


def test_mild_loss_is_blocked():
    # this was the noisy exit: −0.35% is inside 1H noise
    bot = _bot()
    assert bot._llm_close_allowed("BTC", -0.35, 10) == ""
    assert bot._llm_close_allowed("BTC", -1.0, 300) == ""
    assert bot._llm_close_allowed("BTC", 0.05, 300) == ""


def test_banked_profit_may_close():
    bot = _bot()
    assert bot._llm_close_allowed("BTC", 5.0, 30) == "profit_take"
    assert bot._llm_close_allowed("BTC", 7.5, 30) == "profit_take"


def test_aged_flat_trade_closes_only_in_a_dead_trend():
    dead = _bot(adx=10.0)
    assert dead._llm_close_allowed("BTC", 0.10, 260) == "flat_dead_trend"
    live = _bot(adx=30.0)
    assert live._llm_close_allowed("BTC", 0.10, 260) == ""


def test_young_flat_trade_is_not_aged():
    bot = _bot(adx=10.0)
    assert bot._llm_close_allowed("BTC", 0.10, 200) == ""


def test_dead_trend_requirement_can_be_disabled():
    bot = _bot(adx=40.0, llm_close_flat_require_dead_trend=False)
    assert bot._llm_close_allowed("BTC", 0.10, 260) == "flat_aged"


def test_gate_is_config_switchable():
    bot = _bot(llm_close_enable=False)
    assert bot._llm_close_allowed("BTC", -0.35, 5) == "disabled"


def test_thresholds_come_from_config():
    bot = _bot(llm_close_loss_pct=0.50)
    assert bot._llm_close_allowed("BTC", -0.60, 10) == "deep_loss"
    bot2 = _bot(adx=10.0, llm_close_flat_minutes=60.0, min_hold_minutes=45.0)
    assert bot2._llm_close_allowed("BTC", 0.0, 61) == "flat_dead_trend"


def test_missing_indicators_do_not_crash():
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot._latest_indicators = {}
    # no ADX -> treated as a dead trend (0), so an aged flat trade may close
    assert bot._llm_close_allowed("BTC", 0.0, 300) == "flat_dead_trend"
