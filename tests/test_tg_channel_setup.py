"""Signals-channel setup that needs no hosting-panel edits.

Telegram tells the bot when it is added to a channel. Before, the channel id had
to be typed into the Render env by hand; a stale id silently dropped every
channel post ("chat not found"), and nothing in the app said why.
"""
import asyncio

import pytest

from app.services import telegram_notifier as tgmod
from app.services.telegram_bot import TelegramBotPoller
from app.services.telegram_notifier import TelegramNotifier


@pytest.fixture(autouse=True)
def _clean_channel_env(monkeypatch):
    """Each poller must start without a channel inherited from a prior test."""
    monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)
    monkeypatch.setattr(TelegramNotifier, "_channel_cache", {})
    yield


class _DB:
    def __init__(self):
        self.settings = {}

    async def get_setting(self, key):
        return self.settings.get(key)

    async def set_setting(self, key, value):
        self.settings[key] = value


OWNER = "111"
CHANNEL = -1001234567890


def _poller(db=None, chat=OWNER):
    tg = TelegramNotifier(token="tok", chat_id=chat)
    p = TelegramBotPoller(notifier=tg, db=db)
    p.api_calls = []
    p.sent = []

    async def _api(method, **payload):
        p.api_calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 1}}

    async def _send_msg(chat_id, text, parse_mode="HTML", reply_markup=None):
        p.sent.append((chat_id, text))
        return {"ok": True}

    p._api = _api
    p._send_msg = _send_msg
    return p


def _my_chat_member(status, cid=CHANNEL, ctype="channel", title="Rotation Signals"):
    return {
        "chat": {"id": cid, "type": ctype, "title": title},
        "new_chat_member": {"status": status},
    }


def test_bot_added_to_a_channel_is_adopted_and_persisted():
    db = _DB()
    p = _poller(db)

    asyncio.run(p._handle_update({"my_chat_member": _my_chat_member("administrator")}))

    assert p.notifier.channel_id == str(CHANNEL)
    assert db.settings["TELEGRAM_CHANNEL_ID"] == str(CHANNEL)
    # Owner is told, and a real probe post goes to the channel.
    assert p.sent and "Канал подключён" in p.sent[0][1]
    assert any(str(c[1].get("chat_id")) == str(CHANNEL)
               for c in p.api_calls if c[0] == "sendMessage")


def test_bot_added_to_a_group_does_not_touch_the_channel():
    p = _poller()
    asyncio.run(p._handle_update(
        {"my_chat_member": _my_chat_member("administrator", cid=-500, ctype="group")}))
    assert p.notifier.channel_id == ""
    assert p.sent == []


def test_group_added_by_the_owner_is_offered_but_not_adopted():
    """Anyone can add a bot to a group — signals must not leak there silently."""
    p = _poller()
    upd = _my_chat_member("member", cid=-500, ctype="supergroup", title="My signals")
    upd["from"] = {"id": int(OWNER)}
    asyncio.run(p._handle_update({"my_chat_member": upd}))
    assert p.notifier.channel_id == ""
    assert p.sent and "Бот добавлен в группу" in p.sent[0][1]
    assert "/connect" in p.sent[0][1]


def test_connect_in_a_group_makes_it_the_signals_target():
    db = _DB()
    p = _poller(db)
    upd = _my_chat_member("administrator", cid=-500, ctype="supergroup", title="My signals")
    upd["from"] = {"id": int(OWNER)}
    asyncio.run(p._handle_update({"my_chat_member": upd}))
    p.sent.clear()

    asyncio.run(p._handle_update({
        "message": {"chat": {"id": -500, "type": "supergroup", "title": "My signals"},
                    "from": {"id": int(OWNER)},
                    "text": "/connect@RotationTradeBot"}
    }))
    assert p.notifier.channel_id == "-500"
    assert db.settings["TELEGRAM_CHANNEL_ID"] == "-500"
    assert p.sent and "Группа подключена" in p.sent[0][1]
    # A probe post goes into the group itself, not only to the owner DM.
    assert any(str(c[1].get("chat_id")) == "-500"
               for c in p.api_calls if c[0] == "sendMessage")


def test_connect_in_a_group_is_owner_only():
    p = _poller()
    asyncio.run(p._handle_update({
        "message": {"chat": {"id": -500, "type": "group", "title": "strangers"},
                    "from": {"id": 999},
                    "text": "/connect"}
    }))
    assert p.notifier.channel_id == ""
    assert p.sent and "владелец" in p.sent[0][1]


def test_plain_group_chatter_gets_no_reply():
    p = _poller()
    asyncio.run(p._handle_update({
        "message": {"chat": {"id": -500, "type": "group", "title": "g"},
                    "from": {"id": 999}, "text": "hello everyone"}
    }))
    assert p.sent == []


def test_channel_migrated_to_a_supergroup_keeps_posting():
    p = _poller()
    p.notifier.channel_id = str(CHANNEL)
    asyncio.run(p._handle_update({
        "message": {"chat": {"id": CHANNEL, "type": "channel"},
                    "migrate_to_chat_id": -100777}
    }))
    assert p.notifier.channel_id == "-100777"


def test_bot_kicked_from_the_channel_clears_the_setting():
    db = _DB()
    p = _poller(db)
    p.notifier.channel_id = str(CHANNEL)

    asyncio.run(p._handle_update({"my_chat_member": _my_chat_member("kicked")}))

    assert p.notifier.channel_id == ""
    assert db.settings["TELEGRAM_CHANNEL_ID"] == ""
    assert p.sent and "отключён" in p.sent[0][1]


def test_channel_post_is_enough_to_adopt_the_channel():
    p = _poller()
    asyncio.run(p._handle_update({
        "channel_post": {"chat": {"id": CHANNEL, "type": "channel", "title": "X"}, "text": "hi"}
    }))
    assert p.notifier.channel_id == str(CHANNEL)


