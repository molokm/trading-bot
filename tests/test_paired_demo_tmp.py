import asyncio
import pytest


@pytest.mark.asyncio
async def test_demo_closed_rows_surface(tmp_path, monkeypatch):
    import app.main as m
    import app.database as dbm

    db = dbm.Database(str(tmp_path / 'x.db'))
    await db.init()
    await db.ensure_bot('ai_strategy', 'AI', 'd')
    await db.save_trade(bot_id='ai_strategy', side='buy', sz=1, px=100, ord_id='o1',
                        inst_id='BTC-USDT-SWAP', ord_type='market', fee=0.1,
                        fee_ccy='USDT', pnl=0, state='filled', account_mode='demo',
                        account_key='showcase')
    await db.save_trade(bot_id='ai_strategy', side='sell', sz=1, px=105, ord_id='o2',
                        inst_id='BTC-USDT-SWAP', ord_type='market', fee=0.1,
                        fee_ccy='USDT', pnl=4.8, state='closed', account_mode='demo',
                        account_key='showcase')
    monkeypatch.setattr(m, 'db', db)
    monkeypatch.setattr(m, 'ai_bot', None)
    monkeypatch.setattr(m, 'rotation', None)
    monkeypatch.setattr(m, 'impulse', None)
    monkeypatch.setattr(m, 'validation', None)

    async def no_bills(*a, **k): return []
    async def no_fills(*a, **k): return []
    monkeypatch.setattr(m, '_fetch_all_trade_bills', no_bills)
    monkeypatch.setattr(m, '_fetch_okx_fills', no_fills)

    out = await m._get_paired_trades_impl(limit=50, mode='demo')
    print(out)
    assert any(abs(float(t.get('pnl') or 0) - 4.8) < 1e-6 for t in out['trades'])
