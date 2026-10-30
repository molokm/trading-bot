"""«Сделки» panel rows must show a real entry, direction and close reason.

Regression guard for the dashboard panel that rendered "—" in the Вход column
and SHORT for every close: `exchange_close_trades` stores only the exit price
and net PnL, so the endpoint has to enrich rows from the bot's own `trades`
(open row px = entry, close row side = closing side) and from its in-memory
close log (human reason).
"""
import pytest
from fastapi.testclient import TestClient

import app.main as m
from app.services import pnl_engine as pe

NOW_MS = pe.epoch_ms() + 3 * 86400_000


def _exchange_row():
    return {
        "ord_id": "X1",
        "inst_id": "BTC-USDT-SWAP",
        "cl_ord_id": "aid7c",
        "bot_label": "AI Discretionary 1H",
        "pnl": 5.0,
        "fee": 0.1,
        "sz": 1.0,
        "avg_px": 90.0,
        "close_ts": NOW_MS,
        "sub_type": "5",
        "account_mode": "demo",
    }


def _trade_rows():
    return [
        {   # entry fill: px is the entry price, sell = short
            "bot_id": "ai_strategy", "signal_id": 7, "ord_id": "",
            "inst_id": "BTC-USDT-SWAP", "side": "sell", "sz": "1",
            "px": "100.0", "state": "filled", "account_mode": "demo",
            "timestamp": "2026-10-30T01:00:00+00:00",
        },
        {   # close fill: buy closes a short
            "bot_id": "ai_strategy", "signal_id": 7, "ord_id": "X1",
            "inst_id": "BTC-USDT-SWAP", "side": "buy", "sz": "1",
            "px": "90.0", "state": "closed", "account_mode": "demo",
            "timestamp": "2026-10-30T02:00:00+00:00",
        },
    ]


class _DB:
    async def get_exchange_close_trades_detail(self, **kw):
        return [_exchange_row()]

    async def get_trades_multi_bot(self, *a, **k):
        return _trade_rows()

    async def get_trades(self, **kw):
        return []

    async def get_setting(self, key):
        return None


class _Bot:
    _trade_log = [{
        "account_mode": "demo",
        "time": "2026-10-30T02:00:00+00:00",
        "side": "buy", "pos_side": "short", "coin": "BTC",
        "inst_id": "BTC-USDT-SWAP", "symbol": "BTC-USDT-SWAP",
        "entry_price": 100.0, "exit_price": 90.0,
        "pnl": 5.0, "reason": "stop", "signal_id": 7, "ord_id": "X1",
    }]

    def get_status(self):
        return {"running": True, "open_positions": [], "capital": 10000}


@pytest.fixture()
def client(monkeypatch):
    async def _ctx(request):
        return "guest", None, None

    async def _pnl(mode="demo"):
        base = {
            "total": 5.0, "1d": 5.0, "7d": 5.0, "30d": 5.0, "week": 5.0,
            "unrealized": 0.0, "fees": 0.1, "trades_counted": 1,
            "per_bot": {"AI Discretionary 1H": 5.0}, "source": "test",
        }
        if mode == "demo":
            # Mirror _compute_pnl: engine rows carry the raw close-order side
            # ('buy' closes a short) and no reason/entry for exchange rows.
            base["trades"] = [{
                "time": NOW_MS, "inst": "BTC-USDT-SWAP", "side": "buy",
                "pnl": 5.0, "account_mode": "demo",
                "bot": "AI Discretionary 1H", "ord_id": "X1",
                "reason": "closed", "entry_px": None, "exit_px": 90.0,
            }]
        return base

    monkeypatch.setattr(m, "_me_ctx", _ctx)
    monkeypatch.setattr(m, "_get_pnl_cached", _pnl)
    monkeypatch.setattr(m, "db", _DB())
    monkeypatch.setattr(m, "ai_bot", _Bot())
    monkeypatch.setattr(m, "client_manager", None)
    m._ME_DASH_CACHE.clear()
    return TestClient(m.app, raise_server_exceptions=False)


def test_dashboard_trade_row_has_entry_side_and_reason(client):
    resp = client.get("/api/me/dashboard")
    assert resp.status_code == 200
    trades = resp.json().get("trades") or []
    assert len(trades) == 1, trades
    row = trades[0]
    assert row["entry_px"] == 100.0
    assert row["entry_price"] == 100.0
    assert row["exit_px"] == 90.0
    assert row["side"] == "short"          # direction, not the close order side
    assert row["reason"] == "stop"         # not the generic "closed"


def test_entry_survives_restart_without_in_memory_log(client):
    """After a restart the log is empty — entry/direction must come from DB."""
    m.ai_bot._trade_log = []
    m._ME_DASH_CACHE.clear()

    resp = client.get("/api/me/dashboard")
    assert resp.status_code == 200
    trades = resp.json().get("trades") or []
    assert len(trades) == 1, trades
    row = trades[0]
    assert row["entry_px"] == 100.0
    assert row["side"] == "short"
