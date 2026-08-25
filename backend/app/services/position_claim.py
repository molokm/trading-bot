"""Position ownership claims + anti-orphan policy.

Rule: every exchange open from a bot MUST have a DB claim.
If claim fails after fill → flatten the position immediately.
Periodic sweeper closes OKX positions that no bot claims.
"""
from __future__ import annotations
import logging
import os
from typing import Optional

log = logging.getLogger("position_claim")


def norm_side(side: str) -> str:
    s = (side or "long").lower()
    if s in ("sell", "s", "short"):
        return "short"
    return "long"


def orphan_close_enabled() -> bool:
    v = (os.getenv("ORPHAN_CLOSE") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


async def claim_open(db, bot_id: str, inst_id: str, side: str, size: float, entry: float) -> bool:
    if not db or not inst_id or size <= 0 or entry <= 0:
        return False
    side_n = norm_side(side)
    try:
        if hasattr(db, "ensure_bot"):
            try:
                await db.ensure_bot(bot_id, strategy_id=bot_id, name=bot_id)
            except Exception:
                pass
        await db.claim_position(bot_id, inst_id, side_n, size, entry)
        # verify row exists
        row = None
        if hasattr(db, "find_position_any_side"):
            row = await db.find_position_any_side(bot_id, inst_id, side_n)
        elif hasattr(db, "find_position"):
            row = await db.find_position(bot_id, inst_id, side_n)
        if not row:
            await db.save_position(
                bot_id=bot_id, inst_id=inst_id, side=side_n,
                size=float(size), entry_price=float(entry),
            )
            row = await db.find_position(bot_id, inst_id, side_n) if hasattr(db, "find_position") else True
        return bool(row)
    except Exception as e:
        log.warning("claim_open failed %s %s: %s", bot_id, inst_id, e)
        try:
            await db.save_position(
                bot_id=bot_id, inst_id=inst_id, side=side_n,
                size=float(size), entry_price=float(entry),
            )
            return True
        except Exception as e2:
            log.error("claim_open retry failed %s: %s", inst_id, e2)
            return False


async def release_open(db, bot_id: str, inst_id: str = None, side: str = None) -> None:
    if not db:
        return
    try:
        if inst_id and hasattr(db, "delete_position_inst"):
            await db.delete_position_inst(bot_id, inst_id, norm_side(side) if side else None)
        else:
            await db.delete_position(bot_id)
    except Exception as e:
        log.warning("release_open %s: %s", bot_id, e)


async def flatten_position(client, inst_id: str, side: str, size: float) -> dict:
    """Market close a position (anti-orphan)."""
    if not client or not inst_id or size <= 0:
        return {"error": "bad args"}
    side_n = norm_side(side)
    close_side = "sell" if side_n == "long" else "buy"
    try:
        # try hedge posSide first, then net
        for ps in (side_n, "net", None):
            params = {
                "inst_id": inst_id,
                "side": close_side,
                "ord_type": "market",
                "sz": str(size),
                "td_mode": "cross",
            }
            if ps:
                params["pos_side"] = ps
            try:
                resp = await client.place_order(**params)
            except TypeError:
                resp = await client.place_order(
                    inst_id, close_side, "market", str(size), "cross", ps or "net"
                )
            if not resp.get("error"):
                log.warning("ORPHAN flatten ok %s %s sz=%s", inst_id, side_n, size)
                return resp
        return resp
    except Exception as e:
        log.error("ORPHAN flatten failed %s: %s", inst_id, e)
        return {"error": str(e)}


async def claim_or_flatten(db, client, bot_id: str, inst_id: str, side: str,
                           size: float, entry: float) -> bool:
    """Claim ownership; if DB claim fails, close on exchange so no silent orphans."""
    ok = await claim_open(db, bot_id, inst_id, side, size, entry)
    if ok:
        return True
    log.error("CLAIM FAILED after fill %s %s — flattening to prevent orphan", bot_id, inst_id)
    if client and orphan_close_enabled():
        await flatten_position(client, inst_id, side, size)
    return False


async def sweep_exchange_orphans(client, db, memory_keys: set = None) -> list:
    """Close OKX positions that are not claimed by any bot and not in live memory.

    memory_keys: set of (inst_id, side) currently managed in any bot._positions
    """
    if not client or not orphan_close_enabled():
        return []
    memory_keys = memory_keys or set()
    closed = []
    try:
        result = await client.get_positions("SWAP")
        if result.get("error") or not result.get("data"):
            return []
        claimed = set()
        if db:
            try:
                rows = await db.get_all_positions()
                for r in rows or []:
                    iid = r.get("inst_id") or ""
                    sd = norm_side(r.get("side") or "long")
                    if iid:
                        claimed.add((iid, sd))
                        claimed.add((iid, "net"))
            except Exception as e:
                log.warning("sweep claim map: %s", e)
        for p in result.get("data") or []:
            inst_id = p.get("instId") or ""
            if not inst_id:
                continue
            pos_side = (p.get("posSide") or "net").lower()
            side = "short" if pos_side == "short" else "long"
            try:
                sz = abs(float(p.get("pos") or 0))
            except (TypeError, ValueError):
                sz = 0.0
            if sz <= 0:
                continue
            key = (inst_id, side)
            if key in memory_keys or (inst_id, "net") in memory_keys:
                continue
            if key in claimed or (inst_id, norm_side(pos_side)) in claimed:
                continue
            # unclaimed orphan
            log.warning("ORPHAN detected %s %s sz=%s — closing", inst_id, side, sz)
            resp = await flatten_position(client, inst_id, side, sz)
            closed.append({"inst_id": inst_id, "side": side, "size": sz, "resp": str(resp)[:120]})
    except Exception as e:
        log.error("sweep_exchange_orphans: %s", e)
    return closed
