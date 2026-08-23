"""Order-Book Imbalance (OBI) Scalping — micro-structure strategy.

Core signals (industry-standard microstructure):
  OBI_N = (Σ bid_qty_topN − Σ ask_qty_topN) / (Σ bid + Σ ask)   ∈ [-1, 1]
  Micro-price = (ask1 * bid1_qty + bid1 * ask1_qty) / (bid1_qty + ask1_qty)
  Walls = levels with qty ≫ median (potential support/resistance / spoof risk)

Entry (rule engine, not pure HFT):
  • |OBI_5| ≥ threshold and same sign for `persist` consecutive snapshots
  • spread_bps ≤ max_spread_bps (avoid thin books)
  • optional LLM veto/confirm on a compact book summary

Risk envelope (scalp):
  • small notional, tight stop (bps), quick TP, max hold seconds
  • execute only when enabled (default: demo auto / AI_EXECUTE-like)

Not financial advice. Alpha on public L2 is noisy and decays fast.
"""
from __future__ import annotations

import asyncio
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
from .ai_agent import _openai_compatible  # reuse Groq path

SCALP_BOT_ID = "orderbook_scalp"
STRATEGY_NAME = "Order Book Scalp"
STRATEGY_VERSION = "v0.1"
STRATEGY_DESC = (
    "Order Book Scalp v0.1 — скальпинг по дисбалансу стакана (OBI) на OKX SWAP. "
    "Каждые 1–2 с снимается глубина L2 (top-N), считаются OBI₅/OBI₁₀, micro-price, "
    "стены и спред. Вход при устойчивом |OBI|≥порога; стоп/тейк в bps, max hold секунды. "
    "Опционально LLM подтверждает/ветирует сигнал. Не HFT и не гарантия прибыли."
)

CT_VAL = {"BTC": 0.01, "ETH": 0.1, "SOL": 1.0, "XRP": 100.0}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "SOL": 0.1, "XRP": 0.01}


