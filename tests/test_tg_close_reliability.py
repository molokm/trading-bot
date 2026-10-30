"""Telegram close-notification reliability.

Regression guard for the "opens arrive, closes never do" incident:

* a fire-and-forget ``asyncio.create_task`` keeps only a weak reference, so an
  unretained close task can be garbage-collected before it sends;
* a send that Telegram refuses must be parked and retried instead of lost;
* the public channel must never delay the private owner chat;
* the same close must not be posted twice (plain + reply).
"""
import asyncio
import json

import pytest

from app.bots.ai_discretionary.strategy import AIStrategy
from app.services.telegram_notifier import TelegramNotifier


class _FakeDB:
    def __init__(self):
        self.settings = {}

    async def get_setting(self, key):
        return self.settings.get(key)

    async def set_setting(self, key, value):
        self.settings[key] = value


def _bare_bot(db=None):
    bot = object.__new__(AIStrategy)
    bot.db = db
    bot.BOT_ID = "ai_strategy"
    bot.notifier = None
    bot._tg_tasks = set()
    bot._tg_pending = []
    return bot


def test_close_task_is_retained_until_done():
    bot = _bare_bot()
    ran = []

    async def _run(*a, **k):
        await asyncio.sleep(0)
        ran.append(True)

    bot._notify_close_tg_run = _run

    async def scenario():
        await bot._notify_close_tg(None, "BTC", "stop", 1.0, 2.0)
        assert bot._tg_tasks, "close task must be strongly referenced"
        await asyncio.sleep(0.01)
        assert not bot._tg_tasks, "finished task must be released"

    asyncio.run(scenario())
    assert ran == [True]


def test_failed_close_is_persisted_and_retried():
    db = _FakeDB()
    bot = _bare_bot(db)

    async def scenario():
        await bot._queue_tg_close("<b>close</b>", "demo", "BTC")
        assert len(bot._tg_pending) == 1
        blob = json.loads(db.settings[bot._tg_pending_key()])
        assert blob[0]["coin"] == "BTC"

        # Restart: a fresh bot restores the queue from the settings table.
        fresh = _bare_bot(db)
        await fresh._load_tg_pending()
        assert len(fresh._tg_pending) == 1

        # First flush fails -> item stays.
        class _Fail:
            async def send_trade(self, *a, **k):
                return 0

        fresh.notifier = _Fail()
        await fresh._flush_tg_pending()
        assert len(fresh._tg_pending) == 1

        # Second flush succeeds -> queue cleared and persisted empty.
        class _Ok:
            async def send_trade(self, *a, **k):
                return 42

        fresh.notifier = _Ok()
        await fresh._flush_tg_pending()
        assert fresh._tg_pending == []
        assert json.loads(db.settings[fresh._tg_pending_key()]) == []

    asyncio.run(scenario())


def test_demo_send_hits_owner_chat_before_channel_and_never_duplicates():
    tg = TelegramNotifier(token="tok", chat_id="111", channel_id="-222")
    calls = []

    async def _send_to(target, text, parse_mode="HTML", reply_to_message_id=None):
        calls.append((target, reply_to_message_id))
        return 7 if target == "111" else 8

    tg._send_to = _send_to

    async def scenario():
        mid = await tg.send_trade("hello", account_mode="demo")
        assert mid == 7
        # Owner chat first, channel second — exactly one send each.
        assert [c[0] for c in calls] == ["111", "-222"]
        assert all(c[1] is None for c in calls)

    asyncio.run(scenario())


def test_live_send_never_reaches_public_channel():
    tg = TelegramNotifier(token="tok", chat_id="111", channel_id="-222")
    targets = []

    async def _send_to(target, text, parse_mode="HTML", reply_to_message_id=None):
        targets.append(target)
        return 1

    tg._send_to = _send_to

    async def scenario():
        await tg.send_trade("live close", account_mode="live")

    asyncio.run(scenario())
    assert targets == ["111"]
