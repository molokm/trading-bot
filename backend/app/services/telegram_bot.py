"""Telegram bot command & payments poller for paid signal subscriptions.

Long-polls Telegram getUpdates and handles:
  /start, /help, /subscribe, /status        — text commands
  pre_checkout_query                        — Stars invoice confirmation
  successful_payment                        — activate/extend subscription and
                                              issue a one-time invite to the
                                              private signals channel

Payment rail: Telegram Stars (native, no fiat processor needed). The bot must be
an admin (with "Invite users" permission) of the private channel referenced by
TELEGRAM_CHANNEL_ID so it can create invite links after payment.

Runs in its own daemon thread with a private event loop, like the strategies.
"""

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from .telegram_notifier import TelegramNotifier

log = logging.getLogger("telegram_bot")

PRICE_STARS = int(os.getenv("SIGNAL_PRICE_STARS", "100"))
PLAN_DAYS = int(os.getenv("SIGNAL_PLAN_DAYS", "30"))
INVITE_DAYS = int(os.getenv("SIGNAL_INVITE_DAYS", "3"))
PLAN_LABEL = os.getenv("SIGNAL_PLAN_LABEL", "Подписка на сигналы 1 мес")

PRO_PRICE_STARS = int(os.getenv("PRO_PRICE_STARS", "500"))
PRO_PLAN_DAYS = int(os.getenv("PRO_PLAN_DAYS", "30"))
PRO_LABEL = os.getenv("PRO_PLAN_LABEL", "Pro-тариф: мини-ап + свой счёт OKX 1 мес")

# plan -> (price_stars, plan_days, label)
PLANS = {
    "signals": (PRICE_STARS, PLAN_DAYS, PLAN_LABEL),
    "pro": (PRO_PRICE_STARS, PRO_PLAN_DAYS, PRO_LABEL),
}


