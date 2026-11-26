"""Daily trend portfolio — engine, sizing, safety invariants.

The module must stay paper-only: a single test asserts that no order-placing
call exists anywhere in its source, and another that the config cannot be
flipped out of paper mode.
"""
import time

import pytest

from app.bots.trend_portfolio.config import (
    DEFAULT_UNIVERSE,
    TrendPortfolioConfig,
)
from app.bots.trend_portfolio.engine import (
    Bar,
    TrendPortfolio,
    backtest,
)

DAY = 86_400_000
TS0 = 1_700_000_000_000


def _cfg(**kw):
    base = dict(
        universe=("AAA",),
        capital=1000.0,
        entry_lookback=5,
        exit_lookback=3,
        atr_period=2,
        chandelier_mult=2.0,
        warmup_bars=12,
        risk_per_sleeve=0.32,
        sizing_halflife_days=365.0,
        fee_pct=0.0008,
        slippage_pct=0.0002,
        funding_pct_day=0.0,
        max_sleeve_leverage=1000.0,
        regime_symbol="",          # gate is opt-in; off in the unit helpers
        regime_ma=200,
    )
    base.update(kw)
    return TrendPortfolioConfig(**base)


def _accel(n, base=100.0):
    """Close = base + i^2 → an accelerating uptrend that breaks a 5-bar high."""
    return [
        Bar(ts=TS0 + i * DAY, open=base + i * i, high=base + i * i + 1,
            low=base + i * i - 1, close=base + i * i)
        for i in range(n)
    ]


def _feed(book, coin, bars):
    out = []
    for b in bars:
        out += book.on_bar(coin, b)
    return out


# ── config ───────────────────────────────────────────────────────────────

def test_disabled_by_default():
    assert TrendPortfolioConfig().enabled is False


def test_env_gate_off_by_default(monkeypatch):
    monkeypatch.delenv("TREND_PORTFOLIO_ENABLE", raising=False)
    assert TrendPortfolioConfig.from_env().enabled is False


def test_env_gate_on(monkeypatch):
    monkeypatch.setenv("TREND_PORTFOLIO_ENABLE", "1")
    cfg = TrendPortfolioConfig.from_env()
    assert cfg.enabled is True
    assert cfg.paper_only is True


def test_paper_only_cannot_be_flipped():
    cfg = _cfg()
    cfg.paper_only = False
    with pytest.raises(ValueError):
        cfg.validate()


def test_exit_window_must_be_smaller_than_entry():
    with pytest.raises(ValueError):
        _cfg(entry_lookback=10, exit_lookback=10).validate()


def test_risk_per_trade_pct_is_sleeve_over_universe():
    cfg = TrendPortfolioConfig(universe=DEFAULT_UNIVERSE, risk_per_sleeve=0.32)
    assert cfg.risk_per_trade_pct == pytest.approx(0.32 / len(DEFAULT_UNIVERSE))


# ── signals ──────────────────────────────────────────────────────────────

def test_no_entry_before_the_breakout_bar():
    book = TrendPortfolio(_cfg())
    bars = _accel(6)
    events = _feed(book, "AAA", bars)
    assert [e for e in events if e["kind"] == "open"] == []
    assert book.sleeves["AAA"].pending == "enter"


def test_entry_fills_at_next_open_with_slippage():
    cfg = _cfg()
    book = TrendPortfolio(cfg)
    bars = _accel(7)
    events = _feed(book, "AAA", bars)
    opens = [e for e in events if e["kind"] == "open"]
    assert len(opens) == 1
    fill = opens[0]
    assert fill["ts"] == bars[6].ts              # next bar, not the signal bar
    assert fill["price"] == pytest.approx(bars[6].open * (1 + cfg.slippage_pct))


