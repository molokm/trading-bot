"""Smart Money Mirror — follow public Hyperliquid positions and mirror on OKX SWAP.

Not native OKX copy-trading: we poll public HL positions and place our own
orders on the user's OKX account with a fixed capital envelope.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import httpx

log = logging.getLogger("smart_money_mirror")

HL_INFO = "https://api.hyperliquid.xyz/info"
DATA_DIR = os.getenv("DATA_DIR", "/tmp")

# coin -> OKX contract face value (approx; refined from instruments when available)
CT_VAL = {
    "BTC": 0.01, "ETH": 0.1, "SOL": 1.0, "XRP": 100.0, "DOGE": 1000.0,
    "ADA": 100.0, "LINK": 1.0, "AVAX": 1.0, "BNB": 0.01, "DOT": 1.0,
    "LTC": 0.1, "ATOM": 1.0, "NEAR": 1.0, "APT": 1.0, "ARB": 10.0,
    "OP": 1.0, "SUI": 1.0, "PEPE": 1000000.0, "WIF": 1.0, "TRX": 1000.0,
}
LOT_SZ = {
    "BTC": 0.01, "ETH": 0.01, "SOL": 0.1, "XRP": 1.0, "DOGE": 1.0,
    "ADA": 1.0, "LINK": 0.1, "AVAX": 0.1, "BNB": 0.01, "DOT": 0.1,
    "LTC": 0.1, "ATOM": 0.1, "NEAR": 0.1, "APT": 0.1, "ARB": 1.0,
    "OP": 0.1, "SUI": 0.1, "PEPE": 1.0, "WIF": 0.1, "TRX": 1.0,
}

# HL coin names that map 1:1 or need rename
HL_TO_OKX = {
    "BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "XRP": "XRP", "DOGE": "DOGE",
    "ADA": "ADA", "LINK": "LINK", "AVAX": "AVAX", "BNB": "BNB", "DOT": "DOT",
    "LTC": "LTC", "ATOM": "ATOM", "NEAR": "NEAR", "APT": "APT", "ARB": "ARB",
    "OP": "OP", "SUI": "SUI", "kPEPE": "PEPE", "PEPE": "PEPE", "WIF": "WIF",
    "TRX": "TRX", "TON": "TON", "INJ": "INJ", "FIL": "FIL", "AAVE": "AAVE",
}


@dataclass
class MirrorConfig:
    capital_usdt: float = 100.0       # total notional budget across mirrored coins
    max_leverage: float = 3.0
    poll_interval_sec: float = 20.0
    max_positions: int = 5
    execute: bool = True
    min_notional_usd: float = 15.0    # skip dust
    td_mode: str = "cross"


@dataclass
class MirrorTarget:
    address: str
    alias: str = ""
    capital_usdt: float = 100.0
    max_leverage: float = 3.0
    active: bool = True
    started_at: float = 0.0
    last_sync: float = 0.0
    last_error: str = ""
    # coin -> {side, hl_szi, okx_sz, inst_id}
    mirrored: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    log: List[Dict[str, Any]] = field(default_factory=list)


class SmartMoneyMirror:
    def __init__(self, client_manager=None, notifier=None, db=None):
        self.client_manager = client_manager
        self.notifier = None  # TG off for Smart Money
        self.db = db
        self.config = MirrorConfig()
        self._targets: Dict[str, MirrorTarget] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._load()

    # ── lifecycle ──────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="sm-mirror")
        self._thread.start()
        log.info("Smart Money Mirror started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=15)
        self._persist()
        log.info("Smart Money Mirror stopped")

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _run(self):
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                log.error("mirror tick: %s", e, exc_info=True)
            await asyncio.sleep(max(8.0, float(self.config.poll_interval_sec)))

    # ── public API ─────────────────────────────────────────────
    def list_targets(self) -> List[Dict]:
        with self._lock:
            return [self._target_public(t) for t in self._targets.values()]

    def open_positions_list(self) -> list:
        """Flat list of mirrored OKX positions for dashboard tagging."""
        out = []
        with self._lock:
            for t in self._targets.values():
                if not t.active:
                    continue
                for coin, m in (t.mirrored or {}).items():
                    inst = (m or {}).get("inst_id") or f"{coin}-USDT-SWAP"
                    side = ((m or {}).get("side") or "long").lower()
                    if side in ("sell", "s"):
                        side = "short"
                    out.append({
                        "coin": coin,
                        "inst_id": inst,
                        "side": side,
                        "size": float((m or {}).get("okx_sz") or (m or {}).get("hl_szi") or 0),
                        "entry_price": float((m or {}).get("entry") or (m or {}).get("entry_price") or 0),
                        "bot": "Умные деньги",
                        "source": "smart_money_mirror",
                        "lead": t.address[:10] + "…",
                    })
        return out

    def get_status(self) -> Dict:
        with self._lock:
            return {
                "running": self._running,
                "targets": [self._target_public(t) for t in self._targets.values()],
                "config": asdict(self.config),
                "open_positions": self.open_positions_list(),
            }

    async def start_mirror(
        self,
        address: str,
        alias: str = "",
        capital_usdt: float = None,
        max_leverage: float = None,
        execute: bool = True,
    ) -> Dict:
        addr = self._norm_addr(address)
        if not addr:
            return {"ok": False, "msg": "invalid Hyperliquid address"}
        # validate address has public state
        try:
            pos = await self.fetch_hl_positions(addr)
        except Exception as e:
            return {"ok": False, "msg": f"HL fetch failed: {e}"}

        with self._lock:
            t = self._targets.get(addr) or MirrorTarget(address=addr)
            t.alias = alias or t.alias or f"{addr[:6]}…{addr[-4:]}"
            t.capital_usdt = float(capital_usdt if capital_usdt is not None else self.config.capital_usdt)
            t.max_leverage = float(max_leverage if max_leverage is not None else self.config.max_leverage)
            t.active = True
            t.started_at = t.started_at or time.time()
            t.last_error = ""
            self._targets[addr] = t
            self.config.execute = bool(execute)
            self._persist()

        if not self._running:
            self.start()

        # immediate sync
        try:
            await self._sync_target(self._targets[addr])
        except Exception as e:
            with self._lock:
                self._targets[addr].last_error = str(e)
            return {"ok": True, "msg": f"mirror armed, first sync error: {e}", "positions_hl": len(pos)}

        return {
            "ok": True,
            "msg": f"mirror started for {addr[:10]}…",
            "positions_hl": len(pos),
            "target": self._target_public(self._targets[addr]),
        }

    async def stop_mirror(self, address: str, close_positions: bool = False) -> Dict:
        addr = self._norm_addr(address)
        with self._lock:
            t = self._targets.get(addr)
            if not t:
                return {"ok": False, "msg": "not mirroring"}
            t.active = False
        if close_positions:
            try:
                await self._close_all_mirrored(t)
            except Exception as e:
                return {"ok": False, "msg": f"stopped but close failed: {e}"}
        with self._lock:
            self._targets.pop(addr, None)
            self._persist()
        return {"ok": True, "msg": "mirror stopped"}

    # ── HL fetch ───────────────────────────────────────────────
    @staticmethod
    def _norm_addr(address: str) -> str:
        a = (address or "").strip()
        if a.startswith("hl:"):
            a = a[3:]
        a = a.lower()
        if not a.startswith("0x") or len(a) < 10:
            return ""
        return a

    async def fetch_hl_positions(self, address: str) -> List[Dict]:
        addr = self._norm_addr(address)
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(HL_INFO, json={"type": "clearinghouseState", "user": addr})
            r.raise_for_status()
            data = r.json()
        out = []
        for ap in data.get("assetPositions") or []:
            pos = ap.get("position") or {}
            try:
                szi = float(pos.get("szi") or 0)
            except (TypeError, ValueError):
                szi = 0.0
            if abs(szi) < 1e-12:
                continue
            coin = str(pos.get("coin") or "").upper()
            entry = float(pos.get("entryPx") or 0)
            out.append({
                "coin": coin,
                "szi": szi,
                "side": "long" if szi > 0 else "short",
                "entry_px": entry,
                "position_value": float(pos.get("positionValue") or 0),
                "leverage": float((pos.get("leverage") or {}).get("value") or 0)
                if isinstance(pos.get("leverage"), dict)
                else float(pos.get("leverage") or 0),
                "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
            })
        return out

    # ── sync ───────────────────────────────────────────────────
    async def _tick(self):
        with self._lock:
            targets = [t for t in self._targets.values() if t.active]
        for t in targets:
            try:
                await self._sync_target(t)
            except Exception as e:
                with self._lock:
                    if t.address in self._targets:
                        self._targets[t.address].last_error = str(e)
                log.warning("sync %s: %s", t.address[:10], e)

    async def _sync_target(self, t: MirrorTarget):
        hl_pos = await self.fetch_hl_positions(t.address)
        # filter mappable
        desired: Dict[str, Dict] = {}
        for p in hl_pos:
            okx_coin = HL_TO_OKX.get(p["coin"]) or HL_TO_OKX.get(p["coin"].lstrip("K"))
            if not okx_coin:
                continue
            desired[okx_coin] = {**p, "okx_coin": okx_coin, "inst_id": f"{okx_coin}-USDT-SWAP"}

        # limit to top by |position_value|
        if len(desired) > self.config.max_positions:
            top = sorted(desired.values(), key=lambda x: abs(float(x.get("position_value") or 0)), reverse=True)
            desired = {x["okx_coin"]: x for x in top[: self.config.max_positions]}

        client = None
        if self.client_manager:
            client = self.client_manager.get_client()

        our_pos = await self._okx_positions(client)

        # Close mirrors no longer held by leader
        for coin, m in list((t.mirrored or {}).items()):
            if coin not in desired:
                await self._close_coin(client, t, coin, m, reason="leader_flat")

        # Equal-weight capital across desired coins
        n = max(1, len(desired))
        budget = float(t.capital_usdt) / n

        for coin, d in desired.items():
            await self._align_coin(client, t, coin, d, budget, our_pos)

        with self._lock:
            if t.address in self._targets:
                self._targets[t.address].last_sync = time.time()
                self._targets[t.address].last_error = ""
                self._persist()

    async def _okx_positions(self, client) -> Dict[str, Dict]:
        if not client:
            return {}
        try:
            resp = await client.get_positions("SWAP")
            rows = resp.get("data") or []
        except Exception:
            return {}
        out = {}
        for r in rows:
            try:
                sz = float(r.get("pos") or 0)
            except (TypeError, ValueError):
                sz = 0.0
            if abs(sz) < 1e-12:
                continue
            inst = r.get("instId") or ""
            coin = inst.replace("-USDT-SWAP", "").replace("-USDT", "")
            side = (r.get("posSide") or "").lower()
            if side not in ("long", "short"):
                side = "long" if sz > 0 else "short"
            out[coin] = {
                "inst_id": inst,
                "sz": abs(sz),
                "side": side,
                "avg_px": float(r.get("avgPx") or 0),
            }
        return out

    async def _align_coin(self, client, t: MirrorTarget, coin: str, d: Dict, budget: float, our_pos: Dict):
        inst = d["inst_id"]
        side = d["side"]
        entry = float(d.get("entry_px") or 0)
        # mark price approx from position value / |szi|
        szi = abs(float(d.get("szi") or 0))
        px = entry
        if szi > 0 and abs(float(d.get("position_value") or 0)) > 0:
            px = abs(float(d["position_value"])) / szi
        if px <= 0 and client:
            try:
                tick = await client.get_ticker(inst)
                px = float((tick.get("data") or [{}])[0].get("last") or 0)
            except Exception:
                px = 0
        if px <= 0:
            self._log(t, "skip", coin=coin, reason="no_price")
            return

        ct = CT_VAL.get(coin, 0.01)
        # target contracts from budget notional
        notional = max(0.0, float(budget))
        if notional < self.config.min_notional_usd:
            return
        contracts = notional / (px * ct) if px * ct > 0 else 0
        lot = LOT_SZ.get(coin, 0.01)
        contracts = max(lot, round(contracts / lot) * lot)

        cur = our_pos.get(coin)
        mirrored = (t.mirrored or {}).get(coin)

        # Already aligned?
        if cur and cur["side"] == side and abs(cur["sz"] - contracts) / max(contracts, 1e-9) < 0.35:
            t.mirrored[coin] = {
                "side": side, "okx_sz": cur["sz"], "inst_id": inst,
                "hl_szi": d.get("szi"), "updated": time.time(),
                "entry": float(cur.get("avg_px") or 0),
            }
            return

        # Side flip or new
        if cur and cur["side"] != side:
            await self._close_coin(client, t, coin, {"inst_id": inst, "side": cur["side"], "okx_sz": cur["sz"]}, reason="flip")
            cur = None

        if not self.config.execute or not client:
            self._log(t, "signal", coin=coin, side=side, sz=contracts, notional=notional, execute=False)
            t.mirrored[coin] = {
                "side": side, "okx_sz": contracts, "inst_id": inst,
                "hl_szi": d.get("szi"), "updated": time.time(), "paper": True,
                "entry": float(px or 0),
            }
            return

        lev = min(float(t.max_leverage), self.config.max_leverage)
        for ps in (side, "net", None):
            try:
                await client.set_leverage(inst, lev, mgn_mode=self.config.td_mode, pos_side=ps or "net")
            except Exception:
                pass

        order_side = "buy" if side == "long" else "sell"
        # If already same side but size off — simplify: close and reopen
        if cur and cur["side"] == side:
            await self._close_coin(client, t, coin, {"inst_id": inst, "side": side, "okx_sz": cur["sz"]}, reason="resize")

        sz_str = self._fmt_sz(coin, contracts)
        try:
            resp = await client.place_order(
                inst_id=inst,
                side=order_side,
                ord_type="market",
                sz=sz_str,
                td_mode=self.config.td_mode,
                pos_side=side,
            )
            if resp.get("error") or (resp.get("code") and resp.get("code") != "0"):
                # net mode retry
                resp = await client.place_order(
                    inst_id=inst,
                    side=order_side,
                    ord_type="market",
                    sz=sz_str,
                    td_mode=self.config.td_mode,
                    pos_side=None,
                )
            ok = not resp.get("error") and (resp.get("code") in (None, "0"))
            self._log(t, "open" if ok else "open_error", coin=coin, side=side, sz=sz_str, resp=str(resp)[:240])
            if ok:
                t.mirrored[coin] = {
                    "side": side, "okx_sz": contracts, "inst_id": inst,
                    "hl_szi": d.get("szi"), "updated": time.time(),
                    "entry": float(px or 0),
                }
                try:
                    from .smart_money_ledger import get_sm_ledger
                    get_sm_ledger().record_open(
                        kind="mirror", symbol=coin, side=side,
                        size=float(contracts), price=float(px or 0),
                        leader=t.address, source="hyperliquid",
                        note=f"mirror open {inst}",
                    )
                except Exception:
                    pass
                try:
                    from .position_claim import claim_open
                    if self.db:
                        await claim_open(self.db, "smart_money", inst, side, float(contracts), float(px or entry or 0))
                except Exception:
                    pass
                # Telegram notifications disabled for Smart Money (too noisy)
        except Exception as e:
            self._log(t, "open_error", coin=coin, reason=str(e))
            t.last_error = str(e)

    async def _close_coin(self, client, t: MirrorTarget, coin: str, m: Dict, reason: str):
        inst = m.get("inst_id") or f"{coin}-USDT-SWAP"
        side = m.get("side") or "long"
        if not self.config.execute or not client:
            self._log(t, "signal_close", coin=coin, reason=reason, execute=False)
            t.mirrored.pop(coin, None)
            return
        try:
            # try close-position endpoint first
            try:
                resp = await client.close_position(inst_id=inst, mgn_mode=self.config.td_mode, pos_side=side)
            except Exception:
                resp = None
            if not resp or resp.get("error") or (resp.get("code") and resp.get("code") != "0"):
                close_side = "sell" if side == "long" else "buy"
                sz = m.get("okx_sz") or 0
                if sz:
                    resp = await client.place_order(
                        inst_id=inst,
                        side=close_side,
                        ord_type="market",
                        sz=self._fmt_sz(coin, float(sz)),
                        td_mode=self.config.td_mode,
                        pos_side=side,
                        reduce_only=True,
                    )

            # Extract close ordId for fill-based PnL lookup
            close_ord_id = ""
            exit_px = 0.0
            close_pnl = 0.0
            if resp:
                try:
                    d0 = (resp.get("data") or [{}])[0]
                    close_ord_id = str(d0.get("ordId") or "").strip()
                except Exception:
                    pass

            # Real PnL from OKX fills (most accurate)
            if close_ord_id and client:
                try:
                    from .pnl_utils import pnl_from_okx_fills
                    fl = await client.get_fills(inst_id=inst, ordId=close_ord_id, limit=100)
                    frows = fl.get("data") or []
                    if frows:
                        net, avg, _fees, _fsz = pnl_from_okx_fills(frows, 0.0)
                        if avg is not None and avg > 0:
                            exit_px = avg
                        if net is not None:
                            close_pnl = net
                except Exception:
                    pass

            # Fallback: mark price from ticker
            if exit_px <= 0 and client:
                try:
                    tick = await client.get_ticker(inst)
                    exit_px = float((tick.get("data") or [{}])[0].get("last") or 0)
                except Exception:
                    exit_px = 0

            # Fallback compute: if no fill PnL, derive from entry × ct_val
            if close_pnl == 0 and exit_px > 0:
                entry = float(m.get("entry") or 0)
                sz = float(m.get("okx_sz") or 0)
                ct = CT_VAL.get(coin, 0.01)
                if entry > 0 and sz > 0 and ct > 0:
                    if side == "long":
                        close_pnl = (exit_px - entry) * sz * ct
                    else:
                        close_pnl = (entry - exit_px) * sz * ct

            self._log(t, "close", coin=coin, reason=reason,
                      exit_px=round(exit_px, 4), pnl=round(close_pnl, 4),
                      ord=close_ord_id[:12] if close_ord_id else "")
            try:
                from .smart_money_ledger import get_sm_ledger
                get_sm_ledger().record_close(
                    kind="mirror", symbol=coin, side=side,
                    size=float(m.get("okx_sz") or 0),
                    price=round(exit_px, 6),
                    pnl=round(close_pnl, 4),
                    leader=t.address, source="hyperliquid",
                    note=f"mirror close ({reason})",
                )
            except Exception:
                pass
            try:
                from .position_claim import release_open
                if self.db:
                    await release_open(self.db, "smart_money", inst, side)
            except Exception:
                pass
        except Exception as e:
            self._log(t, "close_error", coin=coin, reason=str(e))
        t.mirrored.pop(coin, None)

    async def _close_all_mirrored(self, t: MirrorTarget):
        client = self.client_manager.get_client() if self.client_manager else None
        for coin, m in list((t.mirrored or {}).items()):
            await self._close_coin(client, t, coin, m, reason="stop_mirror")

    # ── utils ──────────────────────────────────────────────────
    @staticmethod
    def _fmt_sz(coin: str, sz: float) -> str:
        lot = LOT_SZ.get(coin, 0.01)
        prec = 8 if lot < 0.01 else (4 if lot < 1 else 2)
        s = f"{float(sz):.{prec}f}".rstrip("0").rstrip(".")
        return s or "0"

    def _log(self, t: MirrorTarget, event: str, **kw):
        rec = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
        t.log.append(rec)
        t.log = t.log[-80:]
        log.info("mirror %s %s %s", t.address[:10], event, kw)

    def _target_public(self, t: MirrorTarget) -> Dict:
        return {
            "address": t.address,
            "alias": t.alias,
            "capital_usdt": t.capital_usdt,
            "max_leverage": t.max_leverage,
            "active": t.active,
            "started_at": t.started_at,
            "last_sync": t.last_sync,
            "last_error": t.last_error,
            "mirrored": t.mirrored,
            "recent_log": list(t.log[-10:]),
            "unique_code": f"hl:{t.address}",
            "source": "hyperliquid",
        }

    def _state_path(self) -> str:
        return os.path.join(DATA_DIR, "smart_money_mirror.json")

    def _snapshot(self) -> dict:
        return {
            "config": asdict(self.config),
            "targets": {
                a: {
                    "address": t.address,
                    "alias": t.alias,
                    "capital_usdt": t.capital_usdt,
                    "max_leverage": t.max_leverage,
                    "active": t.active,
                    "started_at": t.started_at,
                    "mirrored": t.mirrored,
                }
                for a, t in self._targets.items()
            },
        }

    def _apply_snapshot(self, data: dict):
        if not data:
            return
        cfg = data.get("config") or {}
        for k, v in cfg.items():
            if hasattr(self.config, k):
                setattr(self.config, k, v)
        for a, td in (data.get("targets") or {}).items():
            t = MirrorTarget(
                address=td.get("address") or a,
                alias=td.get("alias") or "",
                capital_usdt=float(td.get("capital_usdt") or 100),
                max_leverage=float(td.get("max_leverage") or 3),
                active=bool(td.get("active", True)),
                started_at=float(td.get("started_at") or 0),
                mirrored=td.get("mirrored") or {},
            )
            self._targets[t.address] = t

    def _persist(self):
        """Write mirror state to the local file only.

        The mirror runs in its own thread/loop while asyncpg is bound to the
        main uvicorn loop. Writing DB from this thread raises 'attached to a
        different loop' and can crash the process, so we NEVER touch the DB
        here. Main-thread callers use persist_to_db() separately.
        """
        data = self._snapshot()
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(self._state_path(), "w") as f:
                json.dump(data, f)
        except Exception as e:
            log.warning("persist mirror file: %s", e)

    async def persist_to_db(self):
        """Persist mirror state to DB. MUST be called from the main thread."""
        if not self.db:
            return
        try:
            await self.db.set_setting("sm_mirror_state", json.dumps(self._snapshot(), default=str))
        except Exception as e:
            log.warning("persist_to_db mirror: %s", e)

    def _load(self):
        # file first (fast), DB overrides on async hydrate
        try:
            path = self._state_path()
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                self._apply_snapshot(data)
        except Exception as e:
            log.warning("load mirror file: %s", e)

    async def hydrate_from_db(self):
        """Load durable mirror targets after deploy (Render /tmp is wiped)."""
        if not self.db:
            return
        try:
            raw = await self.db.get_setting("sm_mirror_state")
            if not raw:
                return
            data = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(data, dict):
                return
            # Prefer DB when it has targets
            if data.get("targets"):
                self._targets.clear()
                self._apply_snapshot(data)
                # also refresh local file
                try:
                    os.makedirs(DATA_DIR, exist_ok=True)
                    with open(self._state_path(), "w") as f:
                        json.dump(data, f)
                except Exception:
                    pass
                log.info("mirror hydrated from DB: %d targets", len(self._targets))
                # Re-assert DB claims for mirrored coins after deploy
                try:
                    from .position_claim import claim_open
                    import asyncio
                    async def _reclaim():
                        if not self.db:
                            return
                        for t in self._targets.values():
                            for coin, m in (t.mirrored or {}).items():
                                inst = (m or {}).get("inst_id") or f"{coin}-USDT-SWAP"
                                side = (m or {}).get("side") or "long"
                                sz = float((m or {}).get("okx_sz") or 0)
                                px = float((m or {}).get("entry") or (m or {}).get("entry_price") or 0)
                                if sz > 0 and px > 0:
                                    await claim_open(self.db, "smart_money", inst, side, sz, px)
                    try:
                        loop = asyncio.get_running_loop()
                        asyncio.ensure_future(_reclaim())
                    except RuntimeError:
                        # No running loop in this thread — skip reclaim
                        try:
                            main_loop = getattr(self, "_main_loop", None)
                            if main_loop and main_loop.is_running():
                                main_loop.call_soon_threadsafe(
                                    lambda: asyncio.ensure_future(_reclaim())
                                )
                        except Exception:
                            pass
                except Exception as e:
                    log.warning("mirror reclaim on hydrate: %s", e)
                except Exception as e:
                    log.warning("mirror reclaim on hydrate: %s", e)
                # auto-resume polling if any active
                if any(t.active for t in self._targets.values()) and not self._running:
                    self.start()
        except Exception as e:
            log.warning("hydrate mirror db: %s", e)


# singleton helper for main
_mirror: Optional[SmartMoneyMirror] = None


def get_mirror(client_manager=None, notifier=None, db=None) -> SmartMoneyMirror:
    global _mirror
    if _mirror is None:
        _mirror = SmartMoneyMirror(client_manager=client_manager, notifier=notifier, db=db)
    else:
        if client_manager and not _mirror.client_manager:
            _mirror.client_manager = client_manager
        # do not attach telegram notifier to Smart Money mirror
        if db and not getattr(_mirror, 'db', None):
            _mirror.db = db
    return _mirror
