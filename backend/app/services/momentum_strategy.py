"""Momentum strategy service — daily multi-timeframe momentum with trailing stops."""

import asyncio
import threading
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional

MOM_BOT_ID = "momentum_strategy"

CT_VAL = {"BTC": 0.01, "ETH": 0.1, "BNB": 0.1, "SOL": 0.1}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "BNB": 0.01, "SOL": 0.01}
SWAP_MAP = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP",
            "BNB": "BNB-USDT-SWAP", "SOL": "SOL-USDT-SWAP"}


@dataclass
class MomentumConfig:
    symbols: list = None
    risk_per_trade: float = 0.03
    max_positions: int = 4
    auto_execute: bool = True
    poll_interval_sec: int = 60
    roc_fast: int = 5
    roc_slow: int = 50
    ema_fast: int = 15
    ema_slow: int = 30
    atr_stop_mult: float = 1.5
    trail_pct: float = 0.03
    adx_threshold: float = 20.0
    mom_threshold: float = 0.0
    breakeven_pct: float = 0.005  # 0.5% → move stop to entry
    tp1_pct: float = 0.02         # 2% → partial close
    tp1_frac: float = 0.75        # close 75% at TP1
    sl1_pct: float = 1.0          # 0=off, 1.0=-1% → cascade stop partial close
    sl1_frac: float = 0.5         # close 50% at SL1

    def __post_init__(self):
        if self.symbols is None:
            self.symbols = ["BTC", "ETH", "BNB", "SOL"]


STRATEGY_DESC = (
    "Momentum на дневных свечах: вход при ROC5>0, ROC50>0, EMA15>EMA30, ADX>20, PDI>MDI. "
    "Выход: 4-стадийный — (1) каскадный стоп -1%/close50%, (2) трейлинг 1.5% от пика, "
    "(3) безубыток при +0.5%, (4) частичное закрытие 50% при +2%, остаток с трейлингом."
)


@dataclass
class OpenPosition:
    symbol: str
    entry_price: float
    stop_price: float
    peak_price: float
    size: float
    atr: float
    size_remaining: float = 0.0
    stage: str = "initial"
    opened_at: str = ""
    inst_id: str = ""


