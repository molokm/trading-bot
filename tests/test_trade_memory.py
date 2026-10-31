"""Tests for deterministic trade settlement memory (alpha / R / lessons)."""
from app.services.trade_memory import build_lessons, settle_trade


class TestSettleTrade:
    def test_long_with_edge(self):
        st = settle_trade(
            side="long", entry=100.0, exit=103.0, pnl=30.0,
            risk_usd=10.0, btc_entry=50000.0, btc_exit=50500.0,
        )
        assert st["move_pct"] == 3.0
        assert st["btc_pct"] == 1.0
        assert st["alpha_pct"] == 2.0
        assert st["r_mult"] == 3.0
        assert st["verdict"] == "edge"

    def test_short_direction_adjusted(self):
        st = settle_trade(
            side="short", entry=100.0, exit=98.0, pnl=20.0,
            risk_usd=10.0, btc_entry=50000.0, btc_exit=50000.0,
        )
        assert st["move_pct"] == 2.0
        assert st["alpha_pct"] == 2.0
        assert st["verdict"] == "edge"

    def test_short_win_in_red_market_is_beta(self):
        # Coin -5% while BTC -5% → the short simply rode market beta.
        st = settle_trade(
            side="short", entry=100.0, exit=95.0, pnl=50.0,
            risk_usd=10.0, btc_entry=50000.0, btc_exit=47500.0,
        )
        assert st["move_pct"] == 5.0
        assert st["btc_pct"] == -5.0
        assert st["alpha_pct"] == 0.0
        assert st["verdict"] == "beta_win"

    def test_short_loss_vs_rising_btc_is_bad_thesis(self):
        st = settle_trade(
            side="short", entry=100.0, exit=103.0, pnl=-30.0,
            risk_usd=10.0, btc_entry=50000.0, btc_exit=51000.0,
        )
        assert st["alpha_pct"] == -1.0
        assert st["verdict"] == "bad_thesis"

    def test_win_is_only_beta(self):
        st = settle_trade(
            side="long", entry=100.0, exit=101.0, pnl=10.0,
            risk_usd=10.0, btc_entry=50000.0, btc_exit=50500.0,
        )
        assert st["verdict"] == "beta_win"

    def test_loss_with_negative_alpha(self):
        st = settle_trade(
            side="long", entry=100.0, exit=98.0, pnl=-20.0,
            risk_usd=10.0, btc_entry=50000.0, btc_exit=51000.0,
        )
        assert st["move_pct"] == -2.0
        assert st["verdict"] == "bad_thesis"

    def test_loss_in_line_with_benchmark_is_noise(self):
        st = settle_trade(
            side="long", entry=100.0, exit=99.0, pnl=-10.0,
            risk_usd=10.0, btc_entry=50000.0, btc_exit=49500.0,
        )
        assert st["verdict"] == "noise_loss"

    def test_missing_benchmark_is_unsettled(self):
        st = settle_trade(
            side="short", entry=100.0, exit=101.0, pnl=-10.0, risk_usd=5.0,
        )
        assert st["alpha_pct"] is None
        assert st["btc_pct"] is None
        assert st["verdict"] == "unsettled"

    def test_invalid_prices(self):
        assert settle_trade(side="long", entry=0, exit=100, pnl=0)["verdict"] == "unsettled"


class TestBuildLessons:
    def _row(self, coin, side, pnl, verdict, reason="stop", alpha=None):
        return {
            "coin": coin, "side": side, "pnl": pnl,
            "verdict": verdict, "reason": reason, "alpha_pct": alpha,
        }

    def test_needs_three_rows(self):
        assert build_lessons([self._row("BTC", "long", 1, "edge")]) == []

    def test_negative_alpha_pair_is_flagged(self):
        rows = [
            self._row("XRP", "short", -5, "bad_thesis", alpha=-1.0),
            self._row("XRP", "short", -3, "bad_thesis", alpha=-0.8),
            self._row("ETH", "long", 4, "edge", alpha=1.2),
        ]
        lessons = build_lessons(rows)
        assert any("XRP short" in line and "negative alpha" in line for line in lessons)

    def test_beta_wins_flagged(self):
        rows = [
            self._row("SOL", "long", 3, "beta_win", alpha=0.0),
            self._row("SOL", "long", 2, "beta_win", alpha=0.05),
            self._row("BTC", "long", 4, "edge", alpha=1.5),
        ]
        lessons = build_lessons(rows)
        assert any("SOL wins are mostly BTC beta" in line for line in lessons)

    def test_always_returns_something(self):
        rows = [
            self._row("BTC", "long", 4, "edge", alpha=1.5),
            self._row("ETH", "long", 3, "edge", alpha=1.0),
            self._row("SOL", "long", 2, "edge", alpha=0.9),
        ]
        assert build_lessons(rows)
