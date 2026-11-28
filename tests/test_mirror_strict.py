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


class TestNoAutonomousLiveTrading:
    """The mirror must only copy DEMO events — it may never decide on its own."""

    def test_orphan_sync_never_opens(self):
        """_manage_live_orphans must not call _open_live (no self-healing opens)."""
        from app.services.ai_strategy import AIStrategy
        import inspect
        src = inspect.getsource(AIStrategy._manage_live_orphans)
        assert "_open_live" not in src, (
            "_manage_live_orphans re-opens LIVE on its own — that is autonomous trading"
        )

    def test_clone_requires_explicit_watermark(self):
        """Clone must be fail-closed when live_mirror_connected_at is missing."""
        from app.services.ai_strategy import AIStrategy
        import inspect
        src = inspect.getsource(AIStrategy._clone_missing_to_live)
        assert "_live_clone_watermark" in src, "clone bypasses the connect watermark"
        assert "_started_at" not in src, (
            "clone falls back to bot start time → clones positions opened while mirror was off"
        )

    def test_live_orders_go_through_chokepoint(self):
        from app.services.ai_strategy import AIStrategy
        import inspect
        src = inspect.getsource(AIStrategy._open_live)
        assert "_live_market_order" in src
        assert "_ensure_exchange_sl_tp" not in src, "LIVE must not place exchange SL/TP"

    @pytest.mark.asyncio
    async def test_chokepoint_blocks_open_without_demo_twin(self):
        from unittest.mock import AsyncMock, patch
        from app.services.ai_strategy import AIStrategy, AIConfig
        bot = AIStrategy(config=AIConfig())
        bot._positions = {}
        lc = MagicMock()
        with patch.object(bot, "_place", new=AsyncMock()) as place:
            resp = await bot._live_market_order(
                lc, inst="BTC-USDT-SWAP", order_side="buy", sz=1,
                pos_side="long", cl_ord_id="ailx", coin="BTC",
                is_close=False, reason="test",
            )
        assert resp.get("error") is True
        assert "blocked" in str(resp.get("message"))
        place.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_watermark_fail_closed_without_db(self):
        from app.services.ai_strategy import AIStrategy, AIConfig
        bot = AIStrategy(config=AIConfig())
        bot.db = None
        assert await bot._live_clone_watermark() == 0

    @pytest.mark.asyncio
    async def test_watermark_fail_closed_on_db_error(self):
        from app.services.ai_strategy import AIStrategy, AIConfig
        bot = AIStrategy(config=AIConfig())
        bot.db = MagicMock()
        bot.db.get_setting = AsyncMock(side_effect=RuntimeError("db down"))
        assert await bot._live_clone_watermark() == 0

    @pytest.mark.asyncio
    async def test_foreign_live_position_is_not_flattened(self):
        """A manual LIVE trade (no/other clOrdId) must survive reconcile."""
        from unittest.mock import AsyncMock, patch
        from app.services.ai_strategy import AIStrategy, AIConfig
        bot = AIStrategy(config=AIConfig())
        bot._hydrated = True
        bot._positions = {}          # DEMO flat
        bot._live_positions = {}
        bot._started_at = "2020-01-01T00:00:00+00:00"
        lc = MagicMock()
        lc.get_positions = AsyncMock(return_value={"data": [{
            "instId": "BTC-USDT-SWAP", "pos": "1", "posSide": "long",
            "avgPx": "60000",
        }]})
        bot._live_client = lambda: lc
        bot._mirror_enabled = AsyncMock(return_value=True)
        bot._entry_fill_owner = AsyncMock(return_value="")  # manual
        flatten = AsyncMock()
        with patch("app.services.position_claim.flatten_position", new=flatten):
            await bot._manage_live_orphans(client=None)
        flatten.assert_not_awaited()
        assert not bot._exec_log or all(
            e.get("event") != "mirror_sync_close" for e in bot._exec_log
        )

    @pytest.mark.asyncio
    async def test_purge_live_algos_only_ours_or_mirrored(self):
        """Orphan exchange stops on LIVE must be cancelled — but not user orders."""
        from unittest.mock import AsyncMock, MagicMock
        from types import SimpleNamespace
        from app.services.ai_strategy import AIStrategy, AIConfig
        bot = AIStrategy(config=AIConfig())
        bot._live_positions = {"BTC": SimpleNamespace(side="long", size=1.0)}
        lc = MagicMock()
        lc.get_algo_orders = AsyncMock(side_effect=[
            {"data": [
                {"instId": "BTC-USDT-SWAP", "algoId": "1", "clOrdId": "ail1"},
                {"instId": "ETH-USDT-SWAP", "algoId": "2", "clOrdId": "manual"},
                {"instId": "SOL-USDT-SWAP", "algoId": "3", "clOrdId": "aimanual"},
                {"instId": "XRP-USDT-SWAP", "algoId": "4", "clOrdId": ""},
            ]},
            {"data": []},
            {"data": []},
        ])
        lc.cancel_algo_order = AsyncMock(return_value={"code": "0"})
        n = await bot._purge_live_algos(lc)
        # BTC (mirrored) + SOL (ai* prefix); ETH manual and XRP unknown stay.
        assert n == 2
        cancelled = {c.args[0] for c in lc.cancel_algo_order.await_args_list}
        assert cancelled == {"BTC-USDT-SWAP", "SOL-USDT-SWAP"}
