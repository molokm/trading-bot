"""v1.39 «Реальные филлы»: не бронировать фантомные сделки.

Аудит 28.10.2026 (ADA): позиция исчезала с биржи через 8–18 с после открытия,
а `_record_flat_close` подставлял цену входа как цену выхода и оценивал комиссию
— в журнале появлялись «сделки» `entry == exit == 0.2, pnl = -2.07` (== ровно
round-trip fee), по 2 дубля на одно событие, и они попадали в WR/рефлексию.

Здесь зафиксировано:
  * закрытие бронируется только по реальному филлу с биржи;
  * без филла — `flat_unconfirmed`, без записи в сделки;
  * дубли закрытий невозможны (идемпотентность до первого await);
  * `_open` не создаёт позицию, если биржа не подтвердила филл, и берёт avgPx;
  * анти-чарн кулдаун переживает редеплой;
  * read-only LIVE-ключ не может «успешно подключиться»;
  * строка входа (`state="filled"`) никогда не считается сделкой.
"""
import inspect
from datetime import datetime

import pytest

import app.bots.ai_discretionary.strategy as strat
from app.bots.ai_discretionary.strategy import AIConfig, AIPosition, AIStrategy


OPEN_AT = "2026-10-28T19:33:44+00:00"
OPEN_MS = int(datetime.fromisoformat(OPEN_AT).timestamp() * 1000)


def _pos(side="short", entry=0.2, size=74.0, coin="ADA"):
    return AIPosition(
        coin=coin, inst_id=f"{coin}-USDT-SWAP", side=side, size=size,
        entry_price=entry,
        stop_price=entry * 1.02 if side == "short" else entry * 0.98,
        take_price=entry * 0.96 if side == "short" else entry * 1.04,
        leverage=1.0, opened_at=OPEN_AT, signal_id=81,
    )


def _flat_bot(db=None):
    bot = object.__new__(AIStrategy)
    bot.config = AIConfig()
    bot.db = db
    bot.client_manager = None
    bot._positions = {}
    bot._trade_log = []
    bot._closed_keys = set()
    bot._last_closes = {}
    bot._equity = 1000.0
    bot._daily_realized = 0.0
    bot._weekly_realized = 0.0
    bot._session_pnl = 0.0
    bot._lifetime_pnl = 0.0
    bot._lifetime_trades = 0
    bot._lifetime_wins = 0
    bot._lifetime_fees = 0.0
    bot._engine_stats_ts = 1.0
    bot._executed = []
    bot._exec_log = []
    bot._decision_log = []
    bot._record_exec = lambda event, **kw: bot._executed.append((event, kw))
    bot._persist = lambda: None
    bot._refresh_adaptive = lambda force_log=False: {}
    bot._settle_close_memory = lambda *a, **k: {}

    async def _noop(*a, **k):
        return None

    bot._notify_close_tg = _noop
    return bot


class _Client:
    """Minimal OKX stub: fills / positions / order / ticker / algo."""

    def __init__(self, fills=None, positions=None, order=None, ticker=None):
        self._fills = fills or []
        self._positions = positions or []
        self._order = order or []
        self._ticker = ticker or []
        self.calls = []

    async def get_fills(self, inst_id=None, limit=20, **kw):
        self.calls.append(("get_fills", inst_id))
        return {"data": list(self._fills)}

    async def get_fills_history(self, inst_type="SWAP", limit=100, **kw):
        return {"data": list(self._fills)}

    async def get_positions(self, inst_type="SWAP", inst_id=None):
        return {"data": list(self._positions)}

    async def get_order(self, inst, ord_id=None, cl_ord_id=None):
        return {"data": list(self._order)}

    async def get_ticker(self, inst_id):
        return {"data": list(self._ticker)}

    async def place_algo_order(self, **kw):
        self.calls.append(("algo", kw))
        return {"data": [{"algoId": "algo-1"}]}

    async def cancel_order(self, inst, oid):
        return {"data": [{}]}

    async def set_leverage(self, *a, **k):
        return {"data": [{}]}


def _close_fill(sz="74", px="0.2040", pnl="-29.6", fee="-1.4"):
    return {"ts": str(OPEN_MS + 18_000), "side": "buy", "posSide": "short",
            "fillSz": sz, "fillPx": px, "fillPnl": pnl, "fee": fee,
            "clOrdId": ""}