def test_forwarded_post_from_the_channel_is_adopted():
    p = _poller()
    asyncio.run(p._handle_update({
        "message": {
            "chat": {"id": int(OWNER), "type": "private"},
            "forward_from_chat": {"id": CHANNEL, "type": "channel", "title": "X"},
            "text": "forwarded",
        }
    }))
    assert p.notifier.channel_id == str(CHANNEL)


def test_owner_can_set_the_channel_by_username():
    p = _poller()

    async def _resolve(ref):
        assert ref == "@rotation_signals"
        return {"id": CHANNEL, "type": "channel", "title": "Rotation Signals"}

    p.notifier.resolve_chat = _resolve
    asyncio.run(p._handle_update({
        "message": {"chat": {"id": int(OWNER), "type": "private"},
                    "text": "/setchannel @rotation_signals"}
    }))
    assert p.notifier.channel_id == str(CHANNEL)


def test_owner_can_send_a_channel_link_instead_of_a_command():
    p = _poller()

    async def _resolve(ref):
        assert ref == "@rotation_signals"
        return {"id": CHANNEL, "type": "channel", "title": "Rotation Signals"}

    p.notifier.resolve_chat = _resolve
    asyncio.run(p._handle_update({
        "message": {"chat": {"id": int(OWNER), "type": "private"},
                    "text": "https://t.me/rotation_signals/42"}
    }))
    assert p.notifier.channel_id == str(CHANNEL)


def test_channel_status_is_available_in_a_group_for_the_owner():
    p = _poller()
    p.notifier.channel_id = str(CHANNEL)

    async def _resolve(ref):
        return {"id": CHANNEL, "type": "channel", "title": "Rotation Signals"}

    async def _get(method, **params):
        if method == "getMe":
            return {"ok": True, "result": {"id": 42}}
        if method == "getChatMember":
            return {"ok": True, "result": {"status": "administrator"}}
        return {"ok": False, "description": "no"}

    p.notifier.resolve_chat = _resolve
    p.notifier._api_get = _get
    asyncio.run(p._handle_update({
        "message": {"chat": {"id": -500, "type": "supergroup", "title": "g"},
                    "from": {"id": int(OWNER)}, "text": "/channel"}
    }))
    assert p.sent and "публикуются" in p.sent[0][1].lower()


def test_channel_commands_are_owner_only():
    p = _poller()
    asyncio.run(p._handle_update({
        "message": {"chat": {"id": 999, "type": "private"}, "text": "/setchannel @evil"}
    }))
    assert p.notifier.channel_id == ""
    assert p.sent and "только владельцу" in p.sent[0][1]


def test_channel_status_explains_a_missing_admin_right():
    p = _poller()

    async def _resolve(ref):
        return {"id": CHANNEL, "type": "channel", "title": "Rotation Signals"}

    async def _get(method, **params):
        if method == "getMe":
            return {"ok": True, "result": {"id": 42}}
        if method == "getChatMember":
            return {"ok": True, "result": {"status": "member"}}
        return {"ok": False, "description": "no"}

    p.notifier.channel_id = str(CHANNEL)
    p.notifier.resolve_chat = _resolve
    p.notifier._api_get = _get

    asyncio.run(p._handle_update({
        "message": {"chat": {"id": int(OWNER), "type": "private"}, "text": "/channel"}
    }))
    assert p.sent and "недоступен" in p.sent[0][1]
    assert "администратор" in p.sent[0][1]


def test_disabled_channel_beats_the_env_after_a_restart(monkeypatch):
    """`/unsetchannel` must not be undone by a deploy re-reading the env."""
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", str(CHANNEL))
    db = _DB()
    db.settings["TELEGRAM_CHANNEL_ID"] = ""

    tg = TelegramNotifier(token="tok", chat_id=OWNER)
    assert tg.channel_id == str(CHANNEL)  # boots from the hosting env
    asyncio.run(tg.load_from_db(db))
    assert tg.channel_id == ""  # …then the explicit «off» in the DB wins


def test_unsetchannel_persists_an_explicit_empty_value():
    db = _DB()
    p = _poller(db)
    p.notifier.channel_id = str(CHANNEL)

    asyncio.run(p._handle_update({
        "message": {"chat": {"id": int(OWNER), "type": "private"}, "text": "/unsetchannel"}
    }))
    assert p.notifier.channel_id == ""
    assert db.settings["TELEGRAM_CHANNEL_ID"] == ""


# ── notifier: permanent errors must not be retried ──────────────────────
class _Resp:
    status_code = 400
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    @property
    def text(self):
        return str(self._payload)

    def json(self):
        return self._payload


def test_permanent_send_error_is_not_retried(monkeypatch):
    calls = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append(json)
            return _Resp({"ok": False, "description": "Bad Request: chat not found"})

    monkeypatch.setattr(tgmod.httpx, "AsyncClient", _Client)
    tg = TelegramNotifier(token="tok", chat_id="111")

    mid = asyncio.run(tg._send_to("-10099", "hello"))
    assert mid == 0
    assert len(calls) == 1  # no 3-attempt backoff on a dead chat


def test_transient_send_error_is_still_retried(monkeypatch):
    calls = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append(json)
            if len(calls) == 1:
                return _Resp({"ok": False, "description": "Internal Server Error"})
            return _Resp({"ok": True, "result": {"message_id": 5}})

    monkeypatch.setattr(tgmod.httpx, "AsyncClient", _Client)
    tg = TelegramNotifier(token="tok", chat_id="111")

    mid = asyncio.run(tg._send_to("111", "hello"))
    assert mid == 5
    assert len(calls) == 2
