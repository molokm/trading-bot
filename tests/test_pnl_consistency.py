"""One PnL number everywhere: web dashboard, bot cards, AI status, TG, mini app.

All surfaces must resolve PnL through pnl_engine.load_eligible_rows (set T).
"""
import inspect
from types import SimpleNamespace

import pytest

from app.services import pnl_engine as pe

EP_MS = pe.epoch_ms()
NOW_MS = EP_MS + 3 * 86400_000 + 3_600_000


def close(ord_id, pnl, ts, mode="demo", cl="ai123", bot_id="ai_strategy"):
    return {
        "ord_id": ord_id,
        "inst_id": "BTC-USDT-SWAP",
        "cl_ord_id": cl,
        "bot_id": bot_id,
        "bot_label": "AI Discretionary 1H" if bot_id.startswith("ai") else "",
        "pnl": pnl,
        "fee": 0.0,
        "close_ts": ts,
        "account_mode": mode,
    }


class FakeDB:
    def __init__(self, exchange_rows, trade_rows=None, mode="demo"):
        self.exchange_rows = list(exchange_rows)
        self.trade_rows = list(trade_rows or [])
        self.mode = mode
        self.settings = {}

    async def set_setting(self, k, v):
        self.settings[k] = v

    async def get_exchange_close_trades_detail(self, bot_label=None, epoch_ms=0,
                                               limit=500, account_mode=None):
        return [r for r in self.exchange_rows
                if account_mode in (None, r.get("account_mode"))]

    async def get_trades_multi_bot(self, bot_ids, limit=5000, account_mode=None):
        return [r for r in self.trade_rows
                if account_mode in (None, r.get("account_mode"))]


@pytest.mark.asyncio
async def test_stats_total_equals_dashboard_total():
    rows = [
        close("1", 10.0, NOW_MS),
        close("2", -4.0, NOW_MS - 86_400_000),
        close("3", 1.0, NOW_MS - 2 * 86_400_000),
    ]
    db = FakeDB(rows)
    stats = await pe.compute_stats(db, account_mode="demo", period="all")
    elig, _ = await pe.load_eligible_rows(db, mode="demo")
    agg = pe.aggregate_rows(elig, ai_only=True)
    assert stats["realized_pnl"] == agg["total"] == 7.0
    assert stats["trades"] == agg["trades_counted"] == 3


@pytest.mark.asyncio
async def test_untagged_mode_rows_are_excluded_everywhere():
    rows = [
        close("1", 10.0, NOW_MS, mode="demo"),
        close("2", 99.0, NOW_MS, mode=""),      # legacy untagged
        close("3", 50.0, NOW_MS, mode="live"),  # other account
    ]
    db = FakeDB(rows)
    stats = await pe.compute_stats(db, account_mode="demo", period="all")
    assert stats["realized_pnl"] == 10.0
    assert stats["trades"] == 1


@pytest.mark.asyncio
async def test_zero_ts_rows_are_excluded_everywhere():
    rows = [
        close("1", 10.0, NOW_MS),
        close("2", 99.0, 0),
    ]
    db = FakeDB(rows)
    stats = await pe.compute_stats(db, account_mode="demo", period="all")
    elig, _ = await pe.load_eligible_rows(db, mode="demo")
    assert stats["realized_pnl"] == pe.aggregate_rows(elig)["total"] == 10.0


@pytest.mark.asyncio
async def test_live_bills_match_between_stats_and_pnl_engine():
    bills = [
        {"ordId": "b1", "instId": "ETH-USDT-SWAP", "clOrdId": "ail-1",
         "pnl": "-5.5", "fee": "-0.3", "ts": str(NOW_MS), "subType": "5"},
        {"ordId": "b2", "instId": "ETH-USDT-SWAP", "clOrdId": "ail-2",
         "pnl": "8.0", "fee": "-0.2", "ts": str(NOW_MS), "subType": "6"},
        {"ordId": "b3", "instId": "ETH-USDT-SWAP", "clOrdId": "zzz",
         "pnl": "77.0", "fee": "0", "ts": str(NOW_MS), "subType": "5"},  # not ours
        {"ordId": "b4", "instId": "ETH-USDT-SWAP", "clOrdId": "ail-4",
         "pnl": "1.0", "fee": "0", "ts": str(EP_MS - 1), "subType": "5"},  # pre-epoch
    ]
    db = FakeDB([], mode="live")
    stats = await pe.compute_stats(db, account_mode="live", period="all", bills=bills)
    elig, _ = await pe.load_eligible_rows(db, mode="live", bills=bills)
    assert stats["realized_pnl"] == pe.aggregate_rows(elig)["total"] == 2.5
    assert stats["trades"] == 2


