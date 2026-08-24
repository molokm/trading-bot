"""Order-Book Imbalance (OBI) Scalping — selective micro-structure strategy v0.2."""
from __future__ import annotations

import asyncio
import json
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from .risk_guard import assert_can_open
from .analysis_logger import get_logger
from .pnl_utils import extract_fill_avg, close_pnl, fee_cost
from .ai_agent import _openai_compatible

SCALP_BOT_ID = "orderbook_scalp"
STRATEGY_NAME = "Order Book Scalp"
STRATEGY_VERSION = "v0.2"
STRATEGY_DESC = (
    "Order Book Scalp v0.2 — селективный OBI-скальп. Жёстче фильтр (|OBI|≥0.5, persist 5, "
    "cooldown, лимит сделок/час), тейк с запасом под taker-комиссию (~10 bps RT). "
    "Накопительный PnL (lifetime) не сбрасывается при рестарте. Не HFT / не гарантия прибыли."
)

CT_VAL = {"BTC": 0.01, "ETH": 0.1, "SOL": 1.0, "XRP": 100.0}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "SOL": 0.1, "XRP": 0.01}


def _scalp_state_path() -> str:
    base = os.getenv("DATA_DIR") or os.getenv("RENDER_DISK_PATH") or "/tmp"
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = "/tmp"
    return os.path.join(base, "orderbook_scalp_state.json")


def load_scalp_state() -> dict:
    try:
        with open(_scalp_state_path(), "r") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_scalp_state(payload: dict) -> None:
    try:
        path = _scalp_state_path()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[Scalp] state save error: {e}", flush=True)


@dataclass
class ScalpConfig:
    symbols: list = None
    capital: float = 200.0
    max_leverage: float = 2.0
    risk_per_trade: float = 0.002
    allocation_pct: float = 0.04
    levels: int = 10
    obi_threshold: float = 0.50
    persist_n: int = 5
    max_spread_bps: float = 5.0
    stop_bps: float = 15.0
    take_bps: float = 30.0
    fee_bps_rt: float = 10.0
    max_hold_sec: float = 75.0
    poll_interval_sec: float = 2.5
    cooldown_sec: float = 60.0
    min_signal_gap_sec: float = 45.0
    max_trades_per_hour: int = 8
    use_llm: bool = False
    execute: bool = None
    notify_telegram: bool = False
    min_wall_mult: float = 3.0

    def __post_init__(self):
        if self.symbols is None:
            self.symbols = ["BTC", "ETH", "SOL"]


