"""Momentum strategy service — bilateral (long+short) with trend/range market detection."""

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


def get_slippage_pct(sz: float, ct_val: float, price: float) -> float:
    """Dynamic slippage: sqrt model, base 0.05%, cap 0.15%."""
    notional = sz * ct_val * price
    base = 0.0005
    slip = base * (notional / 100_000) ** 0.5
    return min(slip, 0.0015)

FUNDING_LONG_PER_DAY = 0.0003   # 0.03%/day
FUNDING_SHORT_PER_DAY = 0.0001  # 0.01%/day


@dataclass
class MomentumConfig:
    symbols: list = None
    risk_per_trade: float = 0.03
    max_positions: int = 4
    leverage: int = 3              # V6: 3x leverage
    auto_execute: bool = True
    poll_interval_sec: int = 60
    roc_fast: int = 5
    roc_slow: int = 50
    ema_fast: int = 15
    ema_slow: int = 30
    atr_stop_mult: float = 1.5
    trail_pct: float = 0.015        # 1.5% trailing from peak
    adx_threshold: float = 20.0
    mom_threshold: float = 0.0
    breakeven_pct: float = 0.003  # 0.3%
    tp1_pct: float = 0.015         # V6: 1.5% TP1 (from backtest)
    tp1_frac: float = 0.5          # V6: close 50% at TP1
    sl1_pct: float = 0.0           # 0=off
    sl1_frac: float = 0.5
    # Trend/Range detection
    trend_adx_min: float = 25.0    # V6: stricter ADX for trend
    range_adx_max: float = 18.0
    # Range mode settings
    range_bb_period: int = 20
    range_bb_mult: float = 2.0
    range_rsi_period: int = 14
    range_rsi_oversold: float = 35.0
    range_rsi_overbought: float = 65.0
    range_risk_divisor: float = 2.0
    range_sl_mult: float = 1.0

    def __post_init__(self):
        if self.symbols is None:
            self.symbols = ["BTC", "ETH", "BNB", "SOL"]


