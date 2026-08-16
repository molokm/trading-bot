"""MacdDonchianStrategy — MACD + Donchian breakout + partial TP + breakeven.

Реализация бэктест-конфига MACD+Donchian (external/scripts/honest_backtest_macd_donchian.py,
лучший прогон: dc15 / tp 8% на 30% / tp2 10% / breakeven при +1.5% / max_hold 3 дня /
top_k 4 @ 1x: CAGR ~121%, Sharpe ~1.81, MaxDD ~39%, walk-forward 2024/2025 стабилен).

Вход: Donchian breakout (close > N-дневный максимум без текущего бара) И MACD hist > 0.
Выходы: chandelier-trailing (peak - N*ATR), breakeven-триггер при +be_pct для ВСЕХ
позиций, частичный TP (tp_pct на tp_ratio), второй TP (tp2_pct), ротация.

Класс наследует исполнительную машинерию RotationStrategy (ордера, биржевые стопы,
частичный TP, reconcile, DB, Telegram-нотификации) и переопределяет только сигналы.
BOT_ID сохраняет "validation_strategy", чтобы существующая интеграция (маршруты,
дашборд, Telegram, DB) продолжала работать без переделки.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .rotation_strategy import RotationStrategy, RotationConfig

MACD_BOT_ID = "validation_strategy"  # сохраняем bot_id валидатора


@dataclass
class MacdDonchianConfig(RotationConfig):
    # ── Сигнал (отличается от Momentum Rotation) ──
    donchian_n: int = 15          # пробой N-дневного максимума
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    chandelier_atr: float = 4.0   # trailing = peak - N*ATR (никогда не опускается)
    hard_stop_atr: float = 0.0    # жёсткий стоп = entry - N*ATR (0 = выключен)
    # ── Выходы ──
    tp_pct: float = 0.08          # первый TP: закрыть tp_ratio позиции
    tp_ratio: float = 0.3
    tp2_pct: float = 0.10         # второй TP для остатка
    be_pct: float = 0.015         # breakeven-триггер для ВСЕХ позиций после +1.5%
    max_hold_days: int = 3        # принудительный выход по времени
    # ── Размер ──
    inverse_vol_target: float = 0.20   # неиспользуется (риск-based размер), оставлен для совместимости
    max_notional_pct: float = 1.0      # неиспользуется, оставлен для совместимости
    # ── Нейтрализуем фильтры Momentum, чтобы работал только MACD+Donchian сигнал ──
    min_roc: float = 0.0          # roc = MACD histogram, пропускаем по знаку
    adx_min: float = 25.0         # adx мапится в константу выше порога
    vol_mult: float = 999.0       # отключаем vol-фильтр (в бэктесте его нет)
    rsi_long_max: float = 100.0   # отключаем RSI-фильтры
    rsi_short_min: float = 0.0
    corr_threshold: float = 1.0   # отключаем корреляционный фильтр
    allow_short: bool = False     # бэктест long-only
    min_hold_days: int = 1        # ротация почти мгновенная (как в бэктесте)
    atr_stop_mult: float = 4.0    # initial stop = price - chandelier_atr * ATR
    trail_atr_mult: float = 4.0   # chandelier: peak - 4*ATR
    breakeven_pct: float = 0.015  # be_pct
    partial_tp_pct: float = 0.08  # tp_pct
    partial_tp_ratio: float = 0.3 # tp_ratio
    sma_long: int = 200
    sma_regime: int = 50
    # Второй TP: остаток закрывается при +10% (после частичного TP).
    # Базовый движок ждёт tp2_pct в поле partial_tp_pct? Нет — через roi_table.
    # _roi_target(hold_days) возвращает первый порог из roi_table, где hold_days >= min_hold.
    roi_table: list = field(default_factory=lambda: [(0, 0.10)])


class MacdDonchianStrategy(RotationStrategy):
    BOT_ID: str = MACD_BOT_ID

    @staticmethod
    def macd(closes, fast=12, slow=26, signal=9):
        """MACD line/signal/histogram (EMA-based)."""
        e_fast = RotationStrategy.ema(closes, fast)
        e_slow = RotationStrategy.ema(closes, slow)
        line = [e_fast[i] - e_slow[i] for i in range(len(closes))]
        sig = RotationStrategy.ema(line, signal)
        hist = [line[i] - sig[i] for i in range(len(closes))]
        return line, sig, hist

    @staticmethod
    def donchian_high(highs, period):
        """N-day highest high EXCLUDING current bar (causal, no look-ahead)."""
        n = len(highs)
        out = [0.0] * n
        if n < period:
            return out
        for i in range(period, n):
            out[i] = max(highs[i - period:i])
        return out

    def _compute_daily_indicators(self, candles: list) -> dict:
        """MACD + Donchian signal bar = yesterday (i = len-2).

        Возвращает словарь, совместимый с остальным движком:
          roc        = MACD histogram (положительный → лонг)
          ema_trend  = Donchian breakout (close > N-day high)
          adx        = константа выше adx_min (фильтр всегда проходит)
          rsi        = нейтральное значение (фильтры отключены)
          avg_atr_30 = ATR (vol-фильтр нейтрален, vol_mult=999)
        """
        cfg = self.config
        if len(candles) < 70:
            return None
        closes = [c["C"] for c in candles]
        highs = [c["H"] for c in candles]
        lows = [c["L"] for c in candles]

        atr_arr = self.atr(highs, lows, closes, cfg.atr_period)
        _, _, hist = self.macd(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
        dc = self.donchian_high(highs, cfg.donchian_n)

        i = len(candles) - 2  # signal bar = yesterday
        if i < cfg.ema_slow + 10:
            return None

        h = hist[i]
        atr_val = atr_arr[i]

        atr_30_start = max(0, i - 30)
        atr_values = [atr_arr[j] for j in range(atr_30_start, i + 1) if atr_arr[j] > 0]
        avg_atr_30 = sum(atr_values) / len(atr_values) if atr_values else 0.0

        daily_returns = []
        for j in range(1, i + 1):
            if closes[j - 1] > 0:
                daily_returns.append((closes[j] / closes[j - 1]) - 1)

        breakout = closes[i] > dc[i]

        return {
            "roc": round(h * 100.0, 6),        # MACD histogram → proxy momentum
            "ema_fast": closes[i],
            "ema_slow": dc[i],                 # donchian high как "slow" для тренда
            "ema_trend": breakout,             # Donchian breakout
            "adx": 30.0,                       # константа выше adx_min
            "rsi": 50.0,                       # нейтрально
            "atr": atr_val,
            "avg_atr_30": avg_atr_30,
            "price": closes[i],
            "close_today": closes[-1],
            "daily_returns": daily_returns,
            "date": candles[i]["datetime"].strftime("%Y-%m-%d"),
            "date_today": candles[-1]["datetime"].strftime("%Y-%m-%d"),
            # ── специфичные для MACD+Donchian поля (карточка/дашборд) ──
            "donchian_high": round(dc[i], 4),
            "macd_hist": round(h, 6),
            "breakout": breakout,
        }

    def _get_regime(self, candles: list) -> str:
        """Стратегия long-only без режима — всегда bull (вход по breakout+MACD)."""
        return "bull"

    async def _check_and_trade(self):
        """Базовый движок + принудительный time-exit по max_hold_days.

        Базовый RotationStrategy не закрывает позиции по времени, поэтому после
        стандартной логики (trailing/breakeven/partial TP/ротация) добавляем
        проход, который закрывает позиции, держащиеся дольше max_hold_days.
        """
        await super()._check_and_trade()

        if not self._running:
            return
        client = await self._get_client()
        if not client:
            return

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        max_hold = getattr(self.config, "max_hold_days", 0)
        if not max_hold or max_hold <= 0:
            return

        for coin in list(self._positions.keys()):
            pos = self._positions[coin]
            try:
                opened = datetime.fromisoformat(pos.opened_at)
            except (TypeError, ValueError):
                continue
            hold_days = (datetime.now(timezone.utc) - opened).days
            if hold_days >= max_hold:
                self.analysis.log("rotation", "time_exit",
                                  coin=coin, side=pos.side,
                                  entry_px=round(pos.entry_price, 2),
                                  hold_days=hold_days,
                                  date=today_str)
                await self._cancel_exchange_stop(client, pos)
                await self._close_position(client, pos.inst_id, pos, "time_exit")
                del self._positions[coin]