# ── закрытие без филла не бронируется ────────────────────────────────────

@pytest.mark.asyncio
async def test_close_without_exchange_fill_is_not_booked():
    bot = _flat_bot()
    pos = _pos()
    bot._positions["ADA"] = pos

    await bot._record_flat_close(_Client(), pos, "ADA", 0.0, "exchange_close")

    assert bot._trade_log == []
    assert bot._lifetime_trades == 0
    assert bot._lifetime_pnl == 0.0
    assert bot._positions == {}
    assert [e[0] for e in bot._executed] == ["flat_unconfirmed"]
    # A phantom close must not arm the "wait 3h after a loss" cooldown as a loss.
    assert bot._last_closes == {}


@pytest.mark.asyncio
async def test_close_books_the_real_exchange_fill():
    bot = _flat_bot()
    pos = _pos()
    bot._positions["ADA"] = pos

    await bot._record_flat_close(
        _Client(fills=[_close_fill()]), pos, "ADA", 0.0, "exchange_close")

    assert len(bot._trade_log) == 1
    row = bot._trade_log[0]
    assert row["exit_price"] == pytest.approx(0.2040)
    assert row["pnl"] == pytest.approx(-31.0)   # -29.6 gross + -1.4 fee
    assert row["fee"] == pytest.approx(1.4)
    assert row["confirmed"] is True
    assert bot._lifetime_trades == 1
    assert bot._lifetime_pnl == pytest.approx(-31.0)
    assert bot._lifetime_wins == 0
    assert bot._last_closes["ADA"]["pnl"] == pytest.approx(-31.0)


@pytest.mark.asyncio
async def test_close_is_idempotent_for_the_same_position():
    bot = _flat_bot()
    pos = _pos()
    bot._positions["ADA"] = pos
    client = _Client(fills=[_close_fill()])

    await bot._record_flat_close(client, pos, "ADA", 0.0, "exchange_close")
    await bot._record_flat_close(client, pos, "ADA", 0.0, "exchange_close")

    assert len(bot._trade_log) == 1
    assert bot._lifetime_trades == 1


@pytest.mark.asyncio
async def test_recover_close_fill_ignores_the_opening_fill():
    bot = _flat_bot()
    pos = _pos()
    rows = [
        {"ts": str(OPEN_MS - 1_000), "side": "sell", "posSide": "short",
         "fillSz": "74", "fillPx": "0.2", "fillPnl": "0", "fee": "-0.74"},
        _close_fill(),
    ]
    info = await bot._recover_close_fill(_Client(fills=rows), pos)

    assert info["confirmed"] is True
    assert info["px"] == pytest.approx(0.2040)
    assert info["has_pnl"] is True


@pytest.mark.asyncio
async def test_recover_close_fill_is_unconfirmed_without_rows():
    bot = _flat_bot()
    info = await bot._recover_close_fill(_Client(), _pos())
    assert info["confirmed"] is False


# ── подтверждение филла при открытии ─────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_fill_uses_the_exchange_position():
    bot = _flat_bot()
    client = _Client(positions=[
        {"pos": "-74", "avgPx": "0.2043", "posSide": "short"}])
    px, sz = await bot._confirm_fill(client, "ADA-USDT-SWAP", "short", 74.0,
                                     ord_id="42", cl_ord_id="aidx")
    assert px == pytest.approx(0.2043)
    assert sz == pytest.approx(74.0)


@pytest.mark.asyncio
async def test_confirm_fill_ignores_the_opposite_side():
    bot = _flat_bot()
    client = _Client(positions=[
        {"pos": "10", "avgPx": "0.9", "posSide": "long"}],
        order=[{"state": "canceled"}])
    px, sz = await bot._confirm_fill(client, "ADA-USDT-SWAP", "short", 74.0,
                                     ord_id="42")
    assert (px, sz) == (0.0, 0.0)