def test_sizing_matches_risk_over_stop_distance():
    cfg = _cfg()
    book = TrendPortfolio(cfg)
    _feed(book, "AAA", _accel(7))
    pos = book.sleeves["AAA"].position
    atr = book.sleeves["AAA"].atr(cfg.atr_period)
    stop_dist = cfg.chandelier_mult * atr
    expected_notional = cfg.risk_per_sleeve * cfg.capital * pos.entry / stop_dist
    assert pos.notional == pytest.approx(expected_notional, rel=1e-6)
    assert pos.qty == pytest.approx(pos.notional / pos.entry, rel=1e-9)
    assert pos.risk_usd == pytest.approx(cfg.risk_per_sleeve * cfg.capital, rel=1e-6)
    assert pos.stop == pytest.approx(pos.entry - stop_dist)


def test_risk_usd_scales_with_smoothed_equity():
    cfg = _cfg(universe=("AAA", "BBB"))
    book = TrendPortfolio(cfg)
    book.smooth_equity = 2000.0
    assert book._risk_usd() == pytest.approx(0.32 * 2000.0 / 2)


# ── regime gate ──────────────────────────────────────────────────────────

def _ramp(n, base=100.0, slope=5.0):
    return [
        Bar(ts=TS0 + i * DAY, open=base + slope * i, high=base + slope * i + 1,
            low=base + slope * i - 1, close=base + slope * i)
        for i in range(n)
    ]


def test_regime_gate_off_by_default():
    assert TrendPortfolioConfig().regime_symbol == ""
    cfg = _cfg()
    book = TrendPortfolio(cfg)
    assert book._regime_ok(1) is True


def test_regime_gate_blocks_entry_in_a_downtrend():
    cfg = _cfg(universe=("AAA", "BTC"), regime_symbol="BTC", regime_ma=5)
    book = TrendPortfolio(cfg)
    _feed(book, "BTC", _ramp(7, slope=-5.0))     # BTC below its own mean
    events = _feed(book, "AAA", _accel(7))
    assert [e for e in events if e["kind"] == "open"] == []
    assert book.sleeves["AAA"].position is None


def test_regime_gate_allows_entry_in_an_uptrend():
    cfg = _cfg(universe=("AAA", "BTC"), regime_symbol="BTC", regime_ma=5)
    book = TrendPortfolio(cfg)
    _feed(book, "BTC", _ramp(7, slope=5.0))      # BTC above its own mean
    events = _feed(book, "AAA", _accel(7))
    assert len([e for e in events if e["kind"] == "open"]) == 1


def test_regime_symbol_must_be_in_universe():
    with pytest.raises(ValueError):
        _cfg(universe=("AAA",), regime_symbol="BTC").validate()


def test_regime_env_gate(monkeypatch):
    monkeypatch.setenv("TREND_PORTFOLIO_ENABLE", "1")
    monkeypatch.setenv("TREND_PORTFOLIO_REGIME", "btc")
    monkeypatch.setenv("TREND_PORTFOLIO_REGIME_MA", "180")
    cfg = TrendPortfolioConfig.from_env()
    assert cfg.regime_symbol == "BTC"
    assert cfg.regime_ma == 180


# ── replay order (cross-coin gate) ───────────────────────────────────────
#
# The regime gate reads the regime asset's *latest* close.  Feeding one coin's
# whole history at a time therefore judges every historical alt entry against
# today's regime state instead of the state on that bar — in Nov 2026 it hid
# all but one trade of the live paper book.

def _flat_bars(n, price=100.0, start=0):
    return [
        Bar(ts=TS0 + (start + i) * DAY, open=price, high=price + 1,
            low=price - 1, close=price)
        for i in range(n)
    ]


def _btc_step_series():
    """BTC flat for 20 bars, then a ramp that turns a 5-day mean gate ON."""
    bars = _flat_bars(20)
    bars += [
        Bar(ts=TS0 + i * DAY, open=100 + 2 * (i - 19), high=101 + 2 * (i - 19),
            low=99 + 2 * (i - 19), close=100 + 2 * (i - 19))
        for i in range(20, 40)
    ]
    return bars


