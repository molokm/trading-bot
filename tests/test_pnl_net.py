"""One definition of PnL: close rows are NET of fees, everywhere."""
from datetime import datetime, timezone

import pytest
from app.services.pnl_engine import PNL_TZ

from app.services.pnl_engine import aggregate_stats, rows_from_bills


def _bill(ord_id="1", pnl=3.92, fee=-3.05, clord="ai_demo_eth"):
    return {
        "ordId": ord_id,
        "instId": "ETH-USDT-SWAP",
        "clOrdId": clord,
        "pnl": str(pnl),
        "fee": str(fee),
        "ts": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        "subType": "5",
    }


def _today_msk():
    return datetime.now(timezone.utc).astimezone(PNL_TZ).strftime("%Y-%m-%d")


def test_bill_row_is_net_of_fee():
    rows = rows_from_bills([_bill()])
    assert len(rows) == 1
    assert rows[0]["pnl"] == pytest.approx(0.87)
    assert rows[0]["gross_pnl"] == 3.92
    assert rows[0]["fee"] == 3.05


def test_bill_row_accounts_for_positive_fee_sign():
    rows = rows_from_bills([_bill(fee="3.05")])
    assert rows[0]["pnl"] == pytest.approx(0.87)


def test_aggregate_matches_balance_change():
    rows = rows_from_bills([_bill()])
    agg = aggregate_stats(rows, ai_only=False)
    assert agg["realized_pnl"] == pytest.approx(0.87)


def test_risk_status_derives_daily_pnl_from_engine(monkeypatch):
    from app.services import risk_guard as rg
    from app.services.ai_strategy import AIStrategy
    from app.bots.ai_discretionary.strategy import AIConfig

    bot = AIStrategy(config=AIConfig())
    bot._daily_realized_day = _today_msk()
    bot._daily_realized = -12.5
    monkeypatch.setattr(rg, "_daily_pnl", None)
    monkeypatch.setattr(rg, "_runtime_kill", None)
    st = rg.get_status(ai_bot=bot)
    assert st.daily_pnl_usd == -12.5


def test_status_equity_prefers_exchange_balance():
    from app.services.ai_strategy import AIStrategy
    from app.bots.ai_discretionary.strategy import AIConfig

    bot = AIStrategy(config=AIConfig(capital=10000.0))
    assert bot.get_status()["equity_source"] == "nominal"
    bot._account_equity = 651.4
    st = bot.get_status()
    assert st["equity"] == 651.4
    assert st["equity_source"] == "exchange"
