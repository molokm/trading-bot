"""Core portfolio — config, 6h engine causality, runner and safety.

The module must stay paper-only: tests assert that no order-placing call exists
anywhere in its source and that the config cannot be flipped out of paper mode.
"""
import time

import pytest

from app.bots.core_portfolio.config import CorePortfolioConfig
from app.bots.core_portfolio.engine6h import (
    Trend6hBook,
    feed_chronological,
    gate_map,
)
from app.bots.core_portfolio.runner import CorePaperRunner, backtest_core, curve_stats
from app.bots.trend_portfolio.engine import Bar

H6 = 21_600_000
TS0 = 1_700_000_000_000


def _cfg(**kw):
    base = dict(
        universe=("AAA",),
        capital=1000.0,
        daily_weight=0.5,
        h6_entry=5,
        h6_exit=3,
        h6_atr_period=2,
        h6_chandelier=2.0,
        h6_risk_per_sleeve=0.05,
        h6_max_sleeve_leverage=1000.0,
        daily_entry=5,
        daily_exit=3,
        daily_chandelier=2.0,
        daily_atr_period=2,
        daily_risk_per_sleeve=0.32,
        daily_max_sleeve_leverage=1000.0,
        regime_symbol="",
        regime_ma_days=200,
        h6_candle_limit=50,
        gate_bars=50,
        daily_candle_limit=250,
        fee_pct=0.0008,
        slippage_pct=0.0008,
    )
    base.update(kw)
    return CorePortfolioConfig(**base)


def _accel(n, base=100.0, step=H6):
    """Close = base + i^2 — an accelerating uptrend that breaks a 5-bar high."""
    return [
        Bar(ts=TS0 + i * step, open=base + i * i, high=base + i * i + 1,
            low=base + i * i - 1, close=base + i * i)
        for i in range(n)
    ]


def _flat(n, price=100.0, step=H6):
    return [
        Bar(ts=TS0 + i * step, open=price, high=price + 1, low=price - 1, close=price)
        for i in range(n)
    ]


def _feed(book, coin, bars, gate_on=True):
    out = []
    for b in bars:
        out += book.on_bar(coin, b, gate_on)
    return out


# ── config ───────────────────────────────────────────────────────────────
def test_disabled_by_default():
    assert CorePortfolioConfig().enabled is False
    assert CorePortfolioConfig().paper_only is True


def test_env_gate_off_by_default(monkeypatch):
    monkeypatch.delenv("CORE_PORTFOLIO_ENABLE", raising=False)
    assert CorePortfolioConfig.from_env().enabled is False


def test_default_source_is_binance_utc(monkeypatch):
    # 6h was validated on UTC bars; OKX (UTC+8) must be an explicit opt-in.
    monkeypatch.delenv("CORE_PORTFOLIO_SOURCE", raising=False)
    cfg = CorePortfolioConfig.from_env()
    assert cfg.source == "binance"
    assert cfg.daily_source == "binance"


def test_env_gate_on_and_params(monkeypatch):
    monkeypatch.setenv("CORE_PORTFOLIO_ENABLE", "1")
    monkeypatch.setenv("CORE_PORTFOLIO_DAILY_WEIGHT", "0.6")
    monkeypatch.setenv("CORE_PORTFOLIO_H6_ENTRY", "60")
    monkeypatch.setenv("CORE_PORTFOLIO_DAILY_RISK", "0.5")
    monkeypatch.setenv("CORE_PORTFOLIO_SOURCE", "BINANCE")
    cfg = CorePortfolioConfig.from_env()
    assert cfg.enabled is True and cfg.paper_only is True
    assert cfg.daily_weight == pytest.approx(0.6)
    assert cfg.h6_entry == 60
    assert cfg.daily_risk_per_sleeve == pytest.approx(0.5)
    assert cfg.source == "binance"


def test_paper_only_cannot_be_flipped():
    cfg = _cfg()
    cfg.paper_only = False
    with pytest.raises(ValueError):
        cfg.validate()


def test_capital_splits_and_costs():
    cfg = _cfg(capital=10_000.0, daily_weight=0.4)
    assert cfg.daily_capital == pytest.approx(4_000.0)
    assert cfg.h6_capital == pytest.approx(6_000.0)
    assert cfg.cost_per_side == pytest.approx(0.0016)


def test_daily_book_config_uses_half_capital_and_same_costs():
    cfg = _cfg(capital=10_000.0)
    d = cfg.daily_book_config()
    assert d.capital == pytest.approx(5_000.0)
    assert d.risk_per_sleeve == pytest.approx(cfg.daily_risk_per_sleeve)
    assert d.fee_pct == pytest.approx(cfg.fee_pct)
    assert d.slippage_pct == pytest.approx(cfg.slippage_pct)
    assert d.regime_symbol == ""
    assert d.source in ("okx", "binance")