def _open_bot(client, monkeypatch, ack=None, positions=None):
    bot = _flat_bot()
    bot._latest_indicators = {"ADA": {"close": 0.2, "atr": 0.004}}
    bot.config.execute = True
    bot.notifier = None
    bot._avail = 0.0

    async def _other(*a, **k):
        return False

    async def _avail(*a, **k):
        return 0.0

    async def _mirror(*a, **k):
        return False

    async def _claim(*a, **k):
        return True

    async def _place(client_, inst, side, sz, pos_side, is_close=False, cl_ord_id=None):
        return {"error": False, "data": list(ack or [{"ordId": "42", "sCode": "0"}])}

    bot._other_ai_owns = _other
    bot._avail_usdt = _avail
    bot._mirror_enabled = _mirror
    bot._size_factor = lambda pct: 1.0
    bot._size_order = lambda *a, **k: (74.0, 1.0)
    bot._place = _place
    monkeypatch.setattr(strat, "claim_or_flatten", _claim)
    monkeypatch.setattr(strat, "assert_can_open", lambda **kw: None)
    monkeypatch.setattr(strat.hg, "preflight", lambda **kw: [])
    return bot


@pytest.mark.asyncio
async def test_open_does_not_register_a_position_without_an_exchange_fill(monkeypatch):
    client = _Client(order=[{"state": "canceled"}])
    bot = _open_bot(client, monkeypatch)

    await bot._open(client, "ADA", "short", 0.02, 0.04, "test")

    assert bot._positions == {}
    assert bot._trade_log == []
    assert [e[0] for e in bot._executed] == ["open_no_fill"]


@pytest.mark.asyncio
async def test_open_takes_the_exchange_avg_px_not_the_candle_close(monkeypatch):
    client = _Client(positions=[
        {"pos": "-74", "avgPx": "0.2050", "posSide": "short"}],
        ticker=[{"last": "0.2050"}])
    bot = _open_bot(client, monkeypatch)

    await bot._open(client, "ADA", "short", 0.02, 0.04, "test")

    pos = bot._positions["ADA"]
    assert pos.entry_price == pytest.approx(0.2050)
    assert pos.stop_price == pytest.approx(0.2050 * 1.02)
    assert bot._trade_log[0]["entry_price"] == pytest.approx(0.2050)


def test_open_has_no_entry_price_fallback():
    src = inspect.getsource(AIStrategy._open)
    assert "_confirm_fill" in src
    assert "fill_px = entry" not in src


# ── биржевой SL/TP не ставится уже пробитым ──────────────────────────────

@pytest.mark.asyncio
async def test_sl_tp_rebases_a_stop_the_market_already_crossed():
    bot = _flat_bot()
    pos = _pos("short", entry=0.2)
    pos.stop_price = 0.20404
    client = _Client(ticker=[{"last": "0.21"}])   # already above the short stop

    await bot._place_exchange_sl_tp(client, pos)

    call = [c for c in client.calls if c[0] == "algo"][0][1]
    assert float(call["sl_trigger_px"]) > 0.21
    assert pos.stop_price > 0.21


@pytest.mark.asyncio
async def test_sl_tp_does_not_place_a_crossed_take_profit():
    bot = _flat_bot()
    pos = _pos("short", entry=0.2)
    client = _Client(ticker=[{"last": "0.19"}])   # past the short TP (0.192)

    await bot._place_exchange_sl_tp(client, pos)

    call = [c for c in client.calls if c[0] == "algo"][0][1]
    assert call.get("tp_trigger_px") is None
    assert call["sl_trigger_px"] == "0.204"


# ── анти-чарн кулдаун переживает редеплой ───────────────────────────────

class _DB:
    def __init__(self):
        self.store = {}

    async def set_setting(self, key, value):
        self.store[key] = value

    async def get_setting(self, key):
        return self.store.get(key)


@pytest.mark.asyncio
async def test_last_closes_persist_and_survive_redeploy():
    db = _DB()
    bot = _flat_bot(db)
    await bot._remember_close("ADA", "short", -31.0, "exchange_close")
    assert "last_closes:ai_strategy" in db.store

    fresh = _flat_bot(db)
    await fresh._restore_last_closes()
    assert fresh._last_closes["ADA"]["pnl"] == pytest.approx(-31.0)
    assert fresh._last_closes["ADA"]["side"] == "short"


@pytest.mark.asyncio
async def test_stale_last_closes_are_dropped_on_restore():
    db = _DB()
    await db.set_setting("last_closes:ai_strategy", '{"ADA": {"side": "short", "ts": 1, "pnl": -1}}')
    bot = _flat_bot(db)
    await bot._restore_last_closes()
    assert bot._last_closes == {}