def _alt_two_breakouts():
    """AAA breaks out at i=10 (gate OFF) and again at i=25 (gate ON)."""
    bars = []
    for i in range(40):
        if i == 10:
            bars.append(Bar(ts=TS0 + i * DAY, open=100, high=131, low=99, close=130))
        elif i == 25:
            bars.append(Bar(ts=TS0 + i * DAY, open=101, high=131, low=100, close=130))
        elif i == 26:
            bars.append(Bar(ts=TS0 + i * DAY, open=130, high=131, low=129, close=130))
        elif i == 27:
            bars.append(Bar(ts=TS0 + i * DAY, open=130, high=131, low=55, close=60))
        else:
            bars.append(Bar(ts=TS0 + i * DAY, open=100, high=101, low=99, close=100))
    return bars


def _gate_cfg(**kw):
    base = dict(universe=("BTC", "AAA"), regime_symbol="BTC", regime_ma=5)
    base.update(kw)
    return _cfg(**base)


def test_chronological_feed_judges_the_gate_on_the_bar_date():
    from app.bots.trend_portfolio.engine import feed_chronological

    cfg = _gate_cfg()
    book = TrendPortfolio(cfg)
    feed_chronological(book, {"BTC": _btc_step_series(), "AAA": _alt_two_breakouts()})
    # the i=10 breakout happened while the gate was OFF -> not taken
    assert len(book.closed_trades) == 1
    assert book.closed_trades[0]["entry_ts"] == TS0 + 26 * DAY


def test_per_coin_feed_is_the_bug_the_helper_replaces():
    cfg = _gate_cfg()
    book = TrendPortfolio(cfg)
    _feed(book, "BTC", _btc_step_series())
    _feed(book, "AAA", _alt_two_breakouts())
    # per-coin order lets AAA trade the i=10 breakout against BTC's final bar
    assert book.closed_trades, "expected the greedy replay to trade before the gate opened"
    assert book.closed_trades[0]["entry_ts"] < TS0 + 20 * DAY


def test_state_keeps_the_history_the_regime_gate_needs():
    cfg = _gate_cfg(regime_ma=200, warmup_bars=90)
    book = TrendPortfolio(cfg)
    bars = [
        Bar(ts=TS0 + i * DAY, open=100 + i * 0.5, high=101 + i * 0.5,
            low=99 + i * 0.5, close=100 + i * 0.5)
        for i in range(250)
    ]
    from app.bots.trend_portfolio.engine import feed_chronological

    feed_chronological(book, {"BTC": bars, "AAA": bars})
    state = book.to_state()
    assert len(state["sleeves"]["BTC"]["closes"]) >= 200
    restored = TrendPortfolio.from_state(cfg, state)
    assert len(restored.sleeves["BTC"].closes) >= 200
    assert restored._regime_ok(1) is True       # uptrend: gate must be usable


def test_runner_cycle_replays_in_timestamp_order(monkeypatch):
    import asyncio

    import app.bots.trend_portfolio.runner as runner_mod

    cfg = _gate_cfg(state_name="test_trend_chrono_cycle")
    btc, aaa = _btc_step_series(), _alt_two_breakouts()

    async def fake_fetch(inst_id, limit=300, timeout=20.0, source="okx"):
        coin = inst_id.split("-")[0]
        return list(btc if coin == "BTC" else aaa)

    monkeypatch.setattr(runner_mod, "fetch_daily_bars", fake_fetch)
    monkeypatch.setattr(runner_mod, "load_state", lambda *a, **k: {})
    monkeypatch.setattr(runner_mod, "save_state", lambda *a, **k: None)

    runner = runner_mod.TrendPaperRunner(cfg)
    asyncio.run(runner.cycle())
    assert len(runner.book.closed_trades) == 1
    assert runner.book.closed_trades[0]["entry_ts"] == TS0 + 26 * DAY


# ── market-data venue ────────────────────────────────────────────────────

def test_source_defaults_to_binance_vision_and_is_configurable(monkeypatch):
    monkeypatch.delenv("TREND_PORTFOLIO_SOURCE", raising=False)
    assert TrendPortfolioConfig.from_env().source == "binance_vision"
    for value in ("BINANCE", "BINANCE_VISION", "OKX"):
        monkeypatch.setenv("TREND_PORTFOLIO_SOURCE", value)
        assert TrendPortfolioConfig.from_env().source == value.lower()
    with pytest.raises(ValueError):
        _cfg(source="kraken").validate()


