"""Telegram bot command & payments poller for the Pro subscription.

Long-polls Telegram getUpdates and handles:
  /start, /help, /subscribe_pro, /status, /stats — text commands
  pre_checkout_query                            — Stars invoice confirmation
  successful_payment                            — activate/extend the Pro
                                                  subscription (mini-app access)

Payment rail: Telegram Stars (native, no fiat processor needed).

Only the Pro plan remains: mini-app + the user's own OKX account. The former
paid "signals" channel has been retired — trade signals are free and published
in the bot itself.
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
from .strategy_cards import (
    telegram_metrics_block,
    telegram_profile_description,
    telegram_short_description,
    strategy_versions_line,
    cagr_range_str,
)

log = logging.getLogger("telegram_bot")

PRO_PRICE_STARS = int(os.getenv("PRO_PRICE_STARS", "500"))
PRO_PLAN_DAYS = int(os.getenv("PRO_PLAN_DAYS", "30"))
PRO_LABEL = os.getenv("PRO_PLAN_LABEL", "Pro-тариф: мини-ап + свой счёт OKX 1 мес")
TRACKER_URL = os.getenv("TRACKER_URL", "")

PLANS = {
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
        cq = update.get("callback_query")
        if cq:
            await self._handle_callback(cq)
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
            elif cmd in ("/subscribe", "/subscribe_pro"):
                await self._cmd_subscribe(chat_id, "pro")
            elif cmd in ("/status",):
                await self._cmd_status(chat_id)
            elif cmd in ("/stats", "/stat", "/statistics"):
                # optional period: /stats week
                parts = text.split()
                period = parts[1].lower() if len(parts) > 1 else "all"
                await self._cmd_stats(chat_id, period=period)
            elif cmd in ("/tracker",):
                await self._send_info(chat_id, "results")
            elif cmd == "/about":
                await self._cmd_about(chat_id)
            elif cmd == "/info":
                await self._send_info(chat_id, "overview")
            else:
                await self._send_msg(chat_id, self._menu_text())
            return
        await self._send_msg(chat_id, self._menu_text())

    def _menu_keyboard(self) -> dict:
        return {
            "inline_keyboard": [
                [{"text": f"🚀 Pro · {PRO_PRICE_STARS} ⭐", "callback_data": "sub_pro"}],
                [{"text": "📡 Бесплатные сигналы", "callback_data": "info_signals"},
                 {"text": "🔍 Результаты", "callback_data": "info_results"}],
                [{"text": "💳 Оплата", "callback_data": "info_payment"},
                 {"text": "⚠️ Риски", "callback_data": "info_risks"}],
                [{"text": "📊 Статистика", "callback_data": "stats_all"},
                 {"text": "ℹ️ О боте", "callback_data": "about"}],
                [{"text": "❓ FAQ", "callback_data": "info_faq"}],
            ]
        }

    def _info_keyboard(self) -> dict:
        return {
            "inline_keyboard": [
                [{"text": "📚 Как это работает", "callback_data": "info_overview"},
                 {"text": "📡 Сигналы", "callback_data": "info_signals"}],
                [{"text": "🚀 Pro", "callback_data": "info_pro"},
                 {"text": "🔍 Результаты", "callback_data": "info_results"}],
                [{"text": "💳 Оплата", "callback_data": "info_payment"},
                 {"text": "⚠️ Риски", "callback_data": "info_risks"}],
                [{"text": "❓ FAQ", "callback_data": "info_faq"},
                 {"text": "🔙 В меню", "callback_data": "menu"}],
            ]
        }

    def _info_text(self, section: str) -> str:
        texts = {
            "overview": (
                "📚 <b>Как это работает</b>\n"
                "━━━━━━━━━━━━━━━\n"
                "AI смотрит на рынок каждые 2 минуты и решает: открыть, закрыть или "
                "подождать. Никакого ручного кликания — бот всё делает сам.\n\n"
                "Торгует BTC, ETH, SOL, XRP на фьючерсах OKX, следит за стопами "
                "и риском.\n\n"
                "📡 <b>Сигналы</b> — сделки публикуются здесь бесплатно.\n"
                "🚀 <b>Pro</b> — AI торгует на <b>вашем</b> счёте OKX через мини-ап.\n\n"
                "Подробнее: /about · /status"
            ),
            "signals": (
                "📡 <b>Бесплатные сигналы</b>\n"
                "━━━━━━━━━━━━━━━\n"
                "<b>Теперь бесплатно и прямо в этом боте.</b>\n\n"
                "• Каждая сделка AI публикуется здесь автоматически\n"
                "• Цена входа, стоп-лосс, тейк и итоговый результат\n"
                "• Направление (лонг/шорт) и текущие открытые позиции\n\n"
                "Никакой оплаты и отдельного канала не нужно — следите за "
                "сигналами прямо здесь.\n\n"
                "Хотите, чтобы AI торговал на вашем счёте? → 🚀 Pro: /subscribe_pro"
            ),
            "pro": (
                f"🚀 <b>Pro</b> · {PRO_PRICE_STARS} ⭐ / {PRO_PLAN_DAYS} дн.\n"
                "━━━━━━━━━━━━━━━\n"
                "AI торгует на <b>вашем счёте OKX</b> полностью автоматически.\n\n"
                "<b>Что вы получаете:</b>\n"
                "• Подключаете свои ключи OKX (шифруются на сервере)\n"
                "• Запускаете AI в один тап в мини-апе\n"
                "• Бот сам открывает/закрывает сделки, ставит стопы и ведёт риск\n"
                "• Статистика и управление — в мини-апе в Telegram\n\n"
                "<b>Требования:</b>\n"
                "• Счёт OKX (фьючерсы USDT-M)\n"
                "• Ключи API с правом торговли + включённый IP-whitelist\n"
                "• Понимание, что торговля с плечом рискованна\n\n"
                "Оплатить: /subscribe_pro или кнопкой в меню."
            ),
            "results": (
                "🔍 <b>Результаты</b>\n"
                "━━━━━━━━━━━━━━━\n"
                "Живые сделки и PnL — в мини-апе и на дашборде.\n\n"
                f"{telegram_metrics_block(html=True)}\n\n"
                "Прошлые результаты не гарантируют будущую доходность.\n\n"
                "Отчёт: мини-ап и дашборд"
            ),
            "payment": (
                "💳 <b>Оплата Pro</b>\n"
                "━━━━━━━━━━━━━━━\n"
                "Оплата проходит прямо в Telegram через <b>Telegram Stars</b> — встроенную "
                "валюту. Быстро и безопасно.\n\n"
                "🚀 <b>Pro</b> — мини-ап + торговля на вашем счёте OKX.\n\n"
                "<b>Как оплатить:</b>\n"
                "1. Нажмите «Оплатить» под счётом (кнопка Pro)\n"
                "2. Подтвердите оплату Stars\n"
                "3. Откройте мини-ап через кнопку меню — подключите счёт и запустите AI\n\n"
                "<b>Продление:</b> срок подписки складывается при каждой оплате.\n\n"
                f"Тариф: Pro — {PRO_PRICE_STARS} ⭐ / {PRO_PLAN_DAYS} дн.\n\n"
                "📡 Сигналы — бесплатно, прямо в этом боте."
            ),
            "risks": (
                "⚠️ <b>Важно о рисках</b>\n"
                "━━━━━━━━━━━━━━━\n"
                "Торговля фьючерсами с плечом — высокорисковый инструмент.\n\n"
                "• Любая стратегия может давать убыточные периоды и просадки\n"
                "• Прошлые результаты не гарантируют будущей доходности\n"
                "• Мы не обещаем космической доходности; просим использовать только "
                "свободные средства\n"
                "• Реалистичный ориентир — десятки процентов в год, но возможны и убытки\n"
                "• Бот не является финансовым советником\n\n"
                "Принимайте решение о подписке осознанно."
            ),
            "faq": (
                "❓ <b>FAQ</b>\n"
                "━━━━━━━━━━━━━━━\n"
                "<b>Сигналы платные?</b>\n"
                "Нет — сигналы публикуются прямо в этом боте бесплатно.\n\n"
                "<b>Что нужно для Pro?</b>\n"
                "Счёт OKX, ключи API (торговля + IP-whitelist) и мини-ап.\n\n"
                "<b>Ключи безопасны?</b>\n"
                "Да: шифруются и хранятся на сервере, используются только для торговли "
                "по вашему запросу.\n\n"
                "<b>Можно ли отменить подписку?</b>\n"
                "Да — просто не продлевайте после истечения срока.\n\n"
                "<b>Что после оплаты Pro?</b>\n"
                "Открываете мини-ап, подключаете ключи OKX и запускаете AI одной кнопкой.\n\n"
                "<b>Почему не обещаете +1000%?</b>\n"
                "Потому что это ложь. Мы даём реальные цифры и защиту капитала."
            ),
        }
        return texts.get(section, texts["overview"])

    async def _send_info(self, chat_id, section: str):
        await self._send_msg(chat_id, self._info_text(section),
                             reply_markup=self._info_keyboard())

    def _menu_text(self) -> str:
        return (
            "🤖 <b>COPIX — ваш AI-трейдер на OKX</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "Пока вы отдыхаете, AI анализирует рынок и торгует.\n"
            "BTC, ETH, SOL, XRP — круглосуточно, без эмоций и без сна.\n\n"
            "📡 <b>Бесплатно</b> — каждая сделка публикуется здесь, прямо в боте.\n"
            "🚀 <b>Pro</b> — AI торгует на <b>вашем</b> счёте OKX. "
            "Подключите ключи в мини-апе — и всё, бот делает остальное.\n\n"
            "🛡️ <b>Риск под контролем</b>\n"
            "• Лимит риска на сделку и число позиций\n"
            "• Умеренное плечо, стопы и автоматический выход\n"
            "• Цель — без ликвидаций при штатных настройках\n\n"
            "📊 <b>Честно</b>\n"
            f"{telegram_metrics_block(html=True)}\n\n"
            f"🚀 <b>Pro</b> · {PRO_PRICE_STARS} ⭐ / {PRO_PLAN_DAYS} дн.\n"
            "AI торгует на <b>вашем</b> счёте OKX, управление — в мини-апе.\n\n"
            "Команды: /stats · /subscribe_pro · /info · /status · /about · /start"
        )

    def _about_text(self) -> str:
        return (
            "📈 <b>Как это работает</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "AI анализирует рынок каждые 2 минуты, оценивает тренды, "
            "импульс и риски — и принимает решение: открыть, закрыть или держать.\n\n"
            "• <b>Что делает:</b> торгует BTC, ETH, SOL, XRP на OKX (USDT-SWAP)\n"
            "• <b>Как решает:</b> LLM-модель + индикаторы — фильтр входа и выхода\n"
            "• <b>Защита:</b> лимит риска, стопы, умеренное плечо, контроль позиций\n"
            f"{telegram_metrics_block(html=True)}\n"
            "• <b>Прозрачность:</b> сделки и PnL — в мини-апе и на дашборде\n\n"
            "Отчёт: мини-ап и дашборд\n\n"
            "⚠️ Прошлые и текущие результаты не гарантируют будущей доходности. "
            "Торговля фьючерсами с плечом — высокий риск."
        )

    async def _cmd_about(self, chat_id):
        await self._send_msg(chat_id, self._about_text())

    async def _handle_callback(self, cq: dict):
        data = cq.get("data", "")
        msg = cq.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        cq_id = cq.get("id")
        if not chat_id:
            return
        log.info("[callback] pid=%s chat=%s data=%s cq=%s", os.getpid(), chat_id, data, cq_id)
        await self._api("answerCallbackQuery", callback_query_id=cq_id, text="✓")
        if data in ("sub_signals", "sub_pro"):
            await self._cmd_subscribe(chat_id, "pro")
        elif data == "tracker":
            await self._send_info(chat_id, "results")
        elif data == "about":
            await self._send_msg(chat_id, self._about_text())
        elif data == "menu":
            await self._send_msg(chat_id, self._menu_text(), reply_markup=self._menu_keyboard())
        elif data.startswith("stats_"):
            period = data[len("stats_"):] or "all"
            await self._cmd_stats(chat_id, period=period)
        elif data.startswith("info_"):
            section = data[len("info_"):]
            await self._send_info(chat_id, section)

    def _stats_keyboard(self, period: str = "all") -> dict:
        periods = [
            ("today", "Сегодня"),
            ("week", "Неделя"),
            ("30d", "30д"),
            ("90d", "90д"),
            ("all", "Всё"),
        ]
        row = []
        for pid, label in periods:
            mark = "· " if pid == period else ""
            row.append({"text": f"{mark}{label}", "callback_data": f"stats_{pid}"})
        return {
            "inline_keyboard": [
                row[:3],
                row[3:],
                [{"text": "🔄 Обновить", "callback_data": f"stats_{period}"},
                 {"text": "🔙 В меню", "callback_data": "menu"}],
            ]
        }

    @staticmethod
    def _fmt_pnl(v) -> str:
        try:
            n = float(v or 0)
        except (TypeError, ValueError):
            return "—"
        sign = "+" if n > 0 else ""
        return f"{sign}{n:.2f}"

    async def _cmd_stats(self, chat_id, period: str = "all"):
        """Live DEMO stats — same engine as web /api/stats."""
        period = (period or "all").strip().lower()
        aliases = {
            "today": "today", "1d": "today", "day": "today", "сегодня": "today",
            "week": "week", "w": "week", "неделя": "week",
            "30d": "30d", "month": "30d", "месяц": "30d",
            "90d": "90d", "quarter": "90d",
            "all": "all", "всё": "all", "все": "all", "total": "all",
        }
        period = aliases.get(period, "all")
        period_labels = {
            "today": "сегодня",
            "week": "неделя (с пн)",
            "30d": "30 дней",
            "90d": "90 дней",
            "all": "всё время",
        }

        if not self.db:
            await self._send_msg(chat_id, "⚠️ Статистика временно недоступна (нет БД).")
            return

        try:
            from app.services import pnl_engine
            data = await pnl_engine.compute_stats(
                self.db,
                account_mode="demo",
                ai_only=True,
                period=period,
                sync_fn=None,  # avoid blocking TG on OKX; uses latest stored closes
            )
        except Exception as e:
            log.warning("stats cmd error: %s", e)
            await self._send_msg(chat_id, f"⚠️ Не удалось загрузить статистику: {e}")
            return

        realized = self._fmt_pnl(data.get("realized_pnl"))
        trades = int(data.get("trades") or 0)
        wins = int(data.get("wins") or 0)
        losses = int(data.get("losses") or 0)
        wr = data.get("win_rate")
        pf = data.get("profit_factor")
        avg_w = self._fmt_pnl(data.get("avg_win"))
        avg_l = self._fmt_pnl(data.get("avg_loss"))
        d_from = data.get("from") or "—"
        d_to = data.get("to") or "—"

        by_coin = data.get("by_coin") or []
        coin_lines = []
        for c in by_coin[:6]:
            coin_lines.append(
                f"  {c.get('coin')}: <b>{self._fmt_pnl(c.get('pnl'))}</b> $ "
                f"({c.get('trades', 0)} сд., WR {c.get('win_rate', 0)}%)"
            )
        coins_block = "\n".join(coin_lines) if coin_lines else "  нет закрытий"

        pf_s = f"{pf}" if pf is not None else "—"
        wr_s = f"{wr}%" if wr is not None else "—"

        text = (
            f"📊 <b>Статистика DEMO · AI</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Период: <b>{period_labels.get(period, period)}</b>\n"
            f"{d_from} → {d_to}\n\n"
            f"Realized PnL: <b>{realized} USDT</b>\n"
            f"Сделок: <b>{trades}</b> · ✅ {wins} / ❌ {losses}\n"
            f"Win rate: <b>{wr_s}</b> · PF <b>{pf_s}</b>\n"
            f"Ср. плюс: <b>{avg_w}</b> · Ср. минус: <b>{avg_l}</b>\n\n"
            f"<b>По монетам</b>\n{coins_block}\n\n"
            f"<i>Только закрытые сделки · МСК · /stats</i>"
        )
        await self._send_msg(
            chat_id, text,
            reply_markup=self._stats_keyboard(period),
        )

    async def _cmd_start(self, chat_id):
        await self._send_msg(chat_id, self._menu_text(), reply_markup=self._menu_keyboard())

    async def _cmd_subscribe(self, chat_id, plan: str = "pro"):
        price, days, label = PLANS.get(plan, PLANS["pro"])
        title = "Pro-тариф: мини-ап + ваш счёт OKX"
        desc = f"Мини-ап + торговые боты на вашем счёте OKX на {days} дн."
        invoice = await self._api(
            "sendInvoice",
            chat_id=chat_id,
            title=title,
            description=desc,
            payload=f"sub_pro_{int(time.time())}",
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
                f"/subscribe_pro — Pro (мини-ап + свой счёт) {PRO_PRICE_STARS} ⭐\n\n"
                "📡 Сигналы — бесплатно, прямо в этом боте.",
            )
            return
        until = sub.get("active_until", "")
        active = _is_active(sub)
        plan = sub.get("plan", "pro")
        plan_label = "💎 Pro"
        state = "✅ активна" if active else "⛔ истекла"
        await self._send_msg(
            chat_id,
            f"📋 <b>Статус подписки</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Тариф: {plan_label}\n"
            f"Статус: {state}\n"
            f"Действует до: <b>{until}</b> (UTC)\n\n"
            + ("Продлить: /subscribe_pro" if active
               else "Сигналы бесплатны в боте. Pro: /subscribe_pro"),
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

        # Only the Pro plan exists now; plan is always "pro".
        payload = payment.get("invoice_payload", "") or ""
        plan = "pro"
        days = PRO_PLAN_DAYS

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
        plan_label = "💎 Pro (мини-ап + ваш счёт OKX)"
        await self._send_msg(
            chat_id,
            f"✅ <b>Оплата получена</b> ({amount} ⭐)\n"
            f"Тариф: {plan_label}\n"
            f"Активен до <b>{until_iso}</b> (UTC).",
        )
        await self._send_msg(
            chat_id,
            "Откройте мини-ап через кнопку меню или "
            "https://t.me/<yourbot>/app — подключите ключи OKX и запустите AI.",
        )

    # ─── Lifecycle ───

    async def notify_signals_migration(self) -> None:
        """One-time notice to former paid "signals" subscribers.

        Signals became free and moved into the bot itself, so the private
        channel and the paid signals plan are retired. Existing subscribers
        keep whatever remaining active_until they had, but their plan is not
        "pro" — mini-app access is not auto-granted (only Pro unlocks it).
        """
        if not self.db:
            return
        try:
            rows = await self.db.list_subscriptions()
        except Exception as e:
            log.warning("signals migration: list error: %s", e)
            return
        for r in rows:
            if (r or {}).get("plan") != "signals":
                continue
            uid = r.get("user_id")
            if not uid:
                continue
            try:
                await self._send_msg(
                    uid,
                    "📡 <b>Новость о сигналах</b>\n"
                    "━━━━━━━━━━━━━━━\n"
                    "Спасибо за поддержку! Теперь сигналы стали <b>бесплатными</b> "
                    "и публикуются прямо в этом боте — отдельный платный канал больше "
                    "не нужен.\n\n"
                    "🚀 Остался один платный тариф — <b>Pro</b>: торговля на вашем "
                    "счёте OKX через мини-ап. Подробнее: /subscribe_pro",
                )
            except Exception as e:
                log.warning("signals migration: notify %s error: %s", uid, e)

    async def _update_bot_profile(self):
        """Refresh the bot's Telegram profile (name / description / commands).

        The "What can this bot do?" panel and the profile page show the stored
        description; without this call it keeps the old strategy text forever.
        """
        desc = (
            telegram_profile_description()
            + "\n📡 Сигналы AI — бесплатно в боте."
            + "\n🚀 Pro — AI на вашем счёте OKX: /subscribe_pro"
        )
        short = telegram_short_description()
        await self._api("setMyName", name="COPIX AI Trader")
        await self._api("setMyDescription", description=desc[:512])
        await self._api("setMyShortDescription", short_description=short[:120])
        await self._api("setMyCommands", commands=[
            {"command": "start", "description": "Главное меню"},
            {"command": "stats", "description": "Статистика DEMO (PnL, win rate)"},
            {"command": "info", "description": "Как работает AI"},
            {"command": "status", "description": "Подписка и статус"},
            {"command": "tracker", "description": "Живой отчёт"},
            {"command": "about", "description": "Как работает AI-трейдер"},
            {"command": "subscribe_pro", "description": "Pro — AI на вашем OKX"},
        ])

    async def _poll_loop(self):
        offset = 0
        # Refresh the bot's Telegram profile (description / name / commands) once
        # on startup so the "What can this bot do?" panel and profile show the
        # current strategy info instead of a stale description.
        try:
            await self._update_bot_profile()
        except Exception as e:
            log.warning("bot profile update error: %s", e)
        # Persist the update offset so a restart does not re-deliver old updates
        # (which otherwise duplicates menu replies when a user re-sends /start).
        if self.db:
            try:
                saved = await self.db.get_setting("TG_OFFSET")
                if saved and saved.isdigit():
                    offset = int(saved)
            except Exception as e:
                log.warning("poller offset load error: %s", e)
        while self._running:
            try:
                res = await self._api(
                    "getUpdates",
                    offset=offset, timeout=30,
                    allowed_updates=["message", "callback_query", "pre_checkout_query"],
                )
                if res.get("ok"):
                    for u in res.get("result", []):
                        offset = u["update_id"] + 1
                        if self.db:
                            try:
                                await self.db.set_setting("TG_OFFSET", str(offset))
                            except Exception as e:
                                log.warning("poller offset save error: %s", e)
                            try:
                                dup = await self.db.mark_update_processed(u["update_id"])
                                if dup:
                                    log.info("[dedup] skip already-processed update_id=%s", u["update_id"])
                                    continue
                            except Exception as e:
                                log.warning("poller dedup error update_id=%s: %s", u["update_id"], e)
                        log.info("[update] pid=%s update_id=%s keys=%s",
                                 os.getpid(), u["update_id"], list(u.keys()))
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
        log.info("Telegram bot poller started (price=%s stars/%sd)", PRO_PRICE_STARS, PRO_PLAN_DAYS)
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