@dataclass
class ScalpPosition:
    coin: str
    inst_id: str
    side: str
    size: float
    entry_price: float
    stop_price: float
    take_price: float
    leverage: float
    opened_at: float
    signal: dict = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_book_metrics(bids: list, asks: list, levels: int = 10) -> dict:
    def parse(side):
        out = []
        for row in side[:levels]:
            try:
                px = float(row[0])
                sz = float(row[1])
                if px > 0 and sz > 0:
                    out.append((px, sz))
            except (TypeError, ValueError, IndexError):
                continue
        return out

    b = parse(bids)
    a = parse(asks)
    if not b or not a:
        return {"ok": False, "reason": "empty_book"}

    bid1, bid1_sz = b[0]
    ask1, ask1_sz = a[0]
    mid = (bid1 + ask1) / 2.0
    spread = ask1 - bid1
    spread_bps = (spread / mid) * 10000.0 if mid > 0 else 999.0

    def obi_n(n: int) -> float:
        bv = sum(sz for _, sz in b[:n])
        av = sum(sz for _, sz in a[:n])
        tot = bv + av
        return (bv - av) / tot if tot > 0 else 0.0

    decay = 0.85
    wb = sum(sz * (decay ** i) for i, (_, sz) in enumerate(b[:levels]))
    wa = sum(sz * (decay ** i) for i, (_, sz) in enumerate(a[:levels]))
    wtot = wb + wa
    w_obi = (wb - wa) / wtot if wtot > 0 else 0.0
    micro = (ask1 * bid1_sz + bid1 * ask1_sz) / (bid1_sz + ask1_sz) if (bid1_sz + ask1_sz) else mid
    all_sz = [sz for _, sz in b[:levels]] + [sz for _, sz in a[:levels]]
    med = sorted(all_sz)[len(all_sz) // 2] if all_sz else 0.0
    walls_bid = [{"px": px, "sz": sz} for px, sz in b if med > 0 and sz >= med * 3]
    walls_ask = [{"px": px, "sz": sz} for px, sz in a if med > 0 and sz >= med * 3]
    ratio = (sum(sz for _, sz in b[:5]) / max(1e-12, sum(sz for _, sz in a[:5])))

    return {
        "ok": True,
        "bid1": bid1, "ask1": ask1, "bid1_sz": bid1_sz, "ask1_sz": ask1_sz,
        "mid": mid, "micro": micro, "spread": spread,
        "spread_bps": round(spread_bps, 3),
        "obi_1": round(obi_n(1), 4), "obi_5": round(obi_n(5), 4),
        "obi_10": round(obi_n(min(10, levels)), 4), "w_obi": round(w_obi, 4),
        "bid_vol_5": round(sum(sz for _, sz in b[:5]), 4),
        "ask_vol_5": round(sum(sz for _, sz in a[:5]), 4),
        "ratio_5": round(ratio, 3),
        "walls_bid": walls_bid[:3], "walls_ask": walls_ask[:3],
        "bids": [{"px": px, "sz": sz} for px, sz in b],
        "asks": [{"px": px, "sz": sz} for px, sz in a],
        "ts": _now_iso(),
    }


class OrderBookScalpStrategy:
    BOT_ID = SCALP_BOT_ID
    BOT_NAME = STRATEGY_NAME
    STRATEGY_NAME = STRATEGY_NAME
    STRATEGY_VERSION = STRATEGY_VERSION
    STRATEGY_DESC = STRATEGY_DESC

    def __init__(self, config: ScalpConfig = None, client_manager=None, db=None,
                 notifier=None, analysis=None):
        self.config = config or ScalpConfig()
        self.client_manager = client_manager
        self.db = db
        self.notifier = None
        self.analysis = analysis or get_logger()
        self._running = False
        self._thread = None
        self._loop = None
        st = load_scalp_state()
        self._capital = float(self.config.capital)
        self._session_pnl = 0.0
        self._lifetime_pnl = float(st.get("lifetime_pnl") or 0.0)
        self._lifetime_trades = int(st.get("lifetime_trades") or 0)
        self._lifetime_wins = int(st.get("lifetime_wins") or 0)
        self._lifetime_fees = float(st.get("lifetime_fees") or 0.0)
        self._equity = self._capital
        self._last_close_ts = float(st.get("last_close_ts") or 0.0)
        self._last_open_ts = float(st.get("last_open_ts") or 0.0)
        self._hour_trade_ts: deque = deque(maxlen=200)
        for ts in (st.get("hour_trade_ts") or [])[-50:]:
            try:
                self._hour_trade_ts.append(float(ts))
            except (TypeError, ValueError):
                pass
        self._seed_db = self._lifetime_trades == 0
        self._positions: dict[str, ScalpPosition] = {}
        self._history: dict[str, deque] = {
            s: deque(maxlen=max(20, self.config.persist_n * 4)) for s in self.config.symbols
        }
        self._latest: dict[str, dict] = {}
        self._signals: list = []
        self._trade_log: list = []
        self._last_llm: Optional[dict] = None
        self._last_activity = None
        self._started_at = None
        self._last_exec = None
        self._avail_usdt = None
        self._acct_avail = None
        self._acct_eq = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._started_at = _now_iso()
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="ob-scalp")
        self._thread.start()
        print(f"[Scalp {STRATEGY_VERSION}] started execute={self._execute_enabled()} "
              f"symbols={self.config.symbols}", flush=True)

    def stop(self):
        self._running = False
        try:
            self._persist()
        except Exception:
            pass
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        print(f"[Scalp {STRATEGY_VERSION}] stopped", flush=True)

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def _execute_enabled(self) -> bool:
        if self.config.execute is not None:
            return bool(self.config.execute)
        env = os.getenv("SCALP_EXECUTE", os.getenv("AI_EXECUTE", "")).strip().lower()
        if env in ("1", "true", "yes", "on"):
            return True
        if env in ("0", "false", "no", "off"):
            return False
        return os.getenv("OKX_DEMO", "true").lower() in ("1", "true", "yes", "on")

    def _fmt_sz(self, coin: str, sz: float) -> str:
        lot = LOT_SZ.get(coin, 0.01)
        prec = 8 if lot < 0.01 else (4 if lot < 1 else 2)
        s = f"{float(sz):.{prec}f}".rstrip("0").rstrip(".")
        return s or "0"

    async def _run(self):
        if getattr(self, "_seed_db", False) and self.db is not None:
            try:
                rows = await self.db.get_metrics(bot_id=SCALP_BOT_ID, limit=1)
                if rows:
                    tp = float(rows[0].get("total_pnl") or 0)
                    if abs(tp) > abs(self._lifetime_pnl):
                        self._lifetime_pnl = tp
                        self._persist()
            except Exception as e:
                print(f"[Scalp] seed: {e}", flush=True)
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                print(f"[Scalp] tick error: {e}", flush=True)
            self._last_activity = _now_iso()
            await asyncio.sleep(max(0.5, float(self.config.poll_interval_sec)))

    async def _client(self):
        if not self.client_manager:
            return None
        return self.client_manager.get_client()

    async def _tick(self):
        client = await self._client()
        if not client:
            return
        for coin in list(self._positions.keys()):
            await self._manage_position(client, coin)
        if len(self._positions) >= 1:
            return
        for coin in self.config.symbols:
            try:
                await self._scan_symbol(client, coin)
            except Exception as e:
                print(f"[Scalp] scan {coin}: {e}", flush=True)

    async def _fetch_metrics(self, client, coin: str) -> Optional[dict]:
        inst = f"{coin}-USDT-SWAP"
        resp = await client.get_books(inst, sz=self.config.levels)
        if resp.get("error"):
            return None
        data = (resp.get("data") or [{}])[0]
        metrics = compute_book_metrics(
            data.get("bids") or [], data.get("asks") or [], levels=self.config.levels,
        )
        if not metrics.get("ok"):
            return None
        metrics["coin"] = coin
        metrics["inst_id"] = inst
        self._latest[coin] = metrics
        self._history[coin].append(metrics)
        return metrics

    def _persistence_signal(self, coin: str) -> Optional[str]:
        hist = list(self._history.get(coin) or [])
        n = int(self.config.persist_n)
        if len(hist) < n:
            return None
        window = hist[-n:]
        th = float(self.config.obi_threshold)
        signs = []
        for m in window:
            o = float(m.get("obi_5") or 0)
            if o >= th:
                signs.append(1)
            elif o <= -th:
                signs.append(-1)
            else:
                signs.append(0)
        if all(s == 1 for s in signs):
            return "long"
        if all(s == -1 for s in signs):
            return "short"
        return None

    async def _llm_confirm(self, metrics: dict, side: str) -> dict:
        if not self.config.use_llm:
            return {"confirm": True, "confidence": 0.6, "reason": "llm_off"}
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            return {"confirm": True, "confidence": 0.55, "reason": "no_llm_key"}
        summary = {
            "coin": metrics.get("coin"), "side_candidate": side,
            "obi_1": metrics.get("obi_1"), "obi_5": metrics.get("obi_5"),
            "obi_10": metrics.get("obi_10"), "w_obi": metrics.get("w_obi"),
            "spread_bps": metrics.get("spread_bps"), "ratio_5": metrics.get("ratio_5"),
            "micro": metrics.get("micro"), "mid": metrics.get("mid"),
            "walls_bid": metrics.get("walls_bid"), "walls_ask": metrics.get("walls_ask"),
        }
        system = (
            'You are a crypto order-book scalper. Reply ONLY JSON: '
            '{"confirm": true|false, "confidence": 0-1, "reason": "short"}. '
            "Prefer false when unsure."
        )
        try:
            raw = await _openai_compatible(
                api_key=key,
                base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
                model=os.getenv("AI_LLM_MODEL", "openai/gpt-oss-20b"),
                system=system,
                user=str(summary),
            )
            import re
            m = re.search(r"\{[\s\S]*\}", raw or "")
            data = json.loads(m.group(0)) if m else {}
            return {
                "confirm": bool(data.get("confirm")),
                "confidence": float(data.get("confidence") or 0),
                "reason": str(data.get("reason") or "")[:200],
            }
        except Exception as e:
            return {"confirm": True, "confidence": 0.5, "reason": f"llm_err:{e}"}

    async def _scan_symbol(self, client, coin: str):
        metrics = await self._fetch_metrics(client, coin)
        if not metrics:
            return
        if float(metrics.get("spread_bps") or 99) > float(self.config.max_spread_bps):
            return
        side = self._persistence_signal(coin)
        if not side:
            return
        llm = await self._llm_confirm(metrics, side)
        self._last_llm = {**llm, "coin": coin, "side": side, "time": _now_iso()}
        sig = {
            "time": _now_iso(), "coin": coin, "side": side,
            "obi_5": metrics.get("obi_5"), "w_obi": metrics.get("w_obi"),
            "spread_bps": metrics.get("spread_bps"), "llm": llm, "mid": metrics.get("mid"),
        }
        self._signals.append(sig)
        self._signals = self._signals[-50:]
        if self.config.use_llm:
            if not llm.get("confirm") or float(llm.get("confidence") or 0) < 0.55:
                return
        if abs(float(metrics.get("w_obi") or 0)) < float(self.config.obi_threshold) * 0.85:
            return
        if abs(float(metrics.get("obi_1") or 0)) < 0.25:
            return
        ok, why = self._can_open_now()
        if not ok:
            self._last_exec = {"event": "skip", "reason": why, "coin": coin, "side": side}
            return
        await self._open(client, coin, side, metrics, sig)

    async def _refresh_balance(self, client) -> float:
        try:
            resp = await client.get_balance()
            if resp.get("error"):
                self._avail_usdt = 0.0
                self._acct_avail = 0.0
                return 0.0
            acct_avail = acct_eq = usdt_avail = 0.0
            for row in resp.get("data") or []:
                try:
                    acct_avail = float(row.get("availEq") or 0) or acct_avail
                    acct_eq = float(row.get("totalEq") or row.get("eq") or 0) or acct_eq
                except (TypeError, ValueError):
                    pass
                for d in row.get("details") or []:
                    if (d.get("ccy") or "").upper() != "USDT":
                        continue
                    try:
                        usdt_avail = float(d.get("availEq") or d.get("availBal") or d.get("cashBal") or 0)
                    except (TypeError, ValueError):
                        pass
            self._acct_avail = float(acct_avail)
            self._acct_eq = float(acct_eq)
            self._avail_usdt = float(usdt_avail)
            buying = max(acct_avail, usdt_avail)
            usable = min(float(self.config.capital), buying * 0.85) if buying > 0 else 0.0
            return usable
        except Exception as e:
            print(f"[Scalp] balance: {e}", flush=True)
            self._avail_usdt = 0.0
            self._acct_avail = 0.0
            return 0.0

    def _size_order(self, coin: str, entry: float, equity_cap: float = None) -> tuple:
        ct = CT_VAL.get(coin, 0.01)
        lot = LOT_SZ.get(coin, 0.01)
        avail = max(float(self._acct_avail or 0), float(self._avail_usdt or 0))
        if avail < 8.0:
            return 0.0, 1.0
        # User-allocated capital for this strategy (still capped by free margin)
        alloc = float(self.config.capital)
        base = float(equity_cap if equity_cap is not None else alloc)
        if base <= 0:
            base = alloc
        base = min(base, alloc, avail * 0.85)
        stop_pct = max(0.0008, float(self.config.stop_bps) / 10000.0)
        risk_usd = base * float(self.config.risk_per_trade)
        notional = risk_usd / stop_pct if stop_pct > 0 else 0
        # Leverage 1–3× (scalp-safe); higher lev → less margin for same notional
        lev = max(1.0, min(3.0, float(self.config.max_leverage or 1)))
        max_margin = min(base * float(self.config.allocation_pct), avail * 0.4)
        if lev > 0 and notional / lev > max_margin:
            notional = max_margin * lev
        # Notional ceiling scales with allocated capital
        notional = min(notional, max(40.0, alloc * 0.5))
        req_margin = notional / lev if lev else notional
        if entry <= 0 or ct <= 0 or req_margin > avail * 0.5 or max_margin < 2:
            return 0.0, lev
        raw = notional / (entry * ct)
        steps = math.floor(raw / lot)
        sz = max(0.0, round(steps * lot, 8))
        if sz <= 0:
            min_margin = (lot * entry * ct) / lev
            if min_margin <= avail * 0.4:
                sz = lot
            else:
                return 0.0, lev
        return sz, round(lev, 2)

    async def _open(self, client, coin: str, side: str, metrics: dict, sig: dict):
        entry = float(metrics.get("mid") or 0)
        if entry <= 0:
            return
        usable = await self._refresh_balance(client)
        buying = max(float(self._acct_avail or 0), float(self._avail_usdt or 0))
        if usable <= 0 or buying < 8:
            self._last_exec = {
                "event": "skip", "reason": "insufficient_free_margin",
                "coin": coin, "avail_usdt": self._avail_usdt, "acct_avail": self._acct_avail,
            }
            return
        sz, lev = self._size_order(coin, entry, equity_cap=usable)
        if sz <= 0:
            self._last_exec = {"event": "skip", "reason": "size=0_or_low_margin", "coin": coin}
            return
        try:
            assert_can_open(is_reduce_only=False)
        except Exception as e:
            self._last_exec = {"event": "skip", "reason": f"risk:{e}", "coin": coin}
            return

        inst = f"{coin}-USDT-SWAP"
        order_side = "buy" if side == "long" else "sell"
        stop_pct = float(self.config.stop_bps) / 10000.0
        take_pct = float(self.config.take_bps) / 10000.0

        if not self._execute_enabled():
            self._last_exec = {
                "event": "signal_only", "coin": coin, "side": side,
                "size": sz, "lev": lev, "mid": entry,
            }
            return

        try:
            await client.set_leverage(inst, lev, mgn_mode="cross", pos_side=side)
        except Exception:
            try:
                await client.set_leverage(inst, lev, mgn_mode="cross", pos_side="net")
            except Exception:
                pass

        async def _try_place(size_try, pos_side_try):
            return await client.place_order(
                inst_id=inst, side=order_side, ord_type="market",
                sz=self._fmt_sz(coin, size_try), td_mode="cross", pos_side=pos_side_try,
            )

        resp = None
        size_try = sz
        for _ in range(4):
            try:
                resp = await _try_place(size_try, side)
            except Exception as e:
                self._last_exec = {"event": "open_error", "reason": str(e), "coin": coin}
                return
            if not resp.get("error"):
                sz = size_try
                break
            msg = str(resp.get("message") or "")
            if "pos" in msg.lower() or "posside" in msg.lower():
                resp = await _try_place(size_try, None)
                if not resp.get("error"):
                    sz = size_try
                    break
                msg = str(resp.get("message") or "")
            if "51008" in msg or "Insufficient" in msg or "margin" in msg.lower():
                lot = LOT_SZ.get(coin, 0.01)
                size_try = round(math.floor(size_try / 2 / lot) * lot, 8)
                if size_try <= 0:
                    break
                continue
            break

        if not resp or resp.get("error"):
            self._last_exec = {
                "event": "open_error", "reason": (resp or {}).get("message"),
                "coin": coin, "avail_usdt": self._avail_usdt, "acct_avail": self._acct_avail,
            }
            return

        fills = resp.get("data") or []
        fill_px, fee, _ = extract_fill_avg(fills, entry)
        if not fill_px or fill_px <= 0:
            fill_px = entry
        if side == "long":
            stop = fill_px * (1 - stop_pct)
            take = fill_px * (1 + take_pct)
        else:
            stop = fill_px * (1 + stop_pct)
            take = fill_px * (1 - take_pct)

        pos = ScalpPosition(
            coin=coin, inst_id=inst, side=side, size=sz,
            entry_price=fill_px, stop_price=stop, take_price=take,
            leverage=lev, opened_at=time.time(), signal=sig,
        )
        self._positions[coin] = pos
        self._last_open_ts = time.time()
        self._hour_trade_ts.append(self._last_open_ts)
        self._equity -= fee_cost(fee)
        self._persist()
        self._last_exec = {
            "event": "open_ok", "coin": coin, "side": side,
            "entry": fill_px, "size": sz, "lev": lev,
        }
        self._trade_log.append({
            "time": _now_iso(), "event": "open", "coin": coin, "side": side,
            "px": fill_px, "sz": sz,
        })
        print(f"[Scalp] OPEN {side} {coin} @{fill_px}", flush=True)
        try:
            self.analysis.log("scalp", "open", coin=coin, side=side, entry=fill_px,
                              stop=stop, take=take, size=sz, leverage=lev)
        except Exception:
            pass

    async def _manage_position(self, client, coin: str):
        pos = self._positions.get(coin)
        if not pos:
            return
        metrics = await self._fetch_metrics(client, coin)
        if not metrics:
            return
        mid = float(metrics.get("mid") or 0)
        if mid <= 0:
            return
        held = time.time() - pos.opened_at
        reason = None
        if pos.side == "long":
            if mid <= pos.stop_price:
                reason = "sl"
            elif mid >= pos.take_price:
                reason = "tp"
        else:
            if mid >= pos.stop_price:
                reason = "sl"
            elif mid <= pos.take_price:
                reason = "tp"
        obi = float(metrics.get("obi_5") or 0)
        if reason is None:
            if pos.side == "long" and obi < -float(self.config.obi_threshold) * 0.7:
                reason = "obi_flip"
            elif pos.side == "short" and obi > float(self.config.obi_threshold) * 0.7:
                reason = "obi_flip"
        if reason is None and held >= float(self.config.max_hold_sec):
            reason = "max_hold"
        if reason:
            await self._close(client, coin, reason, mid)

    async def _close(self, client, coin: str, reason: str, mark: float = 0.0):
        pos = self._positions.get(coin)
        if not pos:
            return
        if not self._execute_enabled():
            del self._positions[coin]
            return
        close_side = "sell" if pos.side == "long" else "buy"
        try:
            resp = await client.place_order(
                inst_id=pos.inst_id, side=close_side, ord_type="market",
                sz=self._fmt_sz(coin, pos.size), td_mode="cross", pos_side=pos.side,
            )
        except Exception as e:
            self._last_exec = {"event": "close_error", "reason": str(e), "coin": coin}
            return
        if resp.get("error"):
            resp = await client.place_order(
                inst_id=pos.inst_id, side=close_side, ord_type="market",
                sz=self._fmt_sz(coin, pos.size), td_mode="cross", pos_side=None,
            )
        if resp.get("error"):
            self._last_exec = {"event": "close_error", "reason": resp.get("message"), "coin": coin}
            return
        fills = resp.get("data") or []
        fill_px, fee, _ = extract_fill_avg(fills, mark or pos.entry_price)
        if not fill_px:
            fill_px = mark or pos.entry_price
        fee_c = fee_cost(fee)
        pnl = close_pnl(pos.side, pos.size, pos.entry_price, fill_px, fee, CT_VAL.get(coin, 0.01))
        self._equity += pnl
        self._session_pnl += pnl
        self._lifetime_pnl += pnl
        self._lifetime_trades += 1
        if pnl > 0:
            self._lifetime_wins += 1
        self._lifetime_fees += fee_c
        self._last_close_ts = time.time()
        self._trade_log.append({
            "time": _now_iso(), "event": "close", "coin": coin, "side": pos.side,
            "entry": pos.entry_price, "exit": fill_px, "pnl": round(pnl, 4),
            "fee": round(fee_c, 6), "reason": reason,
        })
        self._persist()
        self._last_exec = {"event": "close_ok", "coin": coin, "pnl": round(pnl, 4), "reason": reason}
        print(f"[Scalp] CLOSE {coin} pnl={pnl:+.4f} ({reason})", flush=True)
        try:
            self.analysis.log("scalp", "close", coin=coin, pnl=round(pnl, 4), reason=reason)
        except Exception:
            pass
        del self._positions[coin]

    def _persist(self):
        save_scalp_state({
            "lifetime_pnl": self._lifetime_pnl,
            "lifetime_trades": self._lifetime_trades,
            "lifetime_wins": self._lifetime_wins,
            "lifetime_fees": self._lifetime_fees,
            "session_pnl": self._session_pnl,
            "last_close_ts": self._last_close_ts,
            "last_open_ts": self._last_open_ts,
            "hour_trade_ts": list(self._hour_trade_ts)[-50:],
            "updated_at": _now_iso(),
        })
        if self.db is not None and self._loop and not self._loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self._db_snapshot(), self._loop)
            except Exception:
                pass

    async def _db_snapshot(self):
        try:
            await self.db.ensure_bot(
                SCALP_BOT_ID, strategy_id="orderbook_scalp", name=STRATEGY_NAME,
            )
            wr = (100.0 * self._lifetime_wins / self._lifetime_trades) if self._lifetime_trades else None
            await self.db.save_metric(
                bot_id=SCALP_BOT_ID,
                equity=self._capital + self._lifetime_pnl,
                total_pnl=self._lifetime_pnl,
                win_rate=wr,
                total_trades=self._lifetime_trades,
            )
        except Exception as e:
            print(f"[Scalp] db snapshot: {e}", flush=True)

    def _trades_last_hour(self) -> int:
        now = time.time()
        while self._hour_trade_ts and now - self._hour_trade_ts[0] > 3600:
            self._hour_trade_ts.popleft()
        return len(self._hour_trade_ts)

    def _can_open_now(self):
        now = time.time()
        cd = float(self.config.cooldown_sec)
        if self._last_close_ts and now - self._last_close_ts < cd:
            return False, f"cooldown:{cd - (now - self._last_close_ts):.0f}s"
        gap = float(self.config.min_signal_gap_sec)
        if self._last_open_ts and now - self._last_open_ts < gap:
            return False, f"signal_gap:{gap - (now - self._last_open_ts):.0f}s"
        if self._trades_last_hour() >= int(self.config.max_trades_per_hour):
            return False, "max_trades_per_hour"
        if float(self.config.take_bps) <= float(self.config.fee_bps_rt) * 1.5:
            return False, "take_bps_too_low_vs_fees"
        return True, "ok"

    def get_status(self) -> dict:
        closed = [t for t in self._trade_log if t.get("event") == "close"]
        return {
            "running": self._running,
            "strategy": STRATEGY_NAME,
            "version": STRATEGY_VERSION,
            "description": STRATEGY_DESC,
            "execute": self._execute_enabled(),
            "avail_usdt": self._avail_usdt,
            "acct_avail": self._acct_avail,
            "acct_eq": self._acct_eq,
            "capital": self._capital,
            "equity": round(self._equity, 4),
            "total_pnl": round(self._session_pnl, 4),
            "session_pnl": round(self._session_pnl, 4),
            "lifetime_pnl": round(self._lifetime_pnl, 4),
            "lifetime_trades": self._lifetime_trades,
            "lifetime_wins": self._lifetime_wins,
            "lifetime_fees": round(self._lifetime_fees, 4),
            "lifetime_win_rate": (
                round(100.0 * self._lifetime_wins / self._lifetime_trades, 1)
                if self._lifetime_trades else None
            ),
            "total_trades": self._lifetime_trades,
            "session_trades": len(closed),
            "win_rate": (
                round(100.0 * self._lifetime_wins / self._lifetime_trades, 1)
                if self._lifetime_trades else None
            ),
            "trades_last_hour": self._trades_last_hour(),
            "open_positions": [
                {
                    "coin": p.coin, "side": p.side, "size": p.size,
                    "entry_price": p.entry_price, "stop_price": p.stop_price,
                    "take_price": p.take_price, "leverage": p.leverage,
                    "held_sec": round(time.time() - p.opened_at, 1),
                }
                for p in self._positions.values()
            ],
            "config": asdict(self.config) if hasattr(self.config, "__dataclass_fields__") else {},
            "books": self._latest,
            "recent_signals": self._signals[-15:],
            "last_llm": self._last_llm,
            "last_exec": self._last_exec,
            "recent_trades": self._trade_log[-20:],
            "last_activity": self._last_activity,
            "started_at": self._started_at,
        }

    async def snapshot(self, coin: str = "BTC") -> dict:
        client = await self._client()
        if not client:
            return {"error": "no_client"}
        m = await self._fetch_metrics(client, coin)
        return m or {"error": "book_failed", "coin": coin}
