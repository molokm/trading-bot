"""Send a test message to Telegram to verify notification setup.

Usage:
  python scripts/telegram_test.py
  # or with explicit values:
  python scripts/telegram_test.py --token 123456:ABC --chat-id 123456789
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from dotenv import load_dotenv
from app.services.telegram_notifier import TelegramNotifier

BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
load_dotenv(os.path.join(BACKEND_DIR, ".env"))


async def main():
    parser = argparse.ArgumentParser(description="Test Telegram notification setup")
    parser.add_argument("--token", default=os.getenv("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--chat-id", default=os.getenv("TELEGRAM_CHAT_ID", ""))
    parser.add_argument("--message", default="Тестовое сообщение от торгового бота ✅")
    args = parser.parse_args()

    notifier = TelegramNotifier(token=args.token, chat_id=args.chat_id)
    if not notifier.configured:
        print("Ошибка: не задан TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID.")
        print("  • Token: получите у @BotFather")
        print("  • Chat ID: узнайте через @userinfobot или @getmyid_bot")
        sys.exit(1)

    print(f"Отправка в чат {notifier.chat_id} ...")
    ok = await notifier.send(args.message)
    if ok:
        print("✅ Сообщение отправлено. Проверьте Telegram!")
    else:
        print("❌ Не удалось отправить. Проверьте token и chat_id.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
