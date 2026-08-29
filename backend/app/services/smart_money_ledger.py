"""Isolated PnL + trade ledger for Smart Money (OKX copy + HL mirror).

Never mixed into strategy bots' trade logs — only served via /api/smart-money/*.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DATA_DIR = os.getenv("DATA_DIR", "/tmp")
BOT_ID = "smart_money"
LEDGER_PATH = os.path.join(DATA_DIR, "smart_money_ledger.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SmTrade:
    id: str
    kind: str  # copy | mirror
    event: str  # open | close | adjust | info
    symbol: str = ""
    side: str = ""
    size: float = 0.0
    price: float = 0.0
    pnl: float = 0.0
    fee: float = 0.0
    leader: str = ""
    source: str = ""  # okx | hyperliquid
    status: str = "open"  # open | closed
    opened_at: str = ""
    closed_at: str = ""
    note: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class SmartMoneyLedger:
    def __init__(self):
        self._lock = threading.Lock()
        self._trades: List[Dict[str, Any]] = []
        self._open: Dict[str, Dict[str, Any]] = {}  # key leader|symbol|side
        self._realized_pnl: float = 0.0
        self._load()

    def _key(self, leader: str, symbol: str, side: str) -> str:
        return f"{leader}|{symbol}|{side}".lower()

    def record_open(
        self,
        *,
        kind: str,
        symbol: str,
        side: str,
        size: float = 0.0,
        price: float = 0.0,
        leader: str = "",
        source: str = "",
        note: str = "",
        meta: Optional[Dict] = None,
    ) -> Dict:
        with self._lock:
            tid = uuid.uuid4().hex[:12]
            tr = {
                "id": tid,
                "bot_id": BOT_ID,
                "kind": kind,
                "event": "open",
                "symbol": symbol,
                "side": side,
                "size": float(size or 0),
                "price": float(price or 0),
                "pnl": 0.0,
                "fee": 0.0,
                "leader": leader,
                "source": source,
                "status": "open",
                "opened_at": _now_iso(),
                "closed_at": "",
                "note": note,
                "meta": meta or {},
                "time": _now_iso(),
            }
            self._trades.insert(0, tr)
            self._open[self._key(leader, symbol, side)] = dict(tr)
            self._trades = self._trades[:500]
            self._persist()
            return tr

    def record_close(
        self,
        *,
        kind: str,
        symbol: str,
        side: str,
        size: float = 0.0,
        price: float = 0.0,
        pnl: float = 0.0,
        fee: float = 0.0,
        leader: str = "",
        source: str = "",
        note: str = "",
        meta: Optional[Dict] = None,
    ) -> Dict:
        with self._lock:
            k = self._key(leader, symbol, side)
            open_tr = self._open.pop(k, None)
            # Also try match by symbol only
            if not open_tr:
                for kk in list(self._open.keys()):
                    if kk.endswith(f"|{symbol.lower()}|{side.lower()}") or (
                        leader and kk.startswith(leader.lower())
                    ):
                        open_tr = self._open.pop(kk, None)
                        break

            entry = float((open_tr or {}).get("price") or 0)
            sz = float(size or (open_tr or {}).get("size") or 0)
            realized = float(pnl or 0)
            if realized == 0 and entry > 0 and price > 0 and sz > 0:
                if side == "long":
                    realized = (float(price) - entry) * sz
                else:
                    realized = (entry - float(price)) * sz
            realized -= float(fee or 0)
            self._realized_pnl += realized

            tid = (open_tr or {}).get("id") or uuid.uuid4().hex[:12]
            # mark original open as closed
            for t in self._trades:
                if t.get("id") == tid and t.get("event") == "open":
                    t["status"] = "closed"
                    t["closed_at"] = _now_iso()
                    t["pnl"] = round(realized, 4)

            tr = {
                "id": tid + "_c",
                "bot_id": BOT_ID,
                "kind": kind,
                "event": "close",
                "symbol": symbol,
                "side": side,
                "size": sz,
                "price": float(price or 0),
                "entry_price": entry,
                "pnl": round(realized, 4),
                "fee": float(fee or 0),
                "leader": leader or (open_tr or {}).get("leader") or "",
                "source": source or (open_tr or {}).get("source") or "",
                "status": "closed",
                "opened_at": (open_tr or {}).get("opened_at") or "",
                "closed_at": _now_iso(),
                "note": note,
                "meta": meta or {},
                "time": _now_iso(),
            }
            self._trades.insert(0, tr)
            self._trades = self._trades[:500]
            self._persist()
            return tr

    def record_info(self, *, kind: str, note: str, leader: str = "", meta: Optional[Dict] = None):
        with self._lock:
            tr = {
                "id": uuid.uuid4().hex[:12],
                "bot_id": BOT_ID,
                "kind": kind,
                "event": "info",
                "symbol": "",
                "side": "",
                "size": 0,
                "price": 0,
                "pnl": 0,
                "leader": leader,
                "source": "",
                "status": "info",
                "note": note,
                "meta": meta or {},
                "time": _now_iso(),
            }
            self._trades.insert(0, tr)
            self._trades = self._trades[:500]
            self._persist()
            return tr

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            closed = [t for t in self._trades if t.get("event") == "close"]
            opens = list(self._open.values())
            wins = sum(1 for t in closed if float(t.get("pnl") or 0) > 0)
            losses = sum(1 for t in closed if float(t.get("pnl") or 0) < 0)
            n = len(closed)
            return {
                "bot_id": BOT_ID,
                "realized_pnl": round(self._realized_pnl, 4),
                "open_count": len(opens),
                "closed_count": n,
                "win_count": wins,
                "loss_count": losses,
                "win_rate": round(wins / n, 4) if n else 0.0,
                "open_positions": opens,
                "trades": list(self._trades[:100]),
            }

    def trades(self, limit: int = 100) -> List[Dict]:
        with self._lock:
            return list(self._trades[: max(1, min(int(limit), 300))])

    def _persist(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(LEDGER_PATH, "w") as f:
                json.dump(
                    {
                        "realized_pnl": self._realized_pnl,
                        "open": self._open,
                        "trades": self._trades[:300],
                        "updated_at": _now_iso(),
                    },
                    f,
                )
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(LEDGER_PATH):
                return
            with open(LEDGER_PATH) as f:
                data = json.load(f)
            self._realized_pnl = float(data.get("realized_pnl") or 0)
            self._open = data.get("open") or {}
            self._trades = data.get("trades") or []
        except Exception:
            pass


_ledger: Optional[SmartMoneyLedger] = None
_ledger_lock = threading.Lock()


def get_sm_ledger() -> SmartMoneyLedger:
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            _ledger = SmartMoneyLedger()
        return _ledger
