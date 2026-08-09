"""Telegram trade notifications for the trading bot.

Sends messages to a Telegram chat via the Bot API. Credentials are read
from the environment (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) and can be
updated at runtime through the dashboard settings endpoint.
"""

import asyncio
import html
import os
import threading
from typing import Optional

import httpx

ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT = "TELEGRAM_CHAT_ID"

_ESCAPE = {ord("<"): "&lt;", ord(">"): "&gt;", ord("&"): "&amp;"}


def _esc(value) -> str:
    """Escape a value for Telegram HTML parse mode."""
    return str(value).translate(_ESCAPE)


class TelegramNotifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token = token or os.getenv(ENV_TOKEN, "")
        self.chat_id = chat_id or os.getenv(ENV_CHAT, "")

    def configure(self, token: str = "", chat_id: str = "") -> None:
        """Update credentials at runtime (used by dashboard settings)."""
        if token:
            self.token = token.strip()
        if chat_id:
            self.chat_id = chat_id.strip()
        os.environ[ENV_TOKEN] = self.token
        os.environ[ENV_CHAT] = self.chat_id

    async def load_from_db(self, db) -> None:
        """Restore credentials persisted in the settings table (survives restarts)."""
        try:
            token = await db.get_setting(ENV_TOKEN)
            chat_id = await db.get_setting(ENV_CHAT)
            if token:
                self.token = token
            if chat_id:
                self.chat_id = chat_id
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

    def fire(self, text: str, parse_mode: str = "HTML") -> None:
        """Fire-and-forget send — never blocks the trading loop or raises."""
        if not self.configured:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.send(text, parse_mode))
        except RuntimeError:
            def _run():
                asyncio.run(self.send(text, parse_mode))
            threading.Thread(target=_run, daemon=True).start()
        except Exception:
            pass

    # ─── Message builders ───

    @staticmethod
    def _arrow(side: str) -> str:
        return "🟢" if side == "long" else "🔴"

    @staticmethod
    def _side_label(side: str) -> str:
        return "LONG" if side == "long" else "SHORT"

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
                 size: float, leverage: float) -> str:
        return (
            f"{self._arrow(side)} <b>ОТКРЫТА ПОЗИЦИЯ</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Инструмент: <b>{_esc(coin)}</b>\n"
            f"Направление: <b>{self._side_label(side)}</b>\n"
            f"Вход: {_esc(price)}\n"
            f"Стоп: {_esc(stop)}\n"
            f"Размер: {_esc(size)}\n"
            f"Плечо: {_esc(leverage)}x"
        )

    def close_msg(self, coin: str, side: str, entry: float, exit_px: float,
                  pnl: float, reason: str) -> str:
        icon = "✅" if pnl >= 0 else "❌"
        sign = "+" if pnl >= 0 else ""
        return (
            f"{icon} <b>ЗАКРЫТА ПОЗИЦИЯ</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Инструмент: <b>{_esc(coin)}</b>\n"
            f"Направление: {self._side_label(side)}\n"
            f"Вход: {_esc(entry)} → Выход: {_esc(exit_px)}\n"
            f"PnL: <b>{sign}{_esc(pnl)} USDT</b>\n"
            f"Причина: {self._reason_label(reason)}"
        )

    def partial_msg(self, coin: str, side: str, entry: float, exit_px: float,
                    pnl: float, closed_sz: float, remaining_sz: float) -> str:
        sign = "+" if pnl >= 0 else ""
        return (
            f"📌 <b>ЧАСТИЧНЫЙ ТЕЙК</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Инструмент: <b>{_esc(coin)}</b>\n"
            f"Закрыто: {_esc(closed_sz)} (осталось {_esc(remaining_sz)})\n"
            f"Вход: {_esc(entry)} → Выход: {_esc(exit_px)}\n"
            f"PnL: {sign}{_esc(pnl)} USDT"
        )

    def add_msg(self, coin: str, side: str, price: float, size: float,
                total: float) -> str:
        return (
            f"⬆️ <b>ДОКУПКА (PYRAMID)</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Инструмент: <b>{_esc(coin)}</b>\n"
            f"Направление: {self._side_label(side)}\n"
            f"Цена: {_esc(price)}\n"
            f"Докупка: {_esc(size)} (всего {_esc(total)})"
        )