def test_fetch_daily_bars_routes_to_the_selected_venue(monkeypatch):
    import asyncio

    import app.bots.trend_portfolio.runner as runner_mod

    calls = []

    async def fake_binance(coin, limit=300, timeout=20.0):
        calls.append(("binance", coin))
        return [Bar(ts=TS0, open=1, high=2, low=0.5, close=1.5)]

    async def fake_vision(coin, limit=300, timeout=20.0):
        calls.append(("binance_vision", coin))
        return [Bar(ts=TS0, open=1, high=2, low=0.5, close=1.5)]

    async def fake_okx(inst_id, limit=300, timeout=20.0):
        calls.append(("okx", inst_id))
        return [Bar(ts=TS0, open=1, high=2, low=0.5, close=1.5)]

    monkeypatch.setattr(runner_mod, "fetch_binance_daily_bars", fake_binance)
    monkeypatch.setattr(runner_mod, "fetch_binance_vision_daily_bars", fake_vision)
    monkeypatch.setattr(runner_mod, "fetch_okx_daily_bars", fake_okx)

    asyncio.run(runner_mod.fetch_daily_bars("BTC-USDT", source="binance"))
    asyncio.run(runner_mod.fetch_daily_bars("BTC-USDT", source="binance_vision"))
    asyncio.run(runner_mod.fetch_daily_bars("BTC-USDT", source="okx"))
    assert calls == [
        ("binance", "BTC"),
        ("binance_vision", "BTC"),
        ("okx", "BTC-USDT"),
    ]


def test_cycle_falls_back_when_the_primary_venue_fails(monkeypatch):
    import asyncio

    import app.bots.trend_portfolio.runner as runner_mod

    cfg = _gate_cfg(
        universe=("BTC", "ETH", "BNB", "ADA"),
        state_name="test_trend_fallback",
        source="binance",
    )
    calls = []

    async def fake_fetch(inst_id, limit=300, timeout=20.0, source="okx"):
        calls.append(source)
        if source == "binance":
            raise RuntimeError("451 blocked")
        return _btc_step_series()

    monkeypatch.setattr(runner_mod, "fetch_daily_bars", fake_fetch)
    monkeypatch.setattr(runner_mod, "load_state", lambda *a, **k: {})
    monkeypatch.setattr(runner_mod, "save_state", lambda *a, **k: None)

    runner = runner_mod.TrendPaperRunner(cfg)
    status = asyncio.run(runner.cycle())
    # the whole book is served by the next venue in the chain
    assert status["sources_used"] == {"binance_vision": 4}
    assert status["source"] == "binance"
    # after 3 failures the cycle stops probing the dead venue (each probe may
    # retry once, but the 4th coin is never probed)
    assert 3 <= calls.count("binance") <= 6
    assert calls.count("binance_vision") == 4
    assert "unavailable" in status["data_note"]
    assert "binance RuntimeError" in status["errors"]["BTC"]
    assert status["last_failed"] == 0
    assert runner.book.sleeves["BTC"].last_ts == TS0 + 39 * DAY


# ── exits ────────────────────────────────────────────────────────────────

def test_chandelier_stop_fires_when_low_breaks_it():
    cfg = _cfg()
    book = TrendPortfolio(cfg)
    _feed(book, "AAA", _accel(7))
    stop_before = book.sleeves["AAA"].position.stop
    crash = Bar(ts=TS0 + 7 * DAY, open=140, high=140, low=20, close=25)
    events = book.on_bar("AAA", crash)
    closes = [e for e in events if e["kind"] == "close"]
    assert len(closes) == 1
    assert closes[0]["reason"] == "stop"
    # the stop may only trail UP between the two bars, never down
    assert closes[0]["price"] >= stop_before * (1 - cfg.slippage_pct) - 1e-9
    assert book.sleeves["AAA"].position is None


