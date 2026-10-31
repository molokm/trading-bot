"""P2 «Качество входа и индикаторов».

Фиксируем четыре правки:
  * P2.1 — индикаторы считаются по ЗАКРЫТЫМ барам (нет репейнта), при этом
    поле `close` остаётся последней торговой ценой, а `last_closed` — цена
    закрытого бара решения;
  * P2.2 — funding fail-closed: неизвестная ставка не пропускает вход;
  * P2.3 — time-stop срабатывает только когда тренд мёртв (живой тренд с
    нормальным ADX в свою сторону получает время);
  * P2.4 — `_diagnose_open` показывает те же гейты, что и реальный вход
    (funding_unknown и др.).
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.bots.ai_discretionary.strategy import AIConfig, AIPosition, AIStrategy


STEP_MS = 3_600_000
TS0 = 1_700_000_000_000


def _flat_bot():
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot.client_manager = None
    bot.db = None
    bot._positions = {}
    bot._latest_indicators = {}
    return bot


class _CandleClient:
    """OKX stub: newest-first candles (as the API returns them)."""

    def __init__(self, rows, last=137.0):
        self._rows = rows
        self._last = last

    async def get_candles(self, inst, bar="1H", limit=100):
        if bar == "4H":
            return {"data": []}
        return {"data": list(self._rows)}

    async def get_ticker(self, inst):
        return {"data": [{"last": str(self._last)}]}


def _rows_flat_closed(n=220, closed_close=100.0, forming_close=137.0):
    """n closed bars @ closed_close, then one wild FORMING bar.

    Data is newest-first, so the forming bar is element 0.
    """
    closed = [
        [str(TS0 + i * STEP_MS), "100", "101", "99", str(closed_close), "1000"]
        for i in range(n)
    ]
    forming_ts = TS0 + n * STEP_MS
    forming = [str(forming_ts), "100", "200", "99", str(forming_close), "1000"]
    return [forming] + list(reversed(closed)), TS0 + (n - 1) * STEP_MS


# ── P2.1: индикаторы по закрытому бару ──────────────────────────────────

@pytest.mark.asyncio
async def test_indicators_use_closed_bars_not_forming_bar():
    bot = _flat_bot()
    bot.config.symbols = ["ADA"]
    rows, last_closed_ts = _rows_flat_closed()

    out = await bot._fetch_indicators(_CandleClient(rows))
    ind = out["ADA"]

    # Decision bar = last CLOSED bar, never the wild forming one.
    assert ind["closed_bar_ts"] == last_closed_ts
    assert ind["last_closed"] == 100.0
    # `close` stays the latest traded price for every price consumer.
    assert ind["close"] == 137.0
    # Indicator itself must be built off closed data: EMA21 of a flat 100.0
    # series is 100.0 — if the 137.0 forming bar leaked in it would be >100.
    assert ind["ema21"] == 100.0
    assert ind["regime"] == "chop"
    assert ind["bar_closes"] == [100.0] * 5


@pytest.mark.asyncio
async def test_bar_closes_are_the_last_five_closed_bars():
    bot = _flat_bot()
    bot.config.symbols = ["ADA"]
    rows, _ = _rows_flat_closed()
    # Give each closed bar a distinct close so ordering is checkable.
    closed = [
        [str(TS0 + i * STEP_MS), "100", "101", "99", str(100.0 + i), "1000"]
        for i in range(80)
    ]
    forming = [str(TS0 + 80 * STEP_MS), "100", "200", "99", "999", "1000"]
    rows = [forming] + list(reversed(closed))

    out = await bot._fetch_indicators(_CandleClient(rows))
    ind = out["ADA"]

    assert ind["last_closed"] == 179.0            # 100 + 79
    assert ind["bar_closes"] == [175.0, 176.0, 177.0, 178.0, 179.0]
    assert ind["close"] == 999.0                  # live price, not 179


# ── P2.2: funding fail-closed ───────────────────────────────────────────

def _veto_bot(build_quant):
    bot = _flat_bot()
    bot._build_quant = build_quant
    bot._effective_min_confidence = lambda: 0.50
    bot._round_trip_cost_pct = lambda: 0.0014
    bot._atr_pct = lambda coin, entry=0.0: 0.02     # 2% ATR -> clears cost
    bot._momentum_against = lambda side, ind: None
    bot._concentration_veto = lambda coin, side, conf=0.0: None
    return bot


def _quant_with_funding(fr):
    return {
        "global_regime": "bear",
        "btc_regime": "bear",
        "btc_roc": 0.0,
        "btc_impulse": None,
        "min_align": 0.50,
        "coins": {
            "ADA": {
                "regime": "bear",
                "align_long": 0.20,
                "align_short": 0.80,
                "adx": 30.0,
                "block_open": False,
                "funding_rate": fr,
            }
        },
    }


def _open_decision():
    return {
        "action": "open",
        "symbol": "ADA",
        "side": "short",
        "confidence": 0.90,
        "stop_pct": 0.03,
        "take_pct": 0.09,
    }


def test_funding_unknown_blocks_open_when_fail_closed():
    bot = _veto_bot(lambda: _quant_with_funding(None))
    bot._latest_indicators = {"ADA": {"close": 0.2, "tf_4h": {}}}

    reason = bot._quant_veto_open(_open_decision())

    assert reason == "quant_veto:funding_unknown"


def test_funding_unknown_allowed_when_fail_open():
    bot = _veto_bot(lambda: _quant_with_funding(None))
    bot.config.funding_fail_closed = False
    bot._latest_indicators = {"ADA": {"close": 0.2, "tf_4h": {}}}

    assert bot._quant_veto_open(_open_decision()) is None


def test_funding_toxic_still_blocks():
    bot = _veto_bot(lambda: _quant_with_funding(-0.0010))
    bot._latest_indicators = {"ADA": {"close": 0.2, "tf_4h": {}}}

    reason = bot._quant_veto_open(_open_decision())

    assert reason is not None and reason.startswith("quant_veto:funding_short")


# ── P2.3: time-stop только при мёртвом тренде ───────────────────────────

def _market_bot(regime, adx, side="short"):
    bot = _flat_bot()
    bot._latest_indicators = {
        "ADA": {"regime": regime, "adx": adx, "align_long": 0.20, "align_short": 0.20}
    }

    async def _noop(*a, **k):
        return None

    async def _false(*a, **k):
        return False

    async def _price(client, coin, pos=None):
        return float(pos.entry_price)

    bot._price_for_exit = _price
    bot._unrealized_pct = lambda pos, px: 0.10       # < 0.20 progress threshold
    bot._atr_pct = lambda coin, entry=0.0: 0.0
    bot._thesis_break_reason = lambda pos, ind, upl, held: None
    bot._indicator_exit_reason = lambda pos, ind, held: None
    bot._scale_out_partial = _false
    bot._persist_peaks = _noop
    bot._notify_close_tg = _noop
    closes = []

    async def _close(client, coin, reason):
        closes.append(reason)

    bot._close = _close
    bot._closed = closes
    return bot


def _stale_pos(side="short", entry=0.2, hours=12.0):
    opened = datetime.now(timezone.utc) - timedelta(hours=hours)
    return AIPosition(
        coin="ADA",
        inst_id="ADA-USDT-SWAP",
        side=side,
        size=74.0,
        entry_price=entry,
        stop_price=entry * 1.02 if side == "short" else entry * 0.98,
        take_price=entry * 0.96 if side == "short" else entry * 1.04,
        leverage=1.0,
        opened_at=opened.isoformat(),
        signal_id=1,
    )


@pytest.mark.asyncio
async def test_time_stop_holds_a_live_trend():
    bot = _market_bot(regime="bear", adx=25.0)      # short + in-direction trend
    bot._positions["ADA"] = _stale_pos()

    await bot._manage_stops(None)

    assert bot._closed == []


@pytest.mark.asyncio
async def test_time_stop_closes_a_dead_trend():
    bot = _market_bot(regime="chop", adx=9.0)       # no trend at all
    bot._positions["ADA"] = _stale_pos()

    await bot._manage_stops(None)

    assert bot._closed == ["time_stop_stale"]


# ── P2.4: _diagnose_open отражает реальные гейты ────────────────────────

def test_diagnose_open_reports_funding_unknown():
    bot = _veto_bot(lambda: _quant_with_funding(None))
    bot._effective_min_align = lambda: 0.50
    bot._latest_indicators = {
        "ADA": {
            "close": 0.19,
            "ema21": 0.19,
            "ema50": 0.20,
            "ema200": 0.0,
            "rsi": 40.0,
            "macd_hist": -0.01,
            "roc_3": 0.50,
            "funding_rate": None,
            "tf_4h": {},
        }
    }

    diag = bot._diagnose_open()
    ada = diag["coins"]["ADA"]

    assert "funding_unknown" in ada["vetoes"]
    assert ada["would_open"] is False
    assert diag["config_snapshot"]["funding_fail_closed"] is True
