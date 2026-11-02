"""LIVE mirror observability: keep the audit trail, tell the owner about gaps.

Context (31 Oct): a DEMO LTC short lived 2h54m without a LIVE twin. Nothing was
logged anywhere durable — the mirror trail was wiped on every restart — so the
question «почему сделка не дошла до зеркала» had no answer. These tests pin:

* the persisted trail survives a restart (and an empty in-memory buffer must not
  overwrite it);
* a DEMO position without a LIVE twin for >10 min produces exactly one owner
  message, and only if it was opened while the mirror was armed.
"""
import asyncio
import json
import time
from types import SimpleNamespace

from app.bots.ai_discretionary.strategy import AIStrategy


class _DB:
    def __init__(self):
        self.settings = {}

    async def get_setting(self, key):
        return self.settings.get(key)

    async def set_setting(self, key, value):
        self.settings[key] = value


def _bot(db=None):
    bot = object.__new__(AIStrategy)
    bot.BOT_ID = "ai_strategy"
    bot.db = db
    bot._live_audit = []
    bot._live_positions = {}
    bot._mirror_gap_since = {}
    bot._mirror_gap_alerted = {}
    bot._last_mirror_fail = "order blocked: insufficient margin"
    bot._exec_log = []
    return bot


def _pos(side="short", opened_at="2026-10-31T12:20:19+00:00"):
    return SimpleNamespace(side=side, opened_at=opened_at, size=1.0)


# ── the persisted trail survives restarts ───────────────────────────────

def test_audit_is_restored_from_the_db():
    db = _DB()
    db.settings["live_audit:ai_strategy_live"] = json.dumps([
        {"action": "open_blocked", "coin": "LTC", "reason": "order blocked: 51008"},
    ])
    bot = _bot(db)

    asyncio.run(bot._load_live_audit())

    assert bot._live_audit[0]["coin"] == "LTC"
    assert "51008" in bot._live_audit[0]["reason"]


def test_empty_memory_does_not_wipe_the_stored_trail():
    db = _DB()
    db.settings["live_audit:ai_strategy_live"] = json.dumps([{"action": "open", "coin": "BTC"}])
    bot = _bot(db)
    bot._live_positions = {}
    bot._live_audit = []  # fresh process, nothing loaded yet

    asyncio.run(bot._persist_live_async())

    assert json.loads(db.settings["live_audit:ai_strategy_live"])[0]["coin"] == "BTC"


# ── gap alert ───────────────────────────────────────────────────────────

def test_unmirrored_demo_position_alerts_the_owner_once(monkeypatch):
    bot = _bot(_DB())
    bot.notifier = SimpleNamespace(sent=[])
    audited = []

    async def _send(text, **k):
        bot.notifier.sent.append(text)
        return True

    async def _enabled():
        return True

    async def _watermark():
        return 0

    bot.notifier.send = _send
    bot._mirror_enabled = _enabled
    bot._live_clone_watermark = _watermark
    bot._audit_live = lambda *a, **k: audited.append(a)
    bot._positions = {"LTC": _pos()}
    bot._mirror_gap_since = {"LTC": time.time() - 700}  # 11m40s without a twin

    asyncio.run(bot._mirror_gap_watch(["LTC"]))
    asyncio.run(bot._mirror_gap_watch(["LTC"]))  # second tick must not repeat

    assert len(bot.notifier.sent) == 1
    text = bot.notifier.sent[0]
    assert "DEMO без зеркала" in text and "LTC" in text
    assert "insufficient margin" in text
    assert [a[0] for a in audited] == ["gap_alert"]


def test_young_gap_does_not_alert():
    bot = _bot(_DB())
    bot.notifier = SimpleNamespace(sent=[])

    async def _send(text, **k):
        bot.notifier.sent.append(text)
        return True

    async def _enabled():
        return True

    async def _watermark():
        return 0

    bot.notifier.send = _send
    bot._mirror_enabled = _enabled
    bot._live_clone_watermark = _watermark
    bot._positions = {"LTC": _pos()}

    asyncio.run(bot._mirror_gap_watch(["LTC"]))
    assert bot.notifier.sent == []
    assert "LTC" in bot._mirror_gap_since  # timer started


def test_position_opened_before_connect_is_not_a_gap():
    bot = _bot(_DB())
    bot.notifier = SimpleNamespace(sent=[])

    async def _send(text, **k):
        bot.notifier.sent.append(text)
        return True

    async def _enabled():
        return True

    async def _watermark():
        # Mirror armed on 1 Nov: the 31 Oct DEMO position is nobody's failure.
        return 1793600000

    bot.notifier.send = _send
    bot._mirror_enabled = _enabled
    bot._live_clone_watermark = _watermark
    bot._positions = {"LTC": _pos(opened_at="2026-10-31T12:20:19+00:00")}
    bot._mirror_gap_since = {"LTC": time.time() - 700}

    asyncio.run(bot._mirror_gap_watch(["LTC"]))
    assert bot.notifier.sent == []
    assert "LTC" not in bot._mirror_gap_since


def test_gap_is_forgotten_once_the_twin_appears():
    bot = _bot(_DB())
    bot._positions = {"LTC": _pos()}
    bot._mirror_gap_since = {"LTC": time.time() - 700}
    bot._mirror_gap_alerted = {"LTC": time.time()}

    async def _enabled():
        return True

    async def _watermark():
        return 0

    bot._mirror_enabled = _enabled
    bot._live_clone_watermark = _watermark

    asyncio.run(bot._mirror_gap_watch([]))
    assert bot._mirror_gap_since == {} and bot._mirror_gap_alerted == {}
