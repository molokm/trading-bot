"""Проверка прав ключа OKX по /api/v5/account/config.

Аудит 29.10.2026: `check_key_permissions` бил в `/api/v5/account/api-key`,
которого у OKX нет → «Not Found» → `ok=False` всегда. Из-за этого:
  * защита «read-only ключ нельзя подключить» не срабатывала ни разу;
  * `/api/health` сначала показывал `key_trade=false` (маскировка под «нет
    права»), после уточнения — `null` (проверка не удалась).

`perm` живёт в `/api/v5/account/config` (например "read_only", "read,trade").
"""
import pytest

from app.services.okx_client import OKXClient


def _client_with(data, error=None):
    c = object.__new__(OKXClient)
    c.api_key = "k"

    async def _request(method, path, params=None, body=None):
        c._last = (method, path, params)
        if error is not None:
            return {"error": True, "message": error}
        return {"data": data}

    c._request = _request
    return c


@pytest.mark.asyncio
async def test_read_only_key_is_not_trade():
    c = _client_with([{"perm": "read_only", "posMode": "net_mode"}])
    p = await c.check_key_permissions()

    assert p["ok"] is True
    assert p["has_trade"] is False
    assert p["has_read"] is True
    assert c._last[1] == "/api/v5/account/config"


@pytest.mark.asyncio
async def test_read_trade_key_can_trade():
    c = _client_with([{"perm": "read,trade", "posMode": "long_short_mode"}])
    p = await c.check_key_permissions()

    assert p["ok"] is True
    assert p["has_trade"] is True
    assert c.pos_mode == "hedge"


@pytest.mark.asyncio
async def test_trade_only_key_has_no_read():
    c = _client_with([{"perm": "trade"}])
    p = await c.check_key_permissions()

    assert p["ok"] is True
    assert p["has_trade"] is True
    assert p["has_read"] is False


@pytest.mark.asyncio
async def test_probe_failure_is_unknown_not_allowed():
    c = _client_with([], error="Not Found")
    p = await c.check_key_permissions()

    assert p["ok"] is False
    assert p["perm"] is None
    assert "has_trade" not in p
