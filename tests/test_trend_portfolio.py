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
    assert "/api/trend/cycle" in paths
    assert "/api/trend/backtest" in paths
    unmarked, _ = audit_routes(app)
    assert unmarked == []