def test_invalid_windows_rejected():
    with pytest.raises(ValueError):
        _cfg(h6_exit=5).validate()
    with pytest.raises(ValueError):
        _cfg(daily_exit=5).validate()
    with pytest.raises(ValueError):
        _cfg(daily_weight=1.0).validate()
    with pytest.raises(ValueError):
        _cfg(source="kraken").validate()


def test_regime_symbol_must_be_in_universe():
    with pytest.raises(ValueError):
        _cfg(regime_symbol="BTC").validate()


def test_binance_vision_maps_to_binance_daily_source():
    assert _cfg(source="binance_vision").daily_source == "binance"
    assert _cfg(source="okx").daily_source == "okx"


# ── 6h signals / fills ──────────────────────────────────────────────────
def test_no_entry_before_the_breakout_bar():
    book = Trend6hBook(_cfg())
    events = _feed(book, "AAA", _accel(5))
    assert [e for e in events if e["kind"] == "enter"] == []
    assert book.sleeves["AAA"].pending is None


def test_signal_on_close_fills_at_next_open():
    cfg = _cfg()
    book = Trend6hBook(cfg)
    bars = _accel(7)
    events = _feed(book, "AAA", bars)
    enters = [e for e in events if e["kind"] == "enter"]
    assert len(enters) == 1
    assert enters[0]["ts"] == bars[6].ts          # next bar, never the signal bar
    assert enters[0]["price"] == pytest.approx(bars[6].open)


def test_initial_stop_is_anchored_to_the_signal_close():
    cfg = _cfg()
    book = Trend6hBook(cfg)
    bars = _accel(7)
    for b in bars[:6]:
        book.on_bar("AAA", b)
    s = book.sleeves["AAA"]
    atr_signal = s.atr
    assert atr_signal > 0 and s.pending == 1
    # the pending stop is anchored to the signal bar's close
    assert s.pending_stop == pytest.approx(bars[5].close - cfg.h6_chandelier * atr_signal)
    book.on_bar("AAA", bars[6])
    assert s.pos == 1
    # the chandelier may only trail up from that anchor, never reset to the fill
    assert s.stop == pytest.approx(
        max(s.pending_stop, bars[6].close - cfg.h6_chandelier * s.atr)
    )


def test_sizing_is_risk_over_stop_distance():
    cfg = _cfg()
    book = Trend6hBook(cfg)
    bars = _accel(7)
    _feed(book, "AAA", bars[:6])
    atr_signal = book.sleeves["AAA"].atr
    book.on_bar("AAA", bars[6])
    s = book.sleeves["AAA"]
    lev = min(cfg.h6_max_sleeve_leverage,
              cfg.h6_risk_per_sleeve / (cfg.h6_chandelier * atr_signal / bars[6].open))
    assert s.qty == pytest.approx(lev * cfg.h6_capital / bars[6].open)


def test_gate_blocks_entries_but_not_exits():
    cfg = _cfg()
    blocked = Trend6hBook(cfg)
    _feed(blocked, "AAA", _accel(7), gate_on=False)
    assert blocked.sleeves["AAA"].pos == 0
    allowed = Trend6hBook(cfg)
    _feed(allowed, "AAA", _accel(7), gate_on=True)
    assert allowed.sleeves["AAA"].pos == 1


def test_exit_fills_at_next_open():
    cfg = _cfg()
    book = Trend6hBook(cfg)
    bars = _accel(7)
    _feed(book, "AAA", bars)
    # crash below the chandelier stop → exit target set, filled next open
    crash = Bar(ts=TS0 + 7 * H6, open=140, high=140, low=20, close=25)
    book.on_bar("AAA", crash)
    assert book.sleeves["AAA"].pending == 0
    nxt = Bar(ts=TS0 + 8 * H6, open=30, high=31, low=29, close=30)
    events = book.on_bar("AAA", nxt)
    assert [e["kind"] for e in events] == ["close"]
    t = book.closed_trades[-1]
    assert t["exit"] == pytest.approx(30)
    assert t["reason"] == "stop"
    assert book.sleeves["AAA"].pos == 0


def test_duplicate_bar_is_ignored():
    book = Trend6hBook(_cfg())
    bars = _accel(7)
    _feed(book, "AAA", bars)
    eq = book.equity
    assert book.on_bar("AAA", bars[-1]) == []
    assert book.equity == eq
    assert len(book.sleeves["AAA"].highs) == 7


