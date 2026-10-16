"""Account isolation: DEMO and LIVE are separate tenants.

All dashboard reads should go through AccountContext so portfolio,
positions, PnL and trades never cross environments.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Awaitable, Literal, Optional

AccountMode = Literal["demo", "live"]


@dataclass(frozen=True)
class AccountContext:
    mode: AccountMode
    account_key: str
    source: str  # showcase | owner | user | guest
    client: Any  # OKXClient

    def tag(self, row: dict) -> dict:
        out = dict(row) if isinstance(row, dict) else {"value": row}
        out["account_mode"] = self.mode
        out["account_key"] = self.account_key
        return out


def mode_from_client(client) -> AccountMode:
    try:
        if client is not None and not getattr(client, "demo", True):
            return "live"
    except Exception:
        pass
    return "demo"


def account_key_for(mode: AccountMode, source: str = "showcase", user_id: str = None) -> str:
    if mode == "live":
        if user_id:
            return f"user:{user_id}"
        return "live"
    return "showcase"


async def call_on_context(ctx: AccountContext, coro_factory: Callable) -> Any:
    """Run OKX coroutine against the context client only."""
    if not ctx or not ctx.client:
        return {"error": True, "message": "API not configured", "account_mode": getattr(ctx, "mode", None)}
    result = await coro_factory(ctx.client)
    if isinstance(result, dict):
        result = dict(result)
        result.setdefault("account_mode", ctx.mode)
        result.setdefault("account_key", ctx.account_key)
        result.setdefault("view_source", ctx.source)
    return result


def filter_rows_for_mode(rows: list, mode: AccountMode) -> list:
    """Drop rows that belong to the other account mode."""
    mode = (mode or "demo").lower()
    out = []
    for tr in rows or []:
        if not isinstance(tr, dict):
            continue
        m = (tr.get("account_mode") or tr.get("mode") or "").strip().lower()
        if m in ("live", "demo"):
            if m == mode:
                out.append(tr)
            continue
        # untagged legacy → demo only
        if mode == "demo":
            out.append(tr)
    return out
