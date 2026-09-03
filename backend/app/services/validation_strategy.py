"""ValidationStrategy — MACD + Donchian breakout.

Реализация бэктест-конфига MACD+Donchian (см. external/backtests/backtrader_macd_donchian.py).
Бэктест на Backtrader, live-faithful (10 монет, OKX-SWAP daily, 2023–2026):
CAGR ~49.7%, Sharpe ~1.19, MaxDD ~48%; сигнал по закрытию предыдущего бара,
исполнение по открытию следующего (комиссия 0.1% + проскальзывание 0.05%).
Параметры оптимизированы sweep+walk-forward (2026-08-16).

Вход: Donchian breakout (close > 15-дневный максимум, без текущего бара) + MACD hist > 0.
Выходы: chandelier 4*ATR, breakeven при +1.5% (для ВСЕХ позиций), частичный TP 10% на 20%,
второй TP 8%, time-exit 3 дня, ротация.
"""

from .macd_donchian_strategy import MacdDonchianStrategy, MacdDonchianConfig
from .rotation_strategy import (
    RotationStrategy,
    ROT_BOT_ID,
)

VAL_BOT_ID = "validation_strategy"
VAL_VERSION = "v3"
VAL_STRATEGY_NAME = f"macd_donchian_validation_{VAL_VERSION}"
VAL_BOT_NAME = "MACD+Donchian Validation v3"

# Вселенная бэктеста: 10 монет, как у Momentum/Impulse (daily OHLC, прокси OKX SWAP).
VAL_COINS = ["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"]

# ctVal / lotSz / тик проверены через OKX (market_get_instruments, 2026-08-12).
VAL_CT_VAL = {
    "BTC": 0.01, "ETH": 0.1, "BNB": 0.01, "SOL": 1, "XRP": 100,
    "DOGE": 1000, "ADA": 100, "TRX": 1000, "AVAX": 1, "LTC": 1,
}
VAL_LOT_SZ = {
    "BTC": 0.01, "ETH": 0.01, "BNB": 1, "SOL": 0.01, "XRP": 0.01,
    "DOGE": 0.01, "ADA": 0.01, "TRX": 0.01, "AVAX": 0.1, "LTC": 0.1,
}
VAL_SWAP_MAP = {
    "BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "BNB": "BNB-USDT-SWAP",
    "SOL": "SOL-USDT-SWAP", "XRP": "XRP-USDT-SWAP", "DOGE": "DOGE-USDT-SWAP",
    "ADA": "ADA-USDT-SWAP", "TRX": "TRX-USDT-SWAP", "AVAX": "AVAX-USDT-SWAP",
    "LTC": "LTC-USDT-SWAP",
}
VAL_PX_DECIMALS = 4

VAL_DESC = (
    "MACD+Donchian Validation v3 (OOS-focused): Donchian 30 + MACD hist>0, top_k=2 @ 2×. "
    "Выходы: chandelier 4×ATR, BE +1.5%, partial +8%×40%, TP2 +8%, max_hold 3д. "
    "BT: CAGR ~24%, MaxDD −31%; OOS 2025-04→2026-08 ~+2% (v2 был −16%). "
    "Full-sample ниже v2 — обмен на выравнивание OOS. DEMO recommended."
)


def make_validation_config(
    capital: float = 300.0,
    top_k: int = 2,  # v3: fewer slots, OOS-focused
    donchian_n: int = 30,  # v3: slower breakout, OOS+
    tp_pct: float = 0.08,  # v2: earlier partial (better OOS vs 0.10)
    tp_ratio: float = 0.4,  # v3: larger first scale-out
    tp2_pct: float = 0.08,
    be_pct: float = 0.015,
    chandelier_atr: float = 4.0,
    max_hold_days: int = 3,
    risk_per_trade: float = 0.07,  # survival
    allocation_pct: float = 0.30,  # survival
    max_leverage: float = 2.0,
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
        adx_min=30.0,  # survival
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