def test_trailing_stop_never_moves_down():
    cfg = _cfg()
    book = TrendPortfolio(cfg)
    _feed(book, "AAA", _accel(7))
    stops = []
    for i in range(7, 12):
        bar = Bar(ts=TS0 + i * DAY, open=200 + (i - 7) * 5, high=210 + (i - 7) * 5,
                  low=199 + (i - 7) * 5, close=205 + (i - 7) * 5)
        book.on_bar("AAA", bar)
        pos = book.sleeves["AAA"].position
        if pos:
            stops.append(pos.stop)
    assert stops == sorted(stops)


def test_donchian_exit_fills_at_next_open():
    cfg = _cfg(exit_lookback=2)
    book = TrendPortfolio(cfg)
    _feed(book, "AAA", _accel(7))
    pos = book.sleeves["AAA"].position
    # close below the low of the previous 2 bars, without touching the stop
    drop = Bar(ts=TS0 + 7 * DAY, open=121, high=121, low=119, close=120)
    assert pos.stop < drop.close < pos.entry
    events = book.on_bar("AAA", drop)
    assert [e["kind"] for e in events] == ["exit_pending"]
    nxt = Bar(ts=TS0 + 8 * DAY, open=120, high=121, low=118, close=119)
    events = book.on_bar("AAA", nxt)
    closes = [e for e in events if e["kind"] == "close"]
    assert len(closes) == 1
    assert closes[0]["reason"] == "signal"
    assert closes[0]["price"] == pytest.approx(nxt.open * (1 - cfg.slippage_pct))


def test_round_trip_charges_both_fees():
    cfg = _cfg()
    book = TrendPortfolio(cfg)
    _feed(book, "AAA", _accel(7))
    entry_notional = book.sleeves["AAA"].position.notional
    book.on_bar("AAA", Bar(ts=TS0 + 7 * DAY, open=140, high=140, low=20, close=25))
    close = [e for e in book.events if e["kind"] == "close"][0]
    expected_fee = entry_notional * cfg.fee_pct + abs(close["price"] * close["qty"]) * cfg.fee_pct
    assert book.fees == pytest.approx(expected_fee, rel=1e-6)
    assert book.realized == pytest.approx(close["pnl"])


# ── determinism / safety ─────────────────────────────────────────────────

def test_duplicate_bar_is_ignored():
    book = TrendPortfolio(_cfg())
    bars = _accel(7)
    _feed(book, "AAA", bars)
    eq_before = book.realized_equity
    events = book.on_bar("AAA", bars[-1])
    assert events == []
    assert book.realized_equity == eq_before
    assert len(book.sleeves["AAA"].closes) == 7


def test_future_bars_cannot_change_past_events():
    cfg = _cfg()
    a = TrendPortfolio(cfg)
    b = TrendPortfolio(cfg)
    head = _accel(7)
    tail_a = [Bar(ts=TS0 + i * DAY, open=100, high=101, low=99, close=100) for i in range(7, 12)]
    tail_b = [Bar(ts=TS0 + i * DAY, open=500, high=600, low=100, close=550) for i in range(7, 12)]
    _feed(a, "AAA", head)
    _feed(b, "AAA", head)
    cut = TS0 + 7 * DAY
    ea = [e for e in _feed(a, "AAA", tail_a) if e["ts"] < cut]
    eb = [e for e in _feed(b, "AAA", tail_b) if e["ts"] < cut]
    assert ea == eb


def test_state_round_trip_preserves_book():
    cfg = _cfg()
    book = TrendPortfolio(cfg)
    _feed(book, "AAA", _accel(7))
    state = book.to_state()
    restored = TrendPortfolio.from_state(cfg, state)
    assert restored.realized_equity == pytest.approx(book.realized_equity)
    assert restored.sleeves["AAA"].position.entry == pytest.approx(
        book.sleeves["AAA"].position.entry
    )
    assert restored.sleeves["AAA"].last_ts == book.sleeves["AAA"].last_ts


