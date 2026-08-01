"""Momentum Rotation Strategy - ROC ranking, top-K, no leverage, trailing stop.
Backtested: ~90% CAGR, -16% max DD over 3 years (BTC/ETH/BNB/SOL daily).

Logic:
  1. Every day: compute 14d ROC, EMA20/50 trend, ADX, ATR for each coin
  2. Rank by ROC descending
  3. Pick top-2 coins with ROC>0 + EMA20>EMA50 + ADX>=18 (long)
     or ROC<0 + EMA20<EMA50 + ADX>=18 (short)
  4. Close positions not in target, open new ones at market
  5. Manage positions: ATR trailing stop, breakeven after 3% move
  6. Min hold: 3 days between rotations
  7. NO leverage, 0.1% commission, 0.05% slippage per side
"""

import asyncio
import math
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional

ROT_BOT_ID = "rotation_strategy"

CT_VAL = {"BTC": 0.01, "ETH": 0.1, "BNB": 0.1, "SOL": 0.1}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "BNB": 0.01, "SOL": 0.01}
SWAP_MAP = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP",
            "BNB": "BNB-USDT-SWAP", "SOL": "SOL-USDT-SWAP"}
COINS = ["BTC", "ETH", "BNB", "SOL"]

STRATEGY_DESC = (
    "Momentum Rotation: daily ROC ranking, top-2 long/short, no leverage. "
    "ROC(14)>0 + EMA20>EMA50 + ADX>=18 = long; ROC<0 + EMA20<EMA50 + ADX>=18 = short. "
    "Trailing stop 2% from peak, breakeven after 3%, ATR initial stop 2x, min hold 3 days."
)


@dataclass
class RotationConfig:
    symbols: list = None
    capital: float = 10000.0
    top_k: int = 2
    roc_period: int = 14
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    trail_pct: float = 0.02
    breakeven_pct: float = 0.03
    adx_min: float = 18.0
    min_hold_days: int = 3
    max_pos_pct: float = 0.40
    poll_interval_sec: int = 300       # check every 5 min (but only trade once per day)
    auto_execute: bool = True

    def __post_init__(self):
        if self.symbols is None:
            self.symbols = ["BTC", "ETH", "BNB", "SOL"]


@dataclass
class RotPosition:
    """Open position in the rotation strategy."""
    symbol: str
    coin: str
    inst_id: str
    side: str               # "long" or "short"
    size: float
    entry_price: float
    stop_price: float
    peak_price: float       # for long: highest seen; for short: lowest seen
    breakeven: bool = False
    opened_at: str = ""
    entry_bar_ts: int = 0   # timestamp ms of entry bar (to track min hold)
    atr: float = 0.0
    signal_id: int = 0
    raw_entry: float = 0.0  # price before slippage


