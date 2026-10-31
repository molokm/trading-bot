"""v1.41 «Извлечение прибыли»: 4 правки ревизии настроек.

1. `size_cap_floor` (0.08) больше не перекрывает защитный `conservative` cap
   (0.06) — раньше дефенсив-сайзинг был обезврежен клампом.
2. `hard_profit_lock` был мёртв (giveback срабатывал раньше). Теперь порог —
   `max(floor, retain×peak)`, правило реально связывает для пиков ≥2.6%.
3. Soft-lock: при пике ≥`soft_lock_peak_pct` стоп паркуется под пик на
   `soft_lock_atr_mult`×ATR — закрывает «мёртвую зону» +1%..+1.8%.
4. Adaptive считает режим по winsorized-среднему, один хвостовой стоп-аут
   больше не запирает пресет в `defensive`.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.bots.ai_discretionary.strategy import (
    AIConfig,
    AIPosition,
    AIStrategy,
    _winsorize,
)


# ── (1) защитный сайзинг ────────────────────────────────────────────────

def test_size_cap_floor_does_not_undo_conservative():
    c = AIConfig()
    assert c.size_cap_floor <= 0.06


def _adapt_bot(rows):
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot._positions = {}
    bot._latest_indicators = {}
    bot._adapt = None
    bot._adapt_log = []
    bot._lifetime_trades = 0
    bot._lifetime_wins = 0
    bot._build_reflection = lambda stats: ""
    bot._closed_trades = lambda demo_only=False: rows
    return bot


def test_conservative_preset_actually_shrinks_size():
    bot = _adapt_bot([{"pnl": -10.0 - i} for i in range(8)])   # 0% WR
    adapt = bot._refresh_adaptive()

    assert adapt["preset"] == "conservative"
    assert adapt["size_cap"] == 0.06          # NOT clamped back up to 0.08


# ── (2) hard_profit_lock теперь достижим ────────────────────────────────

def test_hard_lock_threshold_sits_above_giveback():
    c = AIConfig()
    peak = 2.60
    giveback_thr = peak * c.giveback_retain_frac
    hard_thr = max(c.hard_profit_lock_floor, peak * c.hard_profit_lock_retain)

    assert hard_thr > giveback_thr            # rule can fire before giveback
    assert hard_thr > c.hard_profit_lock_floor  # retain actually engages


# ── (3) soft-lock закрывает мёртвую зону ────────────────────────────────

def _ladder_bot(upl, atr_p=0.012, regime="bear", adx=25.0):
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot.client_manager = None
    bot.db = None
    bot._positions = {}
    bot._latest_indicators = {
        "ADA": {"regime": regime, "adx": adx, "align_long": 0.2, "align_short": 0.2}
    }

    async def _noop(*a, **k):
        return None

    async def _false(*a, **k):
        return False

    async def _price(client, coin, pos=None):
        return float(pos.entry_price) * (1 + upl / 100.0)

    bot._price_for_exit = _price
    bot._unrealized_pct = lambda pos, px: upl
    bot._atr_pct = lambda coin, entry=0.0: atr_p
    bot._thesis_break_reason = lambda pos, ind, u, held: None
    bot._indicator_exit_reason = lambda pos, ind, held: None
    bot._scale_out_partial = _false
    bot._persist_peaks = _noop
    bot._notify_close_tg = _noop
    bot._amend_exchange_sl = _noop
    bot._sync_live_stop = _noop
    closes = []

    async def _close(client, coin, reason):
        closes.append(reason)

    bot._close = _close
    bot._closed = closes
    return bot


def _pos(peak_upl, side="long", entry=100.0):
    opened = datetime.now(timezone.utc) - timedelta(minutes=20)
    return AIPosition(
        coin="ADA", inst_id="ADA-USDT-SWAP", side=side, size=1.0,
        entry_price=entry,
        stop_price=entry * 0.98 if side == "long" else entry * 1.02,
        take_price=entry * 1.04 if side == "long" else entry * 0.96,
        leverage=1.0, opened_at=opened.isoformat(), signal_id=1,
        peak_price=entry * (1 + peak_upl / 100.0),
        peak_upl_pct=peak_upl,
    )


@pytest.mark.asyncio
async def test_soft_lock_lifts_stop_above_breakeven_in_the_dead_zone():
    bot = _ladder_bot(upl=1.55, atr_p=0.012)      # peak 1.6% < giveback 1.8%
    bot._positions["ADA"] = _pos(peak_upl=1.60)

    await bot._manage_stops(None)

    assert bot._closed == []                       # nothing closed
    # stop ≈ peak − 0.8×ATR = 1.6% − 0.96% = +0.64%, well above BE +0.20%
    assert 100.55 < bot._positions["ADA"].stop_price < 100.75


@pytest.mark.asyncio
async def test_hard_lock_fires_for_a_big_peak_that_gave_back():
    bot = _ladder_bot(upl=1.20)                    # peak 2.6% gave back to +1.2%
    bot._positions["ADA"] = _pos(peak_upl=2.60)

    await bot._manage_stops(None)

    assert bot._closed and bot._closed[0].startswith("profit_lock_peak_")


@pytest.mark.asyncio
async def test_small_peak_still_uses_giveback_not_hard_lock():
    bot = _ladder_bot(upl=0.50)
    bot._positions["ADA"] = _pos(peak_upl=2.00)    # giveback zone (1.8..2.6)

    await bot._manage_stops(None)

    assert bot._closed and bot._closed[0].startswith("giveback_peak_")


# ── (4) adaptive устойчив к одному выбросу ──────────────────────────────

def test_winsorize_clips_a_catastrophic_outlier():
    vals = [5, 2, 1, 3, 0.5, 2.5, -3, -2.5, -2, -1.5, -1, -557.7]
    out = _winsorize(vals)

    assert min(out) > -557.7            # tail clipped to the fence
    assert min(out) >= -50


def test_rolling_stats_avg_is_robust_but_raw_is_kept():
    rows = [{"pnl": p} for p in
            (5, 2, 1, 3, 0.5, 2.5, -3, -2.5, -2, -1.5, -1, -557.7)]
    bot = _adapt_bot(rows)

    st = bot._rolling_stats()

    assert st["avg_pnl"] < -40                       # raw is outlier-driven
    assert st["avg_pnl_trim"] > st["avg_pnl"] + 20   # robust estimate
    assert st["median_pnl"] > -1


def test_adaptive_does_not_go_defensive_on_one_outlier_alone():
    # Outlier sits mid-window; newest trades are wins, so the *streak* rule
    # does not fire either — only the raw avg is outlier-driven.
    rows = [{"pnl": p} for p in
            (-557.7, -3, -2.5, -2, -1.5, -1, 5, 2, 1, 3, 0.5, 2.5)]
    bot = _adapt_bot(rows)

    adapt = bot._refresh_adaptive()

    # 50% WR and robust avg ≈ 0 → not defensive even though raw avg < -40
    assert adapt["stats"]["avg_pnl"] < -40
    assert adapt["preset"] != "conservative"