def test_closed_trade_is_recorded_and_persisted():
    cfg = _cfg()
    book = TrendPortfolio(cfg)
    _feed(book, "AAA", _accel(7))
    book.on_bar("AAA", Bar(ts=TS0 + 7 * DAY, open=140, high=140, low=20, close=25))
    assert len(book.closed_trades) == 1
    t = book.closed_trades[0]
    assert t["coin"] == "AAA" and t["side"] == 1
    assert t["entry_ts"] < t["exit_ts"]
    assert t["pnl"] == pytest.approx(book.realized, abs=1e-3)
    assert t["reason"] == "stop"
    # survives a state round-trip
    restored = TrendPortfolio.from_state(cfg, book.to_state())
    assert len(restored.closed_trades) == 1
    assert restored.closed_trades[0]["pnl"] == pytest.approx(t["pnl"])


def test_runner_trades_are_newest_first():
    from app.bots.trend_portfolio.runner import TrendPaperRunner

    runner = TrendPaperRunner(_cfg(state_name="test_trend_trades_xyz"))
    runner.book.closed_trades = [{"coin": "A"}, {"coin": "B"}, {"coin": "C"}]
    assert [x["coin"] for x in runner.trades(2)] == ["C", "B"]
    assert [x["coin"] for x in runner.trades(100)] == ["C", "B", "A"]


def test_replayed_state_does_not_retrade_old_bars():
    cfg = _cfg()
    book = TrendPortfolio(cfg)
    bars = _accel(7)
    _feed(book, "AAA", bars)
    restored = TrendPortfolio.from_state(cfg, book.to_state())
    events = _feed(restored, "AAA", bars)
    assert events == []


def test_module_has_no_order_placement_code():
    import pathlib

    import app.bots.trend_portfolio as pkg

    root = pathlib.Path(pkg.__file__).parent
    banned = ("place_order", "place_algo_order", "close_position", "cancel_order")
    for path in root.glob("*.py"):
        src = path.read_text()
        for token in banned:
            assert token not in src, f"{path.name} references {token}"


def test_backtest_reports_stats_and_curve():
    cfg = _cfg(universe=("AAA", "BBB"))
    series = {"AAA": _accel(40), "BBB": _accel(40, base=50.0)}
    res = backtest(series, cfg)
    assert res["stats"]["days"] == 40
    assert "cagr_pct" in res["stats"]
    assert len(res["curve"]) == 40


def test_router_routes_are_declared_and_policy_marked():
    from fastapi import FastAPI

    from app.routers import include_routers
    from app.services.route_policy import audit_routes

    app = FastAPI()
    include_routers(app)
    paths = {r.path for r in app.routes}
    assert "/api/trend/status" in paths
    assert "/api/trend/positions" in paths
    assert "/api/trend/trades" in paths
    assert "/api/trend/cycle" in paths
    assert "/api/trend/backtest" in paths
    unmarked, _ = audit_routes(app)
    assert unmarked == []


# ── durable state (the book must survive a Render restart) ───────────────

class _FakeDB:
    """Minimal async settings store, same surface as app.database.db."""

    def __init__(self):
        self.store = {}
        self._pg_mode = True

    async def get_setting(self, key):
        return self.store.get(key)

    async def set_setting(self, key, value):
        self.store[key] = value


def _patch_fetch(monkeypatch, runner_mod):
    btc, aaa = _btc_step_series(), _alt_two_breakouts()

    async def fake_fetch(inst_id, limit=300, timeout=20.0, source="okx"):
        return list(btc if inst_id.split("-")[0] == "BTC" else aaa)

    monkeypatch.setattr(runner_mod, "fetch_daily_bars", fake_fetch)
    monkeypatch.setattr(runner_mod, "load_state", lambda *a, **k: {})
    monkeypatch.setattr(runner_mod, "save_state", lambda *a, **k: None)
    return btc, aaa


def test_runner_persists_state_to_db(monkeypatch):
    import asyncio

    import app.bots.trend_portfolio.runner as runner_mod

    cfg = _gate_cfg(state_name="test_trend_db_state")
    _patch_fetch(monkeypatch, runner_mod)

    db = _FakeDB()
    runner = runner_mod.TrendPaperRunner(cfg, db=db)
    status = asyncio.run(runner.cycle())
    key = f"{cfg.state_name}_state"
    assert key in db.store
    assert status["state_store"] == "postgres"
    assert status["restored_from"] == ""
    assert status["live_since"] > 0
    assert status["forward_return_pct"] == 0.0

    # A fresh process restores the same book and keeps the forward baseline.
    restored = runner_mod.TrendPaperRunner(cfg, db=db)
    asyncio.run(restored._restore_from_db())
    assert restored.book.last_ts == runner.book.last_ts
    assert restored.live_since == runner.live_since
    assert restored.live_start_equity == pytest.approx(runner.live_start_equity)
    assert restored.state_store == "postgres"
    assert restored.restored_from == "postgres"


