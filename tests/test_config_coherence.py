"""Config coherence: defaults, persistence round-trip, risk wiring, LLM status."""
from datetime import datetime, timezone

import pytest

from app.services.ai_strategy import AIStrategy
from app.bots.ai_discretionary.strategy import AIConfig


def _bot(**cfg):
    return AIStrategy(config=AIConfig(**cfg))


class TestConfigPersistence:
    def test_export_covers_modern_knobs(self):
        bot = _bot()
        out = bot.export_config_dict()
        for key in (
            "second_entry_margin_pct", "fee_taker_pct", "slippage_pct",
            "cost_min_edge_mult", "atr_stop_mult", "breakeven_min_pct",
            "partial_take_pct", "partial_take_frac", "hard_profit_lock_peak",
            "soft_profit_take_pct", "daily_loss_limit_pct", "llm_min_interval_sec",
            "funding_block_abs", "btc_roc_veto", "scale_in_max_adds",
        ):
            assert key in out, f"{key} missing from export_config_dict"

    def test_export_never_leaks_execute(self):
        assert "execute" not in _bot(execute=True).export_config_dict()

    def test_apply_keeps_execute_when_asked(self):
        bot = _bot(execute=True)
        bot.apply_config_dict({"execute": False, "max_positions": 1}, keep_execute=True)
        assert bot.config.execute is True
        assert bot.config.max_positions == 1

    def test_apply_can_change_execute_when_allowed(self):
        bot = _bot(execute=True)
        bot.apply_config_dict({"execute": False}, keep_execute=False)
        assert bot.config.execute is False

    def test_apply_ignores_unknown_and_private_keys(self):
        bot = _bot()
        before = bot.config.min_confidence
        bot.apply_config_dict({"nope": 1, "_x": 2, "min_confidence": 0.71})
        assert bot.config.min_confidence == 0.71
        assert not hasattr(bot.config, "nope")

    def test_round_trip_preserves_knobs(self):
        bot = _bot()
        bot.config.partial_take_pct = 1.35
        bot.config.hard_profit_lock_peak = 1.6
        snap = bot.export_config_dict()
        fresh = _bot()
        fresh.apply_config_dict(snap)
        assert fresh.config.partial_take_pct == 1.35
        assert fresh.config.hard_profit_lock_peak == 1.6


class TestRiskWiring:
    def test_daily_pnl_is_taken_from_engine(self, monkeypatch):
        from app.services import risk_guard as rg
        monkeypatch.setenv("RISK_MAX_DAILY_LOSS_USD", "10")
        monkeypatch.setattr(rg, "_runtime_kill", None)
        bot = _bot()
        bot._daily_realized = -25.0
        with pytest.raises(RuntimeError, match="max_daily_loss"):
            rg.assert_can_open(ai_bot=bot)

    @staticmethod
    def _today_msk():
        from app.services.pnl_engine import PNL_TZ
        return datetime.now(timezone.utc).astimezone(PNL_TZ).strftime("%Y-%m-%d")

    def test_demo_equity_drives_daily_limit(self):
        bot = _bot(capital=10000.0)
        bot.config.daily_loss_limit_pct = 0.02
        bot._account_equity = 650.0
        bot._daily_realized_day = self._today_msk()
        bot._daily_realized = -20.0
        assert bot._daily_loss_blocks_open() is True

    def test_nominal_capital_alone_is_not_used_when_equity_known(self):
        bot = _bot(capital=10000.0)
        bot.config.daily_loss_limit_pct = 0.02
        bot._account_equity = 650.0
        bot._daily_realized_day = self._today_msk()
        bot._daily_realized = -10.0
        assert bot._daily_loss_blocks_open() is False


class TestLlmStatus:
    def test_openai_only_env_reports_openai(self, monkeypatch):
        from app.services import ai_agent
        monkeypatch.delenv("AI_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setenv("OPENAI_MODEL", "gemini-3.5-flash-lite")
        st = ai_agent.llm_status()
        assert st["provider"] == "openai"
        assert st["model"] == "gemini-3.5-flash-lite"

    def test_execute_unset_mirrors_engine_rule(self, monkeypatch):
        from app.services import ai_agent
        monkeypatch.delenv("AI_EXECUTE", raising=False)
        monkeypatch.setenv("OKX_DEMO", "true")
        assert ai_agent.llm_status()["execute"] is True
        monkeypatch.setenv("OKX_DEMO", "false")
        assert ai_agent.llm_status()["execute"] is False
