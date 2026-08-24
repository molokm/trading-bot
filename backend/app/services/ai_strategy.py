"""AI Discretionary Strategy — 1H BTC/ETH/SOL/XRP with LLM (or mock) decisions.

Safety envelope (anti-liquidation oriented):
  - capital baseline $10_000, max leverage 3x
  - stop distance clamped 1.5–5%; size from risk budget
  - max 1–2 positions; AI_EXECUTE=0 → decide+log only (no orders)
  - risk_guard.assert_can_open on entries
"""
from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from .telegram_notifier import TelegramNotifier
from .pnl_utils import extract_fill_avg, close_pnl, fee_cost
from .position_claim import claim_open, release_open
from .ai_agent import call_llm, ALLOWED_SYMBOLS, llm_status
import json
from .risk_guard import assert_can_open
from .analysis_logger import get_logger

AI_BOT_ID = "ai_strategy"

def _ai_state_path() -> str:
    base = os.getenv("DATA_DIR") or os.getenv("RENDER_DISK_PATH") or "/tmp"
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = "/tmp"
    return os.path.join(base, "ai_discretionary_state.json")


def load_ai_state() -> dict:
    try:
        with open(_ai_state_path(), "r") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_ai_state(payload: dict) -> None:
    try:
        path = _ai_state_path()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[AI] state save: {e}", flush=True)

STRATEGY_NAME = "AI Discretionary 1H"
STRATEGY_VERSION = "v0.3"
STRATEGY_DESC = (
    "AI Discretionary v0.3 — 1H BTC/ETH/SOL/XRP. LLM (или mock) предлагает вход/выход; "
    "исполнение только через risk envelope: депозит-ориентир $10k, плечо ≤3×, "
    "стоп 1.5–5%, max 1–2 позиции. Без ключа LLM работает mock-эвристика. "
    "AI_EXECUTE=0 — только сигналы. Не финансовый совет; демо-рекомендовано."
)

CT_VAL = {"BTC": 0.01, "ETH": 0.1, "SOL": 1.0, "XRP": 100.0}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "SOL": 0.1, "XRP": 0.01}


@dataclass
class AIConfig:
    symbols: list = None
    capital: float = 10000.0
    max_leverage: float = 3.0
    max_positions: int = 1
    risk_per_trade: float = 0.015          # 2% equity at stop
    allocation_pct: float = 0.25          # max margin / equity per pos
    bar: str = "1H"
    candle_limit: int = 120
    poll_interval_sec: int = 180          # decision cadence
    min_confidence: float = 0.70
    min_adx: float = 18.0
    min_roc_abs: float = 0.35          # % move on roc_3
    min_stop_pct: float = 0.02
    max_stop_pct: float = 0.05
    min_take_pct: float = 0.04
    max_hold_hours: float = 18.0
    block_llm_error_opens: bool = True
    ema_fast: int = 12
    ema_slow: int = 26
    adx_period: int = 14
    roc_period: int = 3
    provider: str = None                  # None → env AI_LLM_PROVIDER
    execute: bool = None                  # None → env AI_EXECUTE

    def __post_init__(self):
        if self.symbols is None:
            self.symbols = list(ALLOWED_SYMBOLS)


@dataclass
class AIPosition:
    coin: str
    inst_id: str
    side: str
    size: float
    entry_price: float
    stop_price: float
    take_price: float
    leverage: float
    opened_at: str
    signal_id: int = 0
    peak_price: float = 0.0