def test_hydrate_restores_the_cooldown():
    src = inspect.getsource(AIStrategy._hydrate_from_db)
    assert "_restore_last_closes" in src


# ── read-only LIVE-ключ ──────────────────────────────────────────────────

def test_live_trade_blocked_flag_is_sticky():
    bot = object.__new__(AIStrategy)
    assert bot._live_trade_blocked() is False
    bot._live_trade_blocked_reason = (
        "order blocked: This API Key does not have trading permission for the Crypto")
    assert bot._live_trade_blocked() is True


def test_open_live_marks_permission_errors_as_blocked():
    src = inspect.getsource(AIStrategy._open_live)
    assert "_live_trade_blocked_reason" in src
    assert "permission" in src


def test_pending_mirror_fast_tick_is_skipped_when_key_cannot_trade():
    src = inspect.getsource(AIStrategy._run)
    assert "_live_trade_blocked()" in src


def test_live_connect_refuses_a_read_only_key():
    import app.main as m

    src = inspect.getsource(m.live_connect)
    assert "has_trade" in src
    assert "без права Trade" in src


def test_live_status_exposes_trade_permission():
    import app.main as m

    src = inspect.getsource(m.live_status)
    assert "key_trade" in src
    assert "trade_blocked" in inspect.getsource(AIStrategy.get_status)

# ── строка входа (state="filled") — не сделка ────────────────────────────
#
# Прод 28.10: после деплоя v1.39 в карточке появилась «сделка» NEAR
# pnl=-19.32 (== комиссия входа), exit == entry, при всё ещё открытой позиции,
# а карточка (66.74) разошлась с внутренним счётчиком (86.06). Причина: движок
# брал строки `trades` без фильтра по state, и как только реальная комиссия
# ордера стала приходит от OKX, строка входа стала «eligible» (|pnl| >= 1e-12).

from app.services import pnl_engine as pe


def _open_row(pnl=-19.3165, state="filled", oid="o1"):
    return {
        "ord_id": oid, "inst_id": "NEAR-USDT-SWAP", "bot_id": "ai_strategy",
        "bot_label": "AI Discretionary 1H", "state": state, "pnl": pnl,
        "fee": abs(pnl), "timestamp": pe.epoch_ms() + 86_400_000,
        "account_mode": "demo", "side": "buy",
    }


def _close_row(pnl=12.0, oid="c1"):
    return {
        "ord_id": oid, "inst_id": "BTC-USDT-SWAP", "bot_id": "ai_strategy",
        "bot_label": "AI Discretionary 1H", "state": "closed", "pnl": pnl,
        "fee": 0.5, "timestamp": pe.epoch_ms() + 86_400_000,
        "account_mode": "demo", "side": "sell",
    }


def test_open_state_rows_are_not_eligible():
    ok, reason = pe.pnl_eligible(_open_row(), mode="demo")
    assert ok is False and reason == "not_closed"


def test_filled_row_with_big_pnl_is_still_an_open():
    # Раньше «filled с |pnl| >= $1» считался закрытием — именно эта эвристика
    # и превращала вход в сделку.
    assert pe.normalize_close_row(_open_row(pnl=-19.3165)) is None
    assert pe.normalize_close_row(_close_row()) is not None


@pytest.mark.asyncio
async def test_rows_from_db_trades_skip_entry_rows():
    class _DB:
        async def get_trades_multi_bot(self, bot_ids, limit=5000, account_mode=None):
            return [_open_row(), _close_row()]

    rows = await pe._rows_from_db_trades(_DB(), ai_only=True, account_mode="demo")
    assert [r["ord_id"] for r in rows] == ["c1"]


@pytest.mark.asyncio
async def test_load_eligible_rows_ignores_open_fee_rows():
    class _DB:
        async def set_setting(self, k, v):
            return None

        async def get_exchange_close_trades_detail(self, **kw):
            return []

        async def get_trades_multi_bot(self, bot_ids, limit=5000, account_mode=None):
            return [_open_row(), _close_row()]

    rows, diag = await pe.load_eligible_rows(_DB(), mode="demo")
    assert [r.get("ord_id") for r in rows] == ["c1"]
    agg = pe.aggregate_rows(rows, ai_only=True)
    assert agg["total"] == pytest.approx(12.0)
    assert agg["trades_counted"] == 1