def test_rows_from_bills_dedupes_by_ord_id():
    bills = [
        {"ordId": "b1", "clOrdId": "ail-1", "pnl": "1", "ts": str(NOW_MS), "subType": "5"},
        {"ordId": "b1", "clOrdId": "ail-1", "pnl": "1", "ts": str(NOW_MS), "subType": "5"},
    ]
    assert len(pe.rows_from_bills(bills, epoch=EP_MS)) == 1


def test_all_stats_surfaces_use_the_shared_loader():
    from app.services import telegram_bot
    import app.main as m

    tg_src = inspect.getsource(telegram_bot.TelegramBotPoller._cmd_stats)
    assert "pnl_engine.compute_stats" in tg_src

    stats_src = inspect.getsource(m.get_bot_stats)
    assert "compute_stats" in stats_src
    assert "bills=" in stats_src

    pnl_src = inspect.getsource(m._compute_pnl)
    assert "load_eligible_rows" in pnl_src


@pytest.mark.asyncio
async def test_compute_pnl_endpoint_payload_matches_stats(monkeypatch):
    """Guards the /api/pnl payload itself: no swallowed error, same total as stats."""
    import app.main as m
    from types import SimpleNamespace

    rows = [
        close("1", 10.0, NOW_MS),
        close("2", -4.0, NOW_MS - 86_400_000),
    ]
    db = FakeDB(rows)

    async def _sync(mode=None):
        return None

    monkeypatch.setattr(m, "db", db, raising=False)
    monkeypatch.setattr(m, "sync_exchange_close_trades", _sync, raising=False)
    monkeypatch.setattr(m, "client_manager", SimpleNamespace(get_client=lambda: None), raising=False)
    monkeypatch.setattr(m, "live_manager", None, raising=False)

    payload = await m._compute_pnl("demo")
    assert "error" not in payload, payload.get("error")
    assert payload["total"] == 6.0
    assert payload["trades_counted"] == 2
    assert payload["wins"] == 1 and payload["losses"] == 1
    assert payload["win_rate"] == 50.0

    stats = await pe.compute_stats(db, account_mode="demo", period="all")
    assert payload["total"] == stats["realized_pnl"]


def test_stats_command_parses_mode_and_period():
    from app.services.telegram_bot import _parse_stats_args

    assert _parse_stats_args("all") == ("demo", "all")
    assert _parse_stats_args("live") == ("live", "all")
    assert _parse_stats_args("live week") == ("live", "week")
    assert _parse_stats_args("week live") == ("live", "week")
    assert _parse_stats_args("demo сегодня") == ("demo", "today")
    assert _parse_stats_args("") == ("demo", "all")


def test_ts_ms_any_parses_supported_formats():
    from app.main import _ts_ms_any

    assert _ts_ms_any(1788220801000) == 1788220801000
    assert _ts_ms_any(1788220801) == 1788220801000
    assert _ts_ms_any("1788220801000") == 1788220801000
    assert _ts_ms_any("2026-09-01T00:00:01+00:00") == 1788220801000
    assert _ts_ms_any("2026-09-01T00:00:01Z") == 1788220801000
    assert _ts_ms_any("2026-09-01 00:00:01") == 1788220801000
    assert _ts_ms_any(None) == 0
    assert _ts_ms_any("garbage") == 0


def test_filter_since_epoch_drops_pre_product_rows():
    from app.main import _filter_since_epoch, _epoch_ms_cached

    epoch = _epoch_ms_cached()
    rows = [
        {"time": epoch - 1, "pnl": 1.0},
        {"time": epoch, "pnl": 2.0},
        {"time": "2026-08-31T23:59:59+00:00", "pnl": 3.0},
        {"time": "2026-09-01T00:00:01+00:00", "pnl": 4.0},
        {"pnl": 5.0},  # unknown ts → dropped by default
    ]
    kept = _filter_since_epoch(rows)
    assert [r["pnl"] for r in kept] == [2.0, 4.0]
    kept_unknown = _filter_since_epoch(rows, keep_unknown=True)
    assert [r["pnl"] for r in kept_unknown] == [2.0, 4.0, 5.0]


def test_clean_dsn_is_asyncpg_safe():
    from app.database import _clean_dsn

    src = ("postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:6543/postgres"
           "?channel_binding=require&pgbouncer=true")
    out = _clean_dsn(src)
    assert "channel_binding" not in out
    assert "pgbouncer" not in out
    assert "sslmode=require" in out

    plain = "postgresql://u:p@db.host:5432/postgres"
    assert _clean_dsn(plain).endswith("?sslmode=require")
    assert _clean_dsn("file:./data/trading.db") == "file:./data/trading.db"
    assert _clean_dsn("") == ""
