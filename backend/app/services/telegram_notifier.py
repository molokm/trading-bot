"""Telegram trade notifications for the trading bot.

Sends messages to a Telegram chat via the Bot API. Credentials are read
from the environment (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) and can be
updated at runtime through the dashboard settings endpoint.
"""

import asyncio
import hashlib
import hmac
import html
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qsl

import httpx

ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT = "TELEGRAM_CHAT_ID"
ENV_CHANNEL = "TELEGRAM_CHANNEL_ID"

_ESCAPE = {ord("<"): "&lt;", ord(">"): "&gt;", ord("&"): "&amp;"}


def _esc(value) -> str:
    """Escape a value for Telegram HTML parse mode."""
    return str(value).translate(_ESCAPE)


class TelegramNotifier:
    _open_msg_by_signal: dict = {}

    def __init__(self, token: str = "", chat_id: str = "", channel_id: str = ""):
        self.token = token or os.getenv(ENV_TOKEN, "")
        self.chat_id = chat_id or os.getenv(ENV_CHAT, "")
        self.channel_id = channel_id or os.getenv(ENV_CHANNEL, "")

    def configure(self, token: str = "", chat_id: str = "", channel_id: str = "") -> None:
        """Update credentials at runtime (used by dashboard settings)."""
        if token:
            self.token = token.strip()
        if chat_id:
            self.chat_id = chat_id.strip()
        if channel_id:
            self.channel_id = channel_id.strip()
        os.environ[ENV_TOKEN] = self.token
        os.environ[ENV_CHAT] = self.chat_id
        os.environ[ENV_CHANNEL] = self.channel_id

    async def load_from_db(self, db) -> None:
        """Restore credentials persisted in the settings table (survives restarts)."""
        try:
            token = await db.get_setting(ENV_TOKEN)
            chat_id = await db.get_setting(ENV_CHAT)
            channel_id = await db.get_setting(ENV_CHANNEL)
            if token:
                self.token = token
            if chat_id:
                self.chat_id = chat_id
            if channel_id:
                self.channel_id = channel_id
        except Exception:
            pass

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    @property
    def status(self) -> str:
        if not self.token:
            return "no_token"
        if not self.chat_id:
            return "no_chat"
        return "ok"

    async def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message. Returns True on success. Never raises."""
        if not self.configured:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload)
                return bool(resp.json().get("ok"))
        except Exception:
            return False

    def remember_open(self, signal_id, message_id, pos_key: str = "") -> None:
        try:
            sid = int(signal_id or 0)
            mid = int(message_id or 0)
        except (TypeError, ValueError):
            return
        if mid and sid:
            TelegramNotifier._open_msg_by_signal[sid] = mid
        if mid and pos_key:
            TelegramNotifier._open_msg_by_signal[f"pos:{pos_key}"] = mid
        if len(TelegramNotifier._open_msg_by_signal) > 800:
            for k in list(TelegramNotifier._open_msg_by_signal.keys())[:150]:
                TelegramNotifier._open_msg_by_signal.pop(k, None)

    def open_message_id(self, signal_id=0, pos_key: str = "") -> int:
        try:
            sid = int(signal_id or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid:
            mid = int(TelegramNotifier._open_msg_by_signal.get(sid) or 0)
            if mid:
                return mid
        if pos_key:
            return int(TelegramNotifier._open_msg_by_signal.get(f"pos:{pos_key}") or 0)
        return 0

    async def remember_open_db(self, db, signal_id, message_id, bot_id: str = "", coin: str = "") -> None:
        """Persist open Telegram message_id so close can reply after restart."""
        self.remember_open(signal_id, message_id, pos_key=f"{bot_id}:{coin}" if bot_id and coin else "")
        if not db:
            return
        try:
            mid = int(message_id or 0)
            if not mid:
                return
            if signal_id:
                await db.set_setting(f"tg_open_msg:{int(signal_id)}", str(mid))
            if bot_id and coin:
                await db.set_setting(f"tg_open_pos:{bot_id}:{coin}", str(mid))
        except Exception as e:
            print(f"[TG] remember_open_db: {e}", flush=True)

    async def resolve_open_message_id(self, db, signal_id=0, bot_id: str = "", coin: str = "") -> int:
        """Memory first, then DB settings."""
        pos_key = f"{bot_id}:{coin}" if bot_id and coin else ""
        mid = self.open_message_id(signal_id, pos_key=pos_key)
        if mid:
            return mid
        if not db:
            return 0
        try:
            if signal_id:
                raw = await db.get_setting(f"tg_open_msg:{int(signal_id)}")
                if raw and str(raw).isdigit():
                    mid = int(raw)
                    self.remember_open(signal_id, mid, pos_key=pos_key)
                    return mid
            if bot_id and coin:
                raw = await db.get_setting(f"tg_open_pos:{bot_id}:{coin}")
                if raw and str(raw).isdigit():
                    mid = int(raw)
                    self.remember_open(signal_id, mid, pos_key=pos_key)
                    return mid
        except Exception as e:
            print(f"[TG] resolve_open_message_id: {e}", flush=True)
        return 0

    def fire(self, text: str, parse_mode: str = "HTML") -> None:
        """Fire-and-forget send — never blocks the trading loop or raises.

        Trade signals go straight to the bot's owner chat (TELEGRAM_CHAT_ID).
        There is no separate paid signals channel anymore — signals are free
        and published in the bot itself."""
        if not self.configured:
            return
        self._fire_send(self.chat_id, text, parse_mode)

    def _fire_send(self, chat_id: str, text: str, parse_mode: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._send_to(chat_id, text, parse_mode))
        except RuntimeError:
            def _run():
                asyncio.run(self._send_to(chat_id, text, parse_mode))
            threading.Thread(target=_run, daemon=True).start()
        except Exception:
            pass

    async def _send_to(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        reply_to_message_id=None,
    ) -> int:
        """Send message; return Telegram message_id (0 on failure)."""
        if not self.token:
            return 0
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id:
            try:
                payload["reply_to_message_id"] = int(reply_to_message_id)
                payload["allow_sending_without_reply"] = True
            except (TypeError, ValueError):
                pass
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(url, json=payload)
                    data = resp.json()
                if data.get("ok"):
                    try:
                        return int((data.get("result") or {}).get("message_id") or 0)
                    except (TypeError, ValueError):
                        return 0
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
        print(f"[TG] send FAILED chat={chat_id} text={text[:80]!r}", flush=True)
        return 0

    async def send_trade(self, text: str, parse_mode: str = "HTML", reply_to_message_id=None) -> int:
        """Awaitable trade notify; returns Telegram message_id."""
        if not self.configured:
            return 0
        return await self._send_to(self.chat_id, text, parse_mode, reply_to_message_id=reply_to_message_id)

    # ─── Mini App helpers ───

    def verify_init_data(self, init_data: str, max_age: int = 86400) -> Optional[dict]:
        """Verify a Telegram WebApp initData string against the bot token.

        Returns the parsed payload (incl. ``user``) on success, or None if the
        signature is invalid / expired. Uses the standard Telegram algorithm:
        secret = HMAC_SHA256("WebAppData", bot_token);
        signature = HMAC_SHA256(secret, data_check_string).
        """
        if not init_data or not self.token:
            return None
        try:
            params = dict(parse_qsl(init_data, keep_blank_values=True))
        except Exception:
            return None
        got_hash = params.pop("hash", "")
        if not got_hash:
            return None
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(params.items())
        )
        secret_key = hmac.new(
            b"WebAppData", self.token.encode(), hashlib.sha256
        ).digest()
        computed = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(computed, got_hash):
            return None
        auth_date = params.get("auth_date")
        try:
            if auth_date and time.time() - int(auth_date) > max_age:
                return None
        except (ValueError, TypeError):
            return None
        user = params.get("user")
        if user:
            try:
                params["user"] = json.loads(user)
            except Exception:
                pass
        return params

    async def set_chat_menu_button(self, web_app_url: str, text: str = "Торговый бот") -> bool:
        """Set the bot's chat menu button to open the Mini App. Returns True on success."""
        if not self.token:
            return False
        url = f"https://api.telegram.org/bot{self.token}/setChatMenuButton"
        payload = {
            "menu_button": {
                "type": "web_app",
                "text": text,
                "web_app": {"url": web_app_url},
            }
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload)
                return bool(resp.json().get("ok"))
        except Exception:
            return False

    # ─── Message builders ───

    @staticmethod
    def _arrow(side: str) -> str:
        return "🟢" if side == "long" else "🔴"

    @staticmethod
    def _side_label(side: str) -> str:
        return "LONG" if side == "long" else "SHORT"

    @staticmethod
    def _trade_line(signal_id) -> str:
        """Link message to a trade. Entry/exit/partial/adds of the SAME trade
        share the same signal_id, so Telegram messages can be matched."""
        try:
            sid = int(signal_id or 0)
        except (TypeError, ValueError):
            sid = 0
        return f"Сделка №<b>{sid}</b>" if sid else ""

    @staticmethod
    def _ts_line() -> str:
        msk = datetime.now(timezone.utc).astimezone(
            timezone(timedelta(hours=3)))
        return msk.strftime("🕐 %d.%m %H:%M:%S МСК")

    def _footer(self, signal_id) -> str:
        """Trailing lines shared by every trade message: trade number + time."""
        lines = []
        sid = self._trade_line(signal_id)
        if sid:
            lines.append(sid)
        lines.append(self._ts_line())
        return "\n" + "\n".join(lines)

    @staticmethod
    def _reason_label(reason: str) -> str:
        return {
            "trail_stop": "стоп по трейлингу",
            "rotation_exit": "ротация",
            "exchange_stop": "стоп на бирже",
            "partial_tp": "частичный TP",
            "manual": "вручную",
            "stop_loss": "стоп-лосс",
            "take_profit": "тейк-профит",
        }.get(reason, reason)

    def open_msg(self, coin: str, side: str, price: float, stop: float,
                 size: float, leverage: float, bot_name: str = "",
                 signal_id: int = 0) -> str:
        return (
            f"{self._arrow(side)} <b>ОТКРЫТА ПОЗИЦИЯ</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Бот: <b>{_esc(bot_name)}</b>\n"
            f"Инструмент: <b>{_esc(coin)}</b>\n"
            f"Направление: <b>{self._side_label(side)}</b>\n"
            f"Вход: {_esc(price)}\n"
            f"Стоп: {_esc(stop)}\n"
            f"Размер: {_esc(size)}\n"
            f"Плечо: {_esc(leverage)}x{self._footer(signal_id)}"
        )

    def close_msg(self, coin: str, side: str, entry: float, exit_px: float,
                  pnl: float, reason: str, bot_name: str = "",
                  signal_id: int = 0) -> str:
        icon = "✅" if pnl >= 0 else "❌"
        sign = "+" if pnl >= 0 else ""
        return (
            f"{icon} <b>ЗАКРЫТА ПОЗИЦИЯ</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Бот: <b>{_esc(bot_name)}</b>\n"
            f"Инструмент: <b>{_esc(coin)}</b>\n"
            f"Направление: {self._side_label(side)}\n"
            f"Вход: {_esc(entry)} → Выход: {_esc(exit_px)}\n"
            f"PnL: <b>{sign}{_esc(pnl)} USDT</b>\n"
            f"Причина: {self._reason_label(reason)}{self._footer(signal_id)}"
        )

    def partial_msg(self, coin: str, side: str, entry: float, exit_px: float,
                    pnl: float, closed_sz: float, remaining_sz: float,
                    bot_name: str = "", signal_id: int = 0) -> str:
        sign = "+" if pnl >= 0 else ""
        return (
            f"📌 <b>ЧАСТИЧНЫЙ ТЕЙК</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Бот: <b>{_esc(bot_name)}</b>\n"
            f"Инструмент: <b>{_esc(coin)}</b>\n"
            f"Закрыто: {_esc(closed_sz)} (осталось {_esc(remaining_sz)})\n"
            f"Вход: {_esc(entry)} → Выход: {_esc(exit_px)}\n"
            f"PnL: {sign}{_esc(pnl)} USDT{self._footer(signal_id)}"
        )

    def add_msg(self, coin: str, side: str, price: float, size: float,
                total: float, bot_name: str = "", signal_id: int = 0) -> str:
        return (
            f"⬆️ <b>ДОКУПКА (PYRAMID)</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Бот: <b>{_esc(bot_name)}</b>\n"
            f"Инструмент: <b>{_esc(coin)}</b>\n"
            f"Направление: {self._side_label(side)}\n"
            f"Цена: {_esc(price)}\n"
            f"Докупка: {_esc(size)} (всего {_esc(total)}){self._footer(signal_id)}"
        )
