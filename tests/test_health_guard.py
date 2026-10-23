"""Runtime health guard tests: preflight gate + in-trade watchdog."""
import time

from app.services import health_guard as hg


class TestPreflight:
    def test_clean_state_allows_open(self):
        assert hg.preflight(has_demo_client=True) == []

    def test_no_demo_client_blocks(self):
        assert "no_demo_client" in hg.preflight(has_demo_client=False)

    def test_live_client_as_primary_blocks(self):
        assert "live_client_as_primary" in hg.preflight(
            has_demo_client=True, live_as_primary=True
        )

    def test_stale_engine_blocks(self):
        assert "engine_tick_stale" in hg.preflight(
            has_demo_client=True, tick_age_sec=500, max_tick_age_sec=180
        )

    def test_llm_and_db_problems_block(self):
        reasons = hg.preflight(
            has_demo_client=True, llm_rate_limited=True, llm_error=True, db_ok=False
        )
        assert set(reasons) == {"llm_rate_limited", "llm_error", "db_unavailable"}

    def test_missing_indicators_blocks(self):
        assert "indicators_missing" in hg.preflight(
            has_demo_client=True, indicators_ok=False
        )

    def test_position_cap_blocks(self):
        assert "max_positions" in hg.preflight(
            has_demo_client=True, open_positions=2, max_positions=2
        )


class TestEvaluate:
    def _pos(self, **kw):
        base = {
            "coin": "ETH", "inst_id": "ETH-USDT-SWAP", "side": "short",
            "size": 1.0, "stop_price": 2500.0, "mark_px": 2400.0,
            "opened_at": None,
        }
        base.update(kw)
        return base

    def test_healthy_state_has_no_issues(self):
        issues = hg.evaluate(
            positions=[self._pos()], manage_age_sec=30, tick_age_sec=20
        )
        assert issues == []

    def test_manage_stall_is_critical(self):
        issues = hg.evaluate(
            positions=[self._pos()], manage_age_sec=900, tick_age_sec=20,
            max_manage_age_sec=300,
        )
        assert [i.code for i in issues] == ["manage_stale"]
        assert issues[0].severity == "crit"
        assert issues[0].action == "flatten"

    def test_tick_stall_is_critical(self):
        issues = hg.evaluate(
            positions=[self._pos()], manage_age_sec=30, tick_age_sec=600,
            max_tick_age_sec=180,
        )
        assert [i.code for i in issues] == ["engine_tick_stale"]

    def test_never_managed_is_critical(self):
        issues = hg.evaluate(
            positions=[self._pos()], manage_age_sec=None, tick_age_sec=10
        )
        assert "manage_never" in [i.code for i in issues]

    def test_breached_short_stop_is_detected(self):
        # short stop below current price can never protect the trade
        issues = hg.evaluate(
            positions=[self._pos(stop_price=2390.0, mark_px=2400.0)],
            manage_age_sec=30, tick_age_sec=20,
        )
        assert [i.code for i in issues] == ["stop_wrong_side"]

    def test_valid_long_stop_is_ok(self):
        issues = hg.evaluate(
            positions=[self._pos(side="long", stop_price=2300.0, mark_px=2400.0)],
            manage_age_sec=30, tick_age_sec=20,
        )
        assert issues == []

    def test_hold_overrun_is_detected(self):
        opened = time.time() - 20 * 3600
        issues = hg.evaluate(
            positions=[self._pos(opened_at=opened)],
            manage_age_sec=30, tick_age_sec=20, max_hold_hours=18,
        )
        assert "hold_overrun" in [i.code for i in issues]

    def test_flat_book_has_no_issues_even_if_stale(self):
        issues = hg.evaluate(positions=[], manage_age_sec=99999, tick_age_sec=99999)
        assert issues == []


class TestBeatRegistry:
    def setup_method(self):
        hg.reset()

    def test_beat_and_age(self):
        hg.beat("ai_tick", tick=1)
        assert hg.get_beat("ai_tick")["tick"] == 1
        age = hg.beat_age_sec("ai_tick")
        assert age is not None and age < 2

    def test_missing_beat_age_is_none(self):
        assert hg.beat_age_sec("nope") is None

    def test_snapshot_reports_iso_and_age(self):
        hg.beat("ai_manage", positions=1)
        snap = hg.snapshot()
        assert "ai_manage" in snap
        assert snap["ai_manage"]["positions"] == 1
        assert snap["ai_manage"]["age_sec"] >= 0
        assert "T" in snap["ai_manage"]["ts_iso"]


class TestSummarize:
    def test_summary_ok_and_counts(self):
        assert hg.summarize([])["ok"] is True
        s = hg.summarize([hg.Issue("x", "crit", "d"), hg.Issue("y", "warn", "d")])
        assert s["ok"] is False and s["crit"] == 1 and s["warn"] == 1
