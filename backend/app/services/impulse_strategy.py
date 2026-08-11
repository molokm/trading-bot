"""Impulse 1D Strategy — fast momentum entry + pyramiding + cascade exit (daily bars).

Live implementation of the honest daily-bar backtest (full window 3.39y:
+530.6%, CAGR 72.1%, MaxDD 31.0%, Sharpe 1.26, WR 35.6%, PF 2.38).
  - Signal on yesterday's daily close (causal), entry today at open
  - Entry impulse: 1-day |ROC| >= 4% + volume surge + EMA20>50 trend
  - Initial stop = 5 x daily ATR (both sides)
  - Pyramiding: up to 2 adds within a 5-day window when a new peak is made
    with a volume surge (each add 60% of the current position)
  - Cascade take-profit: 30% at +2 ATR, 30% at +6 ATR, rest on stop/time
  - Trailing stop = 8 x entry ATR (rarely fires, keeps winners running)
  - Time exit: 30 days (max_hold_bars)
  - Risk-per-trade sizing 10% of equity, max leverage 3x, margin cap 50%
"""

import asyncio
import math
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional

from .telegram_notifier import TelegramNotifier
from .analysis_logger import get_logger

IMP_BOT_ID = "impulse_strategy"
STRATEGY_VERSION = "v1"
STRATEGY_NAME = f"impulse_1d_{STRATEGY_VERSION}"

CT_VAL = {"BTC": 0.01, "ETH": 0.1, "BNB": 0.1, "SOL": 0.1}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "BNB": 0.01, "SOL": 0.01}
SWAP_MAP = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP",
            "BNB": "BNB-USDT-SWAP", "SOL": "SOL-USDT-SWAP"}
COINS = ["BTC", "ETH", "BNB", "SOL"]

STRATEGY_DESC = (
    "Бот ежедневно сканирует BTC, ETH, BNB, SOL на дневных барах и входит в сильные "
    "импульсные движения. Сигнал входа: цена выросла на ≥4% за 1 день с всплеском объёма "
    "(выше среднего в 1.5 раза) и трендом EMA20>EMA50; шорты — по симметричному импульсу вниз. "
    "До 4 позиций одновременно, ранжирование по силе импульса (|ROC|). "
    "Стоп = 5× дневной ATR (обе стороны), риск на сделку 10% капитала, плечо до 3× "
    "(чем выше волатильность, тем меньше плечо). После входа: пирамидирование — до 2 докупок "
    "в 5-дневном окне на новых максимумах с всплеском объёма. Выход каскадом: 30% позиции на "
    "+2 ATR, ещё 30% на +6 ATR, остаток держим с широким трейлингом (8× ATR) и принудительный "
    "выход через 30 дней. Режим cross margin, демо/реал переключается env."
)


@dataclass
class ImpulseConfig:
    symbols: list = None
    capital: float = 10000.0
    top_k: int = 4                    # max concurrent positions
    # entry / impulse
    impulse_bars: int = 1             # ROC window for the impulse (1 = 1-day ROC)
    entry_roc: float = 4.0            # |ROC| % over window
    rsi_conf_min: float = 0.0         # long confirmation RSI floor
    rsi_conf_max: float = 100.0       # not chasing extreme overbought
    ema_fast: int = 20
    ema_slow: int = 50
    adx_min: float = 0.0
    vol_mult: float = 1.5             # volume > avg_vol * this
    vol_period: int = 24
    # pyramiding (докупка)
    max_adds: int = 2
    add_size_ratio: float = 0.6       # each add = 60% of current position
    add_window_bars: int = 5          # only add within N bars of entry
    add_atr_mult: float = 0.5         # add when new peak >= last_add_peak + ATR*this
    # risk / sizing
    max_leverage: float = 3.0
    risk_per_trade: float = 0.10
    sl_atr_mult: float = 5.0          # initial stop = entry - ATR*this
    sl_atr_mult_short: float = 5.0    # short-specific stop; 0 = use sl_atr_mult
    trail_atr_mult: float = 8.0       # trail = peak - ATR*this
    trail_atr_mult_short: float = 8.0 # short-specific trail; 0 = use trail_atr_mult
    be_pct: float = 0.005             # move stop to breakeven after +0.5%
    cooldown_bars: int = 5            # min bars between entries on the SAME coin
    # cascade exit (выход частями)
    tp1_atr: float = 2.0
    tp1_frac: float = 0.3
    tp2_atr: float = 6.0
    tp2_frac: float = 0.3
    max_hold_bars: int = 30           # time exit (30 days)
    exit_ema_death: bool = False
    allow_short: bool = True
    max_margin_pct: float = 0.5
    limit_offset_pct: float = 0.001
    limit_wait_sec: int = 300
    poll_interval_sec: int = 300
    auto_execute: bool = True

    def __post_init__(self):
        if self.symbols is None:
            self.symbols = ["BTC", "ETH", "BNB", "SOL"]


@dataclass
class ImpPosition:
    """Open position in the impulse strategy."""
    symbol: str
    coin: str
    inst_id: str
    side: str               # "long" or "short"
    size: float
    size_original: float    # original full base size (before adds)
    entry_price: float
    stop_price: float
    peak_price: float
    breakeven: bool = False
    tp1_done: bool = False
    tp2_done: bool = False
    adds: int = 0
    last_add_peak: float = 0.0
    opened_at: str = ""
    atr: float = 0.0
    avg_vol: float = 0.0
    leverage: float = 3.0
    signal_id: int = 0
    raw_entry: float = 0.0
    tp1_atr_pct: float = 0.0
    tp2_atr_pct: float = 0.0
    algo_id: str = ""            # exchange-side conditional SL algo order
    stop_synced: float = 0.0
    size_synced: float = 0.0


