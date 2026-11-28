"""Shadow mirror for the daily trend book.

WHAT THIS IS
------------
The paper book (``engine.TrendPortfolio``) decides *what* to trade; this module
translates its ``open`` / ``close`` events into the exact OKX USDT-M SWAP orders
a LIVE mirror would have to send: instrument id, side, contracts (rounded to the
exchange lot step), notional and a ``trd*`` client-order id.

WHAT THIS IS NOT
----------------
It never places, amends or cancels an order.  There is no client, no key and no
credential in this module, and ``mode="live"`` is rejected by the config.  The
goal is to prove the bridge against real market data and real contract specs
before anyone connects it to money.

WHY A SHADOW FIRST
------------------
Three things can silently break a trend mirror and each needs evidence:
  * universe: OKX lists 41 of the 45 research coins as USDT swaps;
  * size: a diversified 42-position book needs enough equity to clear OKX
    ``minSz`` on every contract (about $1.5k for the full universe);
  * exits: the paper engine fills the Chandelier stop *intraday*, while a
    bar-close mirror can only react at the next UTC open — that gap must be
    measured, not assumed.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import httpx

log = logging.getLogger("trend_portfolio.mirror")

SWAP_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
SPECS_TTL_SEC = 6 * 3600
MAX_ORDERS_KEPT = 200
MAX_SKIPPED_KEPT = 100


@dataclass(frozen=True)
class SwapSpec:
    """OKX USDT-M contract metadata needed to size an order."""

    inst_id: str
    ct_val: float          # coins per contract
    lot_sz: float          # order size step, in contracts
    min_sz: float          # minimum order size, in contracts
    tick_sz: float         # price step (for a future limit/stop order)

    def contracts(self, notional: float, price: float) -> float:
        """Contracts for ``notional`` USD, floored to the exchange lot step.

        Returns 0.0 when even the minimum order is above the budget: sending a
        sub-minimum size is rejected by OKX, so the mirror must skip the coin
        instead of pretending it traded.
        """
        if notional <= 0 or price <= 0 or self.ct_val <= 0 or self.lot_sz <= 0:
            return 0.0
        raw = notional / (price * self.ct_val)
        steps = math.floor(raw / self.lot_sz + 1e-9)
        sz = round(steps * self.lot_sz, 10)
        if sz < self.min_sz:
            return 0.0
        return sz

    def min_notional(self, price: float) -> float:
        return self.min_sz * self.ct_val * price


async def fetch_swap_specs(timeout: float = 20.0) -> Dict[str, SwapSpec]:
    """Public OKX USDT-M swap metadata, keyed by base coin.

    Read-only public endpoint: no key, no signature, no order path.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            SWAP_INSTRUMENTS_URL,
            params={"instType": "SWAP"},
            headers={"User-Agent": "copix-trend-mirror"},
        )
        resp.raise_for_status()
        payload = resp.json()
    if str(payload.get("code", "0")) != "0":
        raise RuntimeError(f"okx instruments {payload.get('code')}: {payload.get('msg')}")
    out: Dict[str, SwapSpec] = {}
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        inst = str(row.get("instId") or "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        if str(row.get("settleCcy") or "") != "USDT":
            continue
        if str(row.get("state") or "") != "live":
            continue
        try:
            spec = SwapSpec(
                inst_id=inst,
                ct_val=float(row.get("ctVal") or 0),
                lot_sz=float(row.get("lotSz") or 0),
                min_sz=float(row.get("minSz") or 0),
                tick_sz=float(row.get("tickSz") or 0),
            )
        except (TypeError, ValueError):
            continue
        if spec.ct_val <= 0 or spec.lot_sz <= 0 or spec.min_sz <= 0:
            continue
        out[inst.split("-")[0]] = spec
    return out


def _cl_ord_id(kind: str, coin: str, ts: int) -> str:
    """``trd`` + kind + coin + UTC date, alphanumeric, <= 32 chars.

    The ``trd`` prefix is the ownership tag: the AI mirror only ever flattens
    positions whose entry clOrdId starts with ``ai``, so a trend position is
    never swept by that bot (see ``position_claim`` / ``_manage_live_orphans``).
    A UTC date (not a modulo of the timestamp) keeps the id unique per coin per
    day, which is the granularity of this book.
    """
    tag = "o" if kind == "open" else "c"
    day = time.strftime("%y%m%d", time.gmtime(ts / 1000)) if ts else "000000"
    return f"trd{tag}{coin}{day}"[:32]


class ShadowMirror:
    """Translate trend events into planned OKX orders. Places nothing."""

    def __init__(self, mode: str = "off", equity: float = 0.0,
                 max_notional: float = 0.0, state_name: str = "trend_mirror"):
        self.mode = mode
        self.equity = float(equity or 0.0)
        self.max_notional = float(max_notional or 0.0)
        self.state_name = state_name
        self.since = int(time.time() * 1000)
        self.positions: Dict[str, dict] = {}
        self.orders: List[dict] = []
        self.skipped: List[dict] = []
        self.opens = 0
        self.closes = 0
        self.skips = 0
        self.specs: Dict[str, SwapSpec] = {}
        self._specs_ts = 0.0
        self._universe: set = set()
        self.last_error = ""

    # ── persistence ──────────────────────────────────────────────────────
    def to_state(self) -> dict:
        return {
            "mode": self.mode,
            "since": self.since,
            "opens": self.opens,
            "closes": self.closes,
            "skips": self.skips,
            "positions": self.positions,
            "orders": self.orders[-MAX_ORDERS_KEPT:],
            "skipped": self.skipped[-MAX_SKIPPED_KEPT:],
        }

    def from_state(self, state: Optional[dict]) -> None:
        if not isinstance(state, dict):
            return
        self.since = int(state.get("since") or self.since)
        self.opens = int(state.get("opens") or 0)
        self.closes = int(state.get("closes") or 0)
        self.skips = int(state.get("skips") or 0)
        pos = state.get("positions")
        if isinstance(pos, dict):
            self.positions = {str(k): v for k, v in pos.items() if isinstance(v, dict)}
        orders = state.get("orders")
        if isinstance(orders, list):
            self.orders = [o for o in orders if isinstance(o, dict)][-MAX_ORDERS_KEPT:]
        skipped = state.get("skipped")
        if isinstance(skipped, list):
            self.skipped = [o for o in skipped if isinstance(o, dict)][-MAX_SKIPPED_KEPT:]

    # ── specs ────────────────────────────────────────────────────────────
    async def _ensure_specs(self) -> None:
        now = time.time()
        if self.specs and (now - self._specs_ts) < SPECS_TTL_SEC:
            return
        try:
            self.specs = await fetch_swap_specs()
            self._specs_ts = now
            self.last_error = ""
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("trend mirror instruments failed: %s", self.last_error)

    # ── event handling ───────────────────────────────────────────────────
    async def on_events(self, events: Sequence[dict], book) -> List[dict]:
        """Plan orders for the events produced by one book cycle.

        ``events`` are only the events of this cycle (the runner slices
        ``book.events`` before/after feeding), so a restored book never replays
        old signals into the mirror.
        """
        if self.mode == "off" or not events:
            return []
        self._universe = set(getattr(book, "sleeves", {}) or {})
        await self._ensure_specs()
        scale = (self.equity / book.capital) if (self.equity > 0 and book.capital > 0) else 1.0
        planned: List[dict] = []
        for ev in events:
            try:
                kind = str(ev.get("kind") or "")
                if kind == "open":
                    order = self._plan_open(ev, scale)
                elif kind == "close":
                    order = self._plan_close(ev)
                else:
                    continue
            except Exception as exc:  # never break a paper cycle
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("trend mirror event failed: %s", self.last_error)
                continue
            if order is None:
                continue
            self.orders.append(order)
            planned.append(order)
            if order["kind"] == "open":
                self.opens += 1
            else:
                self.closes += 1
        if len(self.orders) > MAX_ORDERS_KEPT:
            self.orders = self.orders[-MAX_ORDERS_KEPT:]
        if len(self.skipped) > MAX_SKIPPED_KEPT:
            self.skipped = self.skipped[-MAX_SKIPPED_KEPT:]
        return planned

    def _skip(self, ev: dict, reason: str, **extra) -> None:
        self.skips += 1
        self.skipped.append({
            "coin": str(ev.get("coin") or ""),
            "kind": str(ev.get("kind") or ""),
            "ts": int(ev.get("ts") or 0),
            "reason": reason,
            **extra,
        })

    def _plan_open(self, ev: dict, scale: float) -> Optional[dict]:
        coin = str(ev.get("coin") or "")
        price = float(ev.get("price") or 0)
        book_notional = float(ev.get("notional") or 0)
        spec = self.specs.get(coin)
        if spec is None:
            self._skip(ev, "no_okx_swap")
            return None
        live_notional = book_notional * scale
        if self.max_notional > 0:
            live_notional = min(live_notional, self.max_notional)
        sz = spec.contracts(live_notional, price)
        if sz <= 0:
            self._skip(ev, "below_min_sz", needed=round(spec.min_notional(price), 2),
                       budget=round(live_notional, 2))
            return None
        ts = int(ev.get("ts") or 0)
        order = {
            "kind": "open",
            "coin": coin,
            "inst_id": spec.inst_id,
            "side": "buy",
            "sz": sz,
            "price": price,
            "notional": round(sz * spec.ct_val * price, 2),
            "book_notional": round(book_notional, 2),
            "stop": float(ev.get("stop") or 0),
            "ts": ts,
            "cl_ord_id": _cl_ord_id("open", coin, ts),
        }
        self.positions[coin] = {
            "inst_id": spec.inst_id,
            "sz": sz,
            "entry": price,
            "stop": float(ev.get("stop") or 0),
            "notional": order["notional"],
            "ts": ts,
        }
        return order

    def _plan_close(self, ev: dict) -> Optional[dict]:
        coin = str(ev.get("coin") or "")
        pos = self.positions.get(coin)
        if pos is None:
            # The paper book held this coin before the mirror started: there is
            # nothing on the exchange to close, so ignore the event instead of
            # inventing a sell.
            self._skip(ev, "no_mirrored_position")
            return None
        ts = int(ev.get("ts") or 0)
        order = {
            "kind": "close",
            "coin": coin,
            "inst_id": str(pos.get("inst_id") or ""),
            "side": "sell",
            "sz": float(pos.get("sz") or 0),
            "price": float(ev.get("price") or 0),
            "notional": round(float(pos.get("notional") or 0), 2),
            "reason": str(ev.get("reason") or ""),
            "ts": ts,
            "cl_ord_id": _cl_ord_id("close", coin, ts),
        }
        self.positions.pop(coin, None)
        return order

    # ── reporting ────────────────────────────────────────────────────────
    def status(self) -> dict:
        open_notional = sum(float(p.get("notional") or 0) for p in self.positions.values())
        return {
            "mode": self.mode,
            "equity": round(self.equity, 2),
            "max_notional": round(self.max_notional, 2),
            "since": self.since,
            "opens": self.opens,
            "closes": self.closes,
            "skips": self.skips,
            "open_positions": len(self.positions),
            "open_notional": round(open_notional, 2),
            # How many coins of the book have an OKX USDT swap at all.
            "universe": len(self._universe),
            "instruments": len(self._universe & set(self.specs)) if self._universe else 0,
            "last_error": self.last_error,
            "orders": list(reversed(self.orders[-10:])),
            "skipped": list(reversed(self.skipped[-5:])),
        }
