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

from .telegram_notifier import TelegramNotifier
from .analysis_logger import get_logger

ROT_BOT_ID = "rotation_strategy"
STRATEGY_VERSION = "v5"
STRATEGY_NAME = f"momentum_rotation_{STRATEGY_VERSION}"

CT_VAL = {"BTC": 0.01, "ETH": 0.1, "BNB": 0.01, "SOL": 1, "XRP": 100,
          "DOGE": 1000, "ADA": 100, "TRX": 1000, "AVAX": 1, "LTC": 1}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "BNB": 1, "SOL": 0.01, "XRP": 0.01,
          "DOGE": 0.01, "ADA": 0.01, "TRX": 0.01, "AVAX": 0.1, "LTC": 0.1}
SWAP_MAP = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP",
            "BNB": "BNB-USDT-SWAP", "SOL": "SOL-USDT-SWAP",
            "XRP": "XRP-USDT-SWAP", "DOGE": "DOGE-USDT-SWAP",
            "ADA": "ADA-USDT-SWAP", "TRX": "TRX-USDT-SWAP",
            "AVAX": "AVAX-USDT-SWAP", "LTC": "LTC-USDT-SWAP"}
COINS = ["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"]

STRATEGY_DESC = (
    "Momentum Rotation v5 (2026-08 tuning). Бот ежедневно сканирует 10 монет на дневных барах и выбирает до 2 самых сильных тренда. "
    "Скоринг: ROC(14) показывает импульс, EMA20/50 — направление тренда, ADX(14) — его силу. "
    "Фильтры отсекают шум: ADX≥25, |ROC|≥4.5%, тренд по EMA, RSI не перекуплен/перепродан, "
    "волатильность не выше среднего (×2.2), корреляция до 0.85. Рыночный режим (bull/bear/chop по BTC SMA50/200): "
    "в бычьем — только лонги, в медвежьем — только шорты, в неопределённости — кэш. "
    "Размер позиции считается от риска 20% капитала, маржа на позицию ≤50% equity: стоп = 4.5× дневной ATR, плечо до 2× "
    "(чем выше волатильность, тем меньше плечо). После входа: трейлинг-стоп 3× дневной ATR (держит победителей), "
    "при +5% стоп в безубыток, при +8% закрывается половина позиции, динамический тейк-профит "
    "(37.6% → 23.7% → 9.2% → безубыток по мере удержания). Минимум держим 11 дней. "
    "Если монета выпадает из топа — закрываем по рынку. Валидация (BT, нативные 1D, 10 монет, 2023-05..2026-08): "
    "CAGR ~60%, Sharpe 1.23, MaxDD −52%. Режим cross margin, демо/реал переключается env."
)


@dataclass
class RotationConfig:
    symbols: list = None
    regime_symbols: list = None    # coins used ONLY for market regime, never traded
    capital: float = 10000.0
    top_k: int = 2
    roc_period: int = 14
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    adx_min: float = 25.0
    min_roc: float = 4.5            # min |roc| to even rank a coin
    sma_long: int = 200            # BTC regime MA
    sma_regime: int = 50           # BTC regime MA (SMA50 < SMA200 => bear)
    min_hold_days: int = 11        # cooldown before rotating again
    max_leverage: float = 2.0
    risk_per_trade: float = 0.20   # risk of equity per trade (v5 tuning)
    allocation_pct: float = 0.5    # max margin per position = eq * this
    atr_stop_mult: float = 4.5     # initial stop = daily ATR * 4.5
    trail_atr_mult: float = 3.0    # trailing = daily ATR * 3.0 (v5: wide)
    breakeven_pct: float = 0.05    # move to BE after 5%
    partial_tp_pct: float = 0.08   # close 50% at +8%
    partial_tp_ratio: float = 0.5  # fraction to close
    roi_table: list = None         # dynamic ROI: [(min_hold_days, tp_pct), ...]
    rsi_period: int = 14
    rsi_long_max: float = 82.0     # no long if RSI > 82
    rsi_short_min: float = 21.0    # no short if RSI < 21
    vol_mult: float = 2.2          # skip if ATR > avg * 2.2 (v5: stricter)
    corr_threshold: float = 0.85   # max correlation between held pairs (v5)
    allow_short: bool = True       # allow shorting bearish coins
    limit_offset_pct: float = 0.001   # 0.1% below price for limit orders
    limit_wait_sec: int = 300      # 5 min fallback to market
    poll_interval_sec: int = 300
    auto_execute: bool = True

    def __post_init__(self):
        if self.symbols is None:
            self.symbols = list(COINS)
        if self.regime_symbols is None:
            self.regime_symbols = []
        if self.roi_table is None:
            # Динамический ROI: чем дольше держим, тем ниже TP.
            self.roi_table = [
                (17, 0.00),
                (8, 0.092),
                (3, 0.237),
                (0, 0.376),
            ]


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
    stop_synced: float = 0.0     # stop price last synced to the exchange
    size_synced: float = 0.0     # size last synced to the exchange stop


