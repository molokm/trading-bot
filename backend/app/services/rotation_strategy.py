"""Momentum Rotation Strategy v3 — daily-bar model (validated +76% CAGR backtest).

Rewritten to exactly match the winning honest-backtest config:
  - Signal computed on yesterday's daily close (causal), entry today
  - Initial stop = daily ATR x atr_stop_mult (was hourly ATR)
  - Risk-per-trade sizing 10% of equity, capped by allocation_pct of equity
  - Dynamic leverage: 1 / (2 * ATR%) capped by max_leverage
  - Long cooldown: min_hold_days before rotating again
  - Volatility / RSI / correlation filters on the daily bar
  - Daily ATR trailing x trail_atr_mult, breakeven, partial TP
  - BTC 200d MA regime: block longs below it
  - Shorts allowed (allow_short)
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
    "Rotation v3 (daily-bar): ROC/EMA/ADX ranking, risk 10%/trade, daily-ATR stops, "
    "long cooldown, RSI+volatility+correlation filters, USD trailing, partial TP. "
    "Backtest +76% CAGR / 20% DD."
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
    adx_min: float = 25.0
    min_roc: float = 3.0            # min |roc| to even rank a coin
    sma_long: int = 200            # BTC regime MA
    min_hold_days: int = 20        # cooldown before rotating again
    max_leverage: float = 2.0
    risk_per_trade: float = 0.10   # 10% risk of equity per trade
    allocation_pct: float = 1.0    # max total margin = eq * this
    atr_stop_mult: float = 3.0     # initial stop = daily ATR * 3.0
    trail_atr_mult: float = 0.2    # trailing = daily ATR * 0.2
    breakeven_pct: float = 0.02    # move to BE after 2%
    partial_tp_pct: float = 0.05   # close 50% at +5%
    partial_tp_ratio: float = 0.5  # fraction to close
    rsi_period: int = 14
    rsi_long_max: float = 75.0     # no long if RSI > 75
    rsi_short_min: float = 25.0    # no short if RSI < 25
    vol_mult: float = 1.5          # skip if ATR > avg * 1.5
    corr_threshold: float = 0.7    # max correlation between held pairs
    allow_short: bool = True       # allow shorting bearish coins
    limit_offset_pct: float = 0.001   # 0.1% below price for limit orders
    limit_wait_sec: int = 300      # 5 min fallback to market
    poll_interval_sec: int = 300
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
    size_original: float    # original full size (before partial TP)
    entry_price: float
    stop_price: float
    peak_price: float
    breakeven: bool = False
    partial_done: bool = False   # 50% already closed at TP1
    opened_at: str = ""
    entry_bar_ts: int = 0
    atr: float = 0.0             # ATR at entry (for dynamic trailing)
    atr_hourly: float = 0.0      # hourly ATR at entry
    leverage: float = 3.0
    signal_id: int = 0
    raw_entry: float = 0.0
    algo_id: str = ""            # exchange-side conditional SL algo order


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
        self._last_rotate_ts: int = 0
        self._last_daily_check: str = ""
        self._btc_200ma: float = 0.0        # BTC long-MA (for long-only filter)

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
    def sma(data, period):
        """Simple moving average."""
        if len(data) < period:
            return [0.0] * len(data)
        result = [0.0] * len(data)
        s = sum(data[:period])
        result[period - 1] = s / period
        for i in range(period, len(data)):
            s += data[i] - data[i - period]
            result[i] = s / period
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

    @staticmethod
    def rsi(closes, period=14):
        """RSI indicator. Returns array same length as closes."""
        n = len(closes)
        if n < period + 1:
            return [50.0] * n
        gains = [0.0] * n
        losses = [0.0] * n
        for i in range(1, n):
            delta = closes[i] - closes[i - 1]
            if delta > 0:
                gains[i] = delta
                losses[i] = 0.0
            else:
                gains[i] = 0.0
                losses[i] = abs(delta)
        # Initial average
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

    @staticmethod
    def correlation(x, y, period=30):
        """Rolling Pearson correlation of two arrays over last `period` values."""
        if len(x) < period or len(y) < period:
            return 0.0
        x = x[-period:]
        y = y[-period:]
        n = len(x)
        mx = sum(x) / n
        my = sum(y) / n
        cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
        sx = math.sqrt(sum((x[i] - mx) ** 2 for i in range(n)))
        sy = math.sqrt(sum((y[i] - my) ** 2 for i in range(n)))
        if sx == 0 or sy == 0:
            return 0.0
        return cov / (sx * sy)

    # ─── Data fetching ───

    async def _fetch_candles(self, client, coin: str, bar: str = "1D", limit: int = 100) -> list:
        """Fetch candles from OKX."""
        inst_id = SWAP_MAP.get(coin, f"{coin}-USDT-SWAP")
        resp = await client.get_candles(inst_id, bar=bar, limit=limit)
        if resp.get("error"):
            print(f"[Rotation] {coin} {bar} candles error: {resp.get('message', '')}", flush=True)
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

    async def _fetch_daily(self, client, coin: str, limit: int = 250) -> list:
        """Fetch daily candles (250 bars for indicators)."""
        return await self._fetch_candles(client, coin, bar="1D", limit=limit)

    def _compute_daily_indicators(self, candles: list) -> dict:
        """Compute all daily indicators. Signal bar = second to last (yesterday)."""
        if len(candles) < 70:
            return None
        closes = [c["C"] for c in candles]
        highs = [c["H"] for c in candles]
        lows = [c["L"] for c in candles]
        cfg = self.config
        roc_arr = self.roc(closes, cfg.roc_period)
        ema_f = self.ema(closes, cfg.ema_fast)
        ema_s = self.ema(closes, cfg.ema_slow)
        atr_arr = self.atr(highs, lows, closes, cfg.atr_period)
        adx_arr = self.adx(highs, lows, closes, 14)
        rsi_arr = self.rsi(closes, cfg.rsi_period)

        i = len(candles) - 2  # signal bar = yesterday
        if i < cfg.ema_slow + 10:
            return None

        # Average ATR over last 30 days (for volatility filter)
        atr_30_start = max(0, i - 30)
        atr_values = [atr_arr[j] for j in range(atr_30_start, i + 1) if atr_arr[j] > 0]
        avg_atr_30 = sum(atr_values) / len(atr_values) if atr_values else 0.0

        # Daily returns for correlation
        daily_returns = []
        for j in range(1, i + 1):
            if closes[j - 1] > 0:
                daily_returns.append((closes[j] / closes[j - 1]) - 1)

        return {
            "roc": roc_arr[i],
            "ema_fast": ema_f[i],
            "ema_slow": ema_s[i],
            "ema_trend": ema_f[i] > ema_s[i],
            "atr": atr_arr[i],
            "avg_atr_30": avg_atr_30,
            "adx": adx_arr[i],
            "rsi": rsi_arr[i],
            "price": closes[i],
            "close_today": closes[-1],
            "daily_returns": daily_returns,
            "date": candles[i]["datetime"].strftime("%Y-%m-%d"),
            "date_today": candles[-1]["datetime"].strftime("%Y-%m-%d"),
        }

    def _compute_btc_200ma(self, candles: list) -> float:
        """Compute BTC long-MA for long-only filter."""
        period = self.config.sma_long
        if len(candles) < period:
            return 0.0
        closes = [c["C"] for c in candles]
        sma = self.sma(closes, period)
        return sma[-1]

    # ─── Dynamic leverage & sizing ───

    def _calc_dynamic_leverage(self, atr: float, price: float) -> float:
        """Dynamic leverage: min(max_leverage, 1 / (daily ATR% x 2))."""
        if atr <= 0 or price <= 0:
            return 1.0
        atr_pct = atr / price
        lev = 1.0 / (atr_pct * 2)
        lev = max(1.0, min(lev, self.config.max_leverage))
        return round(lev, 1)

    def _calc_size(self, coin: str, price: float, stop_distance: float, leverage: float) -> float:
        """Risk-based position sizing: risk_per_trade / (stop_pct x leverage),
        capped so total margin <= equity * allocation_pct."""
        ct_val = CT_VAL.get(coin, 0.01)
        lot = LOT_SZ.get(coin, 0.01)
        cfg = self.config

        if stop_distance <= 0 or price <= 0:
            stop_pct = 0.03  # fallback 3%
        else:
            stop_pct = stop_distance / price

        # Risk amount in USD
        risk_usd = self._equity * cfg.risk_per_trade
        notional = risk_usd / stop_pct
        margin = notional / leverage if leverage > 0 else notional
        # Cap margin at allocation_pct * equity
        max_margin = self._equity * cfg.allocation_pct
        if margin > max_margin:
            margin = max_margin
            notional = margin * leverage

        raw_sz = notional / (ct_val * price)
        sz = math.floor(raw_sz / lot + 1e-12) * lot
        return max(sz, lot)

    # ─── Correlation filter ───

    def _check_correlation(self, candidate_coin: str, all_indicators: dict) -> bool:
        """Check if adding candidate_coin would violate correlation constraint.
        Returns True if OK to add (no violation)."""
        candidate_returns = all_indicators.get(candidate_coin, {}).get("daily_returns", [])
        if not candidate_returns:
            return True

        for held_coin in self._positions:
            held_returns = all_indicators.get(held_coin, {}).get("daily_returns", [])
            if not held_returns:
                continue
            # Align lengths
            min_len = min(len(candidate_returns), len(held_returns))
            if min_len < 15:
                continue
            corr = self.correlation(
                candidate_returns[-min_len:],
                held_returns[-min_len:]
            )
            if abs(corr) > self.config.corr_threshold:
                print(f"[Rotation] Correlation filter: {candidate_coin} corr with {held_coin} = {corr:.2f} > {self.config.corr_threshold} -> SKIP",
                      flush=True)
                return False
        return True

    # ─── Trading ───

    async def _get_client(self):
        if not self.client_manager:
            return None
        return self.client_manager.get_client()

    async def _place_order(self, client, inst_id: str, side: str, sz: float,
                                      pos_side: str = None, ord_type: str = "market",
                                      px: float = None) -> dict:
        """Place order. side='buy'/'sell', pos_side='long'/'short'."""
        params = {
            "inst_id": inst_id, "side": side, "ord_type": ord_type,
            "sz": str(sz), "td_mode": "cross", "pos_side": pos_side,
        }
        if px and ord_type == "limit":
            params["px"] = str(round(px, 2))
        resp = await client.place_order(**params)
        return resp

    # ─── Exchange-side stop orders ───

    async def _place_exchange_stop(self, client, pos: RotPosition) -> str:
        """Place a conditional SL on the exchange for a position. Returns algoId or ''."""
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
            print(f"[Rotation] Place stop error {pos.coin}: {resp.get('message', '')}", flush=True)
            return ""
        algo_id = ""
        if resp.get("data"):
            algo_id = resp["data"][0].get("algoId", "")
        pos.algo_id = algo_id
        print(f"[Rotation] Stop placed {pos.coin} {pos.side} @ {pos.stop_price:.2f} "
              f"sz={pos.size} algoId={algo_id}", flush=True)
        return algo_id

    async def _cancel_exchange_stop(self, client, pos: RotPosition):
        """Cancel the exchange-side SL for a position (if any)."""
        if not pos.algo_id:
            return
        resp = await client.cancel_algo_order(pos.inst_id, pos.algo_id)
        if resp.get("error"):
            print(f"[Rotation] Cancel stop error {pos.coin}: {resp.get('message', '')}", flush=True)
        else:
            print(f"[Rotation] Stop cancelled {pos.coin} algoId={pos.algo_id}", flush=True)
        pos.algo_id = ""

    async def _update_exchange_stop(self, client, pos: RotPosition):
        """Re-place the exchange SL when trailing stop moved or size changed.
        New stop is placed BEFORE the old one is cancelled (no unprotected window)."""
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
            print(f"[Rotation] Update stop place error {pos.coin}: {resp.get('message', '')} "
                  f"— keeping old stop", flush=True)
            return
        if resp.get("data"):
            new_algo_id = resp["data"][0].get("algoId", "")
        if new_algo_id:
            await self._cancel_exchange_stop(client, pos)
            pos.algo_id = new_algo_id
            print(f"[Rotation] Stop updated {pos.coin} {pos.side} @ {pos.stop_price:.2f} "
                  f"sz={pos.size} algoId={new_algo_id}", flush=True)

    async def _close_partial(self, client, inst_id: str, pos: RotPosition, close_ratio: float) -> dict:
        """Close portion of position."""
        close_sz = round(pos.size * close_ratio / LOT_SZ.get(pos.coin, 0.01)) * LOT_SZ.get(pos.coin, 0.01)
        if close_sz <= 0 or close_sz >= pos.size:
            return {}
        close_side = "sell" if pos.side == "long" else "buy"
        resp = await self._place_order(client, inst_id, close_side, close_sz,
                                          pos_side=pos.side)
        if resp.get("error"):
            print(f"[Rotation] Partial close error {pos.coin}: {resp.get('message', '')}", flush=True)
            return {}
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
            "time": now, "side": close_side,
            "symbol": inst_id, "size": close_sz,
            "pnl": round(pnl, 2),
            "entry_price": pos.entry_price, "exit_price": round(fill_px, 2),
            "reason": "partial_tp", "pos_side": pos.side, "coin": pos.coin,
            "signal_id": pos.signal_id,
        })
        pos.size -= close_sz
        pos.partial_done = True
        print(f"[Rotation] PARTIAL {now[:19]} {pos.coin:4} {pos.side:5} "
              f"closed {close_sz} of {pos.size + close_sz} @ {fill_px:.1f} "
              f"pnl={pnl:+.2f}", flush=True)
        return {"fill_px": fill_px, "fee": fee, "pnl": pnl, "close_sz": close_sz}

    async def _close_position(self, client, inst_id: str, pos: RotPosition, reason: str):
        """Close full position at market."""
        await self._cancel_exchange_stop(client, pos)
        close_side = "sell" if pos.side == "long" else "buy"
        resp = await self._place_order(client, inst_id, close_side, pos.size,
                                          pos_side=pos.side)
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
            "time": now, "side": close_side,
            "symbol": inst_id, "size": pos.size,
            "pnl": round(pnl, 2),
            "entry_price": pos.entry_price, "exit_price": round(fill_px, 2),
            "reason": reason, "pos_side": pos.side, "coin": pos.coin,
            "signal_id": pos.signal_id,
        }
        self._trade_log.append(trade_entry)

        if self.db:
            try:
                await self.db.save_trade(
                    bot_id=ROT_BOT_ID, side=close_side, sz=pos.size,
                    px=round(fill_px, 2),
                    ord_id=fills[0].get("ordId", "") if fills else "",
                    inst_id=inst_id, ord_type="market",
                    fee=round(fee, 4), fee_ccy="USDT",
                    pnl=round(pnl, 2), state="filled",
                    signal_id=pos.signal_id,
                )
                await self._sync_positions_db()
            except Exception as e:
                print(f"[Rotation] DB save trade error: {e}", flush=True)

        print(f"[Rotation] CLOSE  {now[:19]} {pos.coin:4} {pos.side:5} "
              f"entry={pos.entry_price:.1f} exit={fill_px:.1f} "
              f"pnl={pnl:+.2f} ({reason})", flush=True)

    async def _open_position(self, client, coin: str, side: str, ind: dict,
                              lev: float):
        """Open a new position with limit order + market fallback."""
        inst_id = SWAP_MAP.get(coin, f"{coin}-USDT-SWAP")
        price = ind["close_today"]
        atr_val = ind["atr"]
        if atr_val <= 0 or price <= 0:
            return

        # Set leverage
        if lev != 1.0:
            lev_resp = await client.set_leverage(
                inst_id=inst_id, leverage=lev, mgn_mode="cross", pos_side=side,
            )
            if lev_resp.get("error"):
                print(f"[Rotation] Set leverage error {coin}: {lev_resp.get('message', '')}", flush=True)

        # Initial stop = price ± daily ATR * atr_stop_mult
        stop_dist = atr_val * self.config.atr_stop_mult
        if side == "long":
            stop = price - stop_dist
        else:
            stop = price + stop_dist

        # Size based on risk
        sz = self._calc_size(coin, price, stop_dist, lev)
        order_side = "buy" if side == "long" else "sell"

        # Save signal to DB
        signal_id = 0
        if self.db:
            try:
                signal_id = await self.db.save_signal(
                    bot_id=ROT_BOT_ID,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    side=order_side, price=price, size=sz,
                    ord_type="limit", status="pending",
                )
            except Exception as e:
                print(f"[Rotation] DB save signal error: {e}", flush=True)

        if not self.config.auto_execute:
            print(f"[Rotation] SIGNAL (no execute) {coin} {side} @ {price:.1f} lev={lev}", flush=True)
            return

        # Try limit order first (0.1% better price)
        limit_px = price * (1 - self.config.limit_offset_pct) if side == "long" else price * (1 + self.config.limit_offset_pct)
        resp = await self._place_order(client, inst_id, order_side, sz,
                                          pos_side=side, ord_type="limit", px=limit_px)

        # Check if filled immediately
        fills = resp.get("data", [])
        if not resp.get("error") and fills:
            fill_state = fills[0].get("state", "")
            if fill_state == "fill":
                # Limit filled immediately
                pass
            else:
                # Wait for fill, then cancel and use market if needed
                await asyncio.sleep(self.config.limit_wait_sec)
                # Check order status
                if fills[0].get("ordId"):
                    await client.cancel_order(inst_id, fills[0]["ordId"])
                resp = await self._place_order(client, inst_id, order_side, sz,
                                                  pos_side=side, ord_type="market")
                fills = resp.get("data", [])
        elif resp.get("error") or not fills:
            # Limit failed, use market
            resp = await self._place_order(client, inst_id, order_side, sz,
                                              pos_side=side, ord_type="market")
            fills = resp.get("data", [])

        if resp.get("error"):
            print(f"[Rotation] Open error {coin}: {resp.get('message', '')}", flush=True)
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
        pos = RotPosition(
            symbol=inst_id, coin=coin, inst_id=inst_id,
            side=side, size=sz, size_original=sz,
            entry_price=fill_px,
            stop_price=stop, peak_price=fill_px,
            opened_at=now, atr=atr_val, atr_hourly=0.0,
            leverage=lev, signal_id=signal_id, raw_entry=price,
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
                    bot_id=ROT_BOT_ID, side=order_side, sz=sz,
                    px=round(fill_px, 2), ord_id=ord_id,
                    inst_id=inst_id, ord_type="market",
                    fee=round(fee, 4), fee_ccy="USDT",
                    pnl=0, state="filled", signal_id=signal_id,
                )
                await self._sync_positions_db()
            except Exception as e:
                print(f"[Rotation] DB save error: {e}", flush=True)

        self._equity -= fee
        print(f"[Rotation] OPEN  {now[:19]} {coin:4} {side:5} "
              f"price={fill_px:.1f} stop={stop:.1f} sz={sz} "
              f"lev={lev} atr={atr_val:.1f} fee={fee:.2f}", flush=True)

    # ─── Core logic ───

    async def _check_and_trade(self):
        """Main logic: indicators, filters, ranking, rotate, manage stops."""
        client = await self._get_client()
        if not client:
            print("[Rotation] No OKX client available", flush=True)
            return

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 1. Fetch data and compute indicators
        indicators = {}
        cfg = self.config

        for coin in cfg.symbols:
            try:
                candles_d = await self._fetch_daily(client, coin, limit=250)
                if not candles_d:
                    continue
                ind = self._compute_daily_indicators(candles_d)
                if ind:
                    indicators[coin] = ind

                # BTC 200-day MA (for long-only filter)
                if coin == "BTC":
                    self._btc_200ma = self._compute_btc_200ma(candles_d)
            except Exception as e:
                print(f"[Rotation] Error fetching {coin}: {e}", flush=True)

        self._latest_indicators = indicators

        if not indicators:
            return

        # 2. Manage existing positions: trailing stops + partial TP
        for coin in list(self._positions.keys()):
            pos = self._positions[coin]
            ind = indicators.get(coin)
            if not ind:
                continue

            current_price = ind["close_today"]
            hit_stop = False
            reason = "trail_stop"

            # Dynamic trailing = entry ATR x trail_atr_mult
            trail_step = pos.atr * cfg.trail_atr_mult
            if trail_step <= 0:
                trail_step = pos.entry_price * 0.02  # fallback 2%

            if pos.side == "long":
                if current_price > pos.peak_price:
                    pos.peak_price = current_price
                new_stop = pos.peak_price - trail_step
                if new_stop > pos.stop_price:
                    pos.stop_price = new_stop
                # Breakeven after 3%
                if not pos.breakeven and current_price >= pos.entry_price * (1 + cfg.breakeven_pct):
                    pos.stop_price = max(pos.stop_price, pos.entry_price * 0.999)
                    pos.breakeven = True
                # Partial TP at +5%
                if not pos.partial_done and current_price >= pos.entry_price * (1 + cfg.partial_tp_pct):
                    await self._close_partial(client, pos.inst_id, pos, cfg.partial_tp_ratio)
                if current_price <= pos.stop_price:
                    hit_stop = True
            else:  # short
                if current_price < pos.peak_price:
                    pos.peak_price = current_price
                new_stop = pos.peak_price + trail_step
                if new_stop < pos.stop_price:
                    pos.stop_price = new_stop
                if not pos.breakeven and current_price <= pos.entry_price * (1 - cfg.breakeven_pct):
                    pos.stop_price = min(pos.stop_price, pos.entry_price * 1.001)
                    pos.breakeven = True
                if not pos.partial_done and current_price <= pos.entry_price * (1 - cfg.partial_tp_pct):
                    await self._close_partial(client, pos.inst_id, pos, cfg.partial_tp_ratio)
                if current_price >= pos.stop_price:
                    hit_stop = True

            if hit_stop:
                await self._cancel_exchange_stop(client, pos)
                await self._close_position(client, pos.inst_id, pos, reason)
                del self._positions[coin]
            elif pos.algo_id:
                # Stop moved (or size changed after partial TP) → sync exchange SL
                await self._update_exchange_stop(client, pos)

        # 3. Check if we should rotate
        slots_full = len(self._positions) >= cfg.top_k
        if slots_full and self._last_daily_check == today_str:
            return  # all slots full, already checked today

        now_ts = int(time.time() * 1000)
        if slots_full and self._last_rotate_ts > 0:
            hold_days = (now_ts - self._last_rotate_ts) / (86400 * 1000)
            if hold_days < cfg.min_hold_days:
                return

        # 4. Weighted ranking with filters
        ranked = []
        btc_trend_bull = self._btc_200ma > 0
        btc_above_200ma = False
        btc_ind = indicators.get("BTC")
        if btc_ind and btc_trend_bull:
            btc_above_200ma = btc_ind["close_today"] > self._btc_200ma

        for coin, ind in indicators.items():
            if ind["atr"] <= 0:
                continue

            # ── FILTER: Volatility ──
            if ind["avg_atr_30"] > 0:
                if ind["atr"] > ind["avg_atr_30"] * cfg.vol_mult:
                    print(f"[Rotation] Vol filter: {coin} ATR={ind['atr']:.1f} > "
                          f"avg30*{cfg.vol_mult}={ind['avg_atr_30'] * cfg.vol_mult:.1f} -> SKIP",
                          flush=True)
                    continue

            # ── FILTER: RSI ──
            if ind["rsi"] > cfg.rsi_long_max and ind["ema_trend"]:
                print(f"[Rotation] RSI filter: {coin} RSI={ind['rsi']:.1f} > {cfg.rsi_long_max} -> no long", flush=True)
                continue
            if ind["rsi"] < cfg.rsi_short_min and not ind["ema_trend"]:
                print(f"[Rotation] RSI filter: {coin} RSI={ind['rsi']:.1f} < {cfg.rsi_short_min} -> no short", flush=True)
                continue

            # ── FILTER: Long-only in bear market ──
            if btc_trend_bull and not btc_above_200ma:
                # BTC below 200MA -> only allow shorts (or skip longs)
                if ind["roc"] > 0 and ind["ema_trend"]:
                    print(f"[Rotation] Bear filter: {coin} -> skip long (BTC < 200MA)", flush=True)
                    continue

            # ── FILTER: min |roc| ──
            if abs(ind["roc"]) < cfg.min_roc:
                print(f"[Rotation] min_roc filter: {coin} ROC={ind['roc']:.1f} < {cfg.min_roc} -> SKIP", flush=True)
                continue

            # ── Weighted score ──
            roc_val = ind["roc"]
            trend_val = (ind["ema_fast"] - ind["ema_slow"]) / ind["ema_slow"] * 100 if ind["ema_slow"] > 0 else 0
            adx_val = ind["adx"]
            score = roc_val * 0.5 + trend_val * 0.3 + (adx_val / 50) * 0.2

            ranked.append((coin, score, ind["roc"], ind["ema_trend"], ind["adx"], ind["atr"]))

        if not ranked:
            return

        # Sort by weighted score descending
        ranked.sort(key=lambda x: x[1], reverse=True)

        # 5. Determine target coins (with correlation filter)
        target_coins = set()
        for coin, score, roc_val, ema_trend, adx_val, atr_val in ranked:
            if len(target_coins) >= cfg.top_k:
                break

            # Direction: based on trend + ROC
            if roc_val > cfg.min_roc and ema_trend and adx_val >= cfg.adx_min:
                side = "long"
            elif (cfg.allow_short and roc_val < -cfg.min_roc and not ema_trend
                  and adx_val >= cfg.adx_min):
                side = "short"
            else:
                continue

            # ── FILTER: Correlation ──
            if not self._check_correlation(coin, indicators):
                continue

            target_coins.add((coin, side))

        # 6. Full rotation: close positions not in target (only on daily check, not when filling slots)
        if slots_full:
            for coin in list(self._positions.keys()):
                pos = self._positions[coin]
                if (coin, pos.side) not in target_coins:
                    await self._close_position(client, pos.inst_id, pos, "rotation_exit")
                    del self._positions[coin]

        # 7. Open new positions (fill empty slots)
        opened_any = False
        for coin, side in target_coins:
            if coin in self._positions:
                continue
            ind = indicators.get(coin)
            if not ind:
                continue
            lev = self._calc_dynamic_leverage(ind["atr"], ind["close_today"])
            await self._open_position(client, coin, side, ind, lev)
            opened_any = True

        # Update daily check only when doing a full rotation or opening a trade
        if slots_full or opened_any:
            self._last_daily_check = today_str
        if slots_full or opened_any:
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
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._poll_loop())

    async def start(self):
        if self._running:
            return
        self._running = True
        self._started_at = datetime.now(timezone.utc).isoformat()
        if self.db:
            await self._ensure_bot()
            await self._reload_equity()
        await self._sync_open_positions()
        self._thread = threading.Thread(target=self._thread_target, daemon=True)
        self._thread.start()
        print(f"[Rotation v3] Started (capital=${self._equity:,.0f}, poll={self.config.poll_interval_sec}s)",
              flush=True)

    async def stop(self):
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
        print("[Rotation v3] Stopped", flush=True)

    def get_status(self) -> dict:
        """Return current status dict."""
        trades = self._trade_log
        closed = [t for t in trades if t.get("pnl", 0) != 0]
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) <= 0]

        realized_pnl = self._equity - self._capital

        unrealized_total = 0.0
        for coin in self._positions:
            unrealized_total += self._calc_unrealized(coin)

        full_equity = self._capital + realized_pnl + unrealized_total

        win_rate = len(wins) / len(closed) * 100 if closed else 0

        open_positions_list = []
        for coin, pos in self._positions.items():
            stage = "trailing" if pos.breakeven else ("partial" if pos.partial_done else "initial")
            ct = CT_VAL.get(coin, 0.01)
            notional = pos.size * ct * pos.entry_price
            margin = notional / pos.leverage if pos.leverage > 0 else notional
            if pos.side == "long":
                tp1_price = pos.entry_price * (1 + self.config.partial_tp_pct)
                be_price = pos.entry_price * 0.999
            else:
                tp1_price = pos.entry_price * (1 - self.config.partial_tp_pct)
                be_price = pos.entry_price * 1.001
            open_positions_list.append({
                "coin": pos.coin, "symbol": pos.inst_id, "inst_id": pos.inst_id,
                "side": pos.side, "size": pos.size, "size_remaining": pos.size,
                "size_original": pos.size_original,
                "entry": pos.entry_price, "entry_price": pos.entry_price,
                "stop": round(pos.stop_price, 2), "stop_price": round(pos.stop_price, 2),
                "tp1": round(tp1_price, 2), "be_price": round(be_price, 2),
                "peak_price": round(pos.peak_price, 2),
                "breakeven": pos.breakeven, "partial_done": pos.partial_done,
                "opened_at": pos.opened_at,
                "unrealized_pnl": self._calc_unrealized(coin),
                "stage": stage, "pos_mode": "cross",
                "notional": round(notional, 2), "margin": round(margin, 2),
                "leverage": pos.leverage,
            })

        cfg = asdict(self.config)
        cfg.setdefault("max_positions", self.config.top_k)
        cfg.setdefault("risk_per_trade_old", 0.0)
        cfg.setdefault("tp1_pct", 0.0)

        # Filter info for dashboard
        filters_active = []
        if self._btc_200ma > 0:
            btc_ind = self._latest_indicators.get("BTC")
            if btc_ind:
                btc_above = btc_ind["close_today"] > self._btc_200ma
                filters_active.append(f"BTC {'>' if btc_above else '<'} 200MA: {'longs OK' if btc_above else 'longs blocked'}")

        return {
            "running": self._running,
            "strategy": "momentum_rotation_v3",
            "config": cfg,
            "equity": round(full_equity, 2),
            "capital": self._capital,
            "total_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_total, 2),
            "open_positions": open_positions_list,
            "total_trades": len(closed),
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "recent_trades": trades[-20:],
            "recent_signals": self._signal_log[-10:],
            "indicators": self._latest_indicators,
            "filters": filters_active,
            "btc_200ma": round(self._btc_200ma, 2) if self._btc_200ma else None,
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

    async def _sync_positions_db(self):
        if not self.db:
            return
        try:
            if self.db._pg_mode:
                await self.db._execute("DELETE FROM positions WHERE bot_id = $1", (ROT_BOT_ID,))
            else:
                await self.db._execute("DELETE FROM positions WHERE bot_id = ?", (ROT_BOT_ID,))
            for coin, pos in self._positions.items():
                await self.db.save_position(
                    bot_id=ROT_BOT_ID, inst_id=pos.inst_id,
                    side=pos.side, size=pos.size,
                    entry_price=round(pos.entry_price, 2),
                    current_price=pos.peak_price,
                )
        except Exception as e:
            print(f"[Rotation] DB sync positions error: {e}", flush=True)

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
                    "VALUES ($1, 'rotation', 'momentum_rotation_v3', 'MULTI', '1D', "
                    "$2, $3, 'running', 'demo', 'momentum', $4, 'Momentum Rotation v3') "
                    "ON CONFLICT (id) DO NOTHING",
                    (ROT_BOT_ID, self._equity, str(params), now),
                )
            else:
                await self.db._execute(
                    "INSERT OR IGNORE INTO bots (id, strategy_id, strategy_code, symbol, timeframe, "
                    "capital, params, status, mode, signal_type, created_at, name) "
                    "VALUES (?, 'rotation', 'momentum_rotation_v3', 'MULTI', '1D', "
                    "?, ?, 'running', 'demo', 'momentum', ?, 'Momentum Rotation v3')",
                    (ROT_BOT_ID, self._equity, str(params), now),
                )
        except Exception as e:
            print(f"[Rotation] DB ensure_bot error: {e}", flush=True)

    async def _reload_equity(self):
        if not self.db:
            return
        try:
            rows = await self.db.get_trades(bot_id=ROT_BOT_ID, limit=500)
            for t in rows:
                db_pnl = float(t.get("pnl", 0) or 0)
                db_fee = float(t.get("fee", 0) or 0)
                effective_pnl = db_pnl
                if db_pnl == 0 and db_fee > 0:
                    effective_pnl = -db_fee
                self._trade_log.append({
                    "time": t.get("timestamp", ""),
                    "side": t.get("side", ""),
                    "symbol": t.get("inst_id", ""),
                    "size": float(t.get("sz", 0) or 0),
                    "pnl": effective_pnl,
                    "entry_price": float(t.get("px", 0) or 0),
                    "reason": "closed",
                    "coin": t.get("inst_id", "").replace("-USDT-SWAP", "").replace("-USD-SWAP", ""),
                    "signal_id": t.get("signal_id", 0),
                })
            total_pnl = sum(t.get("pnl", 0) for t in self._trade_log)
            self._equity = self._capital + total_pnl
        except Exception as e:
            print(f"[Rotation] DB reload error: {e}", flush=True)

    async def _sync_open_positions(self):
        """After restart, detect open positions from OKX and restore _positions +
        re-place exchange-side stops so they survive a process crash."""
        client = await self._get_client()
        if not client:
            return
        try:
            result = await client.get_positions("SWAP")
            if result.get("error") or not result.get("data"):
                return
            for p in result.get("data", []):
                inst_id = p.get("instId", "")
                coin = inst_id.replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
                if coin not in self.config.symbols:
                    continue
                pos_side = p.get("posSide", "net")
                is_long = pos_side != "short"
                entry_px = float(p.get("avgPx", 0) or 0)
                sz = float(p.get("pos", 0) or 0)
                if entry_px <= 0 or sz <= 0:
                    continue
                if coin in self._positions:
                    continue

                side = "long" if is_long else "short"
                estimated_atr = entry_px * 0.015
                if is_long:
                    stop_price = entry_px * 0.985
                else:
                    stop_price = entry_px * 1.015

                pos = RotPosition(
                    symbol=inst_id, coin=coin, inst_id=inst_id,
                    side=side, size=sz, size_original=sz,
                    entry_price=entry_px, stop_price=stop_price,
                    peak_price=entry_px, opened_at=datetime.now(timezone.utc).isoformat(),
                    atr=estimated_atr, atr_hourly=estimated_atr,
                    leverage=self.config.max_leverage,
                )
                self._positions[coin] = pos
                await self._place_exchange_stop(client, pos)
                print(f"[Rotation] Restored {side.upper()} {coin} sz={sz} @ {entry_px:.2f} "
                      f"stop={stop_price:.2f}", flush=True)
        except Exception as e:
            print(f"[Rotation] Sync open positions error: {e}", flush=True)
