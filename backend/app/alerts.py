"""
Telegram Alerts — Sends trade events, R-multiple updates, daily summaries.
"""
import os
import httpx

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


async def send_message(text, parse_mode="HTML"):
    """Send message to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TG] No token/chat configured. Message:\n{text}\n", flush=True)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            else:
                print(f"[TG] Error {resp.status_code}: {resp.text[:200]}", flush=True)
                return False
    except Exception as e:
        print(f"[TG] Error sending: {e}", flush=True)
        return False


async def alert_entry(trade, rules_name="Strategy"):
    """Alert on trade entry."""
    r_value = trade.get("r_value", 0)
    entry = trade.get("entry_price", 0)
    stop = trade.get("stop_loss", 0)
    target = trade.get("target_price", entry + r_value * 4.5)
    side = trade.get("side", "long").upper()
    signal = trade.get("signal_type", "?")

    text = (
        f"\U0001f7e2 <b>ENTRY: {trade['symbol']}</b>\n\n"
        f"Side: <b>{side}</b>\n"
        f"Entry: <code>${entry:,.2f}</code>\n"
        f"Stop: <code>${stop:,.2f}</code>\n"
        f"Target: <code>${target:,.2f}</code>\n"
        f"R-value: <code>${r_value:,.2f}</code>\n"
        f"Signal: {signal}\n"
        f"Strategy: {rules_name}\n\n"
        f"R:R = 1:{4.5/1.8:.1f}"
    )
    return await send_message(text)


async def alert_exit(trade, exit_price, exit_reason, rules_name="Strategy"):
    """Alert on trade exit."""
    final_r = trade.get("final_r", 0)
    pnl = trade.get("pnl_usd", 0)
    r_emoji = "\U0001f7e2" if final_r > 0 else "\U0001f534"

    text = (
        f"{'🟢' if final_r > 0 else '🔴'} <b>EXIT: {trade['symbol']}</b>\n\n"
        f"Entry: <code>${trade['entry_price']:,.2f}</code>\n"
        f"Exit: <code>${exit_price:,.2f}</code>\n"
        f"Reason: {exit_reason}\n"
        f"R-multiple: <b>{final_r:+.2f}R</b> {r_emoji}\n"
        f"PnL: <code>${pnl:+,.2f}</code>\n"
        f"Peak R: {trade.get('peak_r', 0):+.2f}R\n"
        f"Strategy: {rules_name}"
    )
    return await send_message(text)


async def alert_circuit_breaker(dd_pct, cooldown_until, rules_name="Strategy"):
    """Alert on circuit breaker trigger."""
    text = (
        f"\u26a0\ufe0f <b>CIRCUIT BREAKER</b>\n\n"
        f"Drawdown: <b>{dd_pct:.1f}%</b>\n"
        f"Trading paused until: {cooldown_until}\n"
        f"Strategy: {rules_name}"
    )
    return await send_message(text)


async def alert_daily_summary(stats, daily_summary, scan_report=""):
    """Send daily performance summary."""
    text = (
        f"\U0001f4ca <b>DAILY SUMMARY</b>\n\n"
        f"Trades: {stats['total_trades']}\n"
        f"Win Rate: {stats['win_rate']}%\n"
        f"Total R: {stats['total_r']:+.3f}\n"
        f"Profit Factor: {stats['profit_factor']:.2f}\n\n"
        f"{daily_summary}"
    )
    if scan_report:
        text += f"\n\n{scan_report}"
    return await send_message(text)


async def alert_scan_results(scan_results):
    """Send scanner report."""
    if not scan_results:
        return False

    lines = ["\U0001f4ca <b>SCANNER RESULTS</b>\n"]
    for r in scan_results[:5]:
        direction = "\U0001f7e2" if r["change_pct"] > 0 else "\U0001f534"
        vol_m = r["vol_24h_usd"] / 1_000_000
        lines.append(
            f"{direction} <code>{r['instId']}</code> "
            f"{r['change_pct']:+.2f}% "
            f"Vol ${vol_m:.0f}M "
            f"Score:{r['score']}"
        )
    return await send_message("\n".join(lines))


async def alert_planned_trade(planned, rules_name="Strategy"):
    """Send planned trade analysis (AI's reasoning)."""
    action = planned.get("action", "WAIT")
    symbol = planned.get("symbol", "?")
    reasoning = planned.get("reasoning", "No reasoning provided")
    entry_zone = planned.get("entry_zone", [])
    stop = planned.get("stop_loss", 0)
    target = planned.get("target_price", 0)

    action_emoji = {"LONG": "\U0001f7e2", "SHORT": "\U0001f534", "WAIT": "\u23f3", "HOLD": "\u23f8\ufe0f"}
    emoji = action_emoji.get(action, "\u26aa")

    entry_text = ""
    if entry_zone and len(entry_zone) >= 2:
        entry_text = f"Entry Zone: <code>${entry_zone[0]:,.2f} \u2014 ${entry_zone[1]:,.2f}</code>\n"

    text = (
        f"{emoji} <b>PLANNED: {symbol}</b>\n\n"
        f"Action: <b>{action}</b>\n"
        f"{entry_text}"
        f"Stop: <code>${stop:,.2f}</code>\n"
        f"Target: <code>${target:,.2f}</code>\n\n"
        f"<b>AI Analysis:</b>\n{reasoning}\n\n"
        f"Strategy: {rules_name}"
    )
    return await send_message(text)
