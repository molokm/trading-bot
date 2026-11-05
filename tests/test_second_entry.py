"""Second-slot gate tests (v1.33).

Opening a 2nd concurrent position is allowed only when trade #1 already pays
for itself and the new signal is strong AND uncorrelated. These tests pin the
gate rules so a future refactor cannot silently turn it into "always 2x".
"""
from types import SimpleNamespace

import pytest

from app.bots.ai_discretionary.strategy import AIConfig, AIStrategy


def _pos(coin="ETH", side="short", entry=2400.0, size=2.5):
    return SimpleNamespace(coin=coin, side=side, entry_price=entry,
                           size=size, inst_id=f"{coin}-USDT-SWAP")


def _bot(positions=None, inds=None, **cfg):
    b = SimpleNamespace(config=AIConfig(**cfg), _positions=positions or {},
                        _latest_indicators=inds or {})
    b._unrealized_pct = lambda p, px: AIStrategy._unrealized_pct(b, p, px)
    b._round_trip_cost_pct = lambda: AIStrategy._round_trip_cost_pct(b)
    return b


def _veto(bot, coin, side="long", conf=0.9):
    return AIStrategy._second_entry_veto(bot, coin, side, conf)


GREEN = {"ETH": {"close": 2340.0}}   # ETH short from 2400 -> +2.5%


def test_disabled_blocks_second_entry():
    b = _bot({"ETH": _pos()}, GREEN, second_entry_enabled=False)
    assert _veto(b, "SOL") == "disabled"


def test_no_second_entry_while_flat():
    b = _bot({}, {}, second_entry_enabled=True)
    assert _veto(b, "SOL").startswith("open_n")


def test_second_entry_blocked_when_first_trade_red():
    b = _bot({"ETH": _pos()}, {"ETH": {"close": 2450.0}})
    assert _veto(b, "SOL").startswith("first_trade_not_green")


def test_same_coin_is_rejected():
    b = _bot({"ETH": _pos()}, GREEN)
    assert _veto(b, "ETH", side="short") == "same_coin"


def test_correlated_group_is_rejected():
    # SOL and AVAX are both crypto_alt/layer1 -> 2x beta, not diversification
    b = _bot({"SOL": _pos("SOL")}, {"SOL": {"close": 1.0}})
    ind = {"AVAX": {"align_long": 0.9, "adx": 30.0}}
    b._latest_indicators.update(ind)
    assert _veto(b, "AVAX").startswith("correlated:")


def test_weak_align_is_rejected():
    b = _bot({"ETH": _pos()}, {**GREEN, "DOGE": {"align_long": 0.7, "adx": 30.0}})
    assert _veto(b, "DOGE").startswith("weak_align")


def test_low_adx_is_rejected():
    b = _bot({"ETH": _pos()}, {**GREEN, "DOGE": {"align_long": 0.9, "adx": 18.0}})
    assert _veto(b, "DOGE").startswith("low_adx")


def test_btc_is_correlated_with_eth():
    b = _bot({"ETH": _pos()}, {**GREEN, "BTC": {"align_long": 0.9, "adx": 30.0}})
    assert _veto(b, "BTC", conf=0.9) == "correlated:crypto_major"


def test_strong_uncorrelated_setup_is_allowed():
    # ETH (crypto_major) + DOGE (crypto_meme) = independent enough
    b = _bot({"ETH": _pos()}, {**GREEN, "DOGE": {"align_long": 0.9, "adx": 30.0}})
    assert _veto(b, "DOGE", conf=0.85) is None


def test_low_confidence_is_rejected():
    b = _bot({"ETH": _pos()}, {**GREEN, "DOGE": {"align_long": 0.9, "adx": 30.0}})
    assert _veto(b, "DOGE", conf=0.6).startswith("low_conf")


def test_second_slot_sizing_is_half_risk_and_30pct_margin():
    b = _bot({"ETH": _pos()}, GREEN)
    eq = 10000.0
    base_sz, base_lev = AIStrategy._size_order(b, "BTC", 60000.0, 0.02, equity=eq)
    sz2, lev2 = AIStrategy._size_order(
        b, "BTC", 60000.0, 0.02, equity=eq,
        risk_pct=b.config.second_entry_risk_pct,
        margin_pct=b.config.second_entry_margin_pct)
    assert sz2 < base_sz
    # risk 0.6% / stop 2% = 30% notional, margin cap 30% -> <= 30% of equity
    margin = (sz2 * 0.01 * 60000.0) / lev2
    assert margin <= 0.30 * eq + 1e-6
    assert AIStrategy._size_order(b, "BTC", 60000.0, 0.02, equity=eq)[1] >= lev2


def test_second_slot_opens_through_the_single_chokepoint():
    import inspect
    src = inspect.getsource(AIStrategy._try_second_entry)
    assert "await self._open(" in src, "2nd slot must open via _open() (DEMO-first + mirror)"
    assert "_live_market_order" not in src
    assert "_open_live" not in src
    sig = inspect.signature(AIStrategy._open)
    assert "second" in sig.parameters
