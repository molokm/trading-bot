"""Consolidated RETIRED multi-bot stubs (AI_ONLY product — stage 4).

All legacy strategy modules re-export from here so main.py keeps compiling
without shipping full strategy implementations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ── IDs / names ──────────────────────────────────────────────
ROT_BOT_ID = "rotation_strategy"
IMP_BOT_ID = "impulse_strategy"
VAL_BOT_ID = "validation_strategy"
AI_SCALE_BOT_ID = "ai_scale_strategy"
SCALP_BOT_ID = "orderbook_scalp"
VWAP_BOT_ID = "vwap_mean_rev"
SM_BOT_ID = "smart_money"
BOT_ID = SM_BOT_ID  # alias for smart_money_tracker.BOT_ID

STRATEGY_DESC = "retired"
IMPULSE_DESC = STRATEGY_NAME = IMPULSE_NAME = "Impulse 1D"
IMPULSE_VERSION = STRATEGY_VERSION = "off"
AI_SCALE_NAME = "AI Scale-In 1H (retired)"
SCALP_NAME = "Order Book Scalp"
SCALP_VERSION = "off"
SCALP_DESC = "retired"
VWAP_NAME = "VWAP Mean Reversion"
VWAP_VERSION = "off"
VWAP_DESC = "retired"
SM_NAME = STRATEGY_NAME_SM = "Smart Money"
SM_VERSION = "off"

COINS = ["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"]


class _BaseConfig:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class RotationConfig(_BaseConfig):
    pass


class ImpulseConfig(_BaseConfig):
    pass


class AIScaleConfig(_BaseConfig):
    pass


class ScalpConfig(_BaseConfig):
    pass


VWAPScalpConfig = ScalpConfig


def make_validation_config(**kw):
    return type("ValidationConfig", (), kw)()


@dataclass
class RotPosition:
    coin: str = ""
    inst_id: str = ""
    side: str = "long"
    size: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0
    take_price: float = 0.0
    leverage: float = 1.0
    opened_at: str = ""


class _RetiredBot:
    def __init__(self, *a, **k):
        self._running = False
        self._positions: dict = {}
        self._trade_log: list = []
        self._lifetime_pnl = 0.0
        self.config = k.get("config") or _BaseConfig()

    def start(self):
        pass

    async def start_async(self):
        pass

    def stop(self):
        pass

    async def stop_async(self):
        pass

    def get_status(self):
        return {"running": False, "retired": True}


class RotationStrategy(_RetiredBot):
    pass


class ImpulseStrategy(_RetiredBot):
    pass


class ValidationStrategy(_RetiredBot):
    pass


class AIScaleStrategy(_RetiredBot):
    def start(self):
        print("[AI Scale-In] retired — start ignored", flush=True)

    def get_status(self):
        return {"running": False, "strategy": AI_SCALE_NAME, "retired": True}


class OrderBookScalpStrategy(_RetiredBot):
    pass


class VWAPMeanReversion(_RetiredBot):
    def get_status(self):
        return {"running": False, "strategy": VWAP_NAME, "version": "off", "retired": True}


def compute_book_metrics(*a, **k):
    return {}


# ── Smart Money ──────────────────────────────────────────────
class TrackerConfig(_BaseConfig):
    pass


class OKXCopyAPI:
    def __init__(self, *a, **k):
        pass

    async def request(self, *a, **k):
        return {"data": []}

    async def get_leaderboards(self, *a, **k):
        return []

    async def get_positions(self, *a, **k):
        return []


class SmartMoneyTracker(_RetiredBot):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._targets = {}
        self._running = False

    def get_status(self):
        return {"running": False, "retired": True, "tracked": 0}


def get_mirror(*a, **k):
    return None