class MomentumStrategy:
    def __init__(self, config: MomentumConfig, client_manager=None, db=None):
        self.config = config
        self.client_manager = client_manager
        self.db = db
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._positions: dict[str, OpenPosition] = {}
        self._cooldowns: dict[str, int] = {}
        self._signal_log: list = []
        self._trade_log: list = []
        self._equity = 10000.0
        self._latest_indicators: dict = {}

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
                    "VALUES ($1, 'momentum', 'momentum', 'MULTI', '1D', "
                    "$2, $3, 'running', 'demo', 'momentum', $4, 'Momentum Strategy') "
                    "ON CONFLICT (id) DO NOTHING",
                    (MOM_BOT_ID, self._equity, str(params), now),
                )
            else:
                await self.db._execute(
                    "INSERT OR IGNORE INTO bots (id, strategy_id, strategy_code, symbol, timeframe, "
                    "capital, params, status, mode, signal_type, created_at, name) "
                    "VALUES (?, 'momentum', 'momentum', 'MULTI', '1D', "
                    "?, ?, 'running', 'demo', 'momentum', ?, 'Momentum Strategy')",
                    (MOM_BOT_ID, self._equity, str(params), now),
                )
        except Exception as e:
            print(f"[Momentum] DB ensure_bot error: {e}", flush=True)

    async def _reload_from_db(self):
        if not self.db:
            return
        try:
            trades = await self.db.get_trades(bot_id=MOM_BOT_ID, limit=200)
            for t in trades:
                self._trade_log.append({
                    "time": t.get("timestamp", ""),
                    "side": t.get("side", ""),
                    "symbol": t.get("inst_id", ""),
                    "size": float(t.get("sz", 0) or 0),
                    "ord_id": t.get("ord_id", ""),
                    "pnl": float(t.get("pnl", 0) or 0),
                })
            print(f"[Momentum] Reloaded {len(self._trade_log)} trades from DB", flush=True)
        except Exception as e:
            print(f"[Momentum] DB reload error: {e}", flush=True)

    async def start(self):
        if self._running:
            return
        self._running = True
        await self._ensure_bot()
        await self._reload_from_db()
        await self._load_equity()
        self._thread = threading.Thread(target=self._run_thread, daemon=True, name="momentum-strategy")
        self._thread.start()
        print(f"[Momentum] Started — symbols={self.config.symbols} risk={self.config.risk_per_trade}", flush=True)

    async def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
        print("[Momentum] Stopped", flush=True)

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._poll_loop())
        except Exception as e:
            print(f"[Momentum] Thread error: {e}", flush=True)
        finally:
            self._loop.close()

    async def _poll_loop(self):
        while self._running:
            try:
                await self._check_and_trade()
                await asyncio.sleep(self.config.poll_interval_sec)
            except Exception as e:
                print(f"[Momentum] Error: {e}", flush=True)
                await asyncio.sleep(300)

    async def _load_equity(self):
        if not self.client_manager:
            return
        client = self.client_manager.get_client()
        if not client:
            return
        try:
            result = await client.get_balance()
            if not result.get("error"):
                details = result.get("data", [{}])[0].get("details", [])
                for d in details:
                    if d.get("ccy") == "USDT":
                        self._equity = float(d.get("eqUsd", 10000))
                        break
        except Exception as e:
            print(f"[Momentum] Load equity error: {e}", flush=True)

    async def _fetch_daily_candles(self, coin: str, limit: int = 80) -> list:
        if not self.client_manager:
            return []
        client = self.client_manager.get_client()
        if not client:
            return []
        inst_id = SWAP_MAP.get(coin)
        if not inst_id:
            return []
        try:
            result = await client.get_candles(inst_id=inst_id, bar="1D", limit=limit)
            if result.get("error"):
                return []
            candles = result.get("data", [])
            parsed = []
            for c in reversed(candles):
                parsed.append({
                    "ts": int(c[0]),
                    "O": float(c[1]),
                    "H": float(c[2]),
                    "L": float(c[3]),
                    "C": float(c[4]),
                    "V": float(c[5]),
                })
            return parsed
        except Exception as e:
            print(f"[Momentum] Candles error {coin}: {e}", flush=True)
            return []

    def _compute_indicators(self, candles: list) -> dict:
        closes = [c["C"] for c in candles]
        highs = [c["H"] for c in candles]
        lows = [c["L"] for c in candles]
        n = len(closes)
        if n < 55:
            return {}

        roc_fast = self.config.roc_fast
        roc_slow = self.config.roc_slow
        ema_fast = self.config.ema_fast
        ema_slow = self.config.ema_slow

        # ROC
        roc_f = (closes[-1] / closes[-roc_fast] - 1) * 100 if n > roc_fast else None
        roc_s = (closes[-1] / closes[-roc_slow] - 1) * 100 if n > roc_slow else None

        # EMA
        def ema(data, period):
            k = 2 / (period + 1)
            val = data[0]
            for v in data[1:]:
                val = v * k + val * (1 - k)
            return val

        ema_f = ema(closes[-max(ema_fast, 30):], ema_fast) if n >= ema_fast else None
        ema_s = ema(closes[-max(ema_slow, 55):], ema_slow) if n >= ema_slow else None

        # ATR (14-period)
        trs = []
        for i in range(1, min(15, n)):
            tr = max(
                highs[-i] - lows[-i],
                abs(highs[-i] - closes[-(i + 1)]),
                abs(lows[-i] - closes[-(i + 1)])
            )
            trs.append(tr)
        atr = sum(trs) / len(trs) if trs else 0

        # ADX (14-period Wilder smoothing)
        period = 14
        if n >= period + 2:
            plus_dm_arr = []
            minus_dm_arr = []
            tr_arr = []
            for i in range(1, n):
                up = highs[i] - highs[i - 1]
                dn = lows[i - 1] - lows[i]
                plus_dm_arr.append(max(up, 0) if up > dn else 0)
                minus_dm_arr.append(max(dn, 0) if dn > up else 0)
                tr_arr.append(max(highs[i] - lows[i],
                                  abs(highs[i] - closes[i - 1]),
                                  abs(lows[i] - closes[i - 1])))
            # Wilder smoothing for TR, +DM, -DM
            atr_w = sum(tr_arr[:period])
            plus_dm_w = sum(plus_dm_arr[:period])
            minus_dm_w = sum(minus_dm_arr[:period])
            pdi_arr = []
            mdi_arr = []
            for i in range(period, len(tr_arr)):
                atr_w = atr_w - atr_w / period + tr_arr[i]
                plus_dm_w = plus_dm_w - plus_dm_w / period + plus_dm_arr[i]
                minus_dm_w = minus_dm_w - minus_dm_w / period + minus_dm_arr[i]
                pdi = 100 * plus_dm_w / atr_w if atr_w > 0 else 0
                mdi = 100 * minus_dm_w / atr_w if atr_w > 0 else 0
                pdi_arr.append(pdi)
                mdi_arr.append(mdi)
            # DX series
            dx_arr = []
            for p, m in zip(pdi_arr, mdi_arr):
                s = p + m
                dx_arr.append(abs(p - m) / s * 100 if s > 0 else 0)
            # ADX = EMA of DX
            if dx_arr:
                adx = sum(dx_arr[-period:]) / min(period, len(dx_arr))
            else:
                adx = 0
            plus_di = pdi_arr[-1] if pdi_arr else 0
            minus_di = mdi_arr[-1] if mdi_arr else 0
        else:
            adx = 0; plus_di = 0; minus_di = 0

        return {
            "roc_fast": roc_f,
            "roc_slow": roc_s,
            "ema_fast": ema_f,
            "ema_slow": ema_s,
            "atr": atr,
            "adx": adx,
            "pdi": plus_di,
            "mdi": minus_di,
            "price": closes[-1],
        }

    async def _check_and_trade(self):
        await self._load_equity()
        await self._manage_positions()

        for coin in self.config.symbols:
            if coin in self._positions:
                continue
            if self._cooldowns.get(coin, 0) > 0:
                self._cooldowns[coin] -= 1
                continue
            if sum(1 for p in self._positions.values()) >= self.config.max_positions:
                continue

            candles = await self._fetch_daily_candles(coin, 80)
            if not candles or len(candles) < 55:
                continue

            ind = self._compute_indicators(candles)
            if not ind:
                continue

            self._latest_indicators[coin] = {
                "pass": self._check_entry_signal(ind),
                **{k: v for k, v in ind.items() if isinstance(v, (int, float, bool))},
            }

            if self._check_entry_signal(ind):
                await self._open_position(coin, ind)

    def _check_entry_signal(self, ind: dict) -> bool:
        rf = ind.get("roc_fast")
        rm = ind.get("roc_slow")
        ef = ind.get("ema_fast")
        es = ind.get("ema_slow")
        ad = ind.get("adx", 0)
        pdi = ind.get("pdi", 0)
        mdi = ind.get("mdi", 0)

        if None in (rf, rm, ef, es):
            return False

        mom_score = rf * 0.5 + rm * 0.5
        return (
            rf > 0 and rm > 0
            and mom_score > self.config.mom_threshold
            and ef > es
            and ad > self.config.adx_threshold
            and pdi > mdi
        )

    async def _open_position(self, coin: str, ind: dict):
        if not self.client_manager:
            return
        client = self.client_manager.get_client()
        if not client:
            return

        inst_id = SWAP_MAP.get(coin)
        atr = ind["atr"]
        price = ind["price"]

        if atr <= 0 or price <= 0:
            return

        stop_price = price - self.config.atr_stop_mult * atr
        risk_per_contract = price - stop_price
        if risk_per_contract <= 0:
            return

        risk_amount = self._equity * self.config.risk_per_trade
        raw_sz = risk_amount / risk_per_contract
        ct_val = CT_VAL.get(coin, 0.1)
        raw_sz = raw_sz / ct_val
        lot = LOT_SZ.get(coin, 0.01)
        sz = round(raw_sz / lot) * lot

        if sz < lot:
            print(f"[Momentum] {coin}: size too small {sz:.4f}", flush=True)
            return

        try:
            result = await client.place_order(
                inst_id=inst_id, side="buy", ord_type="market",
                sz=f"{sz:.2f}", td_mode="cross", pos_side="long",
            )
            if result.get("error"):
                print(f"[Momentum] {coin}: order failed: {result.get('message')}", flush=True)
                return

            ord_id = result.get("data", [{}])[0].get("ordId", "")
            now = datetime.now(timezone.utc).isoformat()

            self._positions[coin] = OpenPosition(
                symbol=coin, entry_price=price, stop_price=stop_price,
                peak_price=price, size=sz, size_remaining=sz,
                stage="initial", atr=atr, opened_at=now, inst_id=inst_id,
            )

            signal_entry = {
                "time": now, "side": "buy", "symbol": inst_id,
                "size": sz, "ord_id": ord_id,
                "entry": price, "stop": stop_price,
                "adx": round(ind["adx"], 1),
                "roc_f": round(ind["roc_fast"], 2),
                "roc_s": round(ind["roc_slow"], 2),
            }
            self._signal_log.append(signal_entry)
            self._trade_log.append(signal_entry)

            print(f"[Momentum] OPEN {coin} @ {price:.2f} sz={sz:.2f} "
                  f"stop={stop_price:.2f} ADX={ind['adx']:.1f}",
                  flush=True)

            await self._save_signal_db("buy", coin, price)
            await self._save_trade_db(signal_entry)

        except Exception as e:
            print(f"[Momentum] {coin}: open error: {e}", flush=True)

    async def _manage_positions(self):
        if not self.client_manager:
            return
        client = self.client_manager.get_client()
        if not client:
            return

        for coin in list(self._positions.keys()):
            pos = self._positions[coin]
            try:
                if pos.size_remaining <= 0:
                    del self._positions[coin]
                    continue

                ticker = await client.get_ticker(pos.inst_id)
                if ticker.get("error") or not ticker.get("data"):
                    continue
                cur = float(ticker["data"][0]["last"])

                if cur > pos.peak_price:
                    pos.peak_price = cur

                stage = pos.stage
                entry = pos.entry_price
                be_pct = self.config.breakeven_pct
                tp1_pct = self.config.tp1_pct
                trail_pct = self.config.trail_pct

                if stage == "initial":
                    # Trail from peak
                    new_stop = pos.peak_price * (1 - trail_pct)
                    if new_stop > pos.stop_price:
                        pos.stop_price = new_stop

                    # SL1 cascade stop
                    sl1_pct = self.config.sl1_pct
                    sl1_frac = self.config.sl1_frac
                    if sl1_pct > 0 and sl1_frac > 0 and pos.size_remaining == pos.size:
                        if cur <= entry * (1 - sl1_pct / 100):
                            close_sz = round(pos.size * sl1_frac / LOT_SZ.get(coin, 0.01)) * LOT_SZ.get(coin, 0.01)
                            close_sz = min(close_sz, pos.size_remaining)
                            if close_sz >= LOT_SZ.get(coin, 0.01):
                                await self._partial_close_position(coin, pos, cur, close_sz)
                                pos.stage = "sl1_trimmed"
                                print(f"[Momentum] {coin}: SL1 hit @ {cur:.2f}, closed {close_sz}, "
                                      f"remaining {pos.size_remaining}", flush=True)

                    # Breakeven trigger (only if SL1 didn't fire or size remaining > 0)
                    if pos.size_remaining > 0 and cur >= entry * (1 + be_pct):
                        pos.stop_price = max(pos.stop_price, entry * 0.999)
                        pos.stage = "breakeven"
                        print(f"[Momentum] {coin}: breakeven stage @ {cur:.2f}", flush=True)

                elif stage == "sl1_trimmed":
                    # After SL1: trail, then BE → TP1 → trailing
                    new_stop = pos.peak_price * (1 - trail_pct)
                    if new_stop > pos.stop_price:
                        pos.stop_price = new_stop

                    if cur >= entry * (1 + be_pct):
                        pos.stop_price = max(pos.stop_price, entry * 0.999)
                        pos.stage = "breakeven"
                        print(f"[Momentum] {coin}: SL1→BE stage @ {cur:.2f}", flush=True)

                elif stage == "breakeven":
                    # TP1 partial close
                    if cur >= entry * (1 + tp1_pct):
                        if pos.size_remaining == pos.size:
                            close_sz = round(pos.size * self.config.tp1_frac / LOT_SZ.get(coin, 0.01)) * LOT_SZ.get(coin, 0.01)
                            close_sz = min(close_sz, pos.size_remaining)
                            if close_sz >= LOT_SZ.get(coin, 0.01):
                                await self._partial_close_position(coin, pos, cur, close_sz)
                                pos.stop_price = max(pos.stop_price, entry * 0.999)
                                pos.stage = "trailing"
                                print(f"[Momentum] {coin}: TP1 hit @ {cur:.2f}, closed {close_sz}, "
                                      f"remaining {pos.size_remaining}", flush=True)
                            else:
                                pos.stage = "trailing"
                        else:
                            pos.stage = "trailing"
                    else:
                        # Still trail
                        new_stop = max(pos.stop_price, entry * 0.999)
                        trail_stop = pos.peak_price * (1 - trail_pct)
                        if trail_stop > new_stop:
                            new_stop = trail_stop
                        pos.stop_price = new_stop

                elif stage == "trailing":
                    new_stop = pos.peak_price * (1 - trail_pct)
                    if new_stop > pos.stop_price:
                        pos.stop_price = new_stop

                # Generic stop check (all stages)
                if cur <= pos.stop_price:
                    await self._close_position(coin, pos, cur, pos.stage)

            except Exception as e:
                print(f"[Momentum] {coin}: manage error: {e}", flush=True)

    async def _close_position(self, coin: str, pos: OpenPosition, exit_price: float, reason: str):
        if not self.client_manager:
            return
        client = self.client_manager.get_client()
        if not client:
            return

        try:
            close_sz = pos.size_remaining
            if close_sz <= 0:
                return
            result = await client.place_order(
                inst_id=pos.inst_id, side="sell", ord_type="market",
                sz=f"{close_sz:.2f}", td_mode="cross", pos_side="long",
            )
            if result.get("error"):
                print(f"[Momentum] {coin}: close failed: {result.get('message')}", flush=True)
                return

            pnl = close_sz * (exit_price - pos.entry_price) * CT_VAL.get(coin, 0.1)
            fee = close_sz * (pos.entry_price + exit_price) * 0.001
            net_pnl = pnl - fee
            self._equity += net_pnl

            trade = {
                "time": datetime.now(timezone.utc).isoformat(),
                "side": "sell", "symbol": pos.inst_id,
                "size": close_sz, "exit_price": exit_price,
                "entry_price": pos.entry_price, "pnl": round(net_pnl, 2),
                "reason": reason,
            }
            self._trade_log.append(trade)

            print(f"[Momentum] CLOSE {coin} @ {exit_price:.2f} reason={reason} "
                  f"pnl=${net_pnl:+.2f} equity=${self._equity:.0f}",
                  flush=True)

            del self._positions[coin]
            self._cooldowns[coin] = 3

            await self._save_signal_db("sell", coin, exit_price)
            await self._save_trade_db(trade)

        except Exception as e:
            print(f"[Momentum] {coin}: close error: {e}", flush=True)

    async def _partial_close_position(self, coin: str, pos: OpenPosition, exit_price: float, close_sz: float):
        if not self.client_manager:
            return
        client = self.client_manager.get_client()
        if not client:
            return

        try:
            result = await client.place_order(
                inst_id=pos.inst_id, side="sell", ord_type="market",
                sz=f"{close_sz:.2f}", td_mode="cross", pos_side="long",
            )
            if result.get("error"):
                print(f"[Momentum] {coin}: partial close failed: {result.get('message')}", flush=True)
                return

            pnl = close_sz * (exit_price - pos.entry_price) * CT_VAL.get(coin, 0.1)
            fee = close_sz * (pos.entry_price + exit_price) * 0.001
            net_pnl = pnl - fee
            self._equity += net_pnl
            pos.size_remaining = round(pos.size_remaining - close_sz, 4)

            trade = {
                "time": datetime.now(timezone.utc).isoformat(),
                "side": "sell", "symbol": pos.inst_id,
                "size": close_sz, "exit_price": exit_price,
                "entry_price": pos.entry_price, "pnl": round(net_pnl, 2),
                "reason": "tp1",
            }
            self._trade_log.append(trade)
            await self._save_signal_db("sell", coin, exit_price)
            await self._save_trade_db(trade)

        except Exception as e:
            print(f"[Momentum] {coin}: partial close error: {e}", flush=True)

    async def _save_signal_db(self, side: str, coin: str, price: float):
        if not self.db:
            return
        try:
            await self.db.save_signal(
                bot_id=MOM_BOT_ID,
                timestamp=datetime.now(timezone.utc).isoformat(),
                side=side, price=price,
                status="executed" if self.config.auto_execute else "pending",
            )
        except Exception as e:
            print(f"[Momentum] Save signal error: {e}", flush=True)

    async def _save_trade_db(self, trade: dict):
        if not self.db:
            return
        try:
            await self.db.save_trade(
                bot_id=MOM_BOT_ID,
                side=trade["side"],
                sz=f"{trade['size']:.2f}",
                ord_id=trade.get("ord_id", ""),
                inst_id=trade["symbol"],
                pnl=trade.get("pnl", 0),
                state="filled",
            )
        except Exception as e:
            print(f"[Momentum] Save trade error: {e}", flush=True)

    def get_status(self) -> dict:
        open_pos = []
        for coin, pos in self._positions.items():
            open_pos.append({
                "symbol": pos.symbol,
                "inst_id": pos.inst_id,
                "entry": pos.entry_price,
                "stop": pos.stop_price,
                "size": pos.size,
                "size_remaining": pos.size_remaining,
                "stage": pos.stage,
                "peak_ratio": round((pos.peak_price / pos.entry_price - 1) * 100, 2) if pos.entry_price else 0,
            })
        return {
            "running": self._running,
            "config": asdict(self.config),
            "equity": round(self._equity, 2),
            "open_positions": open_pos,
            "total_signals": len(self._signal_log),
            "total_trades": len(self._trade_log),
            "recent_signals": self._signal_log[-10:],
            "recent_trades": self._trade_log[-10:],
        }