def test_state_round_trip_preserves_pending_and_position():
    cfg = _cfg()
    book = Trend6hBook(cfg)
    bars = _accel(7)
    _feed(book, "AAA", bars)
    state = book.to_state()
    restored = Trend6hBook.from_state(cfg, state)
    s, r = book.sleeves["AAA"], restored.sleeves["AAA"]
    assert r.pos == s.pos == 1
    assert r.entry == pytest.approx(s.entry)
    assert r.stop == pytest.approx(s.stop)
    assert r.qty == pytest.approx(s.qty)
    assert r.atr == pytest.approx(s.atr)
    assert r.last_ts == s.last_ts
    assert restored.equity == pytest.approx(book.equity)


def test_future_bars_cannot_change_past_events():
    cfg = _cfg()
    a, b = Trend6hBook(cfg), Trend6hBook(cfg)
    head = _accel(7)
    _feed(a, "AAA", head)
    _feed(b, "AAA", head)
    cut = TS0 + 7 * H6
    tail_a = _flat(5, step=H6)
    tail_b = [Bar(ts=TS0 + i * H6, open=500, high=600, low=100, close=550)
              for i in range(7, 12)]
    ea = [e for e in _feed(a, "AAA", tail_a) if e["ts"] < cut]
    eb = [e for e in _feed(b, "AAA", tail_b) if e["ts"] < cut]
    assert ea == eb


def test_state_keeps_enough_bars_for_the_entry_window():
    cfg = _cfg()
    book = Trend6hBook(cfg)
    _feed(book, "AAA", _accel(40))
    state = book.to_state()
    assert len(state["sleeves"]["AAA"]["highs"]) == cfg.h6_state_bars


# ── gate ────────────────────────────────────────────────────────────────
def test_gate_map_is_causal_and_starts_on():
    bars = _flat(3) + [
        Bar(ts=TS0 + i * H6, open=100 + 10 * (i - 3), high=110 + 10 * (i - 3),
            low=100 + 10 * (i - 3), close=100 + 10 * (i - 3))
        for i in range(3, 8)
    ]
    gates = gate_map(bars, ma_days=1)            # n = 4 bars
    assert gates[bars[0].ts] is True             # warm-up: treated as ON
    assert gates[bars[3].ts] is True
    assert gates[bars[4].ts] is True             # close > mean
    # a falling series turns it off
    down = _flat(4) + [
        Bar(ts=TS0 + i * H6, open=100 - 10 * (i - 4), high=100 - 10 * (i - 4),
            low=90 - 10 * (i - 4), close=100 - 10 * (i - 4))
        for i in range(4, 8)
    ]
    gates = gate_map(down, ma_days=1)
    assert gates[down[-1].ts] is False


def test_chronological_feed_uses_the_gate_of_each_bar():
    cfg = _cfg(universe=("BTC", "AAA"), regime_symbol="BTC", regime_ma_days=5)
    btc = _flat(8) + [
        Bar(ts=TS0 + i * H6, open=100 + 10 * (i - 8), high=101 + 10 * (i - 8),
            low=99 + 10 * (i - 8), close=100 + 10 * (i - 8))
        for i in range(8, 14)
    ]
    aaa = _flat(14)
    # breakout while the gate is OFF (i=6) …
    aaa[6] = Bar(ts=TS0 + 6 * H6, open=100, high=131, low=99, close=130)
    # … and again after BTC has turned the gate ON (i=10)
    aaa[10] = Bar(ts=TS0 + 10 * H6, open=101, high=136, low=100, close=135)
    for i in range(11, 14):
        aaa[i] = Bar(ts=TS0 + i * H6, open=135 + i, high=140 + i,
                     low=134 + i, close=138 + i)
    book = Trend6hBook(cfg)
    feed_chronological(book, {"BTC": btc, "AAA": aaa}, gate_map(btc, 1))
    assert book.sleeves["AAA"].pos == 1
    assert book.sleeves["AAA"].entry_ts == TS0 + 11 * H6


# ── runner ──────────────────────────────────────────────────────────────
def _runner_cfg(**kw):
    base = dict(
        enabled=True,
        universe=("BTC", "AAA"),
        capital=10_000.0,
        h6_entry=5,
        h6_exit=3,
        h6_atr_period=2,
        h6_chandelier=2.0,
        h6_candle_limit=80,
        gate_bars=40,
        regime_symbol="BTC",
        regime_ma_days=5,
        daily_entry=5,
        daily_exit=3,
        daily_chandelier=2.0,
        daily_atr_period=2,
        daily_candle_limit=250,
        poll_interval_sec=1800,
        state_name="test_core_runner",
    )
    base.update(kw)
    return _cfg(**base)