class RotationStrategy:
    # Module-level defaults, overridable in subclasses (e.g. validation bot).
    BOT_ID: str = ROT_BOT_ID
    BOT_NAME: str = f"Momentum Rotation {STRATEGY_VERSION}"
    CT_VAL: dict = CT_VAL
    LOT_SZ: dict = LOT_SZ
    SWAP_MAP: dict = SWAP_MAP
    STRATEGY_NAME: str = STRATEGY_NAME
    STRATEGY_VERSION: str = STRATEGY_VERSION
    STRATEGY_DESC: str = STRATEGY_DESC
    PRICE_DECIMALS: int = 2
    # Client-order-ID prefix: when non-empty, every order gets clOrdId=<prefix>-<n>,
    # so its fills can be identified on the exchange (used to attribute OKX fills
    # to this bot in the PnL aggregation, and to keep demo-validator trades out
    # of Momentum windows).
    CL_ORD_PREFIX: str = "rot"

    def __init__(self, config: RotationConfig, client_manager=None, db=None,
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
        self._regime: str = "unknown"        # market regime: bull/bear/chop
        # cooldowns[coin] = epoch seconds until which the bot must not reopen
        # that coin (set after a manual/external close so it doesn't instantly
        # re-enter the same position the user just closed).
        self._cooldowns: dict[str, float] = {}
        # Manual close cooldown in seconds (overridable per subclass).
        self.MANUAL_CLOSE_COOLDOWN_SEC: float = 4 * 3600.0

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
        inst_id = self.SWAP_MAP.get(coin, f"{coin}-USDT-SWAP")
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

    def _get_regime(self, candles: list) -> str:
        """Market regime: 'bull' (close>200MA), 'bear' (SMA50<200MA), 'chop', 'unknown'."""
        cfg = self.config
        closes = [c["C"] for c in candles]
        if len(closes) < cfg.sma_long:
            return "unknown"
        sma200 = self.sma(closes, cfg.sma_long)
        sma50 = self.sma(closes, cfg.sma_regime)
        if sma200[-1] <= 0:
            return "unknown"
        if closes[-1] > sma200[-1]:
            return "bull"
        if sma50[-1] < sma200[-1]:
            return "bear"
        return "chop"

    def _roi_target(self, hold_days: int) -> float:
        """Dynamic ROI: чем дольше держим, тем ниже TP. Возвращает порог прибыли."""
        for min_hold, tp_pct in self.config.roi_table:
            if hold_days >= min_hold:
                return tp_pct
        return 0.0

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
        """Risk-based position sizing: risk_per_trade / stop_pct.
        Margin (own funds) is capped by allocation_pct of the $10k budget;
        leverage inflates position notional (notional = margin * leverage)."""
        ct_val = self.CT_VAL.get(coin, 0.01)
        lot = self.LOT_SZ.get(coin, 0.01)
        cfg = self.config

        if stop_distance <= 0 or price <= 0:
            stop_pct = 0.03  # fallback 3%
        else:
            stop_pct = stop_distance / price

        # Risk amount in USD
        risk_usd = self._equity * cfg.risk_per_trade
        notional = risk_usd / stop_pct
        # Margin is the bot's own funds (budget $10k); leverage is applied on top.
        margin = notional / leverage if leverage > 0 else notional
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
            if held_coin == candidate_coin:
                # Same instrument: a position on it is not a correlation violation.
                continue
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
                self.analysis.log("rotation", "filter",
                                  coin=candidate_coin, filter="correlation",
                                  with_coin=held_coin, corr=round(corr, 3),
                                  threshold=self.config.corr_threshold, decision="skip")
                return False
        return True

    # ─── Trading ───

    async def _get_client(self):
        if not self.client_manager:
            return None
        return self.client_manager.get_client()

    def _fmt_px(self, px: float) -> str:
        """Format an order price with enough decimals for the instrument tick
        size. Uses fixed-point formatting (f-string) — never scientific notation,
        which OKX rejects for small prices like PEPE (1e-9 tick)."""
        return f"{px:.{self.PRICE_DECIMALS}f}"

    def _fmt_sz(self, coin: str, sz: float) -> str:
        """Format order size to exact lot decimals (avoid float artifacts like
        9.200000000000001 which OKX rejects as an invalid size)."""
        lot = abs(self.LOT_SZ.get(coin, 0.01))
        decimals = 0 if lot >= 1 else len(str(lot).rstrip("0").split(".")[1])
        return f"{sz:.{decimals}f}"

    async def _place_order(self, client, inst_id: str, side: str, sz: float,
                                      pos_side: str = None, ord_type: str = "market",
                                      px: float = None) -> dict:
        """Place order. side='buy'/'sell', pos_side='long'/'short'."""
        coin = inst_id.split("-USDT")[0]
        params = {
            "inst_id": inst_id, "side": side, "ord_type": ord_type,
            "sz": self._fmt_sz(coin, sz), "td_mode": "cross", "pos_side": pos_side,
        }
        if self.CL_ORD_PREFIX:
            # OKX clOrdId must be alphanumeric only (no dashes) and unique;
            # "val" + ms timestamp is safe.
            params["cl_ord_id"] = f"{self.CL_ORD_PREFIX}{int(time.time() * 1000)}"
        if px and ord_type == "limit":
            params["px"] = self._fmt_px(px)
        resp = await client.place_order(**params)
        return resp

    async def _position_exists(self, client, inst_id: str, side: str) -> bool:
        """True if the exchange already has an open position on this instrument
        and side (prevents double entries from limit+market fallback)."""
        try:
            result = await client.get_positions("SWAP", inst_id=inst_id)
            if result.get("error"):
                return False
            for p in result.get("data", []):
                pos_side = p.get("posSide", "net")
                sz = float(p.get("pos", 0) or 0)
                if sz > 0 and pos_side == side:
                    return True
            return False
        except Exception:
            return False

    async def _last_close_fill_px(self, client, inst_id: str, pos_side: str) -> float:
        """Best-effort: return the actual fill price of the most recent closing
        fill for this instrument+side (e.g. a manual/external close). Returns
        0.0 if it cannot be determined."""
        try:
            result = await client.get_fills_history(inst_type="SWAP",
                                                    instId=inst_id, limit=20)
            if result.get("error"):
                return 0.0
            close_side = "sell" if pos_side == "long" else "buy"
            fills = result.get("data", [])
            fills.sort(key=lambda f: f.get("ts", "0"), reverse=True)
            for f in fills:
                if f.get("side") == close_side:
                    try:
                        return float(f.get("fillPx", 0) or 0)
                    except (TypeError, ValueError):
                        return 0.0
            return 0.0
        except Exception:
            return 0.0

    async def _last_close_fill(self, client, inst_id: str, pos_side: str) -> dict:
        """Return the most recent closing fill dict (fillPx + ordId) for this
        instrument+side, or {} if none found. Used to book external closes with
        the REAL ordId so PnL dedupe against DB/OKX fills works."""
        try:
            result = await client.get_fills_history(inst_type="SWAP",
                                                    instId=inst_id, limit=20)
            if result.get("error"):
                return {}
            close_side = "sell" if pos_side == "long" else "buy"
            fills = result.get("data", [])
            fills.sort(key=lambda f: f.get("ts", "0"), reverse=True)
            for f in fills:
                if f.get("side") == close_side:
                    return f
            return {}
        except Exception:
            return {}

    # ─── Exchange-side stop orders ───

    async def _place_exchange_stop(self, client, pos: RotPosition) -> str:
        """Place a conditional SL on the exchange for a position. Returns algoId or ''."""
        if not self.config.auto_execute:
            return ""
        close_side = "sell" if pos.side == "long" else "buy"
        resp = await client.place_algo_order(
            inst_id=pos.inst_id, side=close_side,
            sz=self._fmt_sz(pos.coin, pos.size), td_mode="cross", pos_side=pos.side,
            reduce_only=True, sl_trigger_px=self._fmt_px(pos.stop_price),
            cxl_on_close_pos=True,
            cl_ord_id=f"{self.CL_ORD_PREFIX}{int(time.time() * 1000)}" if self.CL_ORD_PREFIX else None,
        )
        if resp.get("error"):
            print(f"[Rotation] Place stop error {pos.coin}: {resp.get('message', '')}", flush=True)
            return ""
        algo_id = ""
        if resp.get("data"):
            algo_id = resp["data"][0].get("algoId", "")
        pos.algo_id = algo_id
        if algo_id:
            pos.stop_synced = pos.stop_price
            pos.size_synced = pos.size
        print(f"[Rotation] Stop placed {pos.coin} {pos.side} @ {pos.stop_price:.2f} "
              f"sz={pos.size} algoId={algo_id}", flush=True)
        self.analysis.log("rotation", "stop_placed",
                          coin=pos.coin, side=pos.side, stop=round(pos.stop_price, 2),
                          size=pos.size, algo_id=algo_id)
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
            self.analysis.log("rotation", "stop_cancelled",
                              coin=pos.coin, side=pos.side, algo_id=pos.algo_id)
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
            sz=self._fmt_sz(pos.coin, pos.size), td_mode="cross", pos_side=pos.side,
            reduce_only=True, sl_trigger_px=self._fmt_px(pos.stop_price),
            cxl_on_close_pos=True,
            cl_ord_id=f"{self.CL_ORD_PREFIX}{int(time.time() * 1000)}" if self.CL_ORD_PREFIX else None,
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
            pos.stop_synced = pos.stop_price
            pos.size_synced = pos.size
            print(f"[Rotation] Stop updated {pos.coin} {pos.side} @ {pos.stop_price:.2f} "
                  f"sz={pos.size} algoId={new_algo_id}", flush=True)
            self.analysis.log("rotation", "stop_updated",
                              coin=pos.coin, side=pos.side, stop=round(pos.stop_price, 2),
                              size=pos.size, algo_id=new_algo_id)

    async def _close_partial(self, client, inst_id: str, pos: RotPosition, close_ratio: float) -> dict:
        """Close portion of position."""
        close_sz = round(pos.size * close_ratio / self.LOT_SZ.get(pos.coin, 0.01)) * self.LOT_SZ.get(pos.coin, 0.01)
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
            pnl = close_sz * self.CT_VAL[pos.coin] * (fill_px - pos.entry_price) - fee
        else:
            pnl = close_sz * self.CT_VAL[pos.coin] * (pos.entry_price - fill_px) - fee
        self._equity += pnl
        now = datetime.now(timezone.utc).isoformat()
        partial_ord_id = fills[0].get("ordId", "") if fills else ""
        self._trade_log.append({
            "time": now, "side": close_side,
            "symbol": inst_id, "size": close_sz,
            "pnl": round(pnl, 2),
            "entry_price": pos.entry_price, "exit_price": round(fill_px, 2),
            "reason": "partial_tp", "pos_side": pos.side, "coin": pos.coin,
            "signal_id": pos.signal_id, "ord_id": partial_ord_id,
        })
        pos.size -= close_sz
        pos.partial_done = True
        # Immediately re-size the exchange-side stop to the reduced position so
        # the exchange never holds a stop larger than the remaining size.
        if pos.algo_id:
            await self._update_exchange_stop(client, pos)
        if self.db:
            try:
                await self.db.save_trade(
                    bot_id=self.BOT_ID, side=close_side, sz=close_sz,
                    px=round(fill_px, 2), ord_id=partial_ord_id,
                    inst_id=inst_id, ord_type="market",
                    fee=round(fee, 4), fee_ccy="USDT",
                    pnl=round(pnl, 2), state="filled",
                    signal_id=pos.signal_id,
                )
            except Exception as e:
                print(f"[Rotation] DB save partial error: {e}", flush=True)
            await self._sync_positions_db()
        print(f"[Rotation] PARTIAL {now[:19]} {pos.coin:4} {pos.side:5} "
              f"closed {close_sz} of {pos.size + close_sz} @ {fill_px:.1f} "
              f"pnl={pnl:+.2f}", flush=True)
        self.analysis.log("rotation", "partial",
                          coin=pos.coin, side=pos.side,
                          closed_sz=close_sz, remaining_sz=pos.size,
                          exit_px=round(fill_px, 2), entry_px=round(pos.entry_price, 2),
                          pnl=round(pnl, 2), fee=round(fee, 4), reason="partial_tp",
                          signal_id=pos.signal_id)

        if self.notifier:
            try:
                self.notifier.fire(self.notifier.partial_msg(
                    coin=pos.coin, side=pos.side, entry=round(pos.entry_price, 2),
                    exit_px=round(fill_px, 2), pnl=round(pnl, 2),
                    closed_sz=round(close_sz, 4), remaining_sz=round(pos.size, 4),
                    bot_name=self.BOT_NAME, signal_id=pos.signal_id,
                ))
            except Exception as e:
                print(f"[Rotation] TG partial notify error: {e}", flush=True)
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
            pnl = pos.size * self.CT_VAL[pos.coin] * (fill_px - pos.entry_price) - fee
        else:
            pnl = pos.size * self.CT_VAL[pos.coin] * (pos.entry_price - fill_px) - fee

        self._equity += pnl
        now = datetime.now(timezone.utc).isoformat()
        close_ord_id = fills[0].get("ordId", "") if fills else ""
        trade_entry = {
            "time": now, "side": close_side,
            "symbol": inst_id, "size": pos.size,
            "pnl": round(pnl, 2),
            "entry_price": pos.entry_price, "exit_price": round(fill_px, 2),
            "reason": reason, "pos_side": pos.side, "coin": pos.coin,
            "signal_id": pos.signal_id, "ord_id": close_ord_id,
        }
        self._trade_log.append(trade_entry)

        if self.db:
            try:
                await self.db.save_trade(
                    bot_id=self.BOT_ID, side=close_side, sz=pos.size,
                    px=round(fill_px, 2),
                    ord_id=close_ord_id,
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
        self.analysis.log("rotation", "close",
                          coin=pos.coin, side=pos.side, reason=reason,
                          entry_px=round(pos.entry_price, 2), exit_px=round(fill_px, 2),
                          size=pos.size, pnl=round(pnl, 2), fee=round(fee, 4),
                          leverage=pos.leverage, signal_id=pos.signal_id)

        if self.notifier:
            try:
                self.notifier.fire(self.notifier.close_msg(
                    coin=pos.coin, side=pos.side, entry=round(pos.entry_price, 2),
                    exit_px=round(fill_px, 2), pnl=round(pnl, 2), reason=reason,
                    bot_name=self.BOT_NAME, signal_id=pos.signal_id,
                ))
            except Exception as e:
                print(f"[Rotation] TG close notify error: {e}", flush=True)

    async def _open_position(self, client, coin: str, side: str, ind: dict,
                              lev: float):
        """Open a new position with limit order + market fallback."""
        inst_id = self.SWAP_MAP.get(coin, f"{coin}-USDT-SWAP")
        price = ind["close_today"]
        atr_val = ind["atr"]
        if atr_val <= 0 or price <= 0:
            return

        # Hard guard: never open a duplicate position if the exchange already
        # has one on this instrument+side (double-entry protection).
        if await self._position_exists(client, inst_id, side):
            print(f"[Rotation] Position already on exchange for {coin} "
                  f"— skip duplicate open", flush=True)
            self.analysis.log("rotation", "dup_open_blocked",
                              coin=coin, side=side)
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
                    bot_id=self.BOT_ID,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    side=order_side, price=price, size=sz,
                    ord_type="limit", status="pending",
                )
            except Exception as e:
                print(f"[Rotation] DB save signal error: {e}", flush=True)

        if not self.config.auto_execute:
            print(f"[Rotation] SIGNAL (no execute) {coin} {side} @ {price:.1f} lev={lev}", flush=True)
            self.analysis.log("rotation", "signal",
                              coin=coin, side=side, price=round(price, 2),
                              leverage=lev, atr=round(atr_val, 2), size=sz,
                              stop=round(stop, 2))
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
                # Check if the limit actually filled on the exchange before
                # placing a market order — otherwise we open the SAME position
                # twice (limit filled + market added = doubled size).
                if fills[0].get("ordId"):
                    await client.cancel_order(inst_id, fills[0]["ordId"])
                if await self._position_exists(client, inst_id, side):
                    print(f"[Rotation] Limit filled on exchange for {coin} "
                          f"— skipping market fallback", flush=True)
                    resp = {"error": False, "data": []}
                else:
                    resp = await self._place_order(client, inst_id, order_side, sz,
                                                      pos_side=side, ord_type="market")
                fills = resp.get("data", [])
        elif resp.get("error") or not fills:
            # Limit failed, use market (but only if no position appeared)
            if await self._position_exists(client, inst_id, side):
                print(f"[Rotation] Position already exists for {coin} "
                      f"— skipping market fallback", flush=True)
                resp = {"error": False, "data": []}
            else:
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
                    bot_id=self.BOT_ID, side=order_side, sz=sz,
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
        self.analysis.log("rotation", "open",
                          coin=coin, side=side, price=round(fill_px, 2),
                          stop=round(stop, 2), size=sz, leverage=lev,
                          atr=round(atr_val, 2), fee=round(fee, 4),
                          inst_id=inst_id, signal_id=signal_id)

        if self.notifier:
            try:
                self.notifier.fire(self.notifier.open_msg(
                    coin=coin, side=side, price=round(fill_px, 2),
                    stop=round(stop, 2), size=round(sz, 4), leverage=lev,
                    bot_name=self.BOT_NAME, signal_id=signal_id,
                ))
            except Exception as e:
                print(f"[Rotation] TG open notify error: {e}", flush=True)

    # ─── Core logic ───

    async def _reconcile_exchange_positions(self, client):
        """Reconcile locally-tracked positions against the real exchange state.

        Handles:
         * exchange-side SL fired between polls -> position vanished locally,
           book PnL conservatively at stop price and drop the phantom position
         * manual partial close / drift -> align local size, force stop re-sync
        """
        if not self._positions:
            return
        try:
            result = await client.get_positions("SWAP")
            actual = {}
            if not result.get("error") and result.get("data"):
                for p in result.get("data", []):
                    inst_id = p.get("instId", "")
                    coin = inst_id.replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
                    if coin not in self.config.symbols:
                        continue
                    pos_side = p.get("posSide", "net")
                    is_long = pos_side != "short"
                    sz = float(p.get("pos", 0) or 0)
                    if sz <= 0:
                        continue
                    actual[(coin, "long" if is_long else "short")] = sz

            for coin in list(self._positions.keys()):
                pos = self._positions[coin]
                real_sz = actual.get((coin, pos.side))
                if real_sz is None:
                    # Position vanished from the exchange -> its SL fired (or was
                    # closed manually/externally). Try to book PnL at the REAL
                    # close price from fills; fall back to the stop price.
                    fill_px = await self._last_close_fill_px(client, pos.inst_id, pos.side)
                    close_ord_id = ""
                    close_reason = "exchange_stop"
                    if fill_px <= 0:
                        fill_px = pos.stop_price
                    else:
                        close_reason = "manual_close"
                        last_fill = await self._last_close_fill(client, pos.inst_id, pos.side)
                        if last_fill:
                            close_ord_id = str(last_fill.get("ordId", "")).strip()
                    # Whatever the cause (stop or manual), do NOT instantly
                    # re-enter the same coin on the next poll.
                    cd_until = time.time() + self.MANUAL_CLOSE_COOLDOWN_SEC
                    self._cooldowns[coin] = cd_until
                    if self.db:
                        try:
                            await self.db.set_setting(
                                f"cooldown:{self.BOT_ID}:{coin}", str(int(cd_until)))
                        except Exception as e:
                            print(f"[Rotation] Cooldown persist error {coin}: {e}", flush=True)
                    print(f"[Rotation] Position gone {coin} ({close_reason} @ {fill_px:.4f}) "
                          f"— cooldown {self.MANUAL_CLOSE_COOLDOWN_SEC/3600:.1f}h",
                          flush=True)
                    ct = self.CT_VAL.get(coin, 0.01)
                    if pos.side == "long":
                        pnl = pos.size * ct * (fill_px - pos.entry_price)
                    else:
                        pnl = pos.size * ct * (pos.entry_price - fill_px)
                    self._equity += pnl
                    now = datetime.now(timezone.utc).isoformat()
                    self._trade_log.append({
                        "time": now, "side": "sell" if pos.side == "long" else "buy",
                        "symbol": pos.inst_id, "size": pos.size,
                        "pnl": round(pnl, 2),
                        "entry_price": pos.entry_price, "exit_price": round(fill_px, 2),
                        "reason": close_reason, "pos_side": pos.side, "coin": coin,
                        "signal_id": pos.signal_id, "ord_id": close_ord_id,
                    })
                    if self.db:
                        try:
                            await self.db.save_trade(
                                bot_id=self.BOT_ID, side="sell" if pos.side == "long" else "buy",
                                sz=pos.size, px=round(fill_px, 2), ord_id=close_ord_id,
                                inst_id=pos.inst_id, ord_type="market",
                                fee=0.0, fee_ccy="USDT", pnl=round(pnl, 2),
                                state="filled", signal_id=pos.signal_id,
                            )
                            await self._sync_positions_db()
                        except Exception as e:
                            print(f"[Rotation] DB reconcile save error: {e}", flush=True)
                    print(f"[Rotation] RECONCILE {now[:19]} {coin:4} {pos.side:5} "
                          f"gone from exchange, booked {close_reason} pnl={pnl:+.2f}", flush=True)
                    self.analysis.log("rotation", "reconcile",
                                      coin=coin, side=pos.side,
                                      kind="position_gone", reason=close_reason,
                                      entry_px=round(pos.entry_price, 2),
                                      exit_px=round(fill_px, 2), pnl=round(pnl, 2))

                    if self.notifier:
                        try:
                            self.notifier.fire(self.notifier.close_msg(
                                coin=coin, side=pos.side,
                                entry=round(pos.entry_price, 2),
                                exit_px=round(fill_px, 2), pnl=round(pnl, 2),
                                reason=close_reason,
                                bot_name=self.BOT_NAME, signal_id=pos.signal_id,
                            ))
                        except Exception as e:
                            print(f"[Rotation] TG reconcile notify error: {e}", flush=True)
                    del self._positions[coin]
                elif real_sz != pos.size:
                    # Size drift (e.g. manual partial close on the exchange).
                    old = pos.size
                    pos.size = real_sz
                    pos.size_synced = 0.0  # force the exchange stop to re-sync
                    print(f"[Rotation] RECONCILE {coin:4} size {old} -> {real_sz} "
                          f"(exchange drift), stop will re-sync", flush=True)
                    self.analysis.log("rotation", "reconcile",
                                      coin=coin, side=pos.side,
                                      kind="size_drift", old_size=old, new_size=real_sz)
                    if self.db:
                        await self._sync_positions_db()

            # Restore positions that exist on the exchange but are not tracked
            # locally (e.g. after a failed close left the position open). Without
            # this, the next cycle would open a duplicate entry on the same coin.
            for (coin, side), real_sz in actual.items():
                if coin in self._positions:
                    continue
                inst_id = self.SWAP_MAP.get(coin, f"{coin}-USDT-SWAP")
                now = datetime.now(timezone.utc).isoformat()
                # Re-attach the original trade number so close/partial messages
                # keep the same "Сделка №N" as the open.
                restored_signal_id = 0
                if self.db:
                    try:
                        restored_signal_id = await self.db.find_signal_id(inst_id, side)
                    except Exception as e:
                        print(f"[Rotation] reconcile signal_id lookup error: {e}", flush=True)
                pos = RotPosition(
                    symbol=inst_id, coin=coin, inst_id=inst_id,
                    side=side, size=real_sz, size_original=real_sz,
                    entry_price=0.0, stop_price=0.0, peak_price=0.0,
                    opened_at=now, atr=0.0, atr_hourly=0.0,
                    leverage=self.config.max_leverage, signal_id=restored_signal_id,
                    raw_entry=0.0,
                )
                # Re-fetch the position to get avgPx + place a fresh exchange stop.
                pos_result = await client.get_positions("SWAP", inst_id=inst_id)
                if not pos_result.get("error") and pos_result.get("data"):
                    p = pos_result["data"][0]
                    pos.entry_price = float(p.get("avgPx", 0) or 0)
                    pos.peak_price = pos.entry_price
                    pos.leverage = float(p.get("lever", 0) or self.config.max_leverage)
                    est_atr = pos.entry_price * 0.015
                    pos.atr = est_atr
                    pos.stop_price = (pos.entry_price - est_atr * self.config.atr_stop_mult
                                      if side == "long"
                                      else pos.entry_price + est_atr * self.config.atr_stop_mult)
                if pos.entry_price > 0:
                    self._positions[coin] = pos
                    print(f"[Rotation] RECONCILE restored {coin} {side} sz={real_sz} "
                          f"@ {pos.entry_price:.2f}", flush=True)
                    self.analysis.log("rotation", "reconcile",
                                      coin=coin, side=side,
                                      kind="restored", size=real_sz,
                                      entry_px=round(pos.entry_price, 2))
                    await self._place_exchange_stop(client, pos)
                else:
                    print(f"[Rotation] RECONCILE could not restore {coin} {side} "
                          f"(no avgPx)", flush=True)
        except Exception as e:
            print(f"[Rotation] Reconcile error: {e}", flush=True)

    async def _sync_exchange_stops(self, client):
        """Verify exchange-side conditional stops and deduplicate them.

        Problems solved:
         * a trailing/breakeven re-place can briefly leave TWO live SLs on the
           same instrument+side (old not yet cancelled) — keep only the closest
           one and cancel the rest;
         * after a crash/restart there may be orphan stops for instruments that
           are no longer tracked — cancel them too so they never re-fire.
        """
        if not self._positions:
            return
        try:
            result = await client.get_algo_orders(ord_type="conditional", state="live")
            if result.get("error"):
                return
            stops = result.get("data", [])

            # Group live stops by instrument+side.
            by_key: dict = {}
            for s in stops:
                inst_id = s.get("instId", "")
                coin = inst_id.replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
                if coin not in self.config.symbols:
                    continue
                key = (inst_id, s.get("posSide", ""), s.get("side", ""))
                by_key.setdefault(key, []).append(s)

            for (inst_id, pos_side, side), group in by_key.items():
                if len(group) < 2:
                    continue
                # Keep the stop that protects the position most tightly.
                # For longs (sell stop) the highest trigger is the closest;
                # for shorts (buy stop) the lowest trigger is the closest.
                def _trigger(s):
                    try:
                        return float(s.get("slTriggerPx") or 0)
                    except (TypeError, ValueError):
                        return 0.0
                keep = max(group, key=_trigger) if side == "sell" else min(group, key=_trigger)
                keep_id = keep.get("algoId", "")
                for s in group:
                    if s.get("algoId") == keep_id:
                        continue
                    resp = await client.cancel_algo_order(inst_id, s.get("algoId", ""))
                    coin = inst_id.replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
                    if resp.get("error"):
                        print(f"[Rotation] Duplicate stop cancel error {coin}: "
                              f"{resp.get('message', '')}", flush=True)
                    else:
                        print(f"[Rotation] Cancelled duplicate stop {coin} "
                              f"sl={s.get('slTriggerPx', '')} (kept sl={keep.get('slTriggerPx', '')})",
                              flush=True)
                        self.analysis.log("rotation", "stop_dedup",
                                          coin=coin, pos_side=pos_side, side=side,
                                          cancelled_sl=float(_trigger(s) or 0),
                                          kept_sl=float(_trigger(keep) or 0))
        except Exception as e:
            print(f"[Rotation] Sync exchange stops error: {e}", flush=True)

    async def _check_and_trade(self):
        """Main logic: indicators, filters, ranking, rotate, manage stops."""
        client = await self._get_client()
        if not client:
            print("[Rotation] No OKX client available", flush=True)
            return

        # 0. Reconcile with real exchange state before touching positions.
        await self._reconcile_exchange_positions(client)
        # 0b. Verify + dedupe exchange-side stops (crash leftovers / trailing races).
        await self._sync_exchange_stops(client)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 1. Fetch data and compute indicators
        indicators = {}
        cfg = self.config

        for coin in list(cfg.symbols) + list(cfg.regime_symbols or []):
            try:
                candles_d = await self._fetch_daily(client, coin, limit=250)
                if not candles_d:
                    continue
                ind = self._compute_daily_indicators(candles_d)
                if ind:
                    indicators[coin] = ind

                # BTC 200-day MA (for long-only filter)
                if coin == "BTC" or coin in (cfg.regime_symbols or []):
                    self._btc_200ma = self._compute_btc_200ma(candles_d)
                    self._regime = self._get_regime(candles_d)
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
                    self.analysis.log("rotation", "trail",
                                      coin=pos.coin, side=pos.side,
                                      price=round(current_price, 2),
                                      peak=round(pos.peak_price, 2),
                                      new_stop=round(pos.stop_price, 2))
                # Breakeven after 3%
                if not pos.breakeven and current_price >= pos.entry_price * (1 + cfg.breakeven_pct):
                    pos.stop_price = max(pos.stop_price, pos.entry_price * 0.999)
                    pos.breakeven = True
                    self.analysis.log("rotation", "breakeven",
                                      coin=pos.coin, side=pos.side,
                                      price=round(current_price, 2),
                                      entry=round(pos.entry_price, 2),
                                      stop=round(pos.stop_price, 2))
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
                    self.analysis.log("rotation", "trail",
                                      coin=pos.coin, side=pos.side,
                                      price=round(current_price, 2),
                                      peak=round(pos.peak_price, 2),
                                      new_stop=round(pos.stop_price, 2))
                if not pos.breakeven and current_price <= pos.entry_price * (1 - cfg.breakeven_pct):
                    pos.stop_price = min(pos.stop_price, pos.entry_price * 1.001)
                    pos.breakeven = True
                    self.analysis.log("rotation", "breakeven",
                                      coin=pos.coin, side=pos.side,
                                      price=round(current_price, 2),
                                      entry=round(pos.entry_price, 2),
                                      stop=round(pos.stop_price, 2))
                if not pos.partial_done and current_price <= pos.entry_price * (1 - cfg.partial_tp_pct):
                    await self._close_partial(client, pos.inst_id, pos, cfg.partial_tp_ratio)
                if current_price >= pos.stop_price:
                    hit_stop = True

            if hit_stop:
                await self._cancel_exchange_stop(client, pos)
                await self._close_position(client, pos.inst_id, pos, reason)
                del self._positions[coin]
            elif pos.algo_id and (pos.stop_price != pos.stop_synced or pos.size != pos.size_synced):
                # Stop moved (and/or size changed after partial TP) → sync exchange SL.
                # Only re-place when it actually changed (avoids API spam every poll).
                await self._update_exchange_stop(client, pos)

            # ── Dynamic ROI exit (после частичного TP; чем дольше держим, тем ниже TP) ──
            if not hit_stop and current_price > 0 and pos.entry_price > 0:
                pnl = (current_price / pos.entry_price - 1) if pos.side == "long" \
                    else (pos.entry_price / current_price - 1)
                hold_days = (datetime.now(timezone.utc) - datetime.fromisoformat(pos.opened_at)).days
                tp = self._roi_target(hold_days)
                if pnl >= tp and pnl > 0:
                    self.analysis.log("rotation", "roi_exit",
                                      coin=pos.coin, side=pos.side,
                                      price=round(current_price, 2),
                                      entry=round(pos.entry_price, 2),
                                      hold_days=hold_days, tp=round(tp, 4),
                                      pnl_pct=round(pnl * 100, 2))
                    await self._cancel_exchange_stop(client, pos)
                    await self._close_position(client, pos.inst_id, pos, "roi")
                    del self._positions[coin]
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
        regime = getattr(self, "_regime", "unknown")

        for coin, ind in indicators.items():
            if coin in (cfg.regime_symbols or []):
                continue  # regime-only coin, never traded
            if ind["atr"] <= 0:
                continue

            # ── FILTER: Volatility ──
            if ind["avg_atr_30"] > 0:
                if ind["atr"] > ind["avg_atr_30"] * cfg.vol_mult:
                    print(f"[Rotation] Vol filter: {coin} ATR={ind['atr']:.1f} > "
                          f"avg30*{cfg.vol_mult}={ind['avg_atr_30'] * cfg.vol_mult:.1f} -> SKIP",
                          flush=True)
                    self.analysis.log("rotation", "filter",
                                      coin=coin, filter="vol",
                                      atr=round(ind["atr"], 2),
                                      avg_atr_30=round(ind["avg_atr_30"], 2),
                                      threshold=round(ind["avg_atr_30"] * cfg.vol_mult, 2),
                                      decision="skip")
                    continue

            # ── FILTER: RSI ──
            if ind["rsi"] > cfg.rsi_long_max and ind["ema_trend"]:
                print(f"[Rotation] RSI filter: {coin} RSI={ind['rsi']:.1f} > {cfg.rsi_long_max} -> no long", flush=True)
                self.analysis.log("rotation", "filter",
                                  coin=coin, filter="rsi",
                                  rsi=round(ind["rsi"], 2), threshold=cfg.rsi_long_max,
                                  decision="no_long")
                continue
            if ind["rsi"] < cfg.rsi_short_min and not ind["ema_trend"]:
                print(f"[Rotation] RSI filter: {coin} RSI={ind['rsi']:.1f} < {cfg.rsi_short_min} -> no short", flush=True)
                self.analysis.log("rotation", "filter",
                                  coin=coin, filter="rsi",
                                  rsi=round(ind["rsi"], 2), threshold=cfg.rsi_short_min,
                                  decision="no_short")
                continue

            # ── FILTER: min |roc| ──
            if abs(ind["roc"]) < cfg.min_roc:
                print(f"[Rotation] min_roc filter: {coin} ROC={ind['roc']:.1f} < {cfg.min_roc} -> SKIP", flush=True)
                self.analysis.log("rotation", "filter",
                                  coin=coin, filter="min_roc",
                                  roc=round(ind["roc"], 2), threshold=cfg.min_roc,
                                  decision="skip")
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

        # 5. Determine target coins (режим-зависимое направление + correlation filter)
        target_coins = set()
        for coin, score, roc_val, ema_trend, adx_val, atr_val in ranked:
            if len(target_coins) >= cfg.top_k:
                break

            # Направление определяется рыночным режимом:
            #   bull → только лонги, bear → только шорты, chop → кэш
            if regime in ("bull", "unknown"):
                if roc_val > cfg.min_roc and ema_trend and adx_val >= cfg.adx_min:
                    side = "long"
                else:
                    continue
            elif regime == "bear":
                if cfg.allow_short and roc_val < -cfg.min_roc and not ema_trend \
                        and adx_val >= cfg.adx_min:
                    side = "short"
                else:
                    continue
            else:  # chop
                continue

            # ── FILTER: Correlation ──
            if not self._check_correlation(coin, indicators):
                continue

            # ── FILTER: Manual-close cooldown ──
            cd_until = self._cooldowns.get(coin, 0.0)
            if cd_until > time.time():
                print(f"[Rotation] Cooldown filter: {coin} — "
                      f"won't reopen until "
                      f"{datetime.fromtimestamp(cd_until, tz=timezone.utc).strftime('%H:%M')} "
                      f"(manual close)", flush=True)
                self.analysis.log("rotation", "filter",
                                  coin=coin, filter="cooldown",
                                  until=round(cd_until), decision="skip")
                continue

            target_coins.add((coin, side))

        self.analysis.log("rotation", "rotation",
                          regime=regime, ranked=len(ranked),
                          targets=[{"coin": c, "side": s} for c, s in target_coins])

        # 6. Full rotation: close positions not in target (only on daily check, not when filling slots)
        if slots_full:
            for coin in list(self._positions.keys()):
                pos = self._positions[coin]
                if (coin, pos.side) not in target_coins:
                    self.analysis.log("rotation", "rotation_exit",
                                      coin=coin, side=pos.side,
                                      entry_px=round(pos.entry_price, 2),
                                      size=pos.size, leverage=pos.leverage)
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
            await self._load_cooldowns()
        await self._sync_open_positions()
        self._thread = threading.Thread(target=self._thread_target, daemon=True)
        self._thread.start()
        print(f"[Rotation {STRATEGY_VERSION}] Started (capital=${self._equity:,.0f}, poll={self.config.poll_interval_sec}s)",
              flush=True)

    async def _load_cooldowns(self):
        """Restore persisted manual-close cooldowns after a restart."""
        if not self.db:
            return
        try:
            rows = await self.db._fetchall(
                "SELECT key, value FROM settings WHERE key LIKE $1"
                if self.db._pg_mode else
                "SELECT key, value FROM settings WHERE key LIKE ?",
                (f"cooldown:{self.BOT_ID}:%",))
            for r in rows:
                coin = (r.get("key") or "").rsplit(":", 1)[-1]
                try:
                    until = float(r.get("value") or 0)
                except (TypeError, ValueError):
                    continue
                if until > time.time():
                    self._cooldowns[coin] = until
                    print(f"[Rotation] Restored cooldown {coin} until "
                          f"{datetime.fromtimestamp(until, tz=timezone.utc).strftime('%H:%M')}",
                          flush=True)
        except Exception as e:
            print(f"[Rotation] Load cooldowns error: {e}", flush=True)

    async def stop(self):
        self._running = False
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self.db:
            try:
                await self.db.update_bot_stopped(self.BOT_ID)
            except Exception:
                pass
        print(f"[Rotation {STRATEGY_VERSION}] Stopped", flush=True)

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
            ct = self.CT_VAL.get(coin, 0.01)
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
                "side": pos.side, "size": round(pos.size, 6), "size_remaining": round(pos.size, 6),
                "size_original": round(pos.size_original, 6),
                "entry": pos.entry_price, "entry_price": pos.entry_price,
                "stop": round(pos.stop_price, 2), "stop_price": round(pos.stop_price, 2),
                "tp1": round(tp1_price, 2), "be_price": round(be_price, 2),
                "peak_price": round(pos.peak_price, 2),
                "breakeven": pos.breakeven, "partial_done": pos.partial_done,
                "opened_at": pos.opened_at,
                "mark_px": round((self._latest_indicators.get(coin) or {}).get("close_today", pos.entry_price), 2),
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
            "strategy": self.STRATEGY_NAME,
            "version": self.STRATEGY_VERSION,
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
            "entry_estimates": self._entry_estimates(full_equity),
            "filters": filters_active,
            "btc_200ma": round(self._btc_200ma, 2) if self._btc_200ma else None,
            "started_at": self._started_at,
            "description": self.STRATEGY_DESC,
        }

    def _evaluate_entry(self, coin: str, ind: dict) -> tuple:
        """Оценка монеты фильтрами стратегии (как в _check_and_trade).

        Возвращает (passed: bool, side: str, reason: str). Используется
        и для реального открытия позиций, и для отображения на карточке,
        чтобы «вход» показывался только для реально проходных сигналов.
        """
        cfg = self.config
        regime = getattr(self, "_regime", "unknown")

        if ind.get("atr", 0) <= 0:
            return False, "", "нет ATR"

        # ── FILTER: Volatility ──
        if ind.get("avg_atr_30", 0) > 0 and ind["atr"] > ind["avg_atr_30"] * cfg.vol_mult:
            return False, "", f"vol ATR>{ind['avg_atr_30'] * cfg.vol_mult:.0f}"

        # ── FILTER: RSI ──
        if ind["rsi"] > cfg.rsi_long_max and ind["ema_trend"]:
            return False, "", f"RSI {ind['rsi']:.0f}>{cfg.rsi_long_max}"
        if ind["rsi"] < cfg.rsi_short_min and not ind["ema_trend"]:
            return False, "", f"RSI {ind['rsi']:.0f}<{cfg.rsi_short_min}"

        # ── FILTER: min |roc| ──
        if abs(ind["roc"]) < cfg.min_roc:
            return False, "", f"|ROC| {ind['roc']:.1f}%<{cfg.min_roc}%"

        # ── Направление по рыночному режиму ──
        if regime in ("bull", "unknown"):
            if ind["roc"] > cfg.min_roc and ind["ema_trend"] and ind["adx"] >= cfg.adx_min:
                side = "long"
            else:
                return False, "", "нет лонг-условий (ROC/ADX/тренд)"
        elif regime == "bear":
            if cfg.allow_short and ind["roc"] < -cfg.min_roc \
                    and not ind["ema_trend"] and ind["adx"] >= cfg.adx_min:
                side = "short"
            else:
                return False, "", "нет шорт-условий (ROC/ADX/тренд)"
        else:  # chop
            return False, "", "режим chop (кэш)"

        # ── FILTER: Correlation ──
        if not self._check_correlation(coin, self._latest_indicators):
            return False, "", "корреляция с открытой позицией"

        return True, side, ""

    def _entry_estimates(self, equity: float) -> dict:
        """Ориентировочная стоимость входа (маржа) по каждой монете.

        Показывает вход ТОЛЬКО для монет, прошедших фильтры стратегии
        (passed=True). Остальные приходят с blocked=True + причиной, чтобы
        карточка не вводила в заблуждение.
        """
        cfg = self.config
        out = {}
        for coin, ind in self._latest_indicators.items():
            passed, side, reason = self._evaluate_entry(coin, ind)
            price = ind.get("close_today", 0)
            atr = ind.get("atr", 0)

            base = {
                "price": round(price, 2) if price else 0,
                "blocked": not passed,
                "blocked_reason": reason if not passed else "",
            }

            if not passed:
                base["entry_price"] = None
                out[coin] = base
                continue
            if not price or price <= 0 or not atr or atr <= 0:
                base["blocked"] = True
                base["blocked_reason"] = "нет данных"
                base["entry_price"] = None
                out[coin] = base
                continue

            atr_pct = atr / price * 100
            stop_pct = (atr * cfg.atr_stop_mult) / price
            notional = (equity * cfg.risk_per_trade) / stop_pct
            lev = 1.0 / (2 * (atr / price))
            lev = max(1.0, min(lev, cfg.max_leverage))
            margin = notional / lev
            max_margin = equity * cfg.allocation_pct
            margin = min(margin, max_margin)
            out[coin] = {
                **base,
                "side": side,
                "entry_price": round(price, 2),
                "atr_pct": round(atr_pct, 2),
                "stop_pct": round(stop_pct * 100, 2),
                "notional": round(notional, 0),
                "margin": round(margin, 0),
                "leverage": round(lev, 1),
            }
        return out

    def _calc_unrealized(self, coin: str) -> float:
        pos = self._positions.get(coin)
        ind = self._latest_indicators.get(coin)
        if not pos or not ind:
            return 0.0
        current = ind["close_today"]
        ct = self.CT_VAL.get(coin, 0.01)
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
                await self.db._execute("DELETE FROM positions WHERE bot_id = $1", (self.BOT_ID,))
            else:
                await self.db._execute("DELETE FROM positions WHERE bot_id = ?", (self.BOT_ID,))
            for coin, pos in self._positions.items():
                await self.db.save_position(
                    bot_id=self.BOT_ID, inst_id=pos.inst_id,
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
                    "VALUES ($1, 'rotation', $2, 'MULTI', '1D', "
                    "$3, $4, 'running', 'demo', 'momentum', $5, $6) "
                    "ON CONFLICT (id) DO NOTHING",
                    (self.BOT_ID, self.STRATEGY_NAME, self._equity, str(params), now, self.BOT_NAME),
                )
            else:
                await self.db._execute(
                    "INSERT OR IGNORE INTO bots (id, strategy_id, strategy_code, symbol, timeframe, "
                    "capital, params, status, mode, signal_type, created_at, name) "
                    "VALUES (?, 'rotation', ?, 'MULTI', '1D', "
                    "?, ?, 'running', 'demo', 'momentum', ?, ?)",
                    (self.BOT_ID, self.STRATEGY_NAME, self._equity, str(params), now, self.BOT_NAME),
                )
        except Exception as e:
            print(f"[Rotation] DB ensure_bot error: {e}", flush=True)

    async def _reload_equity(self):
        if not self.db:
            return
        try:
            rows = await self.db.get_trades(bot_id=self.BOT_ID, limit=500)
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
                    "ord_id": str(t.get("ord_id", "") or "").strip(),
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

                # Re-attach the original trade number so close/partial messages
                # keep the same "Сделка №N" as the open before the restart.
                restored_signal_id = 0
                if self.db:
                    try:
                        restored_signal_id = await self.db.find_signal_id(inst_id, side)
                    except Exception as e:
                        print(f"[Rotation] restore signal_id lookup error: {e}", flush=True)

                pos = RotPosition(
                    symbol=inst_id, coin=coin, inst_id=inst_id,
                    side=side, size=sz, size_original=sz,
                    entry_price=entry_px, stop_price=stop_price,
                    peak_price=entry_px, opened_at=datetime.now(timezone.utc).isoformat(),
                    atr=estimated_atr, atr_hourly=estimated_atr,
                    leverage=self.config.max_leverage,
                    signal_id=restored_signal_id,
                )
                self._positions[coin] = pos
                await self._place_exchange_stop(client, pos)
                print(f"[Rotation] Restored {side.upper()} {coin} sz={sz} @ {entry_px:.2f} "
                      f"stop={stop_price:.2f}", flush=True)
            # Dedupe any leftover/duplicate stops from before the restart.
            await self._sync_exchange_stops(client)
        except Exception as e:
            print(f"[Rotation] Sync open positions error: {e}", flush=True)
# Deploy trigger 2026-08-03T20:19:04Z
