"""Реконнект LIVE разблокирует зеркало.

Инцидент 29.10.2026: пользователь пересоздал LIVE-ключ с правом Trade и
«переподключил зеркало», но бот остался заблокирован — «липкий» флаг
`_live_trade_blocked_reason` снимался ТОЛЬКО когда реальный LIVE-ордер проходил,
а он не мог пройти, пока флаг стоял. Замкнутый круг: зеркало мертво навсегда,
даже с исправленным ключом.

Фиксируем: успешный реконнект (и авто-восстановление клиента) со
проверенным правом Trade сбрасывает флаг.
"""
import inspect

import pytest


def test_live_connect_clears_the_sticky_flag_when_key_can_trade():
    import app.main as m

    src = inspect.getsource(m.live_connect)
    assert "_live_trade_blocked_reason" in src
    assert "_live_key_perms.get('has_trade')" in src


@pytest.mark.asyncio
async def test_ensure_live_mirror_client_clears_the_flag(monkeypatch):
    import app.main as m

    class _DB:
        async def get_setting(self, name):
            return {
                "live_mirror_enabled": "1",
                "live_mirror_capital": "500",
                "live_mirror_connected_at": "1",
            }.get(name)

        async def set_setting(self, name, value):
            return True

    class _Client:
        demo = False

        def has_credentials(self):
            return True

    class _Mgr:
        async def init_client(self, *a, **k):
            return True

        def get_client(self):
            return _Client()

    class _Bot:
        live_client_manager = None
        _live_capital = 0.0
        _live_trade_blocked_reason = "order blocked: This API Key does not have trading permission"

    async def _creds():
        m._live_key, m._live_secret, m._live_pass = "k", "s", "p"

    async def _perms(client, label=""):
        return {"ok": True, "has_trade": True, "has_read": True}

    monkeypatch.setattr(m, "db", _DB())
    monkeypatch.setattr(m, "live_manager", _Mgr())
    monkeypatch.setattr(m, "_load_live_creds_from_db", _creds)
    monkeypatch.setattr(m, "_verify_key_perms", _perms)
    bot = _Bot()
    monkeypatch.setattr(m, "ai_bot", bot)

    ok = await m._ensure_live_mirror_client()

    assert ok is True
    assert bot._live_trade_blocked_reason == ""


@pytest.mark.asyncio
async def test_ensure_keeps_the_flag_when_key_still_cannot_trade(monkeypatch):
    import app.main as m

    class _DB:
        async def get_setting(self, name):
            return {"live_mirror_enabled": "1"}.get(name)

        async def set_setting(self, name, value):
            return True

    class _Client:
        demo = False

        def has_credentials(self):
            return True

    class _Mgr:
        async def init_client(self, *a, **k):
            return True

        def get_client(self):
            return _Client()

    class _Bot:
        live_client_manager = None
        _live_capital = 0.0
        _live_trade_blocked_reason = "stuck"

    async def _creds():
        m._live_key, m._live_secret, m._live_pass = "k", "s", "p"

    async def _perms(client, label=""):
        return {"ok": True, "has_trade": False, "has_read": True}

    monkeypatch.setattr(m, "db", _DB())
    monkeypatch.setattr(m, "live_manager", _Mgr())
    monkeypatch.setattr(m, "_load_live_creds_from_db", _creds)
    monkeypatch.setattr(m, "_verify_key_perms", _perms)
    bot = _Bot()
    monkeypatch.setattr(m, "ai_bot", bot)

    await m._ensure_live_mirror_client()

    # Read-only key → keep the guard, do not pretend it is fixed.
    assert bot._live_trade_blocked_reason == "stuck"


@pytest.mark.asyncio
async def test_ensure_clears_the_flag_when_probe_is_inconclusive(monkeypatch):
    """Probe failure (ok=False) is 'unknown', not 'read-only'.

    A Trade-only key (no Read) makes `/api/v5/account/api-key` fail, yet it can
    place orders. Keeping the sticky guard here would resurrect the deadlock.
    """
    import app.main as m

    class _DB:
        async def get_setting(self, name):
            return {"live_mirror_enabled": "1"}.get(name)

        async def set_setting(self, name, value):
            return True

    class _Client:
        demo = False

        def has_credentials(self):
            return True

    class _Mgr:
        async def init_client(self, *a, **k):
            return True

        def get_client(self):
            return _Client()

    class _Bot:
        live_client_manager = None
        _live_capital = 0.0
        _live_trade_blocked_reason = "stuck"

    async def _creds():
        m._live_key, m._live_secret, m._live_pass = "k", "s", "p"

    async def _perms(client, label=""):
        return {"ok": False, "error": "50111 permission"}

    monkeypatch.setattr(m, "db", _DB())
    monkeypatch.setattr(m, "live_manager", _Mgr())
    monkeypatch.setattr(m, "_load_live_creds_from_db", _creds)
    monkeypatch.setattr(m, "_verify_key_perms", _perms)
    bot = _Bot()
    monkeypatch.setattr(m, "ai_bot", bot)

    await m._ensure_live_mirror_client()

    assert bot._live_trade_blocked_reason == ""
