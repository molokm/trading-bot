"""Cost-model tests (v1.32).

The LLM decision loop previously had no notion of commissions: a +0.10% gross
"win" looked profitable while a 0.05% taker fee on both legs made it negative.
These tests pin the cost arithmetic and the turnover metric exposed in status.
"""
from types import SimpleNamespace

from app.bots.ai_discretionary.strategy import AIConfig, AIStrategy


def _stub(**cfg):
    s = SimpleNamespace(config=AIConfig(**cfg), _lifetime_pnl=0.0)
    s._round_trip_cost_pct = lambda: AIStrategy._round_trip_cost_pct(s)
    return s


def test_round_trip_cost_default():
    s = _stub()
    # 2 * (0.0005 taker + 0.0002 slippage) * 100 = 0.14%
    assert AIStrategy._round_trip_cost_pct(s) == 0.14


def test_round_trip_cost_respects_config():
    s = _stub(fee_taker_pct=0.0002, slippage_pct=0.0001)
    assert AIStrategy._round_trip_cost_pct(s) == 0.06


def test_turnover_fees_share_of_gross():
    s = _stub()
    # net 100, fees 20 -> gross 120, fees eat 16.7%
    out = AIStrategy._turnover_stats(s, 100.0, 20.0, 10000.0)
    assert out["gross_pnl_usd"] == 120.0
    assert out["fees_usd"] == 20.0
    assert out["fees_pct_of_gross"] == 16.7
    assert out["fees_pct_of_equity"] == 0.2
    assert out["round_trip_pct"] == 0.14


def test_turnover_handles_flat_account():
    s = _stub()
    out = AIStrategy._turnover_stats(s, 0.0, 0.0, 0.0)
    assert out["fees_pct_of_gross"] is None
    assert out["fees_pct_of_equity"] is None
    assert out["gross_pnl_usd"] == 0.0
