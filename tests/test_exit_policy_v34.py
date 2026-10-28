"""P0 exit-policy v2 + concentration limits (AI Discretionary 1H v1.34).

Guards the two behavioural fixes of the audit:
  1. exit thresholds must not cap winners at fee-level gains;
  2. the book must not concentrate in one coin / correlation group.
"""
import inspect
from types import SimpleNamespace

from app.bots.ai_discretionary.strategy import AIConfig, AIStrategy


def _bot(positions=None, trade_log=None, cfg=None):
    bot = object.__new__(AIStrategy)
    bot.config = cfg or AIConfig()
    bot._positions = positions or {}
    bot._trade_log = list(trade_log or [])
    return bot


def _pos(coin, size=1.0):
    return SimpleNamespace(size=size, inst_id=f"{coin}-USDT-SWAP",
                           side="long", entry_price=100.0)


# ── exit policy defaults ────────────────────────────────────────────────

def test_exit_defaults_do_not_cut_winners_at_fee_level():
    c = AIConfig()
    assert c.exit_min_profit_pct >= 0.42          # > 3x round-trip cost
    assert c.soft_profit_take_pct >= c.min_take_pct  # TP fires before soft take
    assert c.partial_take_pct >= 2.0
    assert c.partial_take_frac >= 0.30
    assert c.trail_activate_atr_mult >= 1.2
    assert 1.0 <= c.trail_atr_mult <= 1.3
    assert c.breakeven_lock_pct >= 0.14           # above round-trip cost
    assert c.breakeven_atr_mult >= 0.8
    assert c.giveback_min_peak_pct >= 1.5
    assert c.ind_exit_soft_hold_pct >= 1.5


def test_breakeven_and_trail_use_configurable_lock_pct():
    src = inspect.getsource(AIStrategy._manage_stops)
    assert "breakeven_lock_pct" in src
    assert "1.0005" not in src
    assert "0.9995" not in src


def test_indicator_exit_soft_hold_is_configurable():
    src = inspect.getsource(AIStrategy._indicator_exit_reason)
    assert "ind_exit_soft_hold_pct" in src


def test_migration_patches_persisted_config_once():
    import app.main as m

    assert m._EXIT_POLICY_V34["partial_take_pct"] == AIConfig.partial_take_pct
    assert m._EXIT_POLICY_V34["soft_profit_take_pct"] == AIConfig.soft_profit_take_pct
    assert m._EXIT_POLICY_V34["max_positions_per_group"] == AIConfig.max_positions_per_group
    startup_src = inspect.getsource(m.startup)
    assert "_apply_exit_policy_v34" in startup_src


# ── concentration limits ────────────────────────────────────────────────

def test_one_position_per_correlation_group():
    bot = _bot({"ETH": _pos("ETH")})
    blocked = bot._concentration_veto("BTC", "long", 0.95)
    assert blocked and blocked.startswith("concentration:group")
    assert bot._concentration_veto("SOL", "long", 0.95) is None


def test_group_limit_can_be_disabled():
    cfg = AIConfig()
    cfg.max_positions_per_group = 0
    bot = _bot({"ETH": _pos("ETH")}, cfg=cfg)
    assert bot._concentration_veto("BTC", "long", 0.95) is None


def test_btc_dominance_requires_higher_confidence():
    log = [{"reason": "closed", "pnl": 1.0, "coin": "BTC"} for _ in range(6)]
    log += [{"reason": "closed", "pnl": 1.0, "coin": "SOL"} for _ in range(4)]
    bot = _bot(trade_log=log)
    low = bot._concentration_veto("BTC", "long", 0.80)
    assert low and "btc_share" in low
    assert bot._concentration_veto("BTC", "long", 0.90) is None
    # Below the share threshold BTC is unrestricted
    bot2 = _bot(trade_log=[{"reason": "closed", "pnl": 1.0, "coin": "SOL"}] * 10)
    assert bot2._concentration_veto("BTC", "long", 0.70) is None


def test_corr_group_taxonomy():
    assert AIStrategy._corr_group("BTC") == "crypto_major"
    assert AIStrategy._corr_group("eth") == "crypto_major"
    assert AIStrategy._corr_group("SOL") == "crypto_alt"
    assert AIStrategy._corr_group("DOGE") == "crypto_meme"


def test_veto_open_consults_concentration():
    src = inspect.getsource(AIStrategy._quant_veto_open)
    assert "_concentration_veto" in src