class AIStrategy:
    BOT_ID = AI_BOT_ID
    BOT_NAME = STRATEGY_NAME
    STRATEGY_NAME = STRATEGY_NAME
    STRATEGY_VERSION = STRATEGY_VERSION
    STRATEGY_DESC = STRATEGY_DESC

    def __init__(self, config: AIConfig = None, client_manager=None, db=None,
                 notifier: Optional[TelegramNotifier] = None,
                 analysis=None):
        self.config = config or AIConfig()
        self.client_manager = client_manager
        self.db = db
        self.notifier = notifier
        self.analysis = analysis or get_logger()
        self._last_exec = None  # last open/close attempt result
        self._exec_log = []  # recent execution attempts
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._positions: dict[str, AIPosition] = {}
        self._trade_log: list = []
        self._decision_log: list = []
        st = load_ai_state()
        self._capital = float(self.config.capital)
        self._session_pnl = 0.0
        self._lifetime_pnl = float(st.get("lifetime_pnl") or 0.0)
        self._lifetime_trades = int(st.get("lifetime_trades") or 0)
        self._lifetime_wins = int(st.get("lifetime_wins") or 0)
        self._lifetime_fees = float(st.get("lifetime_fees") or 0.0)
        self._equity = self._capital + self._session_pnl
        self._last_activity = None
        self._started_at = None
        self._latest_indicators: dict = {}
        self._last_decision: dict = {}

    # ── lifecycle ──────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        print(f"[AI {STRATEGY_VERSION}] Started execute={self._execute_enabled()} "
              f"provider={self._provider()} capital={self._capital}", flush=True)

    def stop(self):
        self._running = False
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        print(f"[AI {STRATEGY_VERSION}] Stopped", flush=True)

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

    def _provider(self) -> str:
        if self.config.provider:
            return str(self.config.provider).strip().lower()
        env = (os.getenv("AI_LLM_PROVIDER") or "").strip().lower()
        if env:
            return env
        return "groq" if os.getenv("GROQ_API_KEY", "").strip() else "mock"

    def _execute_enabled(self) -> bool:
        if self.config.execute is not None:
            return bool(self.config.execute)
        env = os.getenv("AI_EXECUTE", "").strip().lower()
        if env in ("1", "true", "yes", "on"):
            return True
        if env in ("0", "false", "no", "off"):
            return False
        # No explicit AI_EXECUTE: allow orders only on OKX demo
        return self._is_demo()

    def _is_demo(self) -> bool:
        if os.getenv("OKX_DEMO", "true").lower() in ("1", "true", "yes", "on"):
            return True
        try:
            c = self.client_manager.get_client() if self.client_manager else None
            if c is not None and getattr(c, "demo", False):
                return True
        except Exception:
            pass
        return False

    async def _client(self):
        if not self.client_manager:
            return None
        return self.client_manager.get_client()

    async def _run(self):
        try:
            if self.db:
                await self.db.ensure_bot(self.BOT_ID, strategy_id="ai_discretionary",
                                         name=STRATEGY_NAME)
        except Exception as e:
            print(f"[AI] ensure_bot: {e}", flush=True)
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                print(f"[AI] tick error: {e}", flush=True)
            self._last_activity = datetime.now(timezone.utc).isoformat()
            await asyncio.sleep(max(30, int(self.config.poll_interval_sec)))

    # ── indicators ─────────────────────────────────────────────
    @staticmethod
    def _ema(values, period):
        if not values or len(values) < period:
            return [None] * len(values)
        k = 2 / (period + 1)
        out = [None] * len(values)
        s = sum(values[:period]) / period
        out[period - 1] = s
        for i in range(period, len(values)):
            s = values[i] * k + s * (1 - k)
            out[i] = s
        return out

    @staticmethod
    def _roc(values, period):
        out = [None] * len(values)
        for i in range(period, len(values)):
            prev = values[i - period]
            if prev:
                out[i] = (values[i] / prev - 1.0) * 100.0
        return out

    @staticmethod
    def _adx(highs, lows, closes, period=14):
        n = len(closes)
        if n < period + 2:
            return [None] * n
        tr = [0.0] * n
        plus_dm = [0.0] * n
        minus_dm = [0.0] * n
        for i in range(1, n):
            up = highs[i] - highs[i - 1]
            down = lows[i - 1] - lows[i]
            plus_dm[i] = up if up > down and up > 0 else 0.0
            minus_dm[i] = down if down > up and down > 0 else 0.0
            tr[i] = max(highs[i] - lows[i],
                        abs(highs[i] - closes[i - 1]),
                        abs(lows[i] - closes[i - 1]))
        def wild(arr):
            out = [None] * n
            s = sum(arr[1:period + 1])
            out[period] = s
            for i in range(period + 1, n):
                s = s - s / period + arr[i]
                out[i] = s
            return out
        atr = wild(tr)
        p_dm = wild(plus_dm)
        m_dm = wild(minus_dm)
        dx = [None] * n
        for i in range(period, n):
            if not atr[i]:
                continue
            pdi = 100 * (p_dm[i] / atr[i]) if atr[i] else 0
            mdi = 100 * (m_dm[i] / atr[i]) if atr[i] else 0
            denom = pdi + mdi
            dx[i] = 100 * abs(pdi - mdi) / denom if denom else 0
        adx = [None] * n
        vals = [d for d in dx[period:period * 2] if d is not None]
        if len(vals) < period:
            return adx
        s = sum(vals[:period]) / period
        idx = period * 2 - 1
        if idx < n:
            adx[idx] = s
            for i in range(idx + 1, n):
                if dx[i] is None:
                    continue
                s = (s * (period - 1) + dx[i]) / period
                adx[i] = s
        return adx

    async def _fetch_indicators(self, client) -> dict:
        out = {}
        cfg = self.config
        for coin in cfg.symbols:
            inst = f"{coin}-USDT-SWAP"
            try:
                resp = await client.get_candles(inst, bar=cfg.bar, limit=cfg.candle_limit)
                rows = resp.get("data") or []
                # OKX: newest first
                rows = list(reversed(rows))
                if len(rows) < 40:
                    continue
                closes = [float(r[4]) for r in rows]
                highs = [float(r[2]) for r in rows]
                lows = [float(r[3]) for r in rows]
                ema_f = self._ema(closes, cfg.ema_fast)
                ema_s = self._ema(closes, cfg.ema_slow)
                roc = self._roc(closes, cfg.roc_period)
                adx = self._adx(highs, lows, closes, cfg.adx_period)
                out[coin] = {
                    "close": closes[-1],
                    "ema_fast": ema_f[-1],
                    "ema_slow": ema_s[-1],
                    "roc_3": roc[-1],
                    "adx": adx[-1],
                    "bar": cfg.bar,
                }
            except Exception as e:
                print(f"[AI] candles {coin}: {e}", flush=True)
        self._latest_indicators = out
        return out

    def _snapshot(self) -> dict:
        open_list = []
        for coin, p in self._positions.items():
            open_list.append({
                "coin": coin, "side": p.side, "size": p.size,
                "entry_price": p.entry_price, "stop_price": p.stop_price,
                "take_price": p.take_price, "leverage": p.leverage,
            })
        return {
            "equity": round(self._equity, 2),
            "capital": self._capital,
            "max_leverage": self.config.max_leverage,
            "max_positions": self.config.max_positions,
            "open_positions": open_list,
            "indicators": self._latest_indicators,
            "server_time": datetime.now(timezone.utc).isoformat(),
            "provider": self._provider(),
            "llm": llm_status(),
            "execute": self._execute_enabled(),
        }

    def _fmt_sz(self, coin: str, sz: float) -> str:
        lot = LOT_SZ.get(coin, 0.01)
        # string without scientific notation; trim trailing zeros
        prec = 8 if lot < 0.01 else (4 if lot < 1 else 2)
        s = f"{float(sz):.{prec}f}".rstrip("0").rstrip(".")
        return s or "0"

    def _record_exec(self, event: str, **data):
        rec = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        }
        self._last_exec = rec
        self._exec_log.append(rec)
        self._exec_log = self._exec_log[-50:]
        self._decision_log.append(rec)
        self._decision_log = self._decision_log[-200:]
        try:
            self.analysis.log("ai", event, **data)
        except Exception:
            pass
        print(f"[AI] exec {event} {data}", flush=True)

    # ── sizing (anti-liq) ──────────────────────────────────────
    def _size_order(self, coin: str, entry: float, stop_pct: float) -> tuple[float, float]:
        """Return (contracts_size, leverage) with risk and margin caps."""
        cfg = self.config
        ct = CT_VAL.get(coin, 0.01)
        lot = LOT_SZ.get(coin, 0.01)
        stop_pct = max(0.015, min(0.05, abs(stop_pct)))
        risk_usd = self._equity * cfg.risk_per_trade
        # notional such that stop_pct * notional ≈ risk_usd
        notional = risk_usd / stop_pct if stop_pct > 0 else 0
        max_margin = self._equity * cfg.allocation_pct
        # leverage chosen ≤ max, so margin = notional/lev ≤ max_margin
        lev = min(cfg.max_leverage, max(1.0, notional / max_margin if max_margin > 0 else 1.0))
        # if still over margin, cut notional
        if notional / lev > max_margin:
            notional = max_margin * lev
        if entry <= 0 or ct <= 0:
            return 0.0, lev
        raw_sz = notional / (entry * ct)
        # round down to lot
        steps = math.floor(raw_sz / lot)
        sz = round(steps * lot, 8)
        return max(0.0, sz), round(lev, 2)

    # ── execution helpers ──────────────────────────────────────
    async def _place(self, client, inst_id, side, sz, pos_side):
        coin = inst_id.split("-")[0]
        return await client.place_order(
            inst_id=inst_id, side=side, ord_type="market",
            sz=self._fmt_sz(coin, sz), td_mode="cross", pos_side=pos_side,
        )

    async def _open(self, client, coin: str, side: str, stop_pct: float, take_pct: float,
                    reason: str):
        ind = self._latest_indicators.get(coin) or {}
        entry = float(ind.get("close") or 0)
        if entry <= 0:
            return
        sz, lev = self._size_order(coin, entry, stop_pct)
        if sz <= 0:
            self._record_exec("open_skip", coin=coin, side=side, reason="size=0",
                              entry=entry, stop_pct=stop_pct)
            return
        try:
            assert_can_open(is_reduce_only=False)
        except Exception as e:
            self._record_exec("open_skip", coin=coin, side=side, reason=f"risk:{e}")
            return
        inst = f"{coin}-USDT-SWAP"
        order_side = "buy" if side == "long" else "sell"
        if not self._execute_enabled():
            print(f"[AI] SIGNAL open {side} {coin} sz={sz} lev={lev} (execute=0)", flush=True)
            rec = {
                "time": datetime.now(timezone.utc).isoformat(),
                "event": "signal_open", "coin": coin, "side": side,
                "size": sz, "leverage": lev, "reason": reason,
            }
            self._decision_log.append(rec)
            try:
                self.analysis.log("ai", "signal_open", **{k: rec[k] for k in rec if k != "time"})
            except Exception:
                pass
            return
        for ps in (side, "net", None):
            try:
                await client.set_leverage(inst, lev, mgn_mode="cross",
                                         pos_side=ps if ps else "net")
            except Exception:
                pass
        pos_side_try = side
        try:
            resp = await self._place(client, inst, order_side, sz, pos_side_try)
        except Exception as e:
            self._record_exec("open_error", coin=coin, side=side, reason=str(e),
                              size=sz, leverage=lev)
            return
        if resp.get("error"):
            msg = str(resp.get("message") or resp)
            # Retry net-mode accounts (no hedge long/short)
            if "pos" in msg.lower() or "51000" in msg or "posside" in msg.lower():
                try:
                    resp = await client.place_order(
                        inst_id=inst, side=order_side, ord_type="market",
                        sz=self._fmt_sz(coin, sz), td_mode="cross", pos_side=None,
                    )
                except Exception as e2:
                    self._record_exec("open_error", coin=coin, side=side,
                                      reason=str(e2), size=sz, leverage=lev)
                    return
            if resp.get("error"):
                self._record_exec(
                    "open_error", coin=coin, side=side,
                    reason=str(resp.get("message") or resp),
                    size=sz, leverage=lev, raw=str(resp)[:300],
                )
                return
        fills = resp.get("data") or []
        # Market ack often has ordId but no avgPx — resolve via get_order
        fill_px, fee, _ = extract_fill_avg(fills, entry)
        if (not fill_px or fill_px <= 0) and fills:
            ord_id = fills[0].get("ordId") or fills[0].get("ord_id")
            if ord_id:
                for _ in range(4):
                    await asyncio.sleep(0.35)
                    try:
                        o = await client.get_order(inst, ord_id=ord_id)
                        rows = o.get("data") or []
                        if rows:
                            fill_px, fee, _ = extract_fill_avg(rows, entry)
                            if fill_px and fill_px > 0:
                                break
                    except Exception:
                        pass
        if not fill_px or fill_px <= 0:
            fill_px = entry
        if side == "long":
            stop = fill_px * (1 - stop_pct)
            take = fill_px * (1 + take_pct)
        else:
            stop = fill_px * (1 + stop_pct)
            take = fill_px * (1 - take_pct)
        pos = AIPosition(
            coin=coin, inst_id=inst, side=side, size=sz,
            entry_price=fill_px, stop_price=stop, take_price=take,
            leverage=lev, opened_at=datetime.now(timezone.utc).isoformat(),
            peak_price=fill_px,
        )
        self._positions[coin] = pos
        self._equity -= fee_cost(fee)
        ok_claim = await claim_open(self.db, self.BOT_ID, inst, side, sz, fill_px)
        if not ok_claim:
            print(f"[AI] CRITICAL: open filled but DB claim failed {coin} — risk of orphan", flush=True)
        self._trade_log.append({
            "time": pos.opened_at, "side": order_side, "symbol": inst,
            "size": sz, "pnl": -fee_cost(fee), "entry_price": fill_px,
            "reason": "open", "pos_side": side, "coin": coin,
        })
        if self.db:
            try:
                await self.db.save_trade(
                    bot_id=self.BOT_ID, side=order_side, sz=sz, px=fill_px,
                    ord_id=(fills[0].get("ordId") if fills else ""),
                    inst_id=inst, ord_type="market",
                    fee=fee_cost(fee), fee_ccy="USDT", pnl=-fee_cost(fee),
                    state="filled",
                )
            except Exception as e:
                print(f"[AI] db open: {e}", flush=True)
        if self.notifier:
            try:
                self.notifier.fire(self.notifier.open_msg(
                    coin=coin, side=side, price=round(fill_px, 4),
                    stop=round(stop, 4), size=sz, leverage=lev,
                    bot_name=self.BOT_NAME,
                ))
            except Exception:
                pass
        self._record_exec(
            "open_ok", coin=coin, side=side, entry=fill_px,
            stop=stop, take=take, size=sz, leverage=lev, reason=reason,
        )

    async def _close(self, client, coin: str, reason: str):
        pos = self._positions.get(coin)
        if not pos:
            return
        if not self._execute_enabled():
            print(f"[AI] SIGNAL close {coin} ({reason}) execute=0", flush=True)
            del self._positions[coin]
            if self.db:
                try:
                    await self.db.delete_position(self.BOT_ID)
                except Exception:
                    pass
            return
        close_side = "sell" if pos.side == "long" else "buy"
        resp = await self._place(client, pos.inst_id, close_side, pos.size, pos.side)
        if resp.get("error"):
            print(f"[AI] close error {coin}: {resp.get('message')}", flush=True)
            return
        fills = resp.get("data") or []
        # Prefer last indicator/mark so PnL is not stuck at entry when fill payload is empty
        mark = float((self._latest_indicators.get(coin) or {}).get("close") or 0) or pos.entry_price
        fill_px, fee, _ = extract_fill_avg(fills, mark)
        if not fill_px or abs(fill_px - pos.entry_price) < 1e-12:
            # poll order once for avgPx
            try:
                oid = (fills[0].get("ordId") if fills else None) or (resp.get("data") or [{}])[0].get("ordId")
                if oid and hasattr(client, "get_order"):
                    od = await client.get_order(pos.inst_id, oid)
                    rows = od.get("data") or []
                    if rows:
                        fill_px, fee, _ = extract_fill_avg(rows, mark)
            except Exception:
                pass
        if not fill_px:
            fill_px = mark
        fee_c = fee_cost(fee)
        pnl = close_pnl(pos.side, pos.size, pos.entry_price, fill_px, fee, CT_VAL.get(coin, 0.01))
        self._equity += pnl
        self._session_pnl += pnl
        self._lifetime_pnl += pnl
        self._lifetime_trades += 1
        if pnl > 0:
            self._lifetime_wins += 1
        self._lifetime_fees += fee_c
        self._persist()
        now = datetime.now(timezone.utc).isoformat()
        self._trade_log.append({
            "time": now, "side": close_side, "symbol": pos.inst_id,
            "size": pos.size, "pnl": round(pnl, 2), "entry_price": pos.entry_price,
            "exit_price": fill_px, "fee": round(fee_c, 6),
            "reason": reason, "pos_side": pos.side, "coin": coin,
        })
        if self.db:
            try:
                await self.db.save_trade(
                    bot_id=self.BOT_ID, side=close_side, sz=pos.size, px=fill_px,
                    ord_id=(fills[0].get("ordId") if fills else ""),
                    inst_id=pos.inst_id, ord_type="market",
                    fee=fee_cost(fee), fee_ccy="USDT", pnl=round(pnl, 2), state="filled",
                )
            except Exception as e:
                print(f"[AI] db close: {e}", flush=True)
        if self.notifier:
            try:
                self.notifier.fire(self.notifier.close_msg(
                    coin=coin, side=pos.side, entry=round(pos.entry_price, 4),
                    exit_px=round(fill_px, 4), pnl=round(pnl, 2), reason=reason,
                    bot_name=self.BOT_NAME,
                ))
            except Exception:
                pass
        del self._positions[coin]
        if self.db:
            try:
                await self.db.delete_position(self.BOT_ID)
            except Exception:
                pass
        print(f"[AI] CLOSE {coin} pnl={pnl:+.2f} ({reason})", flush=True)
        try:
            self.analysis.log(
                "ai", "close", coin=coin, side=pos.side, entry=pos.entry_price,
                exit=fill_px, pnl=round(pnl, 2), reason=reason,
            )
        except Exception:
            pass

    async def _manage_stops(self, client):
        """Hard stop / take from last close price (1H bar) + max hold."""
        for coin, pos in list(self._positions.items()):
            # max hold
            try:
                opened = datetime.fromisoformat(pos.opened_at.replace("Z", "+00:00"))
                held_h = (datetime.now(timezone.utc) - opened).total_seconds() / 3600.0
                if held_h >= float(self.config.max_hold_hours or 24):
                    await self._close(client, coin, "max_hold")
                    continue
            except Exception:
                pass
            ind = self._latest_indicators.get(coin) or {}
            px = float(ind.get("close") or 0)
            if px <= 0:
                continue
            if pos.side == "long":
                pos.peak_price = max(pos.peak_price or px, px)
                if px <= pos.stop_price:
                    await self._close(client, coin, "stop")
                elif px >= pos.take_price:
                    await self._close(client, coin, "take")
            else:
                pos.peak_price = min(pos.peak_price or px, px) if pos.peak_price else px
                if px >= pos.stop_price:
                    await self._close(client, coin, "stop")
                elif px <= pos.take_price:
                    await self._close(client, coin, "take")



    async def _owned_via_trades(self, inst_id: str, side: str) -> bool:
        """True if latest DB trade for this inst by AI is consistent with an open."""
        if not self.db:
            return False
        try:
            rows = await self.db._fetchall(
                (
                    "SELECT side, pnl, state, timestamp FROM trades "
                    "WHERE bot_id = $1 AND inst_id = $2 ORDER BY timestamp DESC LIMIT 5"
                    if self.db._pg_mode else
                    "SELECT side, pnl, state, timestamp FROM trades "
                    "WHERE bot_id = ? AND inst_id = ? ORDER BY timestamp DESC LIMIT 5"
                ),
                (self.BOT_ID, inst_id),
            )
        except Exception as e:
            print(f"[AI] trades ownership: {e}", flush=True)
            return False
        if not rows:
            return False
        # Heuristic: last fill by this bot for inst — buy implies long open, sell short open
        last = rows[0]
        last_side = (last.get("side") or "").lower()
        if side == "long" and last_side == "buy":
            return True
        if side == "short" and last_side == "sell":
            return True
        # if last was closing opposite, not ours
        return False

    async def _restore_open_positions(self, client):
        """Adopt exchange positions owned by this bot (DB) after restart."""
        if self._positions or not client:
            return
        try:
            result = await client.get_positions("SWAP")
            if result.get("error") or not result.get("data"):
                return
            for p in result.get("data") or []:
                inst_id = p.get("instId") or ""
                coin = inst_id.replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
                if coin not in (self.config.symbols or []):
                    continue
                pos_side = (p.get("posSide") or "net").lower()
                side = "short" if pos_side == "short" else "long"
                sz = float(p.get("pos") or 0)
                entry = float(p.get("avgPx") or 0)
                if sz <= 0 or entry <= 0 or coin in self._positions:
                    continue
                if not self.db:
                    continue
                try:
                    if await self.db.other_bot_owns_position_any(self.BOT_ID, inst_id, side):
                        continue
                    mine = await self.db.find_position_any_side(self.BOT_ID, inst_id, side)
                    if not mine:
                        # Ownership from recent trades (claim may have failed mid-deploy)
                        mine = await self._owned_via_trades(inst_id, side)
                    if not mine:
                        continue
                except Exception as e:
                    print(f"[AI] restore ownership: {e}", flush=True)
                    continue
                stop_pct = 0.03
                take_pct = 0.06
                if side == "long":
                    stop, take = entry * (1 - stop_pct), entry * (1 + take_pct)
                else:
                    stop, take = entry * (1 + stop_pct), entry * (1 - take_pct)
                self._positions[coin] = AIPosition(
                    coin=coin, inst_id=inst_id, side=side, size=sz,
                    entry_price=entry, stop_price=stop, take_price=take,
                    leverage=float(self.config.max_leverage or 2),
                    opened_at=datetime.now(timezone.utc).isoformat(),
                    peak_price=entry,
                )
                await claim_open(self.db, self.BOT_ID, inst_id, side, sz, entry)
                print(f"[AI] RESTORE {side} {coin} sz={sz} @ {entry}", flush=True)
        except Exception as e:
            print(f"[AI] restore error: {e}", flush=True)

    async def _tick(self):
        client = await self._client()
        if not client:
            print("[AI] no OKX client", flush=True)
            return
        if not self._positions:
            await self._restore_open_positions(client)
        # Keep DB ownership fresh so UI never loses the badge after restart
        for coin, pos in list(self._positions.items()):
            await claim_open(self.db, self.BOT_ID, pos.inst_id, pos.side, pos.size, pos.entry_price)
        await self._fetch_indicators(client)
        await self._manage_stops(client)
        snap = self._snapshot()
        decision = await call_llm(snap, provider=self._provider())
        self._last_decision = {
            **decision,
            "time": datetime.now(timezone.utc).isoformat(),
            "provider": self._provider(),
            "execute": self._execute_enabled(),
            "demo": self._is_demo(),
            "indicators": {
                k: {kk: vv for kk, vv in (v or {}).items() if kk in (
                    "close", "ema_fast", "ema_slow", "roc_3", "adx")}
                for k, v in (snap.get("indicators") or {}).items()
            },
            "open_positions": snap.get("open_positions") or [],
            "equity": snap.get("equity"),
        }
        self._decision_log.append(self._last_decision)
        self._decision_log = self._decision_log[-200:]
        print(f"[AI] decision {decision} execute={self._execute_enabled()}", flush=True)
        try:
            self.analysis.log(
                "ai", "decision",
                provider=self._provider(),
                execute=self._execute_enabled(),
                demo=self._is_demo(),
                **{k: decision.get(k) for k in (
                    "action", "symbol", "side", "size_pct_equity",
                    "stop_pct", "take_pct", "confidence", "reason")},
                equity=snap.get("equity"),
                open_n=len(snap.get("open_positions") or []),
                indicators=self._last_decision.get("indicators"),
            )
        except Exception as e:
            print(f"[AI] analysis log: {e}", flush=True)

        action = decision.get("action")
        coin = decision.get("symbol")
        if action == "hold":
            return
        if action == "close" and coin:
            await self._close(client, coin, decision.get("reason") or "ai_close")
            return
        if action == "reduce" and coin and coin in self._positions:
            # v0.1: reduce = full close (partial later)
            await self._close(client, coin, decision.get("reason") or "ai_reduce")
            return
        if action == "open" and coin and decision.get("side"):
            conf = float(decision.get("confidence") or 0)
            reason = str(decision.get("reason") or "")
            if conf < float(self.config.min_confidence or 0):
                self._record_exec("open_skip", coin=coin, side=decision.get("side"),
                                  reason=f"low_conf:{conf}")
                return
            if self.config.block_llm_error_opens and (
                "llm_error" in reason.lower() or reason.lower().startswith("fallback")
                or "mock:" in reason.lower()
            ):
                self._record_exec("open_skip", coin=coin, side=decision.get("side"),
                                  reason="blocked_mock_or_llm_error")
                return
            if coin in self._positions:
                self._record_exec("open_skip", coin=coin, side=decision.get("side"),
                                  reason="already_open")
                return
            if len(self._positions) >= self.config.max_positions:
                self._record_exec("open_skip", coin=coin, side=decision.get("side"),
                                  reason="max_positions")
                return
            ind = self._latest_indicators.get(coin) or {}
            adx = float(ind.get("adx") or 0)
            roc = float(ind.get("roc_3") or 0)
            ema_f = float(ind.get("ema_fast") or 0)
            ema_s = float(ind.get("ema_slow") or 0)
            close = float(ind.get("close") or 0)
            if adx < float(self.config.min_adx or 0):
                self._record_exec("open_skip", coin=coin, side=decision.get("side"),
                                  reason=f"low_adx:{adx:.1f}")
                return
            if abs(roc) < float(self.config.min_roc_abs or 0):
                self._record_exec("open_skip", coin=coin, side=decision.get("side"),
                                  reason=f"flat_roc:{roc:.2f}")
                return
            side = decision["side"]
            # Require EMA alignment with side
            if ema_f and ema_s and close:
                if side == "long" and not (ema_f >= ema_s and close >= ema_s):
                    self._record_exec("open_skip", coin=coin, side=side, reason="ema_not_bullish")
                    return
                if side == "short" and not (ema_f <= ema_s and close <= ema_s):
                    self._record_exec("open_skip", coin=coin, side=side, reason="ema_not_bearish")
                    return
                if side == "long" and roc < 0:
                    self._record_exec("open_skip", coin=coin, side=side, reason="roc_against_long")
                    return
                if side == "short" and roc > 0:
                    self._record_exec("open_skip", coin=coin, side=side, reason="roc_against_short")
                    return
            stop_pct = float(decision.get("stop_pct") or 0.03)
            take_pct = float(decision.get("take_pct") or 0.06)
            stop_pct = min(float(self.config.max_stop_pct), max(float(self.config.min_stop_pct), stop_pct))
            take_pct = max(float(self.config.min_take_pct), take_pct)
            # need RR at least ~1.3 after fees
            if take_pct < stop_pct * 1.3:
                take_pct = stop_pct * 1.5
            await self._open(
                client, coin, side,
                stop_pct=stop_pct, take_pct=take_pct,
                reason=reason or "ai_open",
            )

    def _persist(self):
        save_ai_state({
            "lifetime_pnl": self._lifetime_pnl,
            "lifetime_trades": self._lifetime_trades,
            "lifetime_wins": self._lifetime_wins,
            "lifetime_fees": self._lifetime_fees,
            "session_pnl": self._session_pnl,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        if self.db is not None and self._loop and not self._loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self._db_snapshot(), self._loop)
            except Exception:
                pass

    async def _db_snapshot(self):
        try:
            await self.db.ensure_bot(self.BOT_ID, strategy_id="ai_discretionary",
                                     name=STRATEGY_NAME)
            wr = (100.0 * self._lifetime_wins / self._lifetime_trades) if self._lifetime_trades else None
            await self.db.save_metric(
                bot_id=self.BOT_ID,
                equity=self._capital + self._lifetime_pnl,
                total_pnl=self._lifetime_pnl,
                win_rate=wr,
                total_trades=self._lifetime_trades,
            )
        except Exception as e:
            print(f"[AI] db snapshot: {e}", flush=True)

    def get_status(self) -> dict:
        closed = [t for t in self._trade_log if t.get("reason") not in (None, "open") and "pnl" in t]
        session_wins = sum(1 for t in closed if float(t.get("pnl") or 0) > 0)
        return {
            "running": self._running,
            "strategy": STRATEGY_NAME,
            "version": STRATEGY_VERSION,
            "description": STRATEGY_DESC,
            "provider": self._provider(),
            "execute": self._execute_enabled(),
            "capital": self._capital,
            "equity": round(self._equity, 2),
            "total_pnl": round(self._lifetime_pnl, 2),
            "session_pnl": round(self._session_pnl, 2),
            "lifetime_pnl": round(self._lifetime_pnl, 2),
            "lifetime_trades": self._lifetime_trades,
            "lifetime_fees": round(self._lifetime_fees, 2),
            "total_trades": self._lifetime_trades,
            "session_trades": len(closed),
            "open_fills": len([t for t in self._trade_log if t.get("reason") == "open"]),
            "win_rate": (
                round(100.0 * self._lifetime_wins / self._lifetime_trades, 1)
                if self._lifetime_trades else None
            ),
            "open_positions": [
                {
                    "coin": p.coin, "symbol": p.inst_id, "side": p.side,
                    "size": p.size, "entry_price": p.entry_price,
                    "stop_price": p.stop_price, "take_price": p.take_price,
                    "leverage": p.leverage,
                }
                for p in self._positions.values()
            ],
            "config": {
                "symbols": self.config.symbols,
                "capital": self.config.capital,
                "max_leverage": self.config.max_leverage,
                "max_positions": self.config.max_positions,
                "risk_per_trade": self.config.risk_per_trade,
                "bar": self.config.bar,
                "poll_interval_sec": self.config.poll_interval_sec,
                "min_confidence": self.config.min_confidence,
            },
            "indicators": self._latest_indicators,
            "last_decision": self._last_decision,
            "recent_decisions": self._decision_log[-30:],
            "decision_count": len(self._decision_log),
            "last_exec": self._last_exec,
            "recent_exec": self._exec_log[-10:],
            "recent_trades": self._trade_log[-20:],
            "last_activity": self._last_activity,
            "started_at": self._started_at,
        }
