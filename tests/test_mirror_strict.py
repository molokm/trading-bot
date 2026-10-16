"""
Test suite for LIVE MIRROR strict invariants (CRITICAL SAFETY).
These tests MUST PASS before any deploy. They verify that the mirror
guards preventing independent LIVE trading are present and functional.

Run: python -m pytest tests/test_mirror_strict.py -v
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestMirrorStrictGuards:
    """Verify mirror safety guards exist and are callable."""

    def test_require_demo_twin_open_exists(self):
        from app.services.ai_strategy import AIStrategy
        assert hasattr(AIStrategy, "_require_demo_twin_open"), "Missing _require_demo_twin_open"
        assert callable(getattr(AIStrategy, "_require_demo_twin_open")), "_require_demo_twin_open not callable"

    def test_require_demo_flat_for_close_exists(self):
        from app.services.ai_strategy import AIStrategy
        assert hasattr(AIStrategy, "_require_demo_flat_for_close"), "Missing _require_demo_flat_for_close"
        assert callable(getattr(AIStrategy, "_require_demo_flat_for_close")), "_require_demo_flat_for_close not callable"

    def test_live_market_order_exists(self):
        from app.services.ai_strategy import AIStrategy
        assert hasattr(AIStrategy, "_live_market_order"), "Missing _live_market_order"
        assert callable(getattr(AIStrategy, "_live_market_order")), "_live_market_order not callable"

    def test_client_refuses_live(self):
        """_client() must refuse non-demo clients."""
        from app.services.ai_strategy import AIStrategy
        import inspect
        src = inspect.getsource(AIStrategy._client)
        assert "REFUSED live client as primary" in src, "_client() missing live-refusal guard"

    def test_open_blocks_live_client(self):
        """_open() must block live_client_forbidden."""
        from app.services.ai_strategy import AIStrategy
        import inspect
        src = inspect.getsource(AIStrategy._open)
        assert "live_client_forbidden" in src, "_open() missing live_client_forbidden guard"

    def test_mirror_enabled_fail_closed(self):
        """_mirror_enabled() must be fail-closed."""
        from app.services.ai_strategy import AIStrategy
        import inspect
        src = inspect.getsource(AIStrategy._mirror_enabled)
        # Must return False on any error / missing flag
        assert "return False" in src, "_mirror_enabled() missing fail-closed return False"

    def test_verify_mirror_guards_runs_at_init(self):
        """_verify_mirror_guards() is called in __init__."""
        from app.services.ai_strategy import AIStrategy
        import inspect
        src = inspect.getsource(AIStrategy.__init__)
        assert "_verify_mirror_guards()" in src, "__init__ missing _verify_mirror_guards() call"


class TestMirrorLogicBehavior:
    """Behavioral tests for mirror logic (using mocked DEMO positions)."""

    @pytest.mark.asyncio
    async def test_require_demo_twin_open_allows_when_demo_has_same_side(self):
        from app.services.ai_strategy import AIStrategy, AIConfig
        bot = AIStrategy(config=AIConfig())
        # Mock DEMO position with same side
        mock_pos = MagicMock()
        mock_pos.side = "long"
        mock_pos.size = 1.0
        bot._positions = {"BTC": mock_pos}
        ok, reason = bot._require_demo_twin_open("BTC", "long")
        assert ok is True, f"Should allow when DEMO has same side: {reason}"
        assert reason == "ok"

    @pytest.mark.asyncio
    async def test_require_demo_twin_open_blocks_when_demo_flat(self):
        from app.services.ai_strategy import AIStrategy, AIConfig
        bot = AIStrategy(config=AIConfig())
        bot._positions = {}  # No demo position
        ok, reason = bot._require_demo_twin_open("BTC", "long")
        assert ok is False, "Should block when DEMO flat"
        assert reason == "no_demo_position"

    @pytest.mark.asyncio
    async def test_require_demo_twin_open_blocks_when_side_mismatch(self):
        from app.services.ai_strategy import AIStrategy, AIConfig
        bot = AIStrategy(config=AIConfig())
        mock_pos = MagicMock()
        mock_pos.side = "short"
        mock_pos.size = 1.0
        bot._positions = {"BTC": mock_pos}
        ok, reason = bot._require_demo_twin_open("BTC", "long")
        assert ok is False, "Should block when side mismatch"
        assert reason == "demo_side_mismatch"

    @pytest.mark.asyncio
    async def test_require_demo_flat_for_close_allows_when_demo_flat(self):
        from app.services.ai_strategy import AIStrategy, AIConfig
        bot = AIStrategy(config=AIConfig())
        bot._positions = {}  # DEMO flat
        ok, reason = bot._require_demo_flat_for_close("BTC")
        assert ok is True, f"Should allow when DEMO flat: {reason}"
        assert reason == "demo_flat"

    @pytest.mark.asyncio
    async def test_require_demo_flat_for_close_blocks_when_demo_open(self):
        from app.services.ai_strategy import AIStrategy, AIConfig
        bot = AIStrategy(config=AIConfig())
        mock_pos = MagicMock()
        mock_pos.side = "long"
        mock_pos.size = 1.0
        bot._positions = {"BTC": mock_pos}
        ok, reason = bot._require_demo_flat_for_close("BTC")
        assert ok is False, "Should block when DEMO still open"
        assert reason == "demo_still_open"

    @pytest.mark.asyncio
    async def test_require_demo_flat_for_close_allows_paired_marker(self):
        from app.services.ai_strategy import AIStrategy, AIConfig
        bot = AIStrategy(config=AIConfig())
        mock_pos = MagicMock()
        mock_pos.side = "long"
        mock_pos.size = 1.0
        bot._positions = {"BTC": mock_pos}
        bot._demo_closing_coin = "BTC"  # Paired close marker
        ok, reason = bot._require_demo_flat_for_close("BTC")
        assert ok is True, f"Should allow paired close: {reason}"
        assert reason == "paired_close"


class TestRuntimeVerification:
    """Integration test: verify mirror guards work at runtime."""

    @pytest.mark.asyncio
    async def test_verify_mirror_guards_passes_on_valid_bot(self):
        from app.services.ai_strategy import AIStrategy, AIConfig
        # Should not raise
        bot = AIStrategy(config=AIConfig())
        # If we get here, _verify_mirror_guards() passed
        assert hasattr(bot, "_require_demo_twin_open")
        assert hasattr(bot, "_require_demo_flat_for_close")
        assert hasattr(bot, "_live_market_order")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