def test_runner_db_failure_falls_back_to_the_file(monkeypatch):
    import asyncio

    import app.bots.trend_portfolio.runner as runner_mod

    class _BrokenDB:
        async def get_setting(self, key):
            raise RuntimeError("db down")

        async def set_setting(self, key, value):
            raise RuntimeError("db down")

    cfg = _gate_cfg(state_name="test_trend_db_down")
    _patch_fetch(monkeypatch, runner_mod)
    runner = runner_mod.TrendPaperRunner(cfg, db=_BrokenDB())
    status = asyncio.run(runner.cycle())
    assert status["state_store"] == "file"
    assert status["restored_from"] == ""
    assert status["live_since"] > 0
    assert status["forward_return_pct"] == 0.0


def test_runner_keeps_the_newer_file_state_when_db_is_behind(monkeypatch):
    import asyncio
    import json

    import app.bots.trend_portfolio.runner as runner_mod
    from app.bots.trend_portfolio.engine import feed_chronological

    cfg = _gate_cfg(state_name="test_trend_db_behind")
    btc, aaa = _btc_step_series(), _alt_two_breakouts()
    file_book = TrendPortfolio(cfg)
    feed_chronological(file_book, {"BTC": btc, "AAA": aaa})
    db_book = TrendPortfolio(cfg)
    feed_chronological(db_book, {"BTC": btc[:20], "AAA": aaa[:20]})

    monkeypatch.setattr(runner_mod, "load_state", lambda *a, **k: file_book.to_state())
    monkeypatch.setattr(runner_mod, "save_state", lambda *a, **k: None)
    db = _FakeDB()
    db_state = db_book.to_state()
    db_state["schema"] = runner_mod._STATE_SCHEMA
    db.store[f"{cfg.state_name}_state"] = json.dumps(db_state)

    runner = runner_mod.TrendPaperRunner(cfg, db=db)
    asyncio.run(runner._restore_from_db())
    # the DB copy is one bar older (crashed save): keep the file book
    assert runner.book.last_ts == file_book.last_ts
    assert runner.state_store == "file"


def test_runner_ignores_a_state_from_an_older_schema(monkeypatch):
    import asyncio
    import json

    import app.bots.trend_portfolio.runner as runner_mod

    cfg = _gate_cfg(state_name="test_trend_old_schema")
    monkeypatch.setattr(runner_mod, "load_state", lambda *a, **k: {})
    db = _FakeDB()
    db.store[f"{cfg.state_name}_state"] = json.dumps({
        "sleeves": {"BTC": {"closes": [1.0] * 5}},
        "live_since": 123,
        "live_start_equity": 10_000.0,
    })
    runner = runner_mod.TrendPaperRunner(cfg, db=db)
    asyncio.run(runner._restore_from_db())
    # A schema bump starts a fresh replay instead of mixing incompatible books.
    assert runner.live_since == 0
    assert runner.book.last_ts == 0
    assert runner.state_store == "postgres"


def test_runner_uses_the_fallback_venue_and_counts_only_real_gaps(monkeypatch):
    import asyncio

    import app.bots.trend_portfolio.runner as runner_mod

    cfg = _gate_cfg(state_name="test_trend_fallback")
    btc, aaa = _btc_step_series(), _alt_two_breakouts()
    calls = []

    async def fake_fetch(inst_id, limit=300, timeout=20.0, source="binance_vision"):
        coin = inst_id.split("-")[0]
        calls.append((coin, source))
        if coin == "AAA" and source == "binance_vision":
            raise RuntimeError("rate limited")
        return list(btc if coin == "BTC" else aaa)

    monkeypatch.setattr(runner_mod, "fetch_daily_bars", fake_fetch)
    monkeypatch.setattr(runner_mod, "load_state", lambda *a, **k: {})
    monkeypatch.setattr(runner_mod, "save_state", lambda *a, **k: None)
    runner = runner_mod.TrendPaperRunner(cfg, db=_FakeDB())
    status = asyncio.run(runner.cycle())
    # AAA was served by the next venue in the chain: not a failure, book stays
    # complete.
    assert ("AAA", "okx") in calls
    assert status["last_failed"] == 0
    assert runner.book.sleeves["AAA"].last_ts == aaa[-1].ts


