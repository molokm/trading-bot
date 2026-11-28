"""Trend book → OKX mirror.

Two layers, deliberately separate:

``ShadowMirror``
    Plans the exact OKX USDT-M SWAP orders a mirror would send and keeps a
    ledger.  It has no client, no key and no credential: it cannot place
    anything.  Used for ``TREND_MIRROR_MODE=shadow``.

``LiveMirror``
    Same planner, plus execution through an injected client.  It is fail-closed
    at every step and refuses to run unless *all* of these hold:
      * ``TREND_MIRROR_MODE=live``;
      * DB setting ``trend_mirror_live_enabled == '1'`` (explicit opt-in);
      * a real (non-demo) client is available;
      * a coin whitelist is configured (micro-live, never the whole book);
      * per-order and total notional caps are configured;
      * a daily order cap is configured.
    A new position is only kept if its exchange stop was placed successfully;
    if the stop fails, the entry is immediately closed again.

Ownership: every order carries a ``trd*`` clOrdId and every position is
claimed in the DB under ``bot_id="trend_live"``.  The AI mirror only flattens
positions whose entry clOrdId starts with ``ai``, and the orphan sweeper skips
claimed positions, so the two bots cannot close each other's trades.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

log = logging.getLogger("trend_portfolio.mirror")

SWAP_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
SPECS_TTL_SEC = 6 * 3600
MAX_ORDERS_KEPT = 200
MAX_SKIPPED_KEPT = 100
LIVE_FLAG = "trend_mirror_live_enabled"
LIVE_BOT_ID = "trend_live"

ClientProvider = Callable[[], Awaitable[object]]


@dataclass(frozen=True)
class SwapSpec:
    """OKX USDT-M contract metadata needed to size an order."""

    inst_id: str
    ct_val: float          # coins per contract
    lot_sz: float          # order size step, in contracts
    min_sz: float          # minimum order size, in contracts
    tick_sz: float         # price step

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


def _cl_ord_id(kind: str, coin: str, ts: int, tag: str = "trd") -> str:
    """``trd`` + kind + coin + UTC date, alphanumeric, <= 32 chars.

    The ``trd`` prefix is the ownership tag: the AI mirror only ever flattens
    positions whose entry clOrdId starts with ``ai``, so a trend position is
    never swept by that bot (see ``position_claim`` / ``_manage_live_orphans``).
    A UTC date (not a modulo of the timestamp) keeps the id unique per coin per
    day, which is the granularity of this book.
    """
    kind_tag = {"open": "o", "close": "c", "stop": "s"}.get(kind, "x")
    day = time.strftime("%y%m%d", time.gmtime(ts / 1000)) if ts else "000000"
    return f"{tag}{kind_tag}{coin}{day}"[:32]


def _num(x: float) -> str:
    s = f"{float(x):.10f}".rstrip("0").rstrip(".")
    return s or "0"


def _px(px: float, tick: float) -> str:
    """Format a price on the exchange tick grid."""
    if tick <= 0:
        return _num(px)
    try:
        decimals = max(0, -Decimal(str(tick)).as_tuple().exponent)
    except Exception:
        decimals = 8
    steps = round(float(px) / float(tick))
    return f"{steps * float(tick):.{decimals}f}"


def _resp_ok(resp) -> Tuple[bool, str]:
    """OKX response → (ok, reason).  Business errors are not exceptions."""
    if not isinstance(resp, dict):
        return False, "bad response"
    if resp.get("error"):
        return False, str(resp.get("message") or resp.get("msg") or resp)[:200]
    code = str(resp.get("code", "0"))
    if code not in ("0", ""):
        return False, f"{code} {resp.get('msg', '')}".strip()[:200]
    rows = resp.get("data") or []
    if rows and isinstance(rows[0], dict):
        sc = str(rows[0].get("sCode", "0"))
        if sc not in ("0", ""):
            return False, f"{sc} {rows[0].get('sMsg', '')}".strip()[:200]
    return True, ""


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
        # Never inherit another mode's ledger: shadow positions do not exist on
        # the exchange, and adopting them in live would try to close phantoms
        # (or leave real ones untracked).
        if str(state.get("mode") or "") and str(state.get("mode")) != self.mode:
            log.warning("trend mirror ledger mode %r != %r — starting empty",
                        state.get("mode"), self.mode)
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
                    plan = self._plan_open(ev, scale)
                    if plan is None:
                        continue
                    order, pos = plan
                    ok, reason = await self._commit_open(order, pos)
                    if not ok:
                        self._skip(ev, reason or "execution_failed")
                        continue
                    self.positions[order["coin"]] = pos
                    self.opens += 1
                elif kind == "close":
                    plan = self._plan_close(ev)
                    if plan is None:
                        continue
                    order, pos = plan
                    ok, reason = await self._commit_close(order, pos)
                    if not ok:
                        self._skip(ev, reason or "execution_failed")
                        continue
                    self.positions.pop(order["coin"], None)
                    self.closes += 1
                else:
                    continue
            except Exception as exc:  # never break a paper cycle
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("trend mirror event failed: %s", self.last_error)
                continue
            self.orders.append(order)
            planned.append(order)
        if len(self.orders) > MAX_ORDERS_KEPT:
            self.orders = self.orders[-MAX_ORDERS_KEPT:]
        if len(self.skipped) > MAX_SKIPPED_KEPT:
            self.skipped = self.skipped[-MAX_SKIPPED_KEPT:]
        return planned

    async def maintain(self, book) -> None:
        """Per-cycle housekeeping (stop updates, reconciliation). No-op here."""
        return None

    # ── execution hooks (shadow: accept immediately) ─────────────────────
    async def _commit_open(self, order: dict, pos: dict) -> Tuple[bool, str]:
        return True, ""

    async def _commit_close(self, order: dict, pos: dict) -> Tuple[bool, str]:
        return True, ""

    # ── planning ─────────────────────────────────────────────────────────
    def _skip(self, ev: dict, reason: str, **extra) -> None:
        self.skips += 1
        self.skipped.append({
            "coin": str(ev.get("coin") or ""),
            "kind": str(ev.get("kind") or ""),
            "ts": int(ev.get("ts") or 0),
            "reason": reason,
            **extra,
        })

    def _plan_open(self, ev: dict, scale: float) -> Optional[Tuple[dict, dict]]:
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
        pos = {
            "inst_id": spec.inst_id,
            "sz": sz,
            "entry": price,
            "stop": float(ev.get("stop") or 0),
            "notional": order["notional"],
            "ts": ts,
            "algo_id": "",
        }
        return order, pos

    def _plan_close(self, ev: dict) -> Optional[Tuple[dict, dict]]:
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
        return order, pos

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


class LiveMirror(ShadowMirror):
    """Stop-aware executor.  Fail-closed at every step.

    Only a named coin whitelist is allowed (micro-live), every order is capped,
    and the DB flag ``trend_mirror_live_enabled`` must be explicitly set.
    """

    def __init__(self, equity: float, max_notional: float, db,
                 coins: Sequence[str] = (), daily_cap: int = 20,
                 max_total_notional: float = 0.0, bot_id: str = LIVE_BOT_ID):
        super().__init__(mode="live", equity=equity, max_notional=max_notional)
        self.db = db
        self.coins = {str(c).upper() for c in coins}
        self.daily_cap = int(daily_cap or 0)
        self.max_total_notional = float(max_total_notional or 0.0)
        self.bot_id = bot_id
        self.client_provider: Optional[ClientProvider] = None
        self.live_enabled = False
        self.client_ready = False
        self.last_live_error = ""

    # ── guards ───────────────────────────────────────────────────────────
    async def _flag_enabled(self) -> bool:
        if self.db is None:
            return False
        try:
            raw = await self.db.get_setting(LIVE_FLAG)
        except Exception as exc:
            self.last_live_error = f"flag read: {type(exc).__name__}"
            return False
        return str(raw or "").strip().lower() in ("1", "true", "yes", "on")

    async def _client(self):
        if self.client_provider is None:
            return None
        try:
            lc = await self.client_provider()
        except Exception as exc:
            self.last_live_error = f"client: {type(exc).__name__}: {exc}"
            return None
        if lc is None or getattr(lc, "demo", True):
            self.client_ready = False
            return None
        if not getattr(lc, "has_credentials", lambda: False)():
            self.client_ready = False
            return None
        self.client_ready = True
        return lc

    def _orders_on(self, ts: int) -> int:
        """Orders planned for the UTC day of ``ts`` (cap is per event day)."""
        day = time.strftime("%y%m%d", time.gmtime((int(ts) or 0) / 1000))
        return sum(
            1 for o in self.orders
            if o.get("kind") in ("open", "close")
            and time.strftime("%y%m%d", time.gmtime(int(o.get("ts") or 0) / 1000)) == day
        )

    async def _gate(self, ts: int = 0) -> Tuple[bool, str]:
        self.live_enabled = await self._flag_enabled()
        if not self.live_enabled:
            return False, "live_disabled"
        if not self.coins:
            return False, "no_coin_whitelist"
        if self.max_notional <= 0 or self.max_total_notional <= 0:
            return False, "no_notional_cap"
        if self.daily_cap <= 0:
            return False, "no_daily_cap"
        if self._orders_on(ts) >= self.daily_cap:
            return False, "daily_cap"
        return True, ""

    # ── execution ────────────────────────────────────────────────────────
    async def _commit_open(self, order: dict, pos: dict) -> Tuple[bool, str]:
        ok, reason = await self._gate(int(order.get("ts") or 0))
        if not ok:
            return False, reason
        coin = str(order.get("coin") or "")
        if coin not in self.coins:
            return False, "not_whitelisted"
        if self.positions.get(coin):
            return False, "already_open"
        total = sum(float(p.get("notional") or 0) for p in self.positions.values())
        if total + float(order.get("notional") or 0) > self.max_total_notional:
            return False, "total_cap"
        lc = await self._client()
        if lc is None:
            return False, "no_live_client"
        spec = self.specs.get(coin)
        if spec is None:
            return False, "no_okx_swap"
        # Never add to an existing position: the account may hold a manual
        # trade or another bot's book on the same instrument.  Starting from
        # flat is what makes "close = close our size" safe.
        if not await self._is_flat(lc, spec.inst_id):
            return False, "exchange_not_flat"

        resp = await self._market(lc, spec.inst_id, "buy", order["sz"], order["cl_ord_id"])
        ok, reason = _resp_ok(resp)
        if not ok:
            return False, f"open_rejected:{reason}"
        filled, avg = await self._fill(lc, spec.inst_id, resp)
        if filled <= 0 or avg <= 0:
            return False, "open_unfilled"

        stop = float(order.get("stop") or 0)
        if stop <= 0 or stop >= avg:
            await self._flatten(lc, spec, filled, f"trdx{coin}")
            return False, "bad_stop"
        algo_id = await self._place_stop(lc, spec, filled, stop, coin, order["ts"])
        if not algo_id:
            # Never leave an unprotected position: undo the entry immediately.
            await self._flatten(lc, spec, filled, _cl_ord_id("close", coin, order["ts"]))
            return False, "stop_rejected"

        pos.update({
            "sz": filled,
            "entry": avg,
            "notional": round(filled * spec.ct_val * avg, 2),
            "algo_id": algo_id,
        })
        await self._claim(coin, filled, avg)
        return True, ""

    async def _commit_close(self, order: dict, pos: dict) -> Tuple[bool, str]:
        lc = await self._client()
        if lc is None:
            return False, "no_live_client"
        inst = str(pos.get("inst_id") or order.get("inst_id") or "")
        spec = self.specs.get(str(order.get("coin") or ""))
        if spec is None or not inst:
            return False, "no_okx_swap"
        await self._cancel_stop(lc, inst, str(pos.get("algo_id") or ""))
        sz = float(pos.get("sz") or 0)
        if sz <= 0:
            self.positions.pop(str(order.get("coin") or ""), None)
            return True, ""
        resp = await self._market(lc, inst, "sell", sz, order["cl_ord_id"], is_close=True)
        ok, reason = _resp_ok(resp)
        if not ok:
            # The exchange stop may have closed it already: flat is success.
            if await self._is_flat(lc, inst):
                await self._release(str(order.get("coin") or ""))
                return True, ""
            return False, f"close_rejected:{reason}"
        await self._release(str(order.get("coin") or ""))
        return True, ""

    # ── maintenance ──────────────────────────────────────────────────────
    async def maintain(self, book) -> None:
        """Reconcile the ledger with the exchange and trail the stop daily."""
        if not self.positions:
            return
        lc = await self._client()
        if lc is None:
            return
        for coin, pos in list(self.positions.items()):
            inst = str(pos.get("inst_id") or "")
            sleeve = getattr(book, "sleeves", {}).get(coin)
            live_pos = getattr(sleeve, "position", None) if sleeve else None
            if live_pos is None:
                # The book no longer holds it: close whatever is on the exchange.
                order = {
                    "kind": "close", "coin": coin, "inst_id": inst,
                    "side": "sell", "sz": float(pos.get("sz") or 0),
                    "price": 0.0, "notional": float(pos.get("notional") or 0),
                    "reason": "book_flat", "ts": int(time.time() * 1000),
                    "cl_ord_id": _cl_ord_id("close", coin, int(time.time() * 1000)),
                }
                ok, _ = await self._commit_close(order, pos)
                if ok:
                    self.positions.pop(coin, None)
                continue
            if await self._is_flat(lc, inst):
                # Stopped out on the exchange: drop the ledger, cancel the algo.
                await self._cancel_stop(lc, inst, str(pos.get("algo_id") or ""))
                await self._release(coin)
                self.positions.pop(coin, None)
                continue
            new_stop = float(getattr(live_pos, "stop", 0) or 0)
            old_stop = float(pos.get("stop") or 0)
            if new_stop > 0 and new_stop > old_stop * 1.0005:
                algo_id = await self._amend_stop(lc, coin, pos, new_stop)
                if algo_id:
                    pos["stop"] = new_stop
                    pos["algo_id"] = algo_id

    # ── exchange helpers ─────────────────────────────────────────────────
    async def _market(self, lc, inst: str, side: str, sz: float,
                      cl_ord_id: str, is_close: bool = False) -> dict:
        """Market order, hedge first then net (OKX account-mode agnostic).

        ``reduceOnly`` is only sent with the net-mode attempts: OKX rejects it
        in long/short mode, where ``posSide`` + side already define a close.
        Sending it together with ``posSide=long`` would make every exit fail.
        """
        if is_close:
            attempts = (("long", False), ("net", True), (None, True))
        else:
            attempts = (("long", False), ("net", False), (None, False))
        resp: dict = {"error": True, "message": "no attempt"}
        for ps, reduce_only in attempts:
            kwargs = dict(
                inst_id=inst, side=side, ord_type="market", sz=_num(sz),
                td_mode="cross", cl_ord_id=cl_ord_id,
            )
            if ps:
                kwargs["pos_side"] = ps
            if reduce_only:
                kwargs["reduce_only"] = True
            resp = await lc.place_order(**kwargs)
            ok, _ = _resp_ok(resp)
            if ok:
                return resp
            msg = str(resp.get("message") or resp.get("msg") or resp)
            if any(t in msg for t in ("51169", "51111", "51011", "posSide",
                                      "reduceOnly", "51000")):
                continue
            return resp
        return resp

    async def _fill(self, lc, inst: str, resp: dict) -> Tuple[float, float]:
        row = (resp.get("data") or [{}])[0] if isinstance(resp.get("data"), list) else {}
        filled = float(row.get("accFillSz") or 0)
        avg = float(row.get("avgPx") or 0)
        ord_id = str(row.get("ordId") or "")
        if ord_id and (filled <= 0 or avg <= 0):
            try:
                got = await lc.get_order(inst, ord_id=ord_id)
                info = (got.get("data") or [{}])[0]
                filled = float(info.get("accFillSz") or filled or 0)
                avg = float(info.get("avgPx") or avg or 0)
            except Exception:
                pass
        return filled, avg

    async def _place_stop(self, lc, spec: SwapSpec, sz: float, stop: float,
                          coin: str, ts: int) -> str:
        resp = await lc.place_algo_order(
            inst_id=spec.inst_id, side="sell", sz=_num(sz), td_mode="cross",
            pos_side="long", reduce_only=True,
            sl_trigger_px=_px(stop, spec.tick_sz), sl_ord_px="-1",
            cl_ord_id=_cl_ord_id("stop", coin, ts),
        )
        ok, reason = _resp_ok(resp)
        if not ok:
            # Retry in net mode before giving up.
            resp = await lc.place_algo_order(
                inst_id=spec.inst_id, side="sell", sz=_num(sz), td_mode="cross",
                reduce_only=True, sl_trigger_px=_px(stop, spec.tick_sz),
                sl_ord_px="-1", cl_ord_id=_cl_ord_id("stop", coin, ts),
            )
            ok, reason = _resp_ok(resp)
        if not ok:
            self.last_live_error = f"stop: {reason}"
            return ""
        row = (resp.get("data") or [{}])[0]
        return str(row.get("algoId") or "")

    async def _amend_stop(self, lc, coin: str, pos: dict, new_stop: float) -> str:
        inst = str(pos.get("inst_id") or "")
        spec = self.specs.get(coin)
        algo_id = str(pos.get("algo_id") or "")
        if not inst or spec is None:
            return ""
        if algo_id:
            try:
                resp = await lc.amend_algo_order(
                    inst, algo_id, sl_trigger_px=_px(new_stop, spec.tick_sz))
                ok, _ = _resp_ok(resp)
                if ok:
                    return algo_id
            except Exception:
                pass
        await self._cancel_stop(lc, inst, algo_id)
        return await self._place_stop(lc, spec, float(pos.get("sz") or 0),
                                      new_stop, coin, int(time.time() * 1000))

    async def _cancel_stop(self, lc, inst: str, algo_id: str) -> None:
        if not algo_id:
            return
        try:
            await lc.cancel_algo_order(inst, algo_id)
        except Exception:
            pass

    async def _flatten(self, lc, spec: SwapSpec, sz: float, cl_ord_id: str) -> None:
        try:
            await self._market(lc, spec.inst_id, "sell", sz, cl_ord_id, is_close=True)
        except Exception:
            pass

    async def _is_flat(self, lc, inst: str) -> bool:
        try:
            resp = await lc.get_positions("SWAP", inst_id=inst)
            if resp.get("error"):
                return False
            rows = resp.get("data") or []
            for row in rows:
                if str(row.get("instId") or "") != inst:
                    continue
                try:
                    if abs(float(row.get("pos") or 0)) > 0:
                        return False
                except (TypeError, ValueError):
                    continue
            return True
        except Exception:
            return False

    async def _claim(self, coin: str, sz: float, entry: float) -> None:
        if self.db is None:
            return
        try:
            from app.services.position_claim import claim_open
            await claim_open(self.db, self.bot_id, f"{coin}-USDT-SWAP",
                             "long", float(sz), float(entry))
        except Exception as exc:
            self.last_live_error = f"claim: {type(exc).__name__}"

    async def _release(self, coin: str) -> None:
        if self.db is None:
            return
        try:
            from app.services.position_claim import release_open
            await release_open(self.db, self.bot_id, f"{coin}-USDT-SWAP", "long")
        except Exception as exc:
            self.last_live_error = f"release: {type(exc).__name__}"

    # ── reporting ────────────────────────────────────────────────────────
    def status(self) -> dict:
        st = super().status()
        st.update({
            "live_enabled": bool(self.live_enabled),
            "client_ready": bool(self.client_ready),
            "coins": sorted(self.coins),
            "daily_cap": self.daily_cap,
            "orders_today": self._orders_on(int(time.time() * 1000)),
            "max_total_notional": round(self.max_total_notional, 2),
            "live_error": self.last_live_error,
        })
        return st
