"""v1.44 flow control: daily caps, breadth gate and execution telemetry.

Why these exist: on 86 trades fees were 41% of gross PnL. The binding
constraint is not «is this a good setup» but «is one more round trip worth
another fee». Every gate must be switchable by config alone (0 / False), so a
bad idea can be turned off without a code rollback.
"""
from types import SimpleNamespace

import pytest

from app.bots.ai_discretionary.strategy import AIConfig, AIStrategy


def _bot(indicators=None, **cfg):
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig(**cfg)
    bot._latest_indicators = indicators or {}
    bot._flow_day = ""
    bot._daily_opens = 0
    bot._daily_fees = 0.0
    bot._exec_stats = {"since": "t", "n": 0, "adverse_bps_sum": 0.0,
                       "fee_usd": 0.0, "maker": 0, "taker": 0, "last": {}}
    bot._last_fill_liquidity = ""
    return bot


def _ind(adx):
    return {"adx": adx, "close": 100.0}


# ── daily open cap ──────────────────────────────────────────────────────

def test_open_cap_blocks_after_the_configured_number():
    bot = _bot(max_opens_per_day=2)
    assert bot._flow_veto("BTC") is None
    bot.note_open_flow(0.5)
    assert bot._flow_veto("BTC") is None
    bot.note_open_flow(0.5)
    assert bot._flow_veto("BTC") == "flow:opens_today:2/2"


def test_open_cap_zero_disables_the_gate():
    # Fee 0 so only the (disabled) open cap is under test.
    bot = _bot(max_opens_per_day=0)
    for _ in range(20):
        bot.note_open_flow(0.0)
    assert bot._flow_veto("BTC") is None


# ── daily fee budget ────────────────────────────────────────────────────

def test_fee_budget_blocks_once_spent():
    bot = _bot(max_daily_fees_usd=5.0)
    bot.note_open_flow(3.0)
    assert bot._flow_veto("BTC") is None
    bot.note_fee_flow(2.0)          # exit fee pushes the day over budget
    assert bot._flow_veto("BTC") == "flow:daily_fees:5.00/5.00"


def test_fee_budget_zero_disables_the_gate():
    bot = _bot(max_daily_fees_usd=0.0)
    bot.note_fee_flow(500.0)
    assert bot._flow_veto("BTC") is None


def test_fees_accumulate_from_opens_and_exits():
    bot = _bot()
    bot.note_open_flow(1.25)
    bot.note_fee_flow(0.75)
    _opens, fees = bot._flow_day_counters()
    assert fees == pytest.approx(2.0)


# ── breadth gate ────────────────────────────────────────────────────────

THIN = {"BTC": _ind(30), "ETH": _ind(28), "SOL": _ind(12),
        "NEAR": _ind(11), "ADA": _ind(10), "DOGE": _ind(9),
        "XRP": _ind(8), "LINK": _ind(7)}
BROAD = {c: _ind(30) for c in ("BTC", "ETH", "SOL", "NEAR", "ADA", "DOGE")}


def test_thin_breadth_restricts_new_opens_to_core():
    bot = _bot(indicators=THIN)
    # 2 of 8 trending = 0.25 < 0.35
    assert bot._flow_veto("BTC") is None
    assert bot._flow_veto("ETH") is None
    assert bot._flow_veto("SOL") == "flow:breadth:0.25<0.35"


def test_breadth_does_not_block_when_too_few_coins_are_tracked():
    """No data is not the same as a bad market — never veto on a thin sample."""
    bot = _bot(indicators={"BTC": _ind(10), "ETH": _ind(11)})  # n=2 < 6
    assert bot._flow_veto("SOL") is None


def test_healthy_breadth_allows_everything():
    bot = _bot(indicators=BROAD)
    assert bot._flow_veto("NEAR") is None


def test_breadth_gate_can_be_disabled_by_config():
    bot = _bot(indicators=THIN, breadth_gate_enable=False)
    assert bot._flow_veto("SOL") is None


def test_breadth_is_counted_from_tracked_coins():
    bot = _bot(indicators=THIN)
    frac, trend, n = bot._breadth()
    assert (trend, n) == (2, 8)
    assert frac == pytest.approx(0.25)


# ── flow gate runs before the per-coin quant work ───────────────────────

def test_flow_veto_is_first_in_quant_gate():
    bot = _bot(max_opens_per_day=1)
    bot.note_open_flow(0.1)
    reason = bot._quant_veto_open({"action": "open", "symbol": "BTC",
                                   "side": "long", "confidence": 0.99})
    assert reason == "flow:opens_today:1/1"


# ── execution telemetry ─────────────────────────────────────────────────

def test_adverse_slippage_is_signed_against_the_view():
    bot = _bot()
    # Buying 10 bps above the reference is 10 bps against us.
    bot._exec_stat("entry", 100.0, 100.1, 0.5, liquidity="taker", bought=True)
    assert bot._exec_stats["last"]["adverse_bps"] == pytest.approx(10.0)
    # Selling 10 bps below the reference is also against us.
    bot._exec_stat("exit", 100.0, 99.9, 0.5, liquidity="taker", bought=False)
    assert bot._exec_stats["last"]["adverse_bps"] == pytest.approx(10.0)