STRATEGY_DESC = (
    "Bilateral Momentum V6: лонг+шорт, 3x leverage, compounding, dynamic slippage. "
    "Тренд (ADX>25): лонг при ROC>0,EMA15>EMA30,PDI>MDI; шорт при ROC<0,EMA15<EMA30,MDI>PDI. "
    "Флэт (ADX<18): бортовые позиции по Bollinger Bands+RSI. "
    "Выход: трейлинг 1.5%, безубыток 0.3%, частичное 50% при 1.5%."
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
    side: str = "long"            # "long" or "short"
    pos_mode: str = "trend"       # "trend" or "range"
    trough: float = 0.0           # for shorts: lowest price seen
    bb_target: float = 0.0        # for range mode: target price (BB mean)


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
        self._latest_modes: dict[str, str] = {}
        self._started_at: str = ""

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
        self._started_at = datetime.now(timezone.utc).isoformat()
        await self._ensure_bot()
        await self._reload_from_db()
        await self._load_equity()
        self._thread = threading.Thread(target=self._run_thread, daemon=True, name="momentum-strategy")
        self._thread.start()
        print(f"[Momentum] Started — symbols={self.config.symbols} risk={self.config.risk_per_trade}", flush=True)

    async def stop(self):
        self._running = False
        self._started_at = ""
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

        # Bollinger Bands
        bb_period = self.config.range_bb_period
        bb_mult = self.config.range_bb_mult
        if n >= bb_period:
            bb_closes = closes[-bb_period:]
            bb_mean = sum(bb_closes) / len(bb_closes)
            bb_std = (sum((x - bb_mean) ** 2 for x in bb_closes) / len(bb_closes)) ** 0.5
            bb_upper = bb_mean + bb_mult * bb_std
            bb_lower = bb_mean - bb_mult * bb_std
            price = closes[-1]
            bb_width = bb_upper - bb_lower
            bb_pct = (price - bb_lower) / bb_width if bb_width > 0 else 0.5
        else:
            bb_upper = None
            bb_lower = None
            bb_mean = None
            bb_pct = None

        # RSI
        rsi_period = self.config.range_rsi_period
        if n > rsi_period:
            deltas = [closes[i] - closes[i - 1] for i in range(max(1, n - rsi_period * 2), n)]
            gains = [d if d > 0 else 0 for d in deltas]
            losses = [-d if d < 0 else 0 for d in deltas]
            if len(deltas) >= rsi_period:
                avg_gain = sum(gains[:rsi_period]) / rsi_period
                avg_loss = sum(losses[:rsi_period]) / rsi_period
                for i in range(rsi_period, len(deltas)):
                    avg_gain = (avg_gain * (rsi_period - 1) + gains[i]) / rsi_period
                    avg_loss = (avg_loss * (rsi_period - 1) + losses[i]) / rsi_period
                if avg_loss > 0:
                    rs = avg_gain / avg_loss
                    rsi = 100 - (100 / (1 + rs))
                else:
                    rsi = 100.0
            else:
                rsi = None
        else:
            rsi = None

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
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "bb_mean": bb_mean,
            "bb_pct": bb_pct,
            "rsi": rsi,
        }

    def detect_market_mode(self, ind: dict) -> str:
        """Determine market mode: 'trend_long', 'trend_short', or 'range'."""
        adx = ind.get("adx", 0)
        ef = ind.get("ema_fast")
        es = ind.get("ema_slow")
        rf = ind.get("roc_fast")

        if None in (ef, es, rf):
            return "range"

        trend_adx_min = self.config.trend_adx_min
        range_adx_max = self.config.range_adx_max

        # Strong trend
        if adx >= trend_adx_min:
            if ef > es and rf > 0:
                return "trend_long"
            if ef < es and rf < 0:
                return "trend_short"

        # Strong range/choppy
        if adx <= range_adx_max:
            return "range"

        # Grey zone (range_adx_max < ADX < trend_adx_min): EMA/ROC tiebreaker
        if ef > es and rf > 0:
            return "trend_long"
        if ef < es and rf < 0:
            return "trend_short"

        return "range"

    def _check_entry_signal(self, ind: dict, mode: str) -> Optional[str]:
        """Check entry signal based on market mode. Returns None, 'long', or 'short'."""
        if mode == "trend_long":
            rf = ind.get("roc_fast")
            rm = ind.get("roc_slow")
            ef = ind.get("ema_fast")
            es = ind.get("ema_slow")
            if None in (rf, rm, ef, es):
                return None
            mom_score = rf * 0.5 + rm * 0.5
            if (rf > 0 and rm > 0
                    and mom_score > self.config.mom_threshold
                    and ef > es
                    and ind.get("adx", 0) > self.config.adx_threshold
                    and ind.get("pdi", 0) > ind.get("mdi", 0)):
                return "long"
            return None

        elif mode == "trend_short":
            rf = ind.get("roc_fast")
            rm = ind.get("roc_slow")
            ef = ind.get("ema_fast")
            es = ind.get("ema_slow")
            if None in (rf, rm, ef, es):
                return None
            mom_score = rf * 0.5 + rm * 0.5
            if (rf < 0 and rm < 0
                    and mom_score < -self.config.mom_threshold
                    and ef < es
                    and ind.get("adx", 0) > self.config.adx_threshold
                    and ind.get("mdi", 0) > ind.get("pdi", 0)):
                return "short"
            return None

        elif mode == "range":
            bb_pct = ind.get("bb_pct")
            rsi = ind.get("rsi")
            if bb_pct is None or rsi is None:
                return None
            if bb_pct < 0.15 and rsi < self.config.range_rsi_oversold:
                return "long"
            if bb_pct > 0.85 and rsi > self.config.range_rsi_overbought:
                return "short"
            return None

        return None

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

            mode = self.detect_market_mode(ind)
            self._latest_modes[coin] = mode
            side = self._check_entry_signal(ind, mode)

            self._latest_indicators[coin] = {
                "mode": mode,
                "signal": side,
                **{k: v for k, v in ind.items() if isinstance(v, (int, float, bool))},
            }

            if side is not None:
                await self._open_position(coin, ind, side, mode)

    async def _open_position(self, coin: str, ind: dict, side: str = "long", mode: str = "trend"):
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

        # Calculate stop and risk
        if side == "long":
            if mode == "range":
                stop_price = price - self.config.range_sl_mult * atr
            else:
                stop_price = price - self.config.atr_stop_mult * atr
            risk_per_contract = price - stop_price
        else:  # short
            if mode == "range":
                stop_price = price + self.config.range_sl_mult * atr
            else:
                stop_price = price + self.config.atr_stop_mult * atr
            risk_per_contract = stop_price - price

        if risk_per_contract <= 0:
            return

        # Risk sizing — range mode uses reduced risk
        if mode == "range":
            risk_amount = self._equity * self.config.risk_per_trade / self.config.range_risk_divisor
        else:
            risk_amount = self._equity * self.config.risk_per_trade

        ct_val = CT_VAL.get(coin, 0.1)
        raw_sz = risk_amount / risk_per_contract / ct_val

        # V6: Apply leverage cap — max notional = leverage * equity
        max_notional_sz = self.config.leverage * self._equity / (ct_val * price)
        if raw_sz > max_notional_sz:
            raw_sz = max_notional_sz
            print(f"[Momentum] {coin}: leverage cap applied, sz={raw_sz:.2f}", flush=True)

        lot = LOT_SZ.get(coin, 0.01)
        sz = round(raw_sz / lot) * lot

        if sz < lot:
            print(f"[Momentum] {coin}: size too small {sz:.4f}", flush=True)
            return

        # V6: Apply dynamic slippage to entry price
        slip = get_slippage_pct(sz, ct_val, price)
        if side == "long":
            exec_price = price * (1 + slip)
        else:
            exec_price = price * (1 - slip)

        # Recalculate stop from execution price
        if side == "long":
            actual_stop = exec_price - self.config.atr_stop_mult * atr if mode != "range" else exec_price - self.config.range_sl_mult * atr
        else:
            actual_stop = exec_price + self.config.atr_stop_mult * atr if mode != "range" else exec_price + self.config.range_sl_mult * atr

        # Determine order side and pos_side
        if side == "long":
            order_side = "buy"
            pos_side = "long"
        else:
            order_side = "sell"
            pos_side = "short"

        # BB target for range mode
        bb_target = ind.get("bb_mean", 0.0) or 0.0

        try:
            result = await client.place_order(
                inst_id=inst_id, side=order_side, ord_type="market",
                sz=f"{sz:.2f}", td_mode="cross", pos_side=pos_side,
            )
            if result.get("error"):
                print(f"[Momentum] {coin}: order failed: {result.get('message')}", flush=True)
                return

            ord_id = result.get("data", [{}])[0].get("ordId", "")
            now = datetime.now(timezone.utc).isoformat()

            self._positions[coin] = OpenPosition(
                symbol=coin, entry_price=exec_price, stop_price=actual_stop,
                peak_price=exec_price, size=sz, size_remaining=sz,
                stage="initial", atr=atr, opened_at=now, inst_id=inst_id,
                side=side, pos_mode=mode, trough=exec_price, bb_target=bb_target,
            )

            # V6: entry fee
            entry_fee = sz * ct_val * exec_price * 0.001
            self._equity -= entry_fee

            signal_entry = {
                "time": now, "side": order_side, "symbol": inst_id,
                "size": sz, "ord_id": ord_id,
                "entry": exec_price, "stop": actual_stop,
                "adx": round(ind["adx"], 1),
                "roc_f": round(ind["roc_fast"], 2),
                "roc_s": round(ind["roc_slow"], 2),
                "pos_side": side, "mode": mode,
            }
            self._signal_log.append(signal_entry)
            self._trade_log.append(signal_entry)

            print(f"[Momentum] OPEN {side.upper()} {coin} @ {exec_price:.2f} (sig={price:.2f}) sz={sz:.2f} "
                  f"stop={actual_stop:.2f} mode={mode} ADX={ind['adx']:.1f} slip={slip*100:.3f}%",
                  flush=True)

            await self._save_signal_db(order_side, coin, price)
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

                if pos.side == "long":
                    await self._manage_long_position(coin, pos, cur)
                else:
                    await self._manage_short_position(coin, pos, cur)

            except Exception as e:
                print(f"[Momentum] {coin}: manage error: {e}", flush=True)

    async def _manage_long_position(self, coin: str, pos: OpenPosition, cur: float):
        """Manage a LONG position: update peak, apply stage logic, check stops/targets."""
        if cur > pos.peak_price:
            pos.peak_price = cur

        # Range mode: check BB target
        if pos.pos_mode == "range" and pos.bb_target > 0 and cur >= pos.bb_target:
            await self._close_position(coin, pos, pos.bb_target, "range_target")
            return

        stage = pos.stage
        entry = pos.entry_price
        be_pct = self.config.breakeven_pct
        tp1_pct = self.config.tp1_pct
        trail_pct = self.config.trail_pct
        lot = LOT_SZ.get(coin, 0.01)

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
                    close_sz = round(pos.size * sl1_frac / lot) * lot
                    close_sz = min(close_sz, pos.size_remaining)
                    if close_sz >= lot:
                        await self._partial_close_position(coin, pos, cur, close_sz)
                        pos.stage = "sl1_trimmed"
                        print(f"[Momentum] {coin}: SL1 hit @ {cur:.2f}, closed {close_sz}, "
                              f"remaining {pos.size_remaining}", flush=True)

            # Breakeven trigger
            if pos.size_remaining > 0 and cur >= entry * (1 + be_pct):
                pos.stop_price = max(pos.stop_price, entry * 0.999)
                pos.stage = "breakeven"
                print(f"[Momentum] {coin}: breakeven stage @ {cur:.2f}", flush=True)

        elif stage == "sl1_trimmed":
            new_stop = pos.peak_price * (1 - trail_pct)
            if new_stop > pos.stop_price:
                pos.stop_price = new_stop

            if cur >= entry * (1 + be_pct):
                pos.stop_price = max(pos.stop_price, entry * 0.999)
                pos.stage = "breakeven"
                print(f"[Momentum] {coin}: SL1→BE stage @ {cur:.2f}", flush=True)

        elif stage == "breakeven":
            if cur >= entry * (1 + tp1_pct):
                close_sz = round(pos.size_remaining * self.config.tp1_frac / lot) * lot
                close_sz = min(close_sz, pos.size_remaining)
                if close_sz >= lot:
                    await self._partial_close_position(coin, pos, cur, close_sz)
                    pos.stop_price = max(pos.stop_price, entry * 0.999)
                    pos.stage = "trailing"
                    print(f"[Momentum] {coin}: TP1 hit @ {cur:.2f}, closed {close_sz}, "
                          f"remaining {pos.size_remaining}", flush=True)
                else:
                    pos.stage = "trailing"
            else:
                new_stop = max(pos.stop_price, entry * 0.999)
                trail_stop = pos.peak_price * (1 - trail_pct)
                if trail_stop > new_stop:
                    new_stop = trail_stop
                pos.stop_price = new_stop

        elif stage == "trailing":
            new_stop = pos.peak_price * (1 - trail_pct)
            if new_stop > pos.stop_price:
                pos.stop_price = new_stop

        # Stop check (all stages)
        if cur <= pos.stop_price:
            await self._close_position(coin, pos, cur, pos.stage)

    async def _manage_short_position(self, coin: str, pos: OpenPosition, cur: float):
        """Manage a SHORT position: update trough, apply stage logic, check stops/targets."""
        if cur < pos.trough:
            pos.trough = cur

        # Range mode: check BB target
        if pos.pos_mode == "range" and pos.bb_target > 0 and cur <= pos.bb_target:
            await self._close_position(coin, pos, pos.bb_target, "range_target")
            return

        stage = pos.stage
        entry = pos.entry_price
        be_pct = self.config.breakeven_pct
        tp1_pct = self.config.tp1_pct
        trail_pct = self.config.trail_pct
        lot = LOT_SZ.get(coin, 0.01)

        if stage == "initial":
            # Trail stop DOWN from trough
            new_stop = pos.trough * (1 + trail_pct)
            if new_stop < pos.stop_price:
                pos.stop_price = new_stop

            # Breakeven trigger (price drops, so cur <= entry * (1 - be_pct))
            if pos.size_remaining > 0 and cur <= entry * (1 - be_pct):
                pos.stop_price = min(pos.stop_price, entry * 1.001)
                pos.stage = "breakeven"
                print(f"[Momentum] {coin}: breakeven stage (short) @ {cur:.2f}", flush=True)

        elif stage == "breakeven":
            # TP1 for shorts: price drops further, cur <= entry * (1 - tp1_pct)
            if cur <= entry * (1 - tp1_pct):
                close_sz = round(pos.size_remaining * self.config.tp1_frac / lot) * lot
                close_sz = min(close_sz, pos.size_remaining)
                if close_sz >= lot:
                    await self._partial_close_position(coin, pos, cur, close_sz)
                    pos.stop_price = min(pos.stop_price, entry * 1.001)
                    pos.stage = "trailing"
                    print(f"[Momentum] {coin}: TP1 hit (short) @ {cur:.2f}, closed {close_sz}, "
                          f"remaining {pos.size_remaining}", flush=True)
                else:
                    pos.stage = "trailing"
            else:
                new_stop = min(pos.stop_price, entry * 1.001)
                trail_stop = pos.trough * (1 + trail_pct)
                if trail_stop < new_stop:
                    new_stop = trail_stop
                pos.stop_price = new_stop

        elif stage == "trailing":
            new_stop = pos.trough * (1 + trail_pct)
            if new_stop < pos.stop_price:
                pos.stop_price = new_stop

        # Stop check for shorts: cur >= stop means stop hit
        if cur >= pos.stop_price:
            await self._close_position(coin, pos, cur, pos.stage)

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

            # For LONG: sell to close. For SHORT: buy to cover.
            if pos.side == "long":
                order_side = "sell"
                pos_side = "long"
            else:
                order_side = "buy"
                pos_side = "short"

            result = await client.place_order(
                inst_id=pos.inst_id, side=order_side, ord_type="market",
                sz=f"{close_sz:.2f}", td_mode="cross", pos_side=pos_side,
            )
            if result.get("error"):
                print(f"[Momentum] {coin}: close failed: {result.get('message')}", flush=True)
                return

            ct_val = CT_VAL.get(coin, 0.1)
            if pos.side == "long":
                pnl = close_sz * (exit_price - pos.entry_price) * ct_val
            else:
                pnl = close_sz * (pos.entry_price - exit_price) * ct_val
            fee = close_sz * ct_val * (pos.entry_price + exit_price) * 0.001
            net_pnl = pnl - fee
            self._equity += net_pnl

            trade = {
                "time": datetime.now(timezone.utc).isoformat(),
                "side": order_side, "symbol": pos.inst_id,
                "size": close_sz, "exit_price": exit_price,
                "entry_price": pos.entry_price, "pnl": round(net_pnl, 2),
                "reason": reason, "pos_side": pos.side,
            }
            self._trade_log.append(trade)

            print(f"[Momentum] CLOSE {pos.side.upper()} {coin} @ {exit_price:.2f} reason={reason} "
                  f"pnl=${net_pnl:+.2f} equity=${self._equity:.0f}",
                  flush=True)

            del self._positions[coin]
            self._cooldowns[coin] = 3

            await self._save_signal_db(order_side, coin, exit_price)
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
            # For LONG: sell partial. For SHORT: buy to cover partial.
            if pos.side == "long":
                order_side = "sell"
                pos_side = "long"
            else:
                order_side = "buy"
                pos_side = "short"

            result = await client.place_order(
                inst_id=pos.inst_id, side=order_side, ord_type="market",
                sz=f"{close_sz:.2f}", td_mode="cross", pos_side=pos_side,
            )
            if result.get("error"):
                print(f"[Momentum] {coin}: partial close failed: {result.get('message')}", flush=True)
                return

            ct_val = CT_VAL.get(coin, 0.1)
            if pos.side == "long":
                pnl = close_sz * (exit_price - pos.entry_price) * ct_val
            else:
                pnl = close_sz * (pos.entry_price - exit_price) * ct_val
            fee = close_sz * ct_val * (pos.entry_price + exit_price) * 0.001
            net_pnl = pnl - fee
            self._equity += net_pnl
            pos.size_remaining = round(pos.size_remaining - close_sz, 4)

            trade = {
                "time": datetime.now(timezone.utc).isoformat(),
                "side": order_side, "symbol": pos.inst_id,
                "size": close_sz, "exit_price": exit_price,
                "entry_price": pos.entry_price, "pnl": round(net_pnl, 2),
                "reason": "tp1",
            }
            self._trade_log.append(trade)
            await self._save_signal_db(order_side, coin, exit_price)
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
        long_count = 0
        short_count = 0
        for coin, pos in self._positions.items():
            if pos.side == "long":
                long_count += 1
            else:
                short_count += 1
            open_pos.append({
                "symbol": pos.symbol,
                "inst_id": pos.inst_id,
                "entry": pos.entry_price,
                "stop": pos.stop_price,
                "size": pos.size,
                "size_remaining": pos.size_remaining,
                "stage": pos.stage,
                "side": pos.side,
                "pos_mode": pos.pos_mode,
                "peak_ratio": round((pos.peak_price / pos.entry_price - 1) * 100, 2) if pos.entry_price else 0,
                "trough_ratio": round((1 - pos.trough / pos.entry_price) * 100, 2) if pos.entry_price else 0,
            })
        return {
            "running": self._running,
            "started_at": self._started_at,
            "config": asdict(self.config),
            "equity": round(self._equity, 2),
            "open_positions": open_pos,
            "long_positions": long_count,
            "short_positions": short_count,
            "market_mode": self._latest_modes,
            "total_signals": len(self._signal_log),
            "total_trades": len(self._trade_log),
            "recent_signals": self._signal_log[-10:],
            "recent_trades": self._trade_log[-10:],
        }