def _runner_bars(n6=120, nd=120):
    daily = {c: _flat(nd, step=86_400_000) for c in ("BTC", "AAA")}
    six = {c: _flat(n6) for c in ("BTC", "AAA")}
    return daily, six


def _patch(monkeypatch, runner_mod, daily, six, fail=()):
    async def fake_fetch(coin, interval, limit, venue, timeout=20.0):
        if venue in fail:
            raise RuntimeError("451 blocked")
        return list(six[coin] if interval == "6h" else daily[coin])

    monkeypatch.setattr(runner_mod, "fetch_bars", fake_fetch)
    monkeypatch.setattr(runner_mod, "load_state", lambda *a, **k: {})
    monkeypatch.setattr(runner_mod, "save_state", lambda *a, **k: None)


def test_runner_cycle_combines_two_books(monkeypatch):
    import asyncio

    import app.bots.core_portfolio.runner as runner_mod

    daily, six = _runner_bars()
    _patch(monkeypatch, runner_mod, daily, six)
    runner = CorePaperRunner(_runner_cfg())
    status = asyncio.run(runner.cycle())
    assert status["enabled"] is True and status["paper_only"] is True
    assert status["combined"]["equity"] == pytest.approx(10_000.0, abs=1e-6)
    assert status["blocks"]["daily"]["equity"] == pytest.approx(5_000.0, abs=1e-6)
    assert status["blocks"]["h6"]["equity"] == pytest.approx(5_000.0, abs=1e-6)
    assert status["fresh_bars"] > 0
    assert len(status["equity_curve"]) == 1
    assert len(status["equity_curve"][0]["day"]) == 10
    assert status["sources_used"] == {runner.h6_venue: 2}
    assert status["h6_venue"] == runner.daily_venue
    assert status["data_note"] == ""


def test_runner_stops_probing_a_blocked_venue(monkeypatch):
    import asyncio

    import app.bots.core_portfolio.runner as runner_mod

    universe = ("BTC", "AAA", "BBB", "CCC", "DDD", "EEE")
    daily = {c: _flat(120, step=86_400_000) for c in universe}
    six = {c: _flat(120) for c in universe}
    calls = {"binance": 0}

    async def fake_fetch(coin, interval, limit, venue, timeout=20.0):
        if venue == "binance":
            calls["binance"] += 1
            raise RuntimeError("451 blocked")
        return list(six[coin] if interval == "6h" else daily[coin])

    monkeypatch.setattr(runner_mod, "fetch_bars", fake_fetch)
    monkeypatch.setattr(runner_mod, "load_state", lambda *a, **k: {})
    monkeypatch.setattr(runner_mod, "save_state", lambda *a, **k: None)
    runner = CorePaperRunner(
        _runner_cfg(source="binance", universe=universe, h6_candle_limit=130)
    )
    status = asyncio.run(runner.cycle())
    # 1 gate probe for the 6h block + at most 3 daily coins, not 2 x 6.
    assert calls["binance"] <= 4
    assert runner.h6_venue == "binance_vision"
    assert runner.daily_venue == "binance_vision"
    assert status["last_error"] == ""


def test_runner_does_not_start_when_disabled():
    runner = CorePaperRunner(_runner_cfg(enabled=False))
    assert runner.start() is False


def test_runner_falls_back_to_a_working_venue(monkeypatch):
    import asyncio

    import app.bots.core_portfolio.runner as runner_mod

    daily, six = _runner_bars()
    _patch(monkeypatch, runner_mod, daily, six, fail=("binance", "binance_vision"))
    runner = CorePaperRunner(_runner_cfg(source="binance"))
    status = asyncio.run(runner.cycle())
    # The 6h block was validated on UTC bars: it refuses to silently move to
    # OKX (UTC+8) and simply skips the cycle instead.
    assert runner.h6_venue == ""
    assert runner.daily_venue == "okx"
    assert status["sources_used"].get("okx") == 1
    assert "6h block: no data" in status["last_error"]
    assert "unreachable" in status["data_note"]