@dataclass
class ScalpConfig:
    symbols: list = None
    capital: float = 5000.0
    max_leverage: float = 3.0
    risk_per_trade: float = 0.005       # 0.5% equity at stop
    allocation_pct: float = 0.12
    levels: int = 10
    obi_threshold: float = 0.35
    persist_n: int = 3                  # consecutive same-sign OBI
    max_spread_bps: float = 8.0
    stop_bps: float = 12.0              # ~0.12%
    take_bps: float = 18.0
    max_hold_sec: float = 90.0
    poll_interval_sec: float = 1.5
    use_llm: bool = True
    execute: bool = None                # None → demo auto
    min_wall_mult: float = 3.0          # wall if qty > mult * median

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
    """bids/asks: list of [px, sz, ...] from OKX books."""
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

    # distance-weighted OBI (closer levels matter more)
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
        "bid1": bid1,
        "ask1": ask1,
        "bid1_sz": bid1_sz,
        "ask1_sz": ask1_sz,
        "mid": mid,
        "micro": micro,
        "spread": spread,
        "spread_bps": round(spread_bps, 3),
        "obi_1": round(obi_n(1), 4),
        "obi_5": round(obi_n(5), 4),
        "obi_10": round(obi_n(min(10, levels)), 4),
        "w_obi": round(w_obi, 4),
        "bid_vol_5": round(sum(sz for _, sz in b[:5]), 4),
        "ask_vol_5": round(sum(sz for _, sz in a[:5]), 4),
        "ratio_5": round(ratio, 3),
        "walls_bid": walls_bid[:3],
        "walls_ask": walls_ask[:3],
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
        self.notifier = notifier
        self.analysis = analysis or get_logger()
        self._running = False
        self._thread = None
        self._loop = None
        self._capital = float(self.config.capital)
        self._equity = float(self.config.capital)
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
        # manage open positions first
        for coin in list(self._positions.keys()):
            await self._manage_position(client, coin)

        if len(self._positions) >= 1:
            return  # one scalp at a time

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
            data.get("bids") or [],
            data.get("asks") or [],
            levels=self.config.levels,
        )
        if not metrics.get("ok"):
            return None
        metrics["coin"] = coin
        metrics["inst_id"] = inst
        self._latest[coin] = metrics
        self._history[coin].append(metrics)
        return metrics

    def _persistence_signal(self, coin: str) -> Optional[str]:
        """Return 'long'|'short'|None based on consecutive OBI_5."""
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
            "coin": metrics.get("coin"),
            "side_candidate": side,
            "obi_1": metrics.get("obi_1"),
            "obi_5": metrics.get("obi_5"),
            "obi_10": metrics.get("obi_10"),
            "w_obi": metrics.get("w_obi"),
            "spread_bps": metrics.get("spread_bps"),
            "ratio_5": metrics.get("ratio_5"),
            "micro": metrics.get("micro"),
            "mid": metrics.get("mid"),
            "walls_bid": metrics.get("walls_bid"),
            "walls_ask": metrics.get("walls_ask"),
            "bid1": metrics.get("bid1"),
            "ask1": metrics.get("ask1"),
        }
        system = (
            "You are a crypto order-book scalper. Reply ONLY JSON: "
            '{"confirm": true|false, "confidence": 0-1, "reason": "short"}. '
            "confirm=true only if imbalance looks genuine (not obvious spoof wall alone) "
            "and spread is tight. Prefer false when unsure."
        )
        try:
            raw = await _openai_compatible(
                api_key=key,
                base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
                model=os.getenv("AI_LLM_MODEL", "openai/gpt-oss-20b"),
                system=system,
                user=str(summary),
            )
            import json, re
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
            "time": _now_iso(),
            "coin": coin,
            "side": side,
            "obi_5": metrics.get("obi_5"),
            "w_obi": metrics.get("w_obi"),
            "spread_bps": metrics.get("spread_bps"),
            "llm": llm,
            "mid": metrics.get("mid"),
        }
        self._signals.append(sig)
        self._signals = self._signals[-50:]
        try:
            self.analysis.log("scalp", "signal", **{k: sig[k] for k in sig if k != "llm"},
                              llm_confirm=llm.get("confirm"), llm_reason=llm.get("reason"))
        except Exception:
            pass

        if not llm.get("confirm") or float(llm.get("confidence") or 0) < 0.45:
            return
        await self._open(client, coin, side, metrics, sig)

    def _size_order(self, coin: str, entry: float) -> tuple[float, float]:
        ct = CT_VAL.get(coin, 0.01)
        lot = LOT_SZ.get(coin, 0.01)
        stop_pct = max(0.0005, float(self.config.stop_bps) / 10000.0)
        risk_usd = self._equity * float(self.config.risk_per_trade)
        notional = risk_usd / stop_pct if stop_pct > 0 else 0
        max_margin = self._equity * float(self.config.allocation_pct)
        lev = min(float(self.config.max_leverage), max(1.0, notional / max_margin if max_margin else 1))
        if notional / lev > max_margin:
            notional = max_margin * lev
        if entry <= 0 or ct <= 0:
            return 0.0, lev
        raw = notional / (entry * ct)
        steps = math.floor(raw / lot)
        return max(0.0, round(steps * lot, 8)), round(lev, 2)

    async def _open(self, client, coin: str, side: str, metrics: dict, sig: dict):
        entry = float(metrics.get("mid") or 0)
        if entry <= 0:
            return
        sz, lev = self._size_order(coin, entry)
        if sz <= 0:
            self._last_exec = {"event": "skip", "reason": "size=0", "coin": coin}
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
            print(f"[Scalp] SIGNAL {side} {coin} sz={sz} (execute=0)", flush=True)
            return

        try:
            await client.set_leverage(inst, lev, mgn_mode="cross", pos_side=side)
        except Exception:
            try:
                await client.set_leverage(inst, lev, mgn_mode="cross", pos_side="net")
            except Exception:
                pass

        try:
            resp = await client.place_order(
                inst_id=inst, side=order_side, ord_type="market",
                sz=self._fmt_sz(coin, sz), td_mode="cross", pos_side=side,
            )
        except Exception as e:
            self._last_exec = {"event": "open_error", "reason": str(e), "coin": coin}
            return

        if resp.get("error"):
            # net mode retry
            msg = str(resp.get("message") or "")
            if "pos" in msg.lower():
                resp = await client.place_order(
                    inst_id=inst, side=order_side, ord_type="market",
                    sz=self._fmt_sz(coin, sz), td_mode="cross", pos_side=None,
                )
        if resp.get("error"):
            self._last_exec = {
                "event": "open_error", "reason": resp.get("message"), "coin": coin,
            }
            print(f"[Scalp] open error {coin}: {resp.get('message')}", flush=True)
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
        self._equity -= fee_cost(fee)
        self._last_exec = {
            "event": "open_ok", "coin": coin, "side": side,
            "entry": fill_px, "size": sz, "lev": lev,
        }
        self._trade_log.append({
            "time": _now_iso(), "event": "open", "coin": coin, "side": side,
            "px": fill_px, "sz": sz,
        })
        print(f"[Scalp] OPEN {side} {coin} @{fill_px} stop={stop:.4f}", flush=True)
        if self.notifier:
            try:
                self.notifier.fire(self.notifier.open_msg(
                    coin=coin, side=side, price=round(fill_px, 4),
                    stop=round(stop, 4), size=sz, leverage=lev,
                    bot_name=self.BOT_NAME,
                ))
            except Exception:
                pass
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
        # imbalance flip exit
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
        pnl = close_pnl(pos.side, pos.size, pos.entry_price, fill_px, fee, CT_VAL.get(coin, 0.01))
        self._equity += pnl
        self._trade_log.append({
            "time": _now_iso(), "event": "close", "coin": coin, "side": pos.side,
            "entry": pos.entry_price, "exit": fill_px, "pnl": round(pnl, 4), "reason": reason,
        })
        self._last_exec = {"event": "close_ok", "coin": coin, "pnl": round(pnl, 4), "reason": reason}
        print(f"[Scalp] CLOSE {coin} pnl={pnl:+.4f} ({reason})", flush=True)
        if self.notifier:
            try:
                self.notifier.fire(self.notifier.close_msg(
                    coin=coin, side=pos.side, entry=round(pos.entry_price, 4),
                    exit_px=round(fill_px, 4), pnl=round(pnl, 4), reason=reason,
                    bot_name=self.BOT_NAME,
                ))
            except Exception:
                pass
        try:
            self.analysis.log("scalp", "close", coin=coin, pnl=round(pnl, 4), reason=reason)
        except Exception:
            pass
        del self._positions[coin]

    def get_status(self) -> dict:
        closed = [t for t in self._trade_log if t.get("event") == "close"]
        wins = sum(1 for t in closed if float(t.get("pnl") or 0) > 0)
        return {
            "running": self._running,
            "strategy": STRATEGY_NAME,
            "version": STRATEGY_VERSION,
            "description": STRATEGY_DESC,
            "execute": self._execute_enabled(),
            "capital": self._capital,
            "equity": round(self._equity, 4),
            "total_pnl": round(self._equity - self._capital, 4),
            "total_trades": len(closed),
            "win_rate": round(100.0 * wins / len(closed), 1) if closed else None,
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
        """One-shot book + metrics for UI without starting the bot."""
        client = await self._client()
        if not client:
            return {"error": "no_client"}
        m = await self._fetch_metrics(client, coin)
        return m or {"error": "book_failed", "coin": coin}