def test_favourable_fill_is_negative_adverse():
    bot = _bot()
    bot._exec_stat("entry", 100.0, 99.9, 0.0, liquidity="maker", bought=True)
    assert bot._exec_stats["last"]["adverse_bps"] == pytest.approx(-10.0)


def test_exec_summary_reports_maker_share_and_fees():
    bot = _bot()
    bot._exec_stat("entry", 100.0, 100.0, 1.0, liquidity="maker", bought=True)
    bot._exec_stat("exit", 100.0, 100.0, 2.0, liquidity="taker", bought=False)
    s = bot.exec_stats_summary()
    assert s["fills"] == 2
    assert s["fee_usd"] == pytest.approx(3.0)
    assert s["maker"] == 1 and s["taker"] == 1
    assert s["maker_share"] == pytest.approx(50.0)


def test_exec_summary_without_fills_has_no_average():
    bot = _bot()
    s = bot.exec_stats_summary()
    assert s["fills"] == 0
    assert s["adverse_bps_avg"] is None
    assert s["maker_share"] is None


# ── counters survive a restart but not a new day ────────────────────────

def test_counters_reset_on_a_new_day():
    bot = _bot()
    bot.note_open_flow(1.0)
    bot._flow_day = "1999-01-01"      # force a day rollover
    opens, fees = bot._flow_day_counters()
    assert (opens, fees) == (0, 0.0)


def test_flow_status_exposes_limits_for_the_ui():
    bot = _bot(indicators=THIN, max_opens_per_day=5, max_daily_fees_usd=9.0)
    bot.note_open_flow(2.0)
    st = bot._flow_status()
    assert st["opens_today"] == 1
    assert st["max_opens_per_day"] == 5
    assert st["fees_today"] == pytest.approx(2.0)
    assert st["max_daily_fees_usd"] == pytest.approx(9.0)
    assert st["breadth_trending"] == 2
    assert st["breadth_tracked"] == 8


# ── caps survive a redeploy ─────────────────────────────────────────────
# The file snapshot lives under /tmp on Render and is wiped by every deploy.
# Previously the counters were written only by close paths, so a restart
# between entry and exit handed the strategy a fresh 6-open budget and the
# fee cap restarted at 0 while the day was already spent.

def test_note_open_flow_persists_immediately():
    bot = _bot()
    calls = []
    bot._persist = lambda: calls.append(1)
    bot.note_open_flow(1.0)
    assert calls, "an open must persist the daily counters right away"


def test_note_fee_flow_persists_immediately():
    bot = _bot()
    calls = []
    bot._persist = lambda: calls.append(1)
    bot.note_fee_flow(0.5)
    assert calls, "a fee must persist the daily budget right away"


def test_persist_failure_never_breaks_the_tick():
    bot = _bot()

    def _boom():
        raise RuntimeError("disk full")

    bot._persist = _boom
    bot.note_open_flow(1.0)          # must not raise
    assert bot._daily_opens == 1


def test_restore_takes_the_higher_of_file_and_db():
    from datetime import datetime, timezone
    from app.services.pnl_engine import PNL_TZ

    today = datetime.now(timezone.utc).astimezone(PNL_TZ).strftime("%Y-%m-%d")
    bot = _bot()
    bot._flow_day = today
    bot._daily_opens = 2
    bot._daily_fees = 1.0
    bot._restore_flow_counters(
        {"flow_day": today, "daily_opens": 5, "daily_fees": 0.75})
    assert bot._daily_opens == 5      # DB ahead -> DB wins
    assert bot._daily_fees == pytest.approx(1.0)   # file ahead -> file wins


def test_restore_ignores_yesterdays_blob():
    bot = _bot()
    bot._flow_day = "2026-01-01"
    bot._daily_opens = 3
    bot._daily_fees = 2.0
    bot._restore_flow_counters(
        {"flow_day": "1999-01-01", "daily_opens": 99, "daily_fees": 99.0})
    assert bot._daily_opens == 3
    assert bot._daily_fees == pytest.approx(2.0)


def test_persist_blob_carries_the_flow_counters(monkeypatch):
    import app.bots.ai_discretionary.strategy as S

    captured = {}
    monkeypatch.setattr(S, "save_ai_state", lambda payload: captured.update(payload))
    bot = _bot()
    bot._lifetime_pnl = 0.0
    bot._lifetime_trades = 0
    bot._lifetime_wins = 0
    bot._lifetime_fees = 0.0
    bot._session_pnl = 0.0
    bot._daily_realized = 0.0
    bot._daily_realized_day = ""
    bot._weekly_realized = 0.0
    bot._weekly_realized_week = ""
    bot._adapt = {}
    bot._reflection = ""
    bot._journal = []
    bot._daily_lessons = []
    bot._last_lesson_day = ""
    bot._adapt_log = []
    bot.db = None
    bot._loop = None
    bot.note_open_flow(1.25)
    assert captured["daily_opens"] == 1
    assert captured["daily_fees"] == pytest.approx(1.25)
    assert captured["flow_day"] == bot._flow_day
