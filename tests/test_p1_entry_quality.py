"""P1 «Качество входа»: cost-edge gate, per-coin chop veto, size_pct.

Audit findings these lock down:
  1. `_quant_veto_open` checked global chop BEFORE the per-coin gate, so a coin
     in its own trend could never trade while BTC chopped — the global veto
     shadowed `coins[coin]['block_open']` entirely.
  2. `cost_min_edge_mult` only fed the LLM payload; the ≥3× round-trip rule was
     never enforced, so ATR-dead coins were tradable.
  3. The LLM's `size_pct_equity` was validated and then dropped; adaptive
     `size_cap` was advisory even though the model was told it was the ceiling.
  4. The LLM payload was silently cut at 6000 chars (mid-JSON).
"""
import inspect

import pytest

from app.bots.ai_discretionary.strategy import AIConfig, AIStrategy


def _bot(inds):
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot._positions = {}
    bot._trade_log = []
    bot._latest_indicators = inds
    bot._adapt = {"preset": "normal", "min_confidence": 0.62,
                  "quant_min_align": 0.62, "size_cap": 0.10}
    return bot


def _inds(eth_atr=1.0, eth_regime="bull", btc_regime="chop"):
    return {
        "BTC": {"close": 100.0, "atr": 0.6, "align_long": 0.5,
                "align_short": 0.5, "regime": btc_regime, "adx": 18.0},
        "ETH": {"close": 100.0, "atr": eth_atr, "align_long": 0.9,
                "align_short": 0.1, "regime": eth_regime, "adx": 30.0},
    }


def _open_dec(coin="ETH", side="long", conf=0.9):
    return {"action": "open", "symbol": coin, "side": side,
            "confidence": conf, "stop_pct": 0.02, "take_pct": 0.06}


# ── 1. per-coin chop, not global ────────────────────────────────────────

def test_global_chop_does_not_veto_a_trending_coin():
    bot = _bot(_inds(eth_regime="bull", btc_regime="chop"))
    veto = bot._quant_veto_open(_open_dec())
    assert veto is None, veto
    assert "global_chop" not in (veto or "")


def test_coin_in_its_own_chop_is_still_vetoed():
    bot = _bot(_inds(eth_regime="chop", btc_regime="chop"))
    assert bot._quant_veto_open(_open_dec()) == "quant_veto:coin_block:ETH"


# ── 2. cost-aware edge ──────────────────────────────────────────────────

def test_dead_atr_is_vetoed_by_cost_edge():
    # 0.3 ATR on a 100 price = 0.30% expected move < 3 × 0.14% round trip
    bot = _bot(_inds(eth_atr=0.3))
    veto = bot._quant_veto_open(_open_dec())
    assert veto is not None and veto.startswith("quant_veto:cost_edge"), veto


def test_live_atr_clears_the_cost_gate():
    bot = _bot(_inds(eth_atr=1.0))
    assert bot._quant_veto_open(_open_dec()) is None


def test_cost_edge_threshold_follows_config():
    bot = _bot(_inds(eth_atr=1.0))
    bot.config.cost_min_edge_mult = 20.0   # 20 × 0.14% = 2.8% > 1% ATR
    veto = bot._quant_veto_open(_open_dec())
    assert veto is not None and veto.startswith("quant_veto:cost_edge")


def test_cost_edge_falls_back_to_take_when_atr_missing():
    bot = _bot(_inds(eth_atr=0.0))
    # take 6% clears 0.42%; without ATR the take is used instead of vetoing
    assert bot._quant_veto_open(_open_dec()) is None


# ── 3. size_pct / size_cap ──────────────────────────────────────────────

@pytest.mark.parametrize("req,expected", [
    (0.0, 1.0),      # no request → full base size
    (0.10, 1.0),     # exactly the cap
    (0.12, 1.0),     # above the cap → clamped
    (0.05, 0.5),     # half the cap → half risk
    (0.01, 0.25),    # tiny request → floor, still tradable
])
def test_size_factor_maps_request_to_risk_multiplier(req, expected):
    bot = _bot(_inds())
    assert bot._size_factor(req) == pytest.approx(expected)


def test_size_factor_uses_adaptive_cap():
    bot = _bot(_inds())
    bot._adapt["size_cap"] = 0.06
    assert bot._size_factor(0.06) == pytest.approx(1.0)
    assert bot._size_factor(0.03) == pytest.approx(0.5)


def test_open_applies_size_factor_and_tick_passes_size_pct():
    open_src = inspect.getsource(AIStrategy._open)
    assert "_size_factor(size_pct)" in open_src
    tick_src = inspect.getsource(AIStrategy._tick)
    assert 'size_pct=float(decision.get("size_pct_equity") or 0)' in tick_src


# ── 4. LLM payload ──────────────────────────────────────────────────────

def test_llm_payload_default_is_not_silently_short():
    from app.services import ai_agent

    src = inspect.getsource(ai_agent._call_llm_once)
    assert 'AI_LLM_PAYLOAD_CHARS", "16000"' in src
    assert "payload truncated" in src