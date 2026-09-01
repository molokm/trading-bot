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

    def remember_open(self, signal_id, message_id, pos_key: str = "", coin: str = "") -> None:
        try:
            sid = int(signal_id or 0)
            mid = int(message_id or 0)
        except (TypeError, ValueError):
            return
        if not mid:
            return
        if sid:
            TelegramNotifier._open_msg_by_signal[sid] = mid
        if pos_key:
            TelegramNotifier._open_msg_by_signal[f"pos:{pos_key}"] = mid
        coin = (coin or "").upper().replace("-USDT-SWAP", "").strip()
        if coin:
            TelegramNotifier._open_msg_by_signal[f"coin:{coin}"] = mid
        if len(TelegramNotifier._open_msg_by_signal) > 800:
            for k in list(TelegramNotifier._open_msg_by_signal.keys())[:150]:
                TelegramNotifier._open_msg_by_signal.pop(k, None)

    def open_message_id(self, signal_id=0, pos_key: str = "", coin: str = "") -> int:
        try:
            sid = int(signal_id or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid:
            mid = int(TelegramNotifier._open_msg_by_signal.get(sid) or 0)
            if mid:
                return mid
        if pos_key:
            mid = int(TelegramNotifier._open_msg_by_signal.get(f"pos:{pos_key}") or 0)
            if mid:
                return mid
        coin = (coin or "").upper().replace("-USDT-SWAP", "").strip()
        if coin:
            mid = int(TelegramNotifier._open_msg_by_signal.get(f"coin:{coin}") or 0)
            if mid:
                return mid
        return 0

    async def remember_open_db(self, db, signal_id, message_id, bot_id: str = "", coin: str = "") -> None:
        """Persist open Telegram message_id so close can reply after restart."""
        coin_u = (coin or "").upper().replace("-USDT-SWAP", "").strip()
        pos_key = f"{bot_id}:{coin_u}" if bot_id and coin_u else ""
        self.remember_open(signal_id, message_id, pos_key=pos_key, coin=coin_u)
        if not db:
            return
        try:
            mid = int(message_id or 0)
            if not mid:
                return
            if signal_id:
                await db.set_setting(f"tg_open_msg:{int(signal_id)}", str(mid))
            if bot_id and coin_u:
                await db.set_setting(f"tg_open_pos:{bot_id}:{coin_u}", str(mid))
            if coin_u:
                # last-resort key after redeploy when signal_id is lost
                await db.set_setting(f"tg_open_coin:{coin_u}", str(mid))
            print(f"[TG] remembered open mid={mid} signal={signal_id} bot={bot_id} coin={coin_u}", flush=True)
        except Exception as e:
            print(f"[TG] remember_open_db: {e}", flush=True)

    async def resolve_open_message_id(self, db, signal_id=0, bot_id: str = "", coin: str = "") -> int:
        """Memory first, then DB settings (signal → bot:coin → coin)."""
        coin_u = (coin or "").upper().replace("-USDT-SWAP", "").strip()
        pos_key = f"{bot_id}:{coin_u}" if bot_id and coin_u else ""
        mid = self.open_message_id(signal_id, pos_key=pos_key, coin=coin_u)
        if mid:
            return mid
        if not db:
            return 0
        try:
            keys = []
            if signal_id:
                keys.append(f"tg_open_msg:{int(signal_id)}")
            if bot_id and coin_u:
                keys.append(f"tg_open_pos:{bot_id}:{coin_u}")
            if coin_u:
                keys.append(f"tg_open_coin:{coin_u}")
            for key in keys:
                raw = await db.get_setting(key)
                if raw and str(raw).strip().isdigit():
                    mid = int(str(raw).strip())
                    self.remember_open(signal_id, mid, pos_key=pos_key, coin=coin_u)
                    print(f"[TG] resolved open mid={mid} via {key}", flush=True)
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
        """Send message; return Telegram message_id (0 on failure).

        When reply_to is set we require a real reply first. Only if Telegram
        rejects the reply (message not found) we resend without reply and log.
        """
        if not self.token:
            return 0
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        reply_id = None
        if reply_to_message_id:
            try:
                reply_id = int(reply_to_message_id)
            except (TypeError, ValueError):
                reply_id = None

        async def _post(payload: dict) -> tuple:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload)
                try:
                    data = resp.json()
                except Exception:
                    data = {"ok": False, "description": resp.text[:200]}
                return data

        base = {"chat_id": chat_id, "text": text}
        if parse_mode:
            base["parse_mode"] = parse_mode

        # Pass 1: with reply (no allow_sending_without_reply — we want a real thread)
        if reply_id:
            payload = {**base, "reply_to_message_id": reply_id}
            for attempt in range(2):
                try:
                    data = await _post(payload)
                    if data.get("ok"):
                        try:
                            return int((data.get("result") or {}).get("message_id") or 0)
                        except (TypeError, ValueError):
                            return 0
                    desc = str(data.get("description") or data)
                    print(f"[TG] reply FAILED mid={reply_id}: {desc}", flush=True)
                    # permanent reply errors → fall through to plain send
                    low = desc.lower()
                    if "reply" in low or "message to be replied" in low or "not found" in low:
                        break
                    if attempt < 1:
                        await asyncio.sleep(1)
                except Exception as e:
                    print(f"[TG] reply exception: {e}", flush=True)
                    if attempt < 1:
                        await asyncio.sleep(1)

        # Pass 2: plain message (open messages, or close fallback)
        for attempt in range(3):
            try:
                data = await _post(base)
                if data.get("ok"):
                    if reply_id:
                        print(f"[TG] sent CLOSE without reply (open mid={reply_id} missing)", flush=True)
                    try:
                        return int((data.get("result") or {}).get("message_id") or 0)
                    except (TypeError, ValueError):
                        return 0
                print(f"[TG] send not ok: {data.get('description')}", flush=True)
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
            except Exception as e:
                print(f"[TG] send exception: {e}", flush=True)
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