class TelegramBotPoller:
    """Long-polling update loop + subscription (Stars) handling."""

    def __init__(self, notifier: Optional[TelegramNotifier] = None, db=None):
        self.notifier = notifier or TelegramNotifier()
        self.db = db
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def token(self) -> str:
        """Read the token live from the notifier so runtime config changes apply."""
        return self.notifier.token

    @property
    def channel_id(self) -> str:
        return self.notifier.channel_id or os.getenv("TELEGRAM_CHANNEL_ID", "")

    # ─── Bot API helpers ───

    async def _api(self, method: str, **payload) -> dict:
        if not self.token:
            return {"ok": False}
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(url, json=payload)
                return resp.json()
        except Exception as e:
            log.warning("tg %s error: %s", method, e)
            return {"ok": False}

    async def _send_msg(self, chat_id, text: str, parse_mode: str = "HTML",
                        reply_markup: dict = None) -> dict:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        payload["link_preview_options"] = {"is_disabled": True}
        return await self._api("sendMessage", **payload)

    # ─── Command / payment handling ───

    async def _handle_update(self, update: dict):
        if "pre_checkout_query" in update:
            await self._on_pre_checkout(update["pre_checkout_query"])
            return
        msg = update.get("message")
        if not msg:
            return
        if msg.get("successful_payment"):
            await self._on_successful_payment(msg)
            return
        text = (msg.get("text") or "").strip()
        chat_id = msg["chat"]["id"]
        if text.startswith("/"):
            cmd = text.split()[0].lower().split("@")[0]
            if cmd in ("/start", "/help", "/menu"):
                await self._cmd_start(chat_id)
            elif cmd == "/subscribe":
                await self._cmd_subscribe(chat_id, "signals")
            elif cmd == "/subscribe_pro":
                await self._cmd_subscribe(chat_id, "pro")
            elif cmd == "/status":
                await self._cmd_status(chat_id)
            else:
                await self._send_msg(chat_id, self._menu_text())
            return
        await self._send_msg(chat_id, self._menu_text())

    def _menu_text(self) -> str:
        return (
            "🤖 <b>Торговый бот</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "Выберите подписку:\n\n"
            f"📡 <b>Сигналы</b> — {PRICE_STARS} ⭐ / {PLAN_DAYS} дн.\n"
            "   Доступ в приватный канал, где публикуются сигналы стратегий "
            "(Momentum Rotation + Impulse 1D).\n"
            f"💎 <b>Pro</b> — {PRO_PRICE_STARS} ⭐ / {PRO_PLAN_DAYS} дн.\n"
            "   Мини-ап + ваш счёт OKX: подключаете свои ключи, и те же боты "
            "торгуют на вашем счёте.\n\n"
            "Команды:\n"
            "/subscribe — оплатить сигналы\n"
            "/subscribe_pro — оплатить Pro\n"
            "/status — статус подписки"
        )

    async def _cmd_start(self, chat_id):
        await self._send_msg(chat_id, self._menu_text())

    async def _cmd_subscribe(self, chat_id, plan: str = "signals"):
        price, days, label = PLANS.get(plan, PLANS["signals"])
        title = "Pro-тариф" if plan == "pro" else "Сигналы торгового бота"
        desc = (f"Мини-ап + торговые боты на вашем счёте OKX на {days} дн."
                if plan == "pro"
                else f"Доступ к приватному каналу с сигналами на {days} дн.")
        invoice = await self._api(
            "sendInvoice",
            chat_id=chat_id,
            title=title,
            description=desc,
            payload=f"sub_{plan}_{int(time.time())}",
            provider_token="",          # empty => Telegram Stars
            currency="XTR",
            prices=[{"label": label, "amount": price}],
            reply_markup={
                "inline_keyboard": [[{"text": f"Оплатить {price} ⭐", "pay": True}]]
            },
        )
        if not invoice.get("ok"):
            log.warning("sendInvoice failed for chat %s: %s", chat_id, invoice)
            await self._send_msg(
                chat_id,
                "Не удалось создать счёт. Попробуйте позже или напишите администратору.",
            )

    async def _cmd_status(self, chat_id, user_id: str = None):
        uid = user_id
        if uid is None:
            return
        sub = None
        if self.db:
            try:
                sub = await self.db.get_subscription(uid)
            except Exception as e:
                log.warning("subscription lookup error: %s", e)
        if not sub:
            await self._send_msg(
                chat_id,
                "У вас пока нет активной подписки.\n\n"
                f"/subscribe — сигналы {PRICE_STARS} ⭐\n"
                f"/subscribe_pro — Pro (мини-ап + свой счёт) {PRO_PRICE_STARS} ⭐",
            )
            return
        until = sub.get("active_until", "")
        active = _is_active(sub)
        plan = sub.get("plan", "signals")
        plan_label = "💎 Pro" if plan == "pro" else "📡 Сигналы"
        state = "✅ активна" if active else "⛔ истекла"
        await self._send_msg(
            chat_id,
            f"📋 <b>Статус подписки</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Тариф: {plan_label}\n"
            f"Статус: {state}\n"
            f"Действует до: <b>{until}</b> (UTC)\n\n"
            + ("Продлить: /subscribe или /subscribe_pro" if active
               else "/subscribe — сигналы, /subscribe_pro — Pro"),
        )

    async def _on_pre_checkout(self, pcq: dict):
        await self._api("answerPreCheckoutQuery",
                        pre_checkout_query_id=pcq["id"], ok=True)

    async def _on_successful_payment(self, msg: dict):
        user = msg.get("from", {})
        uid = str(user.get("id", ""))
        payment = msg.get("successful_payment", {})
        amount = payment.get("total_amount", 0)
        payment_id = payment.get("provider_payment_charge_id", "")
        username = user.get("username", "")
        first_name = user.get("first_name", "")

        # Plan is carried in the invoice payload ("sub_<plan>_<ts>").
        payload = payment.get("invoice_payload", "") or ""
        plan = "pro" if "sub_pro" in payload else "signals"
        days = PRO_PLAN_DAYS if plan == "pro" else PLAN_DAYS

        # Stack: extend from the later of (now, current expiry).
        base = datetime.now(timezone.utc)
        if self.db:
            try:
                cur = await self.db.get_subscription(uid)
                if cur and _parse_until(cur.get("active_until")):
                    base = _parse_until(cur["active_until"])
            except Exception as e:
                log.warning("subscription read error: %s", e)
        new_until = base + timedelta(days=days)
        until_iso = new_until.strftime("%Y-%m-%d %H:%M")

        if self.db:
            try:
                await self.db.save_subscription(
                    user_id=uid, username=username, first_name=first_name,
                    active_until=until_iso, payment_id=payment_id,
                    plan=plan, status="active",
                )
                # Keep the users table in sync (plan gates mini-app features).
                await self.db.find_or_create_user(uid, username, first_name)
                await self.db.update_user(
                    uid, plan=plan, username=username, first_name=first_name,
                    active_until=until_iso,
                )
            except Exception as e:
                log.warning("subscription save error: %s", e)

        chat_id = msg["chat"]["id"]
        plan_label = "💎 Pro (мини-ап + ваш счёт OKX)" if plan == "pro" else "📡 Сигналы"
        await self._send_msg(
            chat_id,
            f"✅ <b>Оплата получена</b> ({amount} ⭐)\n"
            f"Тариф: {plan_label}\n"
            f"Активен до <b>{until_iso}</b> (UTC).",
        )
        if plan == "pro":
            # Pro users don't need a channel invite; they use the mini-app.
            await self._send_msg(
                chat_id,
                "Откройте мини-ап через кнопку меню или "
                "https://t.me/<yourbot>/app — подключите ключи OKX и запустите ботов.",
            )
        else:
            await self._issue_invite(chat_id)

    async def _issue_invite(self, chat_id):
        """Create a one-time invite to the private channel and send it."""
        ch = self.channel_id
        if not ch:
            await self._send_msg(
                chat_id,
                "Канал сигналов ещё не настроен. Пожалуйста, подождите — "
                "администратор добавит вас вручную.",
            )
            return
        expire_ts = int(time.time()) + INVITE_DAYS * 86400
        res = await self._api("createChatInviteLink",
                              chat_id=ch, member_limit=1, expire_date=expire_ts)
        link = ""
        if res.get("ok") and res.get("result"):
            link = res["result"].get("invite_link", "")
        if not link:
            await self._send_msg(
                chat_id,
                "Оплата принята, но не удалось выдать ссылку (бот не админ канала "
                "или нет права приглашать). Администратор добавит вас вручную.",
            )
            return
        await self._send_msg(
            chat_id,
            f"🔑 <b>Ваша ссылка на канал сигналов</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Действует {INVITE_DAYS} дн., можно использовать один раз.\n\n"
            f"{link}",
        )

    # ─── Lifecycle ───

    async def _poll_loop(self):
        offset = 0
        while self._running:
            try:
                res = await self._api(
                    "getUpdates",
                    offset=offset, timeout=30,
                    allowed_updates=["message", "pre_checkout_query"],
                )
                if res.get("ok"):
                    for u in res.get("result", []):
                        offset = u["update_id"] + 1
                        try:
                            await self._handle_update(u)
                        except Exception as e:
                            log.warning("update %s error: %s", u.get("update_id"), e)
            except Exception as e:
                log.warning("getUpdates error: %s", e)
            await asyncio.sleep(1)

    def _thread_runner(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._poll_loop())
        except RuntimeError:
            if self._running:
                log.warning("poller event loop stopped unexpectedly")
        except Exception as e:
            log.warning("poller thread error: %s", e)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._thread_runner, daemon=True)
        self._thread.start()
        log.info("Telegram bot poller started (price=%s stars/%sd)", PRICE_STARS, PLAN_DAYS)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        log.info("Telegram bot poller stopped")


def _parse_until(value: str) -> Optional[datetime]:
    """Parse 'YYYY-MM-DD HH:MM' (UTC) into a timezone-aware datetime."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _is_active(sub: dict) -> bool:
    until = _parse_until(sub.get("active_until"))
    if not until:
        return False
    return until > datetime.now(timezone.utc)