class ImpulseStrategy:
    def __init__(self, config: ImpulseConfig, client_manager=None, db=None,
                 notifier: Optional[TelegramNotifier] = None,
                 analysis: Optional["AnalysisLogger"] = None):
        self.config = config
        self.client_manager = client_manager
        self.db = db
        self.notifier = notifier or TelegramNotifier()
        self.analysis = analysis or get_logger()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._positions: dict[str, ImpPosition] = {}
        self._trade_log: list = []
        self._equity = config.capital
        self._capital = config.capital
        self._signal_log: list = []
        self._latest_indicators: dict = {}
        self._started_at: str = ""
        self._last_daily_check: str = ""

    # ─── Indicators (no look-ahead) ───

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
    def roc(closes, period):
        result = [0.0] * len(closes)
        for i in range(period, len(closes)):
            result[i] = (closes[i] / closes[i - period] - 1) * 100
        return result

    @staticmethod
    def rsi(closes, period=14):
        n = len(closes)
        if n < period + 1:
            return [50.0] * n
        gains = [0.0] * n
        losses = [0.0] * n
        for i in range(1, n):
            delta = closes[i] - closes[i - 1]
            if delta > 0:
                gains[i] = delta
            else:
                losses[i] = abs(delta)
        avg_gain = sum(gains[1:period + 1]) / period
        avg_loss = sum(losses[1:period + 1]) / period
        result = [50.0] * n
        if avg_loss == 0:
            result[period] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[period] = 100 - 100 / (1 + rs)
        for i in range(period + 1, n):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            if avg_loss == 0:
                result[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                result[i] = 100 - 100 / (1 + rs)
        return result

    # ─── Data fetching ───

    async def _fetch_candles(self, client, coin: str, bar: str = "1D", limit: int = 250) -> list:
        inst_id = SWAP_MAP.get(coin, f"{coin}-USDT-SWAP")
        resp = await client.get_candles(inst_id, bar=bar, limit=limit)
        if resp.get("error"):
            print(f"[Impulse] {coin} {bar} candles error: {resp.get('message', '')}", flush=True)
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

    def _compute_daily_indicators(self, candles: list) -> dict:
        if len(candles) < 70:
            return None
        closes = [c["C"] for c in candles]
        highs = [c["H"] for c in candles]
        lows = [c["L"] for c in candles]
        cfg = self.config
        roc_arr = self.roc(closes, cfg.impulse_bars)
        ema_f = self.ema(closes, cfg.ema_fast)
        ema_s = self.ema(closes, cfg.ema_slow)
        atr_arr = self.atr(highs, lows, closes, 14)
        rsi_arr = self.rsi(closes, 14)

        i = len(candles) - 2  # signal bar = yesterday
        if i < cfg.ema_slow + 10:
            return None

        v_lo = max(0, i - cfg.vol_period + 1)
        vols = [c["V"] for c in candles[v_lo:i + 1] if c["V"] > 0]
        avg_vol = sum(vols) / len(vols) if vols else 0.0

        return {
            "roc": roc_arr[i],
            "ema_fast": ema_f[i],
            "ema_slow": ema_s[i],
            "ema_trend": ema_f[i] > ema_s[i],
            "atr": atr_arr[i],
            "rsi": rsi_arr[i],
            "price": closes[i],
            "vol": candles[i]["V"],
            "avg_vol": avg_vol,
            "close_today": closes[-1],
            "date": candles[i]["datetime"].strftime("%Y-%m-%d"),
            "date_today": candles[-1]["datetime"].strftime("%Y-%m-%d"),
        }

    # ─── Sizing ───

    def _calc_dynamic_leverage(self, atr: float, price: float) -> float:
        if atr <= 0 or price <= 0:
            return 1.0
        atr_pct = atr / price
        lev = 1.0 / (atr_pct * 2)
        lev = max(1.0, min(lev, self.config.max_leverage))
        return round(lev, 1)

    def _calc_size(self, coin: str, price: float, stop_distance: float, leverage: float) -> float:
        ct_val = CT_VAL.get(coin, 0.01)
        lot = LOT_SZ.get(coin, 0.01)
        cfg = self.config
        if stop_distance <= 0 or price <= 0:
            stop_pct = 0.03
        else:
            stop_pct = stop_distance / price
        risk_usd = self._equity * cfg.risk_per_trade
        notional = risk_usd / stop_pct
        # Margin is the bot's own funds (budget $10k); leverage is applied on top.
        margin = notional / leverage if leverage > 0 else notional
        max_margin = self._equity * cfg.max_margin_pct
        if margin > max_margin:
            margin = max_margin
            notional = margin * leverage
        raw_sz = notional / (ct_val * price)
        sz = math.floor(raw_sz / lot + 1e-12) * lot
        return max(sz, lot)

    def _add_size(self, pos: ImpPosition, price: float, leverage: float) -> float:
        cfg = self.config
        base = pos.size * cfg.add_size_ratio
        max_margin = self._equity * cfg.max_margin_pct
        ct = CT_VAL[pos.coin]
        notional = base * ct * price
        margin = notional / leverage if leverage > 0 else notional
        if margin > max_margin:
            base = max_margin * leverage / (ct * price)
        lot = LOT_SZ[pos.coin]
        base = math.floor(base / lot + 1e-12) * lot
        return max(base, lot)

    # ─── Client helpers ───

    async def _get_client(self):
        if not self.client_manager:
            return None
        return self.client_manager.get_client()

    async def _place_order(self, client, inst_id: str, side: str, sz: float,
                           pos_side: str = None, ord_type: str = "market",
                           px: float = None) -> dict:
        params = {
            "inst_id": inst_id, "side": side, "ord_type": ord_type,
            "sz": str(sz), "td_mode": "cross", "pos_side": pos_side,
        }
        if px and ord_type == "limit":
            params["px"] = str(round(px, 2))
        resp = await client.place_order(**params)
        return resp

    async def _place_exchange_stop(self, client, pos: ImpPosition) -> str:
        if not self.config.auto_execute:
            return ""
        close_side = "sell" if pos.side == "long" else "buy"
        resp = await client.place_algo_order(
            inst_id=pos.inst_id, side=close_side,
            sz=str(pos.size), td_mode="cross", pos_side=pos.side,
            reduce_only=True, sl_trigger_px=str(round(pos.stop_price, 2)),
            cxl_on_close_pos=True,
        )
        if resp.get("error"):
            print(f"[Impulse] Place stop error {pos.coin}: {resp.get('message', '')}", flush=True)
            return ""
        algo_id = ""
        if resp.get("data"):
            algo_id = resp["data"][0].get("algoId", "")
        pos.algo_id = algo_id
        if algo_id:
            pos.stop_synced = pos.stop_price
            pos.size_synced = pos.size
        print(f"[Impulse] Stop placed {pos.coin} {pos.side} @ {pos.stop_price:.2f} "
              f"sz={pos.size} algoId={algo_id}", flush=True)
        self.analysis.log("impulse", "stop_placed",
                          coin=pos.coin, side=pos.side, stop=round(pos.stop_price, 2),
                          size=pos.size, algo_id=algo_id)
        return algo_id

    async def _cancel_exchange_stop(self, client, pos: ImpPosition):
        if not pos.algo_id:
            return
        resp = await client.cancel_algo_order(pos.inst_id, pos.algo_id)
        if resp.get("error"):
            print(f"[Impulse] Cancel stop error {pos.coin}: {resp.get('message', '')}", flush=True)
        else:
            print(f"[Impulse] Stop cancelled {pos.coin} algoId={pos.algo_id}", flush=True)
            self.analysis.log("impulse", "stop_cancelled",
                              coin=pos.coin, side=pos.side, algo_id=pos.algo_id)
        pos.algo_id = ""

    async def _update_exchange_stop(self, client, pos: ImpPosition):
        if not pos.algo_id:
            await self._place_exchange_stop(client, pos)
            return
        new_algo_id = ""
        close_side = "sell" if pos.side == "long" else "buy"
        resp = await client.place_algo_order(
            inst_id=pos.inst_id, side=close_side,
            sz=str(pos.size), td_mode="cross", pos_side=pos.side,
            reduce_only=True, sl_trigger_px=str(round(pos.stop_price, 2)),
            cxl_on_close_pos=True,
        )
        if resp.get("error"):
            print(f"[Impulse] Update stop place error {pos.coin}: {resp.get('message', '')} "
                  f"— keeping old stop", flush=True)
            return
        if resp.get("data"):
            new_algo_id = resp["data"][0].get("algoId", "")
        if new_algo_id:
            await self._cancel_exchange_stop(client, pos)
            pos.algo_id = new_algo_id
            pos.stop_synced = pos.stop_price
            pos.size_synced = pos.size
            print(f"[Impulse] Stop updated {pos.coin} {pos.side} @ {pos.stop_price:.2f} "
                  f"sz={pos.size} algoId={new_algo_id}", flush=True)
            self.analysis.log("impulse", "stop_updated",
                              coin=pos.coin, side=pos.side, stop=round(pos.stop_price, 2),
                              size=pos.size, algo_id=new_algo_id)

    async def _close_partial(self, client, pos: ImpPosition, frac: float, tag: str):
        """Close `frac` of the remaining position (cascade TP1/TP2)."""
        close_sz = round(pos.size * frac / LOT_SZ.get(pos.coin, 0.01)) * LOT_SZ.get(pos.coin, 0.01)
        if close_sz <= 0 or close_sz >= pos.size:
            close_sz = pos.size
        if close_sz <= 0:
            return
        close_side = "sell" if pos.side == "long" else "buy"
        resp = await self._place_order(client, pos.inst_id, close_side, close_sz,
                                       pos_side=pos.side)
        if resp.get("error"):
            print(f"[Impulse] Partial close error {pos.coin}: {resp.get('message', '')}", flush=True)
            return
        fills = resp.get("data", [])
        fill_px = pos.entry_price
        fee = 0.0
        if fills:
            fill_px = float(fills[0].get("fillPx", pos.entry_price))
            fee = float(fills[0].get("fee", 0))
        if pos.side == "long":
            pnl = close_sz * CT_VAL[pos.coin] * (fill_px - pos.entry_price) - fee
        else:
            pnl = close_sz * CT_VAL[pos.coin] * (pos.entry_price - fill_px) - fee
        self._equity += pnl
        now = datetime.now(timezone.utc).isoformat()
        self._trade_log.append({
            "time": now, "side": close_side, "symbol": pos.inst_id,
            "size": close_sz, "pnl": round(pnl, 2),
            "entry_price": pos.entry_price, "exit_price": round(fill_px, 2),
            "reason": tag, "pos_side": pos.side, "coin": pos.coin,
            "signal_id": pos.signal_id,
        })
        pos.size -= close_sz
        if tag == "tp1":
            pos.tp1_done = True
        if tag == "tp2":
            pos.tp2_done = True
        if pos.algo_id:
            await self._update_exchange_stop(client, pos)
        if self.db:
            await self._sync_positions_db()
        print(f"[Impulse] PARTIAL {now[:19]} {pos.coin:4} {pos.side:5} "
              f"closed {close_sz} of {pos.size + close_sz} @ {fill_px:.1f} "
              f"pnl={pnl:+.2f} ({tag})", flush=True)
        self.analysis.log("impulse", "partial", tag=tag,
                          coin=pos.coin, side=pos.side,
                          closed_sz=close_sz, remaining_sz=pos.size,
                          exit_px=round(fill_px, 2), entry_px=round(pos.entry_price, 2),
                          pnl=round(pnl, 2), fee=round(fee, 4),
                          signal_id=pos.signal_id)
        if self.notifier:
            try:
                self.notifier.fire(self.notifier.partial_msg(
                    coin=pos.coin, side=pos.side, entry=round(pos.entry_price, 2),
                    exit_px=round(fill_px, 2), pnl=round(pnl, 2),
                    closed_sz=round(close_sz, 4), remaining_sz=round(pos.size, 4),
                ))
            except Exception as e:
                print(f"[Impulse] TG partial notify error: {e}", flush=True)

    async def _close_position(self, client, pos: ImpPosition, reason: str):
        """Close full remaining position at market."""
        await self._cancel_exchange_stop(client, pos)
        close_side = "sell" if pos.side == "long" else "buy"
        resp = await self._place_order(client, pos.inst_id, close_side, pos.size,
                                       pos_side=pos.side)
        if resp.get("error"):
            print(f"[Impulse] Close error {pos.coin}: {resp.get('message', '')}", flush=True)
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
        self._trade_log.append({
            "time": now, "side": close_side, "symbol": pos.inst_id,
            "size": pos.size, "pnl": round(pnl, 2),
            "entry_price": pos.entry_price, "exit_price": round(fill_px, 2),
            "reason": reason, "pos_side": pos.side, "coin": pos.coin,
            "signal_id": pos.signal_id, "adds": pos.adds,
        })
        if self.db:
            try:
                await self.db.save_trade(
                    bot_id=IMP_BOT_ID, side=close_side, sz=pos.size,
                    px=round(fill_px, 2),
                    ord_id=fills[0].get("ordId", "") if fills else "",
                    inst_id=pos.inst_id, ord_type="market",
                    fee=round(fee, 4), fee_ccy="USDT",
                    pnl=round(pnl, 2), state="filled",
                    signal_id=pos.signal_id,
                )
                await self._sync_positions_db()
            except Exception as e:
                print(f"[Impulse] DB save trade error: {e}", flush=True)
        print(f"[Impulse] CLOSE  {now[:19]} {pos.coin:4} {pos.side:5} "
              f"entry={pos.entry_price:.1f} exit={fill_px:.1f} "
              f"pnl={pnl:+.2f} ({reason})", flush=True)
        self.analysis.log("impulse", "close",
                          coin=pos.coin, side=pos.side, reason=reason,
                          entry_px=round(pos.entry_price, 2), exit_px=round(fill_px, 2),
                          size=pos.size, pnl=round(pnl, 2), fee=round(fee, 4),
                          leverage=pos.leverage, adds=pos.adds,
                          signal_id=pos.signal_id)
        if self.notifier:
            try:
                self.notifier.fire(self.notifier.close_msg(
                    coin=pos.coin, side=pos.side, entry=round(pos.entry_price, 2),
                    exit_px=round(fill_px, 2), pnl=round(pnl, 2), reason=reason,
                ))
            except Exception as e:
                print(f"[Impulse] TG close notify error: {e}", flush=True)

    async def _open_position(self, client, coin: str, side: str, ind: dict, lev: float):
        """Open a new position with limit order + market fallback."""
        inst_id = SWAP_MAP.get(coin, f"{coin}-USDT-SWAP")
        price = ind["close_today"]
        atr_val = ind["atr"]
        if atr_val <= 0 or price <= 0:
            return

        if lev != 1.0:
            lev_resp = await client.set_leverage(
                inst_id=inst_id, leverage=lev, mgn_mode="cross", pos_side=side,
            )
            if lev_resp.get("error"):
                print(f"[Impulse] Set leverage error {coin}: {lev_resp.get('message', '')}", flush=True)

        sl_m = self.config.sl_atr_mult_short if side == "short" and self.config.sl_atr_mult_short \
            else self.config.sl_atr_mult
        stop_dist = atr_val * sl_m
        if side == "long":
            stop = price - stop_dist
        else:
            stop = price + stop_dist
        sz = self._calc_size(coin, price, stop_dist, lev)
        order_side = "buy" if side == "long" else "sell"

        signal_id = 0
        if self.db:
            try:
                signal_id = await self.db.save_signal(
                    bot_id=IMP_BOT_ID,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    side=order_side, price=price, size=sz,
                    ord_type="limit", status="pending",
                )
            except Exception as e:
                print(f"[Impulse] DB save signal error: {e}", flush=True)

        if not self.config.auto_execute:
            print(f"[Impulse] SIGNAL (no execute) {coin} {side} @ {price:.1f} lev={lev}", flush=True)
            self.analysis.log("impulse", "signal",
                              coin=coin, side=side, price=round(price, 2),
                              leverage=lev, atr=round(atr_val, 2), size=sz,
                              stop=round(stop, 2))
            return

        limit_px = price * (1 - self.config.limit_offset_pct) if side == "long" \
            else price * (1 + self.config.limit_offset_pct)
        resp = await self._place_order(client, inst_id, order_side, sz,
                                       pos_side=side, ord_type="limit", px=limit_px)
        fills = resp.get("data", [])
        if not resp.get("error") and fills:
            fill_state = fills[0].get("state", "")
            if fill_state == "fill":
                pass
            else:
                await asyncio.sleep(self.config.limit_wait_sec)
                if fills[0].get("ordId"):
                    await client.cancel_order(inst_id, fills[0]["ordId"])
                resp = await self._place_order(client, inst_id, order_side, sz,
                                               pos_side=side, ord_type="market")
                fills = resp.get("data", [])
        elif resp.get("error") or not fills:
            resp = await self._place_order(client, inst_id, order_side, sz,
                                           pos_side=side, ord_type="market")
            fills = resp.get("data", [])

        if resp.get("error"):
            print(f"[Impulse] Open error {coin}: {resp.get('message', '')}", flush=True)
            if self.db and signal_id:
                await self.db.update_signal_status(signal_id, "rejected",
                                                   resp.get("message", ""))
            return

        fill_px = price
        fee = 0.0
        ord_id = ""
        if fills:
            fill_px = float(fills[0].get("fillPx", price))
            fee = float(fills[0].get("fee", 0))
            ord_id = fills[0].get("ordId", "")

        now = datetime.now(timezone.utc).isoformat()
        tp1_pct = self.config.tp1_atr * atr_val / fill_px
        tp2_pct = self.config.tp2_atr * atr_val / fill_px
        pos = ImpPosition(
            symbol=inst_id, coin=coin, inst_id=inst_id,
            side=side, size=sz, size_original=sz,
            entry_price=fill_px, stop_price=stop, peak_price=fill_px,
            last_add_peak=fill_px, opened_at=now,
            atr=atr_val, avg_vol=ind["avg_vol"],
            leverage=lev, signal_id=signal_id, raw_entry=price,
            tp1_atr_pct=tp1_pct, tp2_atr_pct=tp2_pct,
        )
        self._positions[coin] = pos
        await self._place_exchange_stop(client, pos)

        self._trade_log.append({
            "time": now, "side": order_side, "symbol": inst_id,
            "size": sz, "pnl": -round(fee, 2), "entry": fill_px, "entry_price": fill_px,
            "stop": round(stop, 2), "reason": "open", "pos_side": side,
            "coin": coin, "signal_id": signal_id, "leverage": lev,
        })
        if self.db:
            try:
                if signal_id:
                    await self.db.update_signal_status(signal_id, "filled", ord_id)
                await self.db.save_trade(
                    bot_id=IMP_BOT_ID, side=order_side, sz=sz,
                    px=round(fill_px, 2), ord_id=ord_id,
                    inst_id=inst_id, ord_type="market",
                    fee=round(fee, 4), fee_ccy="USDT",
                    pnl=0, state="filled", signal_id=signal_id,
                )
                await self._sync_positions_db()
            except Exception as e:
                print(f"[Impulse] DB save error: {e}", flush=True)
        self._equity -= fee
        print(f"[Impulse] OPEN  {now[:19]} {coin:4} {side:5} "
              f"price={fill_px:.1f} stop={stop:.1f} sz={sz} "
              f"lev={lev} atr={atr_val:.1f} fee={fee:.2f}", flush=True)
        self.analysis.log("impulse", "open",
                          coin=coin, side=side, price=round(fill_px, 2),
                          stop=round(stop, 2), size=sz, leverage=lev,
                          atr=round(atr_val, 2), fee=round(fee, 4),
                          inst_id=inst_id, signal_id=signal_id)
        if self.notifier:
            try:
                self.notifier.fire(self.notifier.open_msg(
                    coin=coin, side=side, price=round(fill_px, 2),
                    stop=round(stop, 2), size=round(sz, 4), leverage=lev,
                ))
            except Exception as e:
                print(f"[Impulse] TG open notify error: {e}", flush=True)

    async def _add_position(self, client, coin: str, ind: dict, lev: float):
        """Pyramid: add a sized slice on a new peak (volume confirmed)."""
        pos = self._positions[coin]
        cfg = self.config
        inst_id = pos.inst_id
        if lev != pos.leverage and lev != 1.0:
            lev_resp = await client.set_leverage(
                inst_id=inst_id, leverage=lev, mgn_mode="cross", pos_side=pos.side,
            )
            if lev_resp.get("error"):
                print(f"[Impulse] Set leverage (add) error {coin}: {lev_resp.get('message', '')}",
                      flush=True)
        add_sz = self._add_size(pos, ind["close_today"], lev)
        order_side = "buy" if pos.side == "long" else "sell"
        limit_px = ind["close_today"] * (1 - cfg.limit_offset_pct) if pos.side == "long" \
            else ind["close_today"] * (1 + cfg.limit_offset_pct)
        resp = await self._place_order(client, inst_id, order_side, add_sz,
                                       pos_side=pos.side, ord_type="limit", px=limit_px)
        fills = resp.get("data", [])
        if not resp.get("error") and fills and fills[0].get("state", "") != "fill":
            if fills[0].get("ordId"):
                await client.cancel_order(inst_id, fills[0]["ordId"])
            resp = await self._place_order(client, inst_id, order_side, add_sz,
                                           pos_side=pos.side, ord_type="market")
            fills = resp.get("data", [])
        elif resp.get("error") or not fills:
            resp = await self._place_order(client, inst_id, order_side, add_sz,
                                           pos_side=pos.side, ord_type="market")
            fills = resp.get("data", [])

        if resp.get("error"):
            print(f"[Impulse] Add error {coin}: {resp.get('message', '')}", flush=True)
            return
        fill_px = float(fills[0].get("fillPx", ind["close_today"])) if fills else ind["close_today"]
        fee = float(fills[0].get("fee", 0)) if fills else 0.0

        prev_size = pos.size
        pos.size += add_sz
        pos.adds += 1
        if pos.side == "long":
            pos.entry_price = (pos.entry_price * prev_size + fill_px * add_sz) / pos.size
            pos.last_add_peak = fill_px
        else:
            pos.entry_price = (pos.entry_price * prev_size + fill_px * add_sz) / pos.size
            pos.last_add_peak = fill_px
        pos.stop_price = pos.entry_price - cfg.sl_atr_mult * pos.atr \
            if pos.side == "long" else pos.entry_price + cfg.sl_atr_mult_short * pos.atr
        self._equity -= fee
        now = datetime.now(timezone.utc).isoformat()
        self._trade_log.append({
            "time": now, "side": order_side, "symbol": inst_id,
            "size": add_sz, "pnl": -round(fee, 2), "entry": fill_px,
            "entry_price": fill_px, "reason": "add", "pos_side": pos.side,
            "coin": coin, "signal_id": pos.signal_id, "leverage": lev,
        })
        await self._place_exchange_stop(client, pos)
        if self.db:
            await self._sync_positions_db()
        print(f"[Impulse] ADD   {now[:19]} {coin:4} {pos.side:5} "
              f"price={fill_px:.1f} add={add_sz} total={pos.size} "
              f"new_stop={pos.stop_price:.1f} fee={fee:.2f}", flush=True)
        self.analysis.log("impulse", "add",
                          coin=coin, side=pos.side, price=round(fill_px, 2),
                          add_sz=add_sz, total_sz=pos.size,
                          entry_px=round(pos.entry_price, 2),
                          new_stop=round(pos.stop_price, 2),
                          leverage=lev, adds=pos.adds, fee=round(fee, 4),
                          signal_id=pos.signal_id)
        if self.notifier:
            try:
                self.notifier.fire(self.notifier.add_msg(
                    coin=coin, side=pos.side, price=round(fill_px, 2),
                    size=round(add_sz, 4), total=round(pos.size, 4),
                ))
            except Exception as e:
                print(f"[Impulse] TG add notify error: {e}", flush=True)

    async def _sync_exchange_position(self, client, coin: str):
        """Sync realized exchange stop/size with strategy book if they drift."""
        pos = self._positions.get(coin)
        if not pos:
            return
        try:
            pos_resp = await client.get_positions(inst_type="SWAP")
            if pos_resp.get("error"):
                return
            data = pos_resp.get("data", [])
            if not data:
                return
            for p in data:
                if p.get("posSide") != pos.side:
                    continue
                realized_sz = float(p.get("pos", 0))
                if pos.size != realized_sz:
                    print(f"[Impulse] SYNC {coin} pos size {pos.size} -> {realized_sz}", flush=True)
                    self.analysis.log("impulse", "sync",
                                      coin=coin, side=pos.side,
                                      event="size_drift", old_size=pos.size, new_size=realized_sz)
                    pos.size = realized_sz
                break
        except Exception as e:
            print(f"[Impulse] Sync pos error {coin}: {e}", flush=True)

    # ─── Core trading logic (runs once per poll cycle) ───

    async def _check_and_trade(self):
        client = await self._get_client()
        if not client:
            print("[Impulse] No client — skip cycle", flush=True)
            return

        # 1) Refresh candles & indicators for every coin
        indicators = {}
        for coin in self.config.symbols:
            candles = await self._fetch_candles(client, coin, bar="1D", limit=250)
            ind = self._compute_daily_indicators(candles)
            indicators[coin] = ind
            if ind:
                self._latest_indicators[coin] = ind

        # 2) Manage open positions: exits first (stop / cascade TP / time)
        for coin in list(self._positions.keys()):
            pos = self._positions[coin]
            ind = indicators.get(coin)
            if not ind:
                continue
            await self._sync_exchange_position(client, coin)
            price = ind["close_today"]
            closed = await self._check_exit(client, pos, ind, price)
            if closed:
                continue
            # cascade TP1/TP2 partial closes (only when not already done)
            if not pos.tp1_done and pos.side == "long" and price >= pos.entry_price * (1 + pos.tp1_atr_pct):
                await self._close_partial(client, pos, self.config.tp1_frac, "tp1")
            if not pos.tp1_done and pos.side == "short" and price <= pos.entry_price * (1 - pos.tp1_atr_pct):
                await self._close_partial(client, pos, self.config.tp1_frac, "tp1")
            if not pos.tp2_done and pos.side == "long" and price >= pos.entry_price * (1 + pos.tp2_atr_pct):
                await self._close_partial(client, pos, self.config.tp2_frac, "tp2")
            if not pos.tp2_done and pos.side == "short" and price <= pos.entry_price * (1 - pos.tp2_atr_pct):
                await self._close_partial(client, pos, self.config.tp2_frac, "tp2")
            await self._manage_position(client, pos, ind)

        # 3) Pyramiding for open positions (adds)
        for coin in list(self._positions.keys()):
            pos = self._positions[coin]
            ind = indicators.get(coin)
            if not ind:
                continue
            if pos.adds >= self.config.max_adds:
                continue
            if self._days_held(pos) > self.config.add_window_bars:
                continue
            price = ind["close_today"]
            cfg = self.config
            atr = pos.atr
            if pos.side == "long":
                new_peak = price >= pos.last_add_peak + cfg.add_atr_mult * atr
            else:
                new_peak = price <= pos.last_add_peak - cfg.add_atr_mult * atr
            vol_surge = ind["vol"] >= ind["avg_vol"] * cfg.vol_mult if ind["avg_vol"] > 0 else False
            if new_peak and vol_surge:
                lev = self._calc_dynamic_leverage(atr, price)
                await self._add_position(client, coin, ind, lev)
                pos.last_add_peak = price

        # 4) New entries
        await self._new_entries(client, indicators)

    async def _check_exit(self, client, pos: ImpPosition, ind: dict, price: float) -> bool:
        """Check stop / time-exit / breakeven / trailing. Returns True if fully closed."""
        cfg = self.config
        trail_m = cfg.trail_atr_mult_short if pos.side == "short" and cfg.trail_atr_mult_short \
            else cfg.trail_atr_mult

        # stop-loss
        hit = (pos.side == "long" and price <= pos.stop_price) or \
              (pos.side == "short" and price >= pos.stop_price)
        if hit:
            await self._close_position(client, pos, "stop")
            return True

        # time exit
        if self._days_held(pos) >= cfg.max_hold_bars:
            await self._close_position(client, pos, "time_exit")
            return True

        # trailing stop
        if pos.side == "long":
            pos.peak_price = max(pos.peak_price, price)
            trail = pos.peak_price - trail_m * pos.atr
            if trail > pos.stop_price:
                pos.stop_price = trail
                self.analysis.log("impulse", "trail",
                                  coin=pos.coin, side=pos.side,
                                  price=round(price, 2), peak=round(pos.peak_price, 2),
                                  new_stop=round(pos.stop_price, 2))
        else:
            pos.peak_price = min(pos.peak_price, price)
            trail = pos.peak_price + trail_m * pos.atr
            if trail < pos.stop_price:
                pos.stop_price = trail
                self.analysis.log("impulse", "trail",
                                  coin=pos.coin, side=pos.side,
                                  price=round(price, 2), peak=round(pos.peak_price, 2),
                                  new_stop=round(pos.stop_price, 2))

        # breakeven after min profit
        if not pos.breakeven:
            if pos.side == "long" and price >= pos.entry_price * (1 + cfg.be_pct):
                pos.stop_price = max(pos.stop_price, pos.entry_price)
                pos.breakeven = True
                self.analysis.log("impulse", "breakeven",
                                  coin=pos.coin, side=pos.side,
                                  price=round(price, 2), entry=round(pos.entry_price, 2),
                                  stop=round(pos.stop_price, 2))
            if pos.side == "short" and price <= pos.entry_price * (1 - cfg.be_pct):
                pos.stop_price = min(pos.stop_price, pos.entry_price)
                pos.breakeven = True
                self.analysis.log("impulse", "breakeven",
                                  coin=pos.coin, side=pos.side,
                                  price=round(price, 2), entry=round(pos.entry_price, 2),
                                  stop=round(pos.stop_price, 2))

        if pos.stop_price != pos.stop_synced or pos.size != pos.size_synced:
            await self._update_exchange_stop(client, pos)
        return False

    async def _manage_position(self, client, pos: ImpPosition, ind: dict):
        """Optional per-bar management hook (kept for symmetry with rotation bot)."""
        pass

    async def _new_entries(self, client, indicators: dict):
        """Rank candidate coins by impulse strength, open up to free slots."""
        cfg = self.config
        free_slots = cfg.top_k - len(self._positions)
        if free_slots <= 0:
            return

        candidates = []
        for coin, ind in indicators.items():
            if not ind:
                continue
            if coin in self._positions:
                continue
            if self._in_cooldown(coin, ind["date"]):
                continue
            side, strength = self._entry_signal(coin, ind)
            if not side:
                continue
            candidates.append((strength, coin, side, ind))
        if not candidates:
            return

        candidates.sort(key=lambda x: -x[0])
        for strength, coin, side, ind in candidates[:free_slots]:
            if coin in self._positions:
                continue
            lev = self._calc_dynamic_leverage(ind["atr"], ind["price"])
            if not self.config.auto_execute:
                print(f"[Impulse] SIGNAL {coin} {side} strength={strength:.1f} "
                      f"roc={ind['roc']:.1f}%", flush=True)
                self.analysis.log("impulse", "signal",
                                  coin=coin, side=side, strength=round(strength, 2),
                                  roc=round(ind["roc"], 2), rsi=round(ind["rsi"], 2),
                                  price=round(ind["close_today"], 2),
                                  vol=round(ind["vol"], 2), avg_vol=round(ind["avg_vol"], 2))
                continue
            await self._open_position(client, coin, side, ind, lev)

    def _in_cooldown(self, coin: str, bar_date: str) -> bool:
        """Cooldown per coin (cooldown_bars days)."""
        for pos in self._positions.values():
            if pos.coin != coin:
                continue
            if self._days_held(pos) < self.config.cooldown_bars:
                return True
        return False

    @staticmethod
    def _days_held(pos: "ImpPosition") -> int:
        """Calendar days since position opened (matches rotation bot semantics)."""
        try:
            return (datetime.now(timezone.utc) - datetime.fromisoformat(pos.opened_at)).days
        except Exception:
            return 0

    def _entry_signal(self, coin: str, ind: dict):
        """Return (side, strength) or (None, 0) if no entry signal."""
        cfg = self.config
        price = ind["price"]
        if price <= 0 or ind["atr"] <= 0:
            return None, 0
        vol_surge = ind["avg_vol"] > 0 and ind["vol"] >= ind["avg_vol"] * cfg.vol_mult
        if not vol_surge:
            return None, 0
        rsi = ind["rsi"]
        roc = ind["roc"]
        strength = abs(roc)
        if strength < cfg.entry_roc:
            return None, 0

        if cfg.allow_short and roc <= -cfg.entry_roc:
            if not ind["ema_trend"] and rsi <= cfg.rsi_conf_max:
                return "short", strength
        if roc >= cfg.entry_roc:
            if ind["ema_trend"] and rsi >= cfg.rsi_conf_min:
                return "long", strength
        return None, 0

    # ─── DB helpers ───

    async def _sync_positions_db(self):
        if not self.db:
            return
        try:
            if self.db._pg_mode:
                await self.db._execute("DELETE FROM positions WHERE bot_id = $1", (IMP_BOT_ID,))
            else:
                await self.db._execute("DELETE FROM positions WHERE bot_id = ?", (IMP_BOT_ID,))
            for coin, pos in self._positions.items():
                await self.db.save_position(
                    bot_id=IMP_BOT_ID, inst_id=pos.inst_id,
                    side=pos.side, size=pos.size,
                    entry_price=round(pos.entry_price, 2),
                    current_price=pos.peak_price,
                )
        except Exception as e:
            print(f"[Impulse] DB sync positions error: {e}", flush=True)

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
                    "VALUES ($1, 'impulse', $2, 'MULTI', '1D', "
                    "$3, $4, 'running', 'demo', 'momentum', $5, 'Impulse 1D v1') "
                    "ON CONFLICT (id) DO NOTHING",
                    (IMP_BOT_ID, STRATEGY_NAME, self._equity, str(params), now),
                )
            else:
                await self.db._execute(
                    "INSERT OR IGNORE INTO bots (id, strategy_id, strategy_code, symbol, timeframe, "
                    "capital, params, status, mode, signal_type, created_at, name) "
                    "VALUES (?, 'impulse', ?, 'MULTI', '1D', "
                    "?, ?, 'running', 'demo', 'momentum', ?, 'Impulse 1D v1')",
                    (IMP_BOT_ID, STRATEGY_NAME, self._equity, str(params), now),
                )
        except Exception as e:
            print(f"[Impulse] DB ensure bot error: {e}", flush=True)

    # ─── Lifecycle ───

    async def _poll_loop(self):
        while self._running:
            try:
                await self._check_and_trade()
            except Exception as e:
                print(f"[Impulse] Poll error: {e}", flush=True)
            await asyncio.sleep(self.config.poll_interval_sec)

    def _thread_runner(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._start_async())
        except RuntimeError:
            if self._running:
                print("[Impulse] Thread error: event loop stopped unexpectedly", flush=True)
        except Exception as e:
            print(f"[Impulse] Thread error: {e}", flush=True)

    async def _start_async(self):
        self._started_at = datetime.now(timezone.utc).isoformat()
        await self._ensure_bot()
        await self._check_and_trade()
        await self._poll_loop()

    async def start(self):
        if self._running:
            return
        self._running = True
        self._started_at = datetime.now(timezone.utc).isoformat()
        if self.db:
            await self._ensure_bot()
        self._thread = threading.Thread(target=self._thread_runner, daemon=True)
        self._thread.start()
        print(f"[Impulse] Started (capital=${self._equity:,.0f}, poll={self.config.poll_interval_sec}s)",
              flush=True)

    async def stop(self):
        if not self._running:
            return
        self._running = False
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self.db:
            try:
                await self.db.update_bot_stopped(IMP_BOT_ID)
            except Exception:
                pass
        print("[Impulse] Stopped", flush=True)

    # ─── Status / report ───

    def get_status(self) -> dict:
        equity = self._equity
        total_pnl = equity - self._capital
        total_pnl_pct = (total_pnl / self._capital * 100) if self._capital else 0.0
        closed = [t for t in self._trade_log if t.get("reason") not in ("open", "add")]
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) <= 0]
        win_rate = (len(wins) / len(closed) * 100) if closed else 0.0
        gross_profit = sum(t.get("pnl", 0) for t in wins)
        gross_loss = abs(sum(t.get("pnl", 0) for t in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss else 0.0
        sum_sq = sum(t.get("pnl", 0) ** 2 for t in closed)
        count = len(closed)
        avg = sum(t.get("pnl", 0) for t in closed) / count if count else 0.0
        var = (sum_sq / count - avg ** 2) if count else 0.0
        std = math.sqrt(max(var, 0.0))

        positions = []
        for coin, pos in self._positions.items():
            ind = self._latest_indicators.get(coin) or {}
            cur = ind.get("close_today", pos.peak_price)
            ct = CT_VAL.get(coin, 0.01)
            if pos.side == "long":
                upnl = pos.size * ct * (cur - pos.entry_price)
            else:
                upnl = pos.size * ct * (pos.entry_price - cur)
            positions.append({
                "coin": coin, "side": pos.side, "size": round(pos.size, 4),
                "entry_price": round(pos.entry_price, 2),
                "stop_price": round(pos.stop_price, 2),
                "leverage": pos.leverage, "adds": pos.adds,
                "opened_at": pos.opened_at,
                "unrealized_pnl": round(upnl, 2),
                "stage": "tp1_done" if pos.tp1_done else "tp2_done" if pos.tp2_done else "running",
            })

        return {
            "running": self._running,
            "strategy": STRATEGY_NAME,
            "version": STRATEGY_VERSION,
            "started_at": self._started_at,
            "equity": round(equity, 2),
            "capital": round(self._capital, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "open_positions": positions,
            "positions": positions,
            "total_trades": count,
            "closed_trades": count,
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_trade_pnl": round(avg, 2),
            "std_dev": round(std, 2),
            "last_daily_check": self._last_daily_check,
            "config": asdict(self.config),
            "description": STRATEGY_DESC,
            "signals": self._signal_log[-20:],
        }

    def get_trade_log(self, limit: int = 100) -> list:
        return self._trade_log[-limit:]