def test_runner_counts_a_coin_with_no_bars_at_all(monkeypatch):
    import asyncio

    import app.bots.trend_portfolio.runner as runner_mod

    cfg = _gate_cfg(state_name="test_trend_missing")
    btc, _aaa = _btc_step_series(), _alt_two_breakouts()

    async def fake_fetch(inst_id, limit=300, timeout=20.0, source="okx"):
        if inst_id.split("-")[0] == "AAA":
            raise RuntimeError("no data")
        return list(btc)

    monkeypatch.setattr(runner_mod, "fetch_daily_bars", fake_fetch)
    monkeypatch.setattr(runner_mod, "load_state", lambda *a, **k: {})
    monkeypatch.setattr(runner_mod, "save_state", lambda *a, **k: None)
    runner = runner_mod.TrendPaperRunner(cfg, db=_FakeDB())
    status = asyncio.run(runner.cycle())
    assert status["last_failed"] == 1
    assert "AAA" in status["data_note"]
    assert runner.book.sleeves["AAA"].last_ts == 0


def test_runner_seeds_a_coin_first_seen_after_the_baseline(monkeypatch):
    import asyncio

    import app.bots.trend_portfolio.runner as runner_mod

    cfg = _gate_cfg(state_name="test_trend_late_coin")
    btc, aaa = _btc_step_series(), _alt_two_breakouts()

    async def fake_fetch(inst_id, limit=300, timeout=20.0, source="binance_vision"):
        return list(btc if inst_id.split("-")[0] == "BTC" else aaa)

    monkeypatch.setattr(runner_mod, "fetch_daily_bars", fake_fetch)
    monkeypatch.setattr(runner_mod, "load_state", lambda *a, **k: {})
    monkeypatch.setattr(runner_mod, "save_state", lambda *a, **k: None)
    runner = runner_mod.TrendPaperRunner(cfg, db=_FakeDB())
    # Simulate a live book that has never seen AAA: only the newest bar may be
    # fed, otherwise the forward record would inherit 40 bars of fake history.
    runner.live_since = 1
    runner.live_start_equity = runner.book.mark_equity()
    asyncio.run(runner.cycle())
    assert len(runner.book.sleeves["AAA"].closes) == 1
    assert runner.book.sleeves["AAA"].last_ts == aaa[-1].ts


def test_runner_skips_delisted_coins_instead_of_replaying_old_prices(monkeypatch):
    import asyncio

    import app.bots.trend_portfolio.runner as runner_mod

    cfg = _gate_cfg(state_name="test_trend_stale")
    btc = _btc_step_series()
    dead = _flat_bars(11)          # stopped trading 29 days before BTC

    async def fake_fetch(inst_id, limit=300, timeout=20.0, source="binance_vision"):
        return list(btc if inst_id.split("-")[0] == "BTC" else dead)

    monkeypatch.setattr(runner_mod, "fetch_daily_bars", fake_fetch)
    monkeypatch.setattr(runner_mod, "load_state", lambda *a, **k: {})
    monkeypatch.setattr(runner_mod, "save_state", lambda *a, **k: None)
    runner = runner_mod.TrendPaperRunner(cfg, db=_FakeDB())
    status = asyncio.run(runner.cycle())
    assert status["stale_coins"] == 1
    # no phantom position at 2024 prices from a delisted market
    assert runner.book.sleeves["AAA"].last_ts == 0
    assert runner.book.sleeves["BTC"].last_ts == btc[-1].ts


