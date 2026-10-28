"""Adaptive/reflection must only see realized closes.

Regression: `_hydrate_from_db` rebuilt `_trade_log` with `reason = state`, so
every executed entry (`state='filled'`, pnl≈0) was counted as a losing trade.
Adaptive then reported WR 8.3% / streak=loss8 against an actual 55% WR and
locked the bot into a false "defensive" preset.
"""
import inspect

import pytest

from app.bots.ai_discretionary.strategy import AIStrategy


def _bot(rows):
    bot = object.__new__(AIStrategy)
    bot._trade_log = rows
    return bot


def _row(reason, pnl, mode="demo", coin="BTC"):
    return {"reason": reason, "pnl": pnl, "account_mode": mode, "coin": coin}


def test_entries_are_not_counted_as_closed_trades():
    bot = _bot([
        _row("open", -0.4),       # entry fee
        _row("filled", 0.0),      # hydrate output for an entry
        _row("pending", 0.0),
        _row("db", 0.0),
        _row("closed", 12.5),
        _row("sl", -8.0),
        _row("tp", 3.2),
        _row("close", 1.0, mode="live"),
    ])
    closed = bot._closed_trades()
    assert [r["reason"] for r in closed] == ["closed", "sl", "tp", "close"]
    assert bot._closed_trades(demo_only=True) == closed[:3]


def test_zero_and_near_zero_pnl_rows_are_excluded():
    bot = _bot([
        _row("closed", 0.0),
        _row("closed", 1e-12),
        _row("closed", None),
        _row("closed", "garbage"),
        _row("closed", 0.01),
    ])
    closed = bot._closed_trades()
    assert len(closed) == 1 and closed[0]["pnl"] == 0.01


def test_rolling_stats_ignores_hydrated_open_rows():
    rows = [_row("filled", 0.0) for _ in range(8)]
    rows += [_row("closed", 10.0), _row("closed", -5.0), _row("closed", 2.0)]
    bot = _bot(rows)
    bot.config = type("C", (), {"adapt_window": 12})()
    stats = bot._rolling_stats()
    assert stats["n"] == 3
    assert stats["win_rate"] == pytest.approx(66.7, abs=0.1)
    assert stats["sum_pnl"] == pytest.approx(7.0)


def test_trades_summary_counts_only_closed_state():
    from app.database import Database

    src = inspect.getsource(Database.get_trades_summary)
    assert "state = 'closed'" in src
    assert "state IN ('filled', 'closed')" not in src


def test_hydrate_maps_filled_rows_to_open():
    """The rebuild path must not fabricate `filled` as a close reason."""
    from app.bots.ai_discretionary import strategy as mod

    # The row mapping moved into _rebuild_trade_log (v1.35 engine overlay);
    # hydration now delegates to it.
    src = inspect.getsource(mod.AIStrategy._rebuild_trade_log)
    assert 'reason = "open" if state in ("", "filled", "new", "pending")' in src
    assert '"reason": reason' in src
    assert "_rebuild_trade_log" in inspect.getsource(mod.AIStrategy._hydrate_from_db)


def test_booking_a_close_invalidates_the_engine_cache():
    """Fresh internal counters must not be overwritten by a stale snapshot.

    The 300s engine-stats cache was re-applied on the next tick, so right after
    a close `lifetime_pnl_internal` fell back to the pre-close value and only
    matched the card minutes later. Each booking now expires the cache.
    """
    assert "_engine_stats_ts = 0.0" in inspect.getsource(AIStrategy._close)
    assert "_engine_stats_ts = 0.0" in inspect.getsource(AIStrategy._book_partial)
