"""Ensure every bot open is claimed in DB so restarts never create 'orphan' UI rows."""
from __future__ import annotations
import logging

log = logging.getLogger("position_claim")


def norm_side(side: str) -> str:
    s = (side or "long").lower()
    if s in ("sell", "s", "short"):
        return "short"
    return "long"


async def claim_open(db, bot_id: str, inst_id: str, side: str, size: float, entry: float) -> bool:
    if not db or not inst_id or size <= 0 or entry <= 0:
        return False
    try:
        await db.claim_position(bot_id, inst_id, norm_side(side), size, entry)
        return True
    except Exception as e:
        log.warning("claim_open failed %s %s: %s", bot_id, inst_id, e)
        # one retry
        try:
            await db.save_position(
                bot_id=bot_id, inst_id=inst_id, side=norm_side(side),
                size=float(size), entry_price=float(entry),
            )
            return True
        except Exception as e2:
            log.error("claim_open retry failed %s: %s", inst_id, e2)
            return False


async def release_open(db, bot_id: str) -> None:
    if not db:
        return
    try:
        await db.delete_position(bot_id)
    except Exception as e:
        log.warning("release_open %s: %s", bot_id, e)
