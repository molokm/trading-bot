"""ValidationStrategy — MACD + Donchian breakout.

Реализация бэктест-конфига MACD+Donchian (см. external/scripts/honest_backtest_macd_donchian.py).
Лучший прогон (dc15 / tp 8% на 30% / tp2 10% / breakeven +1.5% / max_hold 3 дня / top_k 4 @ 1x):
CAGR ~121%, Sharpe ~1.81, MaxDD ~39%; walk-forward 2024/2025 стабилен (CAGR 219%/113%).

Вход: Donchian breakout (close > 15-дневный максимум) + MACD hist > 0.
Выходы: chandelier 4*ATR, breakeven при +1.5% (для ВСЕХ позиций), частичный TP 8% на 30%,
второй TP 10%, ротация.
"""

from .macd_donchian_strategy import MacdDonchianStrategy, MacdDonchianConfig
from .rotation_strategy import (
    RotationStrategy,
    ROT_BOT_ID,
)

VAL_BOT_ID = "validation_strategy"
VAL_VERSION = "v1"
VAL_STRATEGY_NAME = f"macd_donchian_validation_{VAL_VERSION}"
VAL_BOT_NAME = "MACD+Donchian Validation v1"

# Вселенная бэктеста: BTC, ETH, BNB, SOL (daily OHLC, прокси OKX SWAP).
VAL_COINS = ["BTC", "ETH", "BNB", "SOL"]

# ctVal / lotSz / тик проверены через OKX (market_get_instruments, 2026-08-12).
VAL_CT_VAL = {
    "BTC": 0.01, "ETH": 0.1, "BNB": 0.01, "SOL": 1,
}
VAL_LOT_SZ = {
    "BTC": 0.01, "ETH": 0.01, "BNB": 1, "SOL": 0.01,
}
VAL_SWAP_MAP = {
    coin: f"{coin}-USDT-SWAP" for coin in VAL_COINS
}
VAL_PX_DECIMALS = 4

VAL_DESC = (
    "MACD+Donchian Validation v1: Donchian breakout (close > 15-дневный максимум) "
    "с подтверждением MACD-гистограммы > 0. Выходы: chandelier 4×ATR, breakeven при +1.5% "
    "для всех позиций, частичный тейк 8% (30% позиции), второй тейк 10%, max_hold 3 дня, "
    "top_k 4 @ 1×. Бэктест (daily OHLC BTC/ETH/BNB/SOL, 2023–2026): CAGR ~121%, Sharpe ~1.81, "
    "MaxDD ~39%, walk-forward 2024/2025 стабилен."
)


def make_validation_config(
    capital: float = 300.0,
    top_k: int = 4,
    donchian_n: int = 15,
    tp_pct: float = 0.08,
    tp_ratio: float = 0.3,
    tp2_pct: float = 0.10,
    be_pct: float = 0.015,
    chandelier_atr: float = 4.0,
    max_hold_days: int = 3,
    risk_per_trade: float = 0.14,
    allocation_pct: float = 0.15,
    max_leverage: float = 1.0,
    poll_interval_sec: int = 300,
    auto_execute: bool = True,
) -> MacdDonchianConfig:
    cfg = MacdDonchianConfig(
        symbols=list(VAL_COINS),
        regime_symbols=[],
        capital=capital,
        top_k=top_k,
        donchian_n=donchian_n,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        chandelier_atr=chandelier_atr,
        hard_stop_atr=0.0,
        tp_pct=tp_pct,
        tp_ratio=tp_ratio,
        tp2_pct=tp2_pct,
        be_pct=be_pct,
        max_hold_days=max_hold_days,
        risk_per_trade=risk_per_trade,
        allocation_pct=allocation_pct,
        max_leverage=max_leverage,
        min_hold_days=1,
        atr_stop_mult=chandelier_atr,
        trail_atr_mult=chandelier_atr,
        breakeven_pct=be_pct,
        partial_tp_pct=tp_pct,
        partial_tp_ratio=tp_ratio,
        adx_min=25.0,
        min_roc=0.0,
        vol_mult=999.0,
        rsi_long_max=100.0,
        rsi_short_min=0.0,
        corr_threshold=1.0,
        allow_short=False,
        poll_interval_sec=poll_interval_sec,
        auto_execute=auto_execute,
        limit_offset_pct=0.001,
        limit_wait_sec=30,
        roi_table=[(0, tp2_pct)],
    )
    return cfg


class ValidationStrategy(MacdDonchianStrategy):
    BOT_ID = VAL_BOT_ID
    BOT_NAME = VAL_BOT_NAME
    CT_VAL = VAL_CT_VAL
    LOT_SZ = VAL_LOT_SZ
    SWAP_MAP = VAL_SWAP_MAP
    STRATEGY_NAME = VAL_STRATEGY_NAME
    STRATEGY_VERSION = VAL_VERSION
    STRATEGY_DESC = VAL_DESC
    PRICE_DECIMALS = VAL_PX_DECIMALS
    CL_ORD_PREFIX = "val"
