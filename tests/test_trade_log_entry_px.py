"""Entry price for closes restored from the `trades` table.

`trades` keeps one price per row: the entry px on an open fill, the CLOSE px on
a close fill. The hydrate path used to copy that single px into BOTH
entry_price and exit_price, so after every redeploy the «Сделки» panel showed
"entry == exit" for DEMO closes and fed a bogus entry price into the adaptive
layer.
"""
from app.bots.ai_discretionary.strategy import AIStrategy


def _bot():
    b = object.__new__(AIStrategy)
    b._trade_log = []
    return b


def _row(mode, inst, state, side, px, ts, oid):
    return {
        "account_mode": mode, "inst_id": inst, "state": state,
        "side": side, "sz": "2", "px": str(px),
        "timestamp": ts, "ord_id": oid,
    }


def test_close_takes_entry_from_the_preceding_open():
    bot = _bot()
    rows = [
        _row("demo", "BTC-USDT-SWAP", "filled", "sell", 100.0,
             "2026-10-30T10:00:00+00:00", "o1"),
        _row("demo", "BTC-USDT-SWAP", "closed", "buy", 110.0,
             "2026-10-30T11:00:00+00:00", "o2"),
    ]
    log, _ = bot._rebuild_trade_log(rows, [])
    close = [t for t in log if t["reason"] != "open"]
    assert len(close) == 1
    assert close[0]["entry_price"] == 100.0
    assert close[0]["exit_price"] == 110.0


def test_pairing_is_independent_of_db_row_order():
    bot = _bot()
    rows = [
        _row("demo", "BTC-USDT-SWAP", "filled", "sell", 100.0,
             "2026-10-30T10:00:00+00:00", "o1"),
        _row("demo", "BTC-USDT-SWAP", "closed", "buy", 110.0,
             "2026-10-30T11:00:00+00:00", "o2"),
    ]
    newest_first = list(reversed(rows))
    log, _ = bot._rebuild_trade_log(newest_first, [])
    close = [t for t in log if t["reason"] != "open"]
    assert close[0]["entry_price"] == 100.0


def test_close_without_open_has_no_invented_entry():
    bot = _bot()
    rows = [
        _row("demo", "ETH-USDT-SWAP", "closed", "buy", 2241.0,
             "2026-10-30T18:00:00+00:00", "o9"),
    ]
    log, _ = bot._rebuild_trade_log(rows, [])
    assert log[0]["entry_price"] is None
    assert log[0]["exit_price"] == 2241.0


def test_next_trade_open_is_not_used_as_entry():
    """The open AFTER the close belongs to the next trade, not this one."""
    bot = _bot()
    rows = [
        _row("demo", "SOL-USDT-SWAP", "filled", "sell", 90.0,
             "2026-10-30T08:00:00+00:00", "a1"),
        _row("demo", "SOL-USDT-SWAP", "closed", "buy", 96.0,
             "2026-10-30T12:00:00+00:00", "a2"),
        _row("demo", "SOL-USDT-SWAP", "filled", "sell", 96.1,
             "2026-10-30T12:01:00+00:00", "a3"),
    ]
    log, _ = bot._rebuild_trade_log(rows, [])
    close = [t for t in log if t["reason"] != "open"]
    assert close[0]["entry_price"] == 90.0
    assert close[0]["exit_price"] == 96.0


def test_engine_overlay_still_wins_for_entry_and_exit():
    bot = _bot()
    rows = [
        _row("demo", "BTC-USDT-SWAP", "closed", "buy", 110.0,
             "2026-10-30T11:00:00+00:00", "o2"),
    ]
    eng = [{"ord_id": "o2", "pnl": -5.0, "entry_px": 101.5, "exit_px": 110.0}]
    log, stats = bot._rebuild_trade_log(rows, eng)
    close = [t for t in log if t["reason"] != "open"]
    assert close[0]["entry_price"] == 101.5
    assert close[0]["exit_price"] == 110.0
    assert stats["trades"] == 1
