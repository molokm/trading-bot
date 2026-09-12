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


async def persist_open_snapshot(db, bot_id: str, positions: list) -> None:
    """Save open list for bot_id into settings (survives trade-table wipes)."""
    if not db or not bot_id:
        return
    try:
        import json
        payload = []
        for p in positions or []:
            if isinstance(p, dict):
                payload.append({
                    "coin": p.get("coin") or "",
                    "inst_id": p.get("inst_id") or p.get("instId") or "",
                    "side": norm_side(p.get("side") or "long"),
                    "size": float(p.get("size") or 0),
                    "entry_price": float(p.get("entry_price") or p.get("entry") or 0),
                })
            else:
                payload.append({
                    "coin": getattr(p, "coin", "") or "",
                    "inst_id": getattr(p, "inst_id", "") or getattr(p, "symbol", "") or "",
                    "side": norm_side(getattr(p, "side", "long")),
                    "size": float(getattr(p, "size", 0) or 0),
                    "entry_price": float(getattr(p, "entry_price", 0) or 0),
                })
        await db.set_setting(f"open_positions:{bot_id}", json.dumps(payload))
    except Exception as e:
        log.warning("persist_open_snapshot %s: %s", bot_id, e)


async def upsert_snapshot_position(db, bot_id: str, inst_id: str, side: str, size: float, entry: float) -> None:
    """Merge one open into durable snapshot."""
    if not db or not bot_id or not inst_id:
        return
    try:
        import json
        raw = await db.get_setting(f"open_positions:{bot_id}")
        data = []
        if raw:
            data = json.loads(raw) if isinstance(raw, str) else list(raw or [])
        side_n = norm_side(side)
        coin = inst_id.replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
        out = []
        found = False
        for p in data:
            if (p.get("inst_id") == inst_id and norm_side(p.get("side") or "long") == side_n):
                out.append({
                    "coin": coin, "inst_id": inst_id, "side": side_n,
                    "size": float(size), "entry_price": float(entry),
                })
                found = True
            else:
                out.append(p)
        if not found:
            out.append({
                "coin": coin, "inst_id": inst_id, "side": side_n,
                "size": float(size), "entry_price": float(entry),
            })
        await db.set_setting(f"open_positions:{bot_id}", json.dumps(out))
    except Exception as e:
        log.warning("upsert_snapshot_position %s: %s", bot_id, e)


async def remove_snapshot_position(db, bot_id: str, inst_id: str = None, side: str = None) -> None:
    if not db or not bot_id:
        return
    try:
        import json
        raw = await db.get_setting(f"open_positions:{bot_id}")
        if not raw:
            return
        data = json.loads(raw) if isinstance(raw, str) else list(raw or [])
        if not inst_id:
            await db.set_setting(f"open_positions:{bot_id}", "[]")
            return
        side_n = norm_side(side) if side else None
        out = []
        for p in data:
            if p.get("inst_id") != inst_id:
                out.append(p)
                continue
            if side_n and norm_side(p.get("side") or "long") != side_n:
                out.append(p)
                continue
            # drop match
        await db.set_setting(f"open_positions:{bot_id}", json.dumps(out))
    except Exception as e:
        log.warning("remove_snapshot_position %s: %s", bot_id, e)


def orphan_close_enabled() -> bool:
    # Default OFF — auto-closing "orphans" wiped real strategy positions after
    # claim loss on deploy/PnL reset. Enable explicitly with ORPHAN_CLOSE=1.
    v = (os.getenv("ORPHAN_CLOSE") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


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
        try:
            await upsert_snapshot_position(db, bot_id, inst_id, side_n, float(size), float(entry))
        except Exception:
            pass
        return bool(row)
    except Exception as e:
        log.warning("claim_open failed %s %s: %s", bot_id, inst_id, e)
        try:
            await db.save_position(
                bot_id=bot_id, inst_id=inst_id, side=side_n,
                size=float(size), entry_price=float(entry),
            )
            try:
                await upsert_snapshot_position(db, bot_id, inst_id, side_n, float(size), float(entry))
            except Exception:
                pass
            return True
        except Exception as e2:
            log.error("claim_open retry failed %s: %s", inst_id, e2)
            return False


async def release_open(db, bot_id: str, inst_id: str = None, side: str = None) -> None:
    if not db:
        return
    try:
        try:
            await remove_snapshot_position(db, bot_id, inst_id, side)
        except Exception:
            pass
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


async def restore_snapshots_to_claims(db, bot_ids: list = None) -> dict:
    """Re-apply open_positions:{bot_id} snapshots into positions claims after deploy.

    Does not touch exchange — only DB ownership so UI/reclaim bind correctly.
    """
    out = {"restored": 0, "bots": []}
    if not db:
        return out
    try:
        import json
        ids = list(bot_ids or [])
        if not ids:
            # discover keys from settings if API exists
            try:
                if hasattr(db, "list_settings_prefix"):
                    keys = await db.list_settings_prefix("open_positions:")
                    ids = [k.split(":", 1)[-1] for k in keys if ":" in k]
            except Exception:
                pass
        for bot_id in ids:
            if not bot_id:
                continue
            raw = await db.get_setting(f"open_positions:{bot_id}")
            if not raw:
                continue
            data = json.loads(raw) if isinstance(raw, str) else list(raw or [])
            n = 0
            for p in data:
                if not isinstance(p, dict):
                    continue
                inst = p.get("inst_id") or ""
                if not inst:
                    continue
                side = norm_side(p.get("side") or "long")
                size = float(p.get("size") or 0)
                entry = float(p.get("entry_price") or p.get("entry") or 0)
                if size <= 0:
                    continue
                try:
                    await claim_open(db, bot_id, inst, side, size, entry)
                    n += 1
                except Exception as e:
                    log.warning("restore claim %s %s: %s", bot_id, inst, e)
            if n:
                out["restored"] += n
                out["bots"].append({"bot_id": bot_id, "n": n})
        return out
    except Exception as e:
        log.warning("restore_snapshots_to_claims: %s", e)
        out["error"] = str(e)
        return out
