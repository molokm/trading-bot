from __future__ import annotations

import asyncio
from datetime import datetime


async def place_order_with_retry(client, inst_id: str, side: str, sz: str,
                                 ord_type: str = "market", td_mode: str = "cash",
                                 pos_side: str = None,
                                 max_retries: int = 3) -> dict:
    for attempt in range(max_retries):
        result = await client.place_order(
            inst_id=inst_id, side=side, ord_type=ord_type,
            sz=sz, td_mode=td_mode, pos_side=pos_side,
        )
        if not result.get("error"):
            return result
        err = result.get("message", "")
        if "429" in err or "Too Many Requests" in err:
            wait = 2 ** attempt
            await asyncio.sleep(wait)
            continue
        return result
    return {"error": True, "message": "Rate limit retries exhausted"}


def _is_swap(symbol: str) -> bool:
    return symbol.upper().endswith("-SWAP")


def _fee_rate(symbol: str) -> float:
    return 0.0005 if _is_swap(symbol) else 0.001  # taker: swap 0.05%, spot 0.1%


def _calc_fee(notional: float, rate: float) -> float:
    return round(notional * rate, 8)


async def execute_open(bot, side: str, price: float, db, signal_id: int = None) -> dict:
    client = bot.client_manager.get_client()
    if not client:
        return {"error": True, "message": "no client"}

    if _is_swap(bot.symbol):
        td_mode = "cross"
        pos_side = "long" if side == "buy" else "short"
        sz_pct = bot.params.get("size_pct", 0.80)
        leverage = float(bot.params.get("leverage", 1))
        notional = bot.capital * leverage * sz_pct
        ct_val = 0.01  # BTC-USDT-SWAP: 1 contract = 0.01 BTC
        sz_dec = notional / (ct_val * price)
        sz = f"{sz_dec:.2f}"
        if float(sz) < 0.01:
            return {"error": True, "message": f"position too small: {sz}"}
        order_result = await place_order_with_retry(
            client, inst_id=bot.symbol, side=side, sz=sz,
            td_mode=td_mode, pos_side=pos_side,
        )
        pos_sz = float(sz) * ct_val  # convert contracts to BTC for internal tracking
        open_notional = notional
    else:
        if side != "buy":
            bot.error = "short_not_supported_in_spot_cash"
            return {"error": True, "message": "short not supported"}
        td_mode = "cash"
        sz = f"{bot.capital * 0.95:.2f}"
        pos_sz = float(sz) / price
        order_result = await place_order_with_retry(
            client, inst_id=bot.symbol, side=side, sz=sz, td_mode=td_mode,
        )
        open_notional = float(sz)

    if order_result.get("error"):
        bot.error = f"order_failed: {order_result.get('message', '')}"
        return {"error": True, "message": order_result.get("message", "")}

    entry_fee = _calc_fee(open_notional, _fee_rate(bot.symbol))
    bot.capital -= entry_fee

    bot.position = pos_sz
    bot.entry_price = price
    bot._entry_fee = entry_fee
    ord_data = order_result.get("data", [{}])[0]
    ord_id = ord_data.get("ordId", "")

    bot.orders.append({
        "time": datetime.now().isoformat(),
        "side": side,
        "price": round(price, 2),
        "size": sz,
        "fee": round(entry_fee, 6),
        "result": ord_data,
    })

    await db.save_trade(
        bot_id=bot.id, side=side, sz=sz,
        px=str(round(price, 2)), ord_id=ord_id,
        fee=str(round(entry_fee, 6)), fee_ccy="USDT",
        inst_id=bot.symbol, state="filled",
        signal_id=signal_id,
    )
    await db.save_position(
        bot_id=bot.id, inst_id=bot.symbol,
        side="long" if side == "buy" else "short",
        size=pos_sz, entry_price=price, current_price=price,
    )

    return {"ok": True, "ord_id": ord_id, "size": sz, "pos_sz": pos_sz}


async def execute_close(bot, reason: str, db, signal_id: int = None) -> dict:
    client = bot.client_manager.get_client()
    if not client or bot.position == 0:
        return {"error": True, "message": "no position"}

    close_side = "sell" if bot.position > 0 else "buy"

    if _is_swap(bot.symbol):
        ct_val = 0.01  # BTC-USDT-SWAP: 1 contract = 0.01 BTC
        close_sz = f"{abs(bot.position) / ct_val:.2f}"
        td_mode = "cross"
        pos_side = "long" if bot.position > 0 else "short"
    else:
        close_sz = f"{abs(bot.position):.6f}" if close_side == "sell" else \
                   f"{abs(bot.position) * (bot.entry_price or 1):.2f}"
        td_mode = "cash"

    order_result = await place_order_with_retry(
        client, inst_id=bot.symbol, side=close_side, sz=close_sz,
        td_mode=td_mode,
        pos_side=pos_side if _is_swap(bot.symbol) else None,
    )

    if order_result.get("error"):
        bot.error = f"close_failed: {order_result.get('message', '')}"
        return {"error": True, "message": order_result.get("message", "")}

    tk = await client.get_ticker(bot.symbol)
    cur_price = float(tk.get("data", [{}])[0].get("last", 0)) \
        if not tk.get("error") else bot.entry_price
    rate = _fee_rate(bot.symbol)
    entry_notional = abs(bot.position) * bot.entry_price
    close_notional = abs(bot.position) * cur_price
    total_fee = _calc_fee(entry_notional, rate) + _calc_fee(close_notional, rate)
    pnl_val = bot.position * (cur_price - bot.entry_price) - total_fee

    bot.pnl += pnl_val
    bot.capital += pnl_val
    bot.trade_count += 1
    if pnl_val > 0:
        bot.win_count += 1
    else:
        bot.loss_count += 1

    bot.orders.append({
        "time": datetime.now().isoformat(),
        "side": close_side,
        "price": round(cur_price, 2),
        "sz": close_sz,
        "pnl": round(pnl_val, 2),
        "fee": round(total_fee, 6),
        "reason": reason,
    })

    await db.save_trade(
        bot_id=bot.id, side=close_side, sz=close_sz,
        px=str(round(cur_price, 2)),
        ord_id=order_result.get("data", [{}])[0].get("ordId", ""),
        fee=str(round(total_fee, 6)),
        inst_id=bot.symbol, pnl=round(pnl_val, 2), state="closed",
        signal_id=signal_id,
    )
    await db.delete_position(bot.id)

    bot.position = 0.0
    bot.entry_price = 0.0
    bot._entry_fee = 0.0

    return {"ok": True, "pnl": round(pnl_val, 2), "capital": round(bot.capital, 2)}
