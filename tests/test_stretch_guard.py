"""v1.44.6 — stretch guard: do not enter when price ran away from EMA21.

Source: ForexFactory "MA Crossover System that works for me" (AlistairC),
rule 3 — never buy/sell once the "rubber band" is stretched. Measured on our
own 1440×1H bars as |close − EMA21| / ATR1H; the cap only bites in a WEAK
trend (ADX < ``stretch_adx_free``), because with a live ADX the extension is
a real trend leg rather than a late chase.

Backtest (14 coins × 60d, cost model of ``backtest_lab``): PF full 0.93→0.97,
older half 1.17→1.20, newer half 0.85→0.90, last 10d 1.86→1.97.
"""
from app.bots.ai_discretionary.strategy import AIConfig, AIStrategy


def _bot(cap=1.4, free=30.0):
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot.config.max_stretch_atr = cap
    bot.config.stretch_adx_free = free
    return bot


def _ind(close=100.0, ema21=100.0, atr=1.0, adx=26.0):
    """ATR 1.0 on a 100 price = 1% ATR, so the distance in % is ATRs."""
    return {
        "close": close,
        "ema21": ema21,
        "ema_fast": ema21,
        "roc": 0.1,
        "roc_3": 0.1,
        # aligned with the impulse so only the stretch gate can reject
        "macd_hist": 1.0,
        "rsi": 55.0,
        "atr": atr,
        "adx": adx,
    }


def test_rejects_long_when_price_is_stretched_in_a_weak_trend():
    bot = _bot()
    reason = bot._momentum_against("long", _ind(close=102.0, ema21=100.0, adx=26.0))
    assert reason and reason.startswith("stretched_from_ema21")


def test_rejects_short_when_price_is_stretched_in_a_weak_trend():
    bot = _bot()
    ind = _ind(close=98.0, ema21=100.0, adx=26.0)
    ind["macd_hist"] = -1.0
    ind["roc"] = ind["roc_3"] = -0.1
    reason = bot._momentum_against("short", ind)
    assert reason and reason.startswith("stretched_from_ema21")


def test_strong_adx_earns_the_right_to_extend():
    bot = _bot()
    assert bot._momentum_against("long", _ind(close=102.0, ema21=100.0, adx=31.0)) is None


def test_entry_near_the_mean_is_allowed_in_a_weak_trend():
    bot = _bot()
    assert bot._momentum_against("long", _ind(close=101.0, ema21=100.0, adx=26.0)) is None


def test_threshold_is_inclusive_at_the_cap():
    bot = _bot()
    # exactly 1.4 ATR away is not "beyond" the cap
    assert bot._momentum_against("long", _ind(close=101.4, ema21=100.0, adx=26.0)) is None


def test_filter_is_config_switchable():
    bot = _bot(cap=0.0)
    assert bot._momentum_against("long", _ind(close=110.0, ema21=100.0, adx=26.0)) is None


def test_zero_free_level_blocks_even_a_strong_trend():
    bot = _bot(cap=1.4, free=0.0)
    reason = bot._momentum_against("long", _ind(close=102.0, ema21=100.0, adx=45.0))
    assert reason and reason.startswith("stretched_from_ema21")


def test_missing_atr_does_not_block_entries():
    bot = _bot()
    assert bot._momentum_against("long", _ind(close=110.0, ema21=100.0, atr=0.0)) is None


def test_missing_ema_does_not_block_entries():
    bot = _bot()
    ind = _ind(close=110.0, ema21=0.0)
    assert bot._momentum_against("long", ind) is None
