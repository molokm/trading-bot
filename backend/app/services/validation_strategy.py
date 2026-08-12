"""ValidationStrategy — переопределённая копия RotationStrategy v4 с ослабленными
фильтрами, отдельным bot_id и непересекающимся набором монет.

Цель: принудительно открыть сделки на демо-счёте, чтобы проверить исполнительный
механизм (рыночные/лимитные ордера, биржевые стопы, trailing, partial TP, roi-exit,
реконсиляция) и не трогать работающие Momentum/Impulse стратегии.

ВНИМАНИЕ: ослабленные фильтры (min_roc, adx_min и т.п.) генерируют много сделок.
Использовать только на ДЕМО для валидации исполнения, НЕ для реальной торговли.
"""

from .rotation_strategy import (
    RotationStrategy,
    RotationConfig,
    ROT_BOT_ID,
)

VAL_BOT_ID = "validation_strategy"
VAL_VERSION = "v1"
VAL_STRATEGY_NAME = f"momentum_validation_{VAL_VERSION}"
VAL_BOT_NAME = "Momentum Validation v1"

# Монеты валидатора — не пересекаются с BTC/ETH/BNB/XRP/SOL/DOGE/ADA/TRX/AVAX/LTC
# у Momentum/Impulse, т.к. один аккаунт OKX.
# ВАЖНО: доступны в demo-режиме OKX. APT/UNI/MKR существуют в live, но НЕ в demo
# (x-simulated-trading=1) — исключены.
VAL_COINS = ["ARB", "OP", "FIL", "SUI", "ATOM", "LINK", "DOT", "NEAR", "PEPE"]

# ctVal / lotSz / тик проверены через OKX (market_get_instruments, 2026-08-12).
VAL_CT_VAL = {
    "ARB": 10, "OP": 1, "FIL": 0.1, "SUI": 1,
    "ATOM": 1, "LINK": 1, "DOT": 1, "NEAR": 10, "PEPE": 10_000_000,
}
VAL_LOT_SZ = {
    "ARB": 0.1, "OP": 1, "FIL": 1, "SUI": 1,
    "ATOM": 1, "LINK": 0.01, "DOT": 0.1, "NEAR": 0.1, "PEPE": 0.1,
}
VAL_SWAP_MAP = {
    coin: f"{coin}-USDT-SWAP" for coin in VAL_COINS
}
# Максимальное число знаков после запятой для цены ордера/стопа.
# PEPE имеет тик 1e-9, поэтому 2 знаков (как у базовой стратегии) не хватит.
VAL_PX_DECIMALS = 10

VAL_DESC = (
    "Валидатор исполнительного механизма (демо): копия Momentum Rotation v4 с "
    "ослабленными фильтрами (min_roc=1.5, adx_min=18, top_k=1, min_hold_days=1) "
    "на непересекающемся наборе монет (ARB, OP, FIL, SUI, ATOM, LINK, DOT, NEAR, PEPE). "
    "Принудительно открывает сделки, чтобы проверить ордера, биржевые стопы, "
    "трейлинг, частичный тейк и закрытие. НЕ для реальной торговли."
)


def make_validation_config(
    capital: float = 300.0,
    top_k: int = 1,
    min_roc: float = 1.5,
    adx_min: float = 18.0,
    min_hold_days: int = 1,
    max_leverage: float = 2.0,
    risk_per_trade: float = 0.14,
    allocation_pct: float = 0.15,
    poll_interval_sec: int = 300,
    auto_execute: bool = True,
) -> RotationConfig:
    cfg = RotationConfig(
        symbols=list(VAL_COINS),
        capital=capital,
        top_k=top_k,
        roc_period=14,
        ema_fast=20,
        ema_slow=50,
        atr_period=14,
        adx_min=adx_min,
        min_roc=min_roc,
        sma_long=200,
        sma_regime=50,
        min_hold_days=min_hold_days,
        max_leverage=max_leverage,
        risk_per_trade=risk_per_trade,
        allocation_pct=allocation_pct,
        atr_stop_mult=2.7,
        trail_atr_mult=0.2,
        breakeven_pct=0.05,
        partial_tp_pct=0.08,
        partial_tp_ratio=0.5,
        rsi_period=14,
        rsi_long_max=82.0,
        rsi_short_min=21.0,
        vol_mult=1.8,
        corr_threshold=0.99,
        allow_short=True,
        limit_offset_pct=0.001,
        limit_wait_sec=30,
        poll_interval_sec=poll_interval_sec,
        auto_execute=auto_execute,
    )
    # Динамический ROI: для ускоренной валидации закрываем при +5% уже на 1-й день.
    cfg.roi_table = [
        (1, 0.05),
        (0, 0.08),
    ]
    return cfg


class ValidationStrategy(RotationStrategy):
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