def test_runner_keeps_the_book_when_only_the_utc_host_changes(monkeypatch):
    import asyncio

    import app.bots.core_portfolio.runner as runner_mod

    daily, six = _runner_bars()
    _patch(monkeypatch, runner_mod, daily, six, fail=("binance",))
    cfg = _runner_cfg(source="binance")
    runner = CorePaperRunner(cfg)
    runner.h6_venue = "binance"
    runner.h6_bucket = "utc"
    runner.h6.sleeves["AAA"].last_ts = TS0 + 50 * H6
    status = asyncio.run(runner.cycle())
    # binance → binance_vision is the same UTC bucketing: no restart.
    assert runner.h6_venue == "binance_vision"
    assert runner.h6_bucket == "utc"
    assert "restarted" not in status["data_note"]


def test_runner_restarts_a_block_when_the_bucketing_changes(monkeypatch):
    import asyncio

    import app.bots.core_portfolio.runner as runner_mod

    daily, six = _runner_bars()
    _patch(monkeypatch, runner_mod, daily, six, fail=("binance", "binance_vision"))
    cfg = _runner_cfg(source="okx")
    runner = CorePaperRunner(cfg)
    runner.h6_venue = "binance"
    runner.h6_bucket = "utc"
    runner.h6.sleeves["AAA"].last_ts = TS0 + 50 * H6
    status = asyncio.run(runner.cycle())
    assert runner.h6_venue == "okx"
    assert runner.h6_bucket == "utc+8"
    assert "block restarted" in status["data_note"]


def test_runner_positions_trades_and_equity_curve(monkeypatch):
    import asyncio

    import app.bots.core_portfolio.runner as runner_mod

    daily, six = _runner_bars()
    _patch(monkeypatch, runner_mod, daily, six)
    runner = CorePaperRunner(_runner_cfg())
    asyncio.run(runner.cycle())
    runner.daily.closed_trades = [{"coin": "AAA", "exit_ts": 1, "pnl": 1.0}]
    runner.h6.closed_trades = [{"coin": "BTC", "exit_ts": 2, "pnl": -1.0}]
    trades = runner.trades(10)
    assert [t["coin"] for t in trades] == ["BTC", "AAA"]
    assert [t["block"] for t in trades] == ["6h", "daily"]
    curve = runner.status()["equity_curve"]
    assert curve and len(curve[-1]["day"]) == 10


def test_runner_trades_are_bounded_and_newest_first():
    runner = CorePaperRunner(_runner_cfg(state_name="test_core_trades_xyz"))
    runner.h6.closed_trades = [{"coin": str(i), "exit_ts": i} for i in range(20)]
    assert [t["coin"] for t in runner.trades(5)] == ["19", "18", "17", "16", "15"]


def test_backtest_core_blends_two_accounts(monkeypatch):
    cfg = _cfg(
        universe=("BTC", "AAA"), regime_symbol="BTC", regime_ma_days=5,
        h6_candle_limit=50, gate_bars=50,
    )
    daily = {c: _flat(120, step=86_400_000) for c in cfg.universe}
    six = {c: _flat(480) for c in cfg.universe}
    res = backtest_core(daily, six, cfg)
    assert res["ok"] is True
    assert res["stats"]["days"] > 50
    assert set(res["blocks"]) == {"daily", "h6"}
    assert res["trades"] == {"daily": 0, "h6": 0}
    assert res["curve"]


def test_curve_stats_math():
    pts = [{"equity": 100.0}, {"equity": 110.0}, {"equity": 99.0}]
    st = curve_stats(pts, 100.0)
    assert st["days"] == 3
    assert st["max_dd_pct"] > 0
    assert st["final_equity"] == 99.0


# ── safety ──────────────────────────────────────────────────────────────
def test_module_has_no_order_placement_code():
    import pathlib

    import app.bots.core_portfolio as pkg

    root = pathlib.Path(pkg.__file__).parent
    banned = ("place_order", "place_algo_order", "close_position", "cancel_order",
              "withdraw", "transfer")
    for path in root.glob("*.py"):
        src = path.read_text()
        for token in banned:
            assert token not in src, f"{path.name} references {token}"


def test_runner_reads_only_public_market_data():
    import pathlib

    import app.bots.core_portfolio.runner as runner_mod

    src = pathlib.Path(runner_mod.__file__).read_text()
    assert "api/v5/market/candles" in src
    assert "api/v3/klines" in src
    assert "api_key" not in src.lower()
    assert "secret" not in src.lower()


def test_router_routes_are_declared_and_policy_marked():
    from fastapi import FastAPI

    from app.routers import include_routers
    from app.services.route_policy import audit_routes

    app = FastAPI()
    include_routers(app)
    paths = {r.path for r in app.routes}
    for p in ("/api/core/status", "/api/core/positions", "/api/core/trades",
              "/api/core/cycle", "/api/core/backtest"):
        assert p in paths
    unmarked, _ = audit_routes(app)
    assert unmarked == []