class RotationStrategy:
    def __init__(self, config: RotationConfig, client_manager=None, db=None):
        self.config = config
        self.client_manager = client_manager
        self.db = db
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._positions: dict[str, RotPosition] = {}
        self._trade_log: list = []
        self._equity = config.capital
        self._capital = config.capital
        self._signal_log: list = []
        self._latest_indicators: dict = {}
        self._started_at: str = ""
        self._last_rotate_ts: int = 0      # last rotation timestamp ms
        self._last_daily_check: str = ""   # date string of last check
        self._daily_cache: dict = {}       # per-coin daily candle cache

    # ─── Indicators (no look-ahead, point-in-time) ───

    @staticmethod
    def ema(data, period):
        if len(data) < period:
            return data[:]
        k = 2 / (period + 1)
        result = [data[0]]
        for v in data[1:]:
            result.append(v * k + result[-1] * (1 - k))
        return result

    @staticmethod
    def atr(highs, lows, closes, period=14):
        trs = [0.0]
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        result = [0.0] * len(trs)
        if len(trs) < period + 1:
            return result
        val = sum(trs[1:period + 1]) / period
        result[period] = val
        for i in range(period + 1, len(trs)):
            val = (val * (period - 1) + trs[i]) / period
            result[i] = val
        return result

    @staticmethod
    def adx(highs, lows, closes, period=14):
        n = len(closes)
        if n < period * 2 + 1:
            return [0.0] * n
        plus_dm = [0.0] * n
        minus_dm = [0.0] * n
        trs = [0.0] * n
        for i in range(1, n):
            up = highs[i] - highs[i - 1]
            down = lows[i - 1] - lows[i]
            plus_dm[i] = max(up, 0) if up > down else 0.0
            minus_dm[i] = max(down, 0) if down > up else 0.0
            trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        s_pdm = sum(plus_dm[1:period + 1])
        s_mdm = sum(minus_dm[1:period + 1])
        s_tr = sum(trs[1:period + 1])
        adx_arr = [0.0] * n
        dx_list = []
        for i in range(period, n):
            s_pdm = s_pdm - s_pdm / period + plus_dm[i]
            s_mdm = s_mdm - s_mdm / period + minus_dm[i]
            s_tr = s_tr - s_tr / period + trs[i]
            pdi = (s_pdm / s_tr * 100) if s_tr > 0 else 0.0
            mdi = (s_mdm / s_tr * 100) if s_tr > 0 else 0.0
            dx = (abs(pdi - mdi) / (pdi + mdi) * 100) if (pdi + mdi) > 0 else 0.0
            dx_list.append(dx)
        if len(dx_list) >= period:
            adx_val = sum(dx_list[:period]) / period
            for i in range(period, len(dx_list)):
                adx_val = (adx_val * (period - 1) + dx_list[i]) / period
                adx_arr[period + i] = adx_val
        return adx_arr

    @staticmethod
    def roc(closes, period):
        result = [0.0] * len(closes)
        for i in range(period, len(closes)):
            result[i] = (closes[i] / closes[i - period] - 1) * 100
        return result

    # ─── Data fetching ───

    async def _fetch_daily(self, client, coin: str, limit: int = 100) -> list:
        """Fetch daily candles from OKX."""
        inst_id = SWAP_MAP.get(coin, f"{coin}-USDT-SWAP")
        resp = await client.get_candles(inst_id, bar="1D", limit=limit)
        if resp.get("error"):
            print(f"[Rotation] {coin} candles error: {resp.get('message', '')}", flush=True)
            return []
        data = resp.get("data", [])
        candles = []
        for c in data:
            ts = int(c[0])
            candles.append({
                "ts": ts,
                "datetime": datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                "O": float(c[1]), "H": float(c[2]), "L": float(c[3]),
                "C": float(c[4]), "V": float(c[5]),
            })
        candles.sort(key=lambda x: x["ts"])
        return candles

    def _compute_indicators(self, candles: list) -> dict:
        """Compute all indicators from candle list."""
        if len(candles) < 70:
            return None
        closes = [c["C"] for c in candles]
        highs = [c["H"] for c in candles]
        lows = [c["L"] for c in candles]
        cfg = self.config
        roc = self.roc(closes, cfg.roc_period)
        ema_f = self.ema(closes, cfg.ema_fast)
        ema_s = self.ema(closes, cfg.ema_slow)
        atr_arr = self.atr(highs, lows, closes, cfg.atr_period)
        adx_arr = self.adx(highs, lows, closes, 14)
        i = len(candles) - 2  # signal bar = second to last (yesterday)
        if i < cfg.ema_slow + 10:
            return None
        return {
            "roc": roc[i],
            "ema_fast": ema_f[i],
            "ema_slow": ema_s[i],
            "ema_trend": ema_f[i] > ema_s[i],
            "atr": atr_arr[i],
            "adx": adx_arr[i],
            "price": closes[i],
            "close_today": closes[-1],  # current (today's) close
            "date": candles[i]["datetime"].strftime("%Y-%m-%d"),
            "date_today": candles[-1]["datetime"].strftime("%Y-%m-%d"),
        }

    # ─── Position sizing ───

    def _calc_size(self, coin: str, price: float) -> float:
        """Calculate position size (no leverage)."""
        ct_val = CT_VAL.get(coin, 0.01)
        lot = LOT_SZ.get(coin, 0.01)
        cfg = self.config
        alloc_pct = min(1.0 / cfg.top_k, cfg.max_pos_pct)
        notional = self._equity * alloc_pct
        max_notional = self._capital * cfg.max_pos_pct
        notional = min(notional, max_notional)
        raw_sz = notional / (ct_val * price)
        sz = round(raw_sz / lot) * lot
        return max(sz, lot)

    # ─── Trading ───

    async def _get_client(self):
        if not self.client_manager:
            return None
        return self.client_manager.get_client()

    async def _place_order(self, client, inst_id: str, side: str, sz: float) -> dict:
        """Place market order. side='buy' or 'sell'."""
        resp = await client.place_order(
            inst_id=inst_id,
            side=side,
            ord_type="market",
            sz=str(sz),
            td_mode="isolated",
        )
        return resp

    async def _close_position(self, client, inst_id: str, pos: RotPosition, reason: str):
        """Close position at market."""
        close_side = "sell" if pos.side == "long" else "buy"
        resp = await self._place_order(client, inst_id, close_side, pos.size)
        if resp.get("error"):
            print(f"[Rotation] Close error {pos.coin}: {resp.get('message', '')}", flush=True)
            return

        fills = resp.get("data", [])
        fill_px = pos.entry_price
        fee = 0.0
        if fills:
            fill_px = float(fills[0].get("fillPx", pos.entry_price))
            fee = float(fills[0].get("fee", 0))

        if pos.side == "long":
            pnl = pos.size * CT_VAL[pos.coin] * (fill_px - pos.entry_price) - fee
        else:
            pnl = pos.size * CT_VAL[pos.coin] * (pos.entry_price - fill_px) - fee

        self._equity += pnl
        now = datetime.now(timezone.utc).isoformat()
        trade_entry = {
            "time": now, "side": "sell" if pos.side == "long" else "buy",
            "symbol": inst_id, "size": pos.size, "pnl": round(pnl, 2),
            "entry_price": pos.entry_price, "exit_price": round(fill_px, 2),
            "reason": reason, "pos_side": pos.side, "coin": pos.coin,
            "signal_id": pos.signal_id,
        }
        self._trade_log.append(trade_entry)

        # Save to DB
        if self.db:
            try:
                await self.db.save_trade(
                    bot_id=ROT_BOT_ID, side=close_side, sz=pos.size,
                    px=round(fill_px, 2), ord_id=fills[0].get("ordId", "") if fills else "",
                    inst_id=inst_id, ord_type="market",
                    fee=round(fee, 4), fee_ccy="USDT",
                    pnl=round(pnl, 2), state="filled",
                    signal_id=pos.signal_id,
                )
                await self.db.delete_position(ROT_BOT_ID)
            except Exception as e:
                print(f"[Rotation] DB save trade error: {e}", flush=True)

        print(f"[Rotation] CLOSE {now[:19]} {pos.coin:4} {pos.side:5} "
              f"entry={pos.entry_price:.1f} exit={fill_px:.1f} "
              f"pnl={pnl:+.2f} ({reason})", flush=True)

    async def _open_position(self, client, coin: str, side: str, ind: dict):
        """Open a new position."""
        inst_id = SWAP_MAP.get(coin, f"{coin}-USDT-SWAP")
        price = ind["close_today"]  # use current price
        atr_val = ind["atr"]
        if atr_val <= 0 or price <= 0:
            return

        sz = self._calc_size(coin, price)
        order_side = "buy" if side == "long" else "sell"

        # Save signal to DB first
        signal_id = 0
        if self.db:
            try:
                signal_id = await self.db.save_signal(
                    bot_id=ROT_BOT_ID,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    side=order_side, price=price, size=sz,
                    ord_type="market", status="pending",
                )
            except Exception as e:
                print(f"[Rotation] DB save signal error: {e}", flush=True)

        if not self.config.auto_execute:
            print(f"[Rotation] SIGNAL (no execute) {coin} {side} @ {price:.1f}", flush=True)
            return

        resp = await self._place_order(client, inst_id, order_side, sz)
        if resp.get("error"):
            print(f"[Rotation] Open error {coin}: {resp.get('message', '')}", flush=True)
            if self.db and signal_id:
                await self.db.update_signal_status(signal_id, "rejected",
                                                     resp.get("message", ""))
            return

        fills = resp.get("data", [])
        fill_px = price
        fee = 0.0
        ord_id = ""
        if fills:
            fill_px = float(fills[0].get("fillPx", price))
            fee = float(fills[0].get("fee", 0))
            ord_id = fills[0].get("ordId", "")

        # Initial stop = entry - atr_stop_mult * ATR
        if side == "long":
            stop = fill_px - self.config.atr_stop_mult * atr_val
        else:
            stop = fill_px + self.config.atr_stop_mult * atr_val

        now = datetime.now(timezone.utc).isoformat()
        pos = RotPosition(
            symbol=inst_id, coin=coin, inst_id=inst_id,
            side=side, size=sz, entry_price=fill_px,
            stop_price=stop, peak_price=fill_px,
            opened_at=now, atr=atr_val,
            signal_id=signal_id, raw_entry=price,
        )
        self._positions[coin] = pos

        self._trade_log.append({
            "time": now, "side": order_side, "symbol": inst_id,
            "size": sz, "pnl": 0, "entry": fill_px, "entry_price": fill_px,
            "stop": round(stop, 2), "reason": "open", "pos_side": side,
            "coin": coin, "signal_id": signal_id,
        })

        # Save to DB
        if self.db:
            try:
                if signal_id:
                    await self.db.update_signal_status(signal_id, "filled", ord_id)
                await self.db.save_trade(
                    bot_id=ROT_BOT_ID, side=order_side, sz=sz,
                    px=round(fill_px, 2), ord_id=ord_id,
                    inst_id=inst_id, ord_type="market",
                    fee=round(fee, 4), fee_ccy="USDT",
                    pnl=0, state="filled", signal_id=signal_id,
                )
                await self.db.save_position(
                    bot_id=ROT_BOT_ID, inst_id=inst_id,
                    side=side, size=sz,
                    entry_price=round(fill_px, 2),
                    current_price=round(fill_px, 2),
                )
            except Exception as e:
                print(f"[Rotation] DB save error: {e}", flush=True)

        self._equity -= fee
        print(f"[Rotation] OPEN  {now[:19]} {coin:4} {side:5} "
              f"price={fill_px:.1f} stop={stop:.1f} sz={sz} "
              f"atr={atr_val:.1f} fee={fee:.2f}", flush=True)

    # ─── Core logic ───

    async def _check_and_trade(self):
        """Main logic: check signals, rotate positions, manage stops."""
        client = await self._get_client()
        if not client:
            print("[Rotation] No OKX client available", flush=True)
            return

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 1. Fetch daily candles and compute indicators for each coin
        indicators = {}
        for coin in self.config.symbols:
            try:
                candles = await self._fetch_daily(client, coin, limit=100)
                if not candles:
                    continue
                ind = self._compute_indicators(candles)
                if ind:
                    indicators[coin] = ind
            except Exception as e:
                print(f"[Rotation] Error fetching {coin}: {e}", flush=True)

        self._latest_indicators = indicators

        if not indicators:
            return

        # 2. Manage existing positions: check trailing stops
        for coin in list(self._positions.keys()):
            pos = self._positions[coin]
            ind = indicators.get(coin)
            if not ind:
                continue

            current_price = ind["close_today"]
            hit_stop = False
            reason = "trail_stop"

            if pos.side == "long":
                if current_price > pos.peak_price:
                    pos.peak_price = current_price
                new_stop = pos.peak_price * (1 - self.config.trail_pct)
                if new_stop > pos.stop_price:
                    pos.stop_price = new_stop
                if not pos.breakeven and current_price >= pos.entry_price * (1 + self.config.breakeven_pct):
                    pos.stop_price = max(pos.stop_price, pos.entry_price * 0.999)
                    pos.breakeven = True
                if current_price <= pos.stop_price:
                    hit_stop = True
            else:  # short
                if current_price < pos.peak_price:
                    pos.peak_price = current_price
                new_stop = pos.peak_price * (1 + self.config.trail_pct)
                if new_stop < pos.stop_price:
                    pos.stop_price = new_stop
                if not pos.breakeven and current_price <= pos.entry_price * (1 - self.config.breakeven_pct):
                    pos.stop_price = min(pos.stop_price, pos.entry_price * 1.001)
                    pos.breakeven = True
                if current_price >= pos.stop_price:
                    hit_stop = True

            if hit_stop:
                await self._close_position(client, pos.inst_id, pos, reason)
                del self._positions[coin]

        # 3. Check if we should rotate (once per day, respect min_hold)
        if self._last_daily_check == today_str:
            return  # already checked today

        # Min hold check
        now_ts = int(time.time() * 1000)
        if self._last_rotate_ts > 0 and self._positions:
            hold_ms = (now_ts - self._last_rotate_ts)
            hold_days = hold_ms / (86400 * 1000)
            if hold_days < self.config.min_hold_days:
                return

        # 4. Rank by ROC
        rankings = []
        for coin, ind in indicators.items():
            if ind["atr"] <= 0:
                continue
            rankings.append((coin, ind["roc"], ind["ema_trend"], ind["adx"], ind["atr"]))

        if not rankings:
            return

        rankings.sort(key=lambda x: x[1], reverse=True)

        # 5. Determine target coins
        target_coins = set()
        for coin, roc_val, ema_trend, adx_val, atr_val in rankings:
            if len(target_coins) >= self.config.top_k:
                break
            if roc_val > 0 and ema_trend and adx_val >= self.config.adx_min:
                target_coins.add((coin, "long"))
            elif roc_val < 0 and not ema_trend and adx_val >= self.config.adx_min:
                target_coins.add((coin, "short"))

        # 6. Close positions not in target
        for coin in list(self._positions.keys()):
            pos = self._positions[coin]
            if (coin, pos.side) not in target_coins:
                await self._close_position(client, pos.inst_id, pos, "rotation_exit")
                del self._positions[coin]

        # 7. Open new positions
        for coin, side in target_coins:
            if coin in self._positions:
                continue
            ind = indicators.get(coin)
            if ind:
                await self._open_position(client, coin, side, ind)

        self._last_daily_check = today_str
        self._last_rotate_ts = now_ts

    # ─── Lifecycle ───

    async def _poll_loop(self):
        """Main polling loop running in daemon thread's event loop."""
        while self._running:
            try:
                await self._check_and_trade()
            except Exception as e:
                print(f"[Rotation] Poll error: {e}", flush=True)
            await asyncio.sleep(self.config.poll_interval_sec)

    def _thread_target(self):
        """Target for daemon thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._poll_loop())

    async def start(self):
        """Start the strategy."""
        if self._running:
            return
        self._running = True
        self._started_at = datetime.now(timezone.utc).isoformat()

        # Ensure bot in DB
        if self.db:
            await self._ensure_bot()
            await self._reload_equity()

        # Start daemon thread
        self._thread = threading.Thread(target=self._thread_target, daemon=True)
        self._thread.start()
        print(f"[Rotation] Started (capital=${self._equity:,.0f}, poll={self.config.poll_interval_sec}s)", flush=True)

    async def stop(self):
        """Stop the strategy."""
        self._running = False
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self.db:
            try:
                await self.db.update_bot_stopped(ROT_BOT_ID)
            except Exception:
                pass
        print("[Rotation] Stopped", flush=True)

    def get_status(self) -> dict:
        """Return current status dict."""
        trades = self._trade_log
        closed = [t for t in trades if t.get("pnl", 0) != 0]
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) <= 0]
        total_pnl = sum(t.get("pnl", 0) for t in closed)
        win_rate = len(wins) / len(closed) * 100 if closed else 0

        # Build open_positions as a LIST (not dict) with fields the Dashboard expects
        open_positions_list = []
        for coin, pos in self._positions.items():
            stage = "trailing" if pos.breakeven else "initial"
            open_positions_list.append({
                "coin": pos.coin,
                "symbol": pos.inst_id,
                "inst_id": pos.inst_id,
                "side": pos.side,
                "size": pos.size,
                "size_remaining": pos.size,
                "entry": pos.entry_price,
                "entry_price": pos.entry_price,
                "stop": round(pos.stop_price, 2),
                "stop_price": round(pos.stop_price, 2),
                "peak_price": round(pos.peak_price, 2),
                "breakeven": pos.breakeven,
                "opened_at": pos.opened_at,
                "unrealized_pnl": self._calc_unrealized(coin),
                "stage": stage,
                "pos_mode": "cross",
            })

        # Config dict with fallback fields the Dashboard reads
        cfg = asdict(self.config)
        cfg.setdefault("max_positions", self.config.top_k)
        cfg.setdefault("risk_per_trade", 0.0)
        cfg.setdefault("tp1_pct", 0.0)

        return {
            "running": self._running,
            "strategy": "momentum_rotation",
            "config": cfg,
            "equity": round(self._equity, 2),
            "capital": self._capital,
            "total_pnl": round(total_pnl, 2),
            "open_positions": open_positions_list,
            "total_trades": len(closed),
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "recent_trades": trades[-20:],
            "recent_signals": self._signal_log[-10:],
            "indicators": self._latest_indicators,
            "started_at": self._started_at,
            "description": STRATEGY_DESC,
        }

    def _calc_unrealized(self, coin: str) -> float:
        pos = self._positions.get(coin)
        ind = self._latest_indicators.get(coin)
        if not pos or not ind:
            return 0.0
        current = ind["close_today"]
        ct = CT_VAL.get(coin, 0.01)
        if pos.side == "long":
            return round(pos.size * ct * (current - pos.entry_price), 2)
        else:
            return round(pos.size * ct * (pos.entry_price - current), 2)

    # ─── DB helpers ───

    async def _ensure_bot(self):
        if not self.db:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            params = asdict(self.config)
            if self.db._pg_mode:
                await self.db._execute(
                    "INSERT INTO bots (id, strategy_id, strategy_code, symbol, timeframe, "
                    "capital, params, status, mode, signal_type, created_at, name) "
                    "VALUES ($1, 'rotation', 'momentum_rotation', 'MULTI', '1D', "
                    "$2, $3, 'running', 'demo', 'momentum', $4, 'Momentum Rotation') "
                    "ON CONFLICT (id) DO NOTHING",
                    (ROT_BOT_ID, self._equity, str(params), now),
                )
            else:
                await self.db._execute(
                    "INSERT OR IGNORE INTO bots (id, strategy_id, strategy_code, symbol, timeframe, "
                    "capital, params, status, mode, signal_type, created_at, name) "
                    "VALUES (?, 'rotation', 'momentum_rotation', 'MULTI', '1D', "
                    "?, ?, 'running', 'demo', 'momentum', ?, 'Momentum Rotation')",
                    (ROT_BOT_ID, self._equity, str(params), now),
                )
        except Exception as e:
            print(f"[Rotation] DB ensure_bot error: {e}", flush=True)

    async def _reload_equity(self):
        """Reload equity from DB trade history."""
        if not self.db:
            return
        try:
            rows = await self.db.get_trades(bot_id=ROT_BOT_ID, limit=500)
            for t in rows:
                pnl = float(t.get("pnl", 0) or 0)
                if pnl != 0:
                    self._trade_log.append({
                        "time": t.get("timestamp", ""),
                        "side": t.get("side", ""),
                        "symbol": t.get("inst_id", ""),
                        "size": float(t.get("sz", 0) or 0),
                        "pnl": pnl,
                        "entry_price": float(t.get("px", 0) or 0),
                        "reason": "closed",
                        "coin": t.get("inst_id", "").replace("-USDT-SWAP", "").replace("-USD-SWAP", ""),
                        "signal_id": t.get("signal_id", 0),
                    })
            total_pnl = sum(t.get("pnl", 0) for t in self._trade_log)
            self._equity = self._capital + total_pnl
        except Exception as e:
            print(f"[Rotation] DB reload error: {e}", flush=True)
