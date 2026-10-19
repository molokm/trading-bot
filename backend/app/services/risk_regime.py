"""Simple regime detection for risk sizing.

The module provides a light‑weight heuristic to classify a market regime
(bull, bear, sideways) based on EMA20 vs price. It is deliberately kept
dependency‑free – only ``pandas`` is used as the project already depends on it
for data handling elsewhere.

The function ``get_regime`` returns a string identifier that can be used by
risk logic to apply a size multiplier.
"""

from __future__ import annotations

import os
from typing import Literal

import pandas as pd

Regime = Literal["bull", "bear", "sideways"]


def _f(name: str, default: float) -> float:
    """Read float env var with fallback."""
    try:
        return float(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default

# Multipliers configurable via environment – default values match current
# risk policy (no scaling).
BULL_MULT: float = _f("RISK_REGIME_MULT_BULL", 1.0)
BEAR_MULT: float = _f("RISK_REGIME_MULT_BEAR", 0.5)
SIDEWAYS_MULT: float = _f("RISK_REGIME_MULT_SIDEWAYS", 0.3)


def get_regime(prices: pd.Series) -> Regime:
    """Return market regime based on recent price action.

    * ``prices`` – a time‑ordered series of closing prices (most recent last).
    * Uses EMA20 and simple price change over the same window.
    * If EMA20 > price and price trending up → *bull*.
    * If EMA20 < price and price trending down → *bear*.
    * Otherwise → *sideways*.
    """
    if prices.empty:
        return "sideways"
    ema = prices.ewm(span=20, adjust=False).mean()
    # latest values
    price_now = prices.iloc[-1]
    ema_now = ema.iloc[-1]
    # recent delta (last 5 periods)
    delta = price_now - prices.iloc[-min(5, len(prices))]
    if ema_now > price_now and delta > 0:
        return "bull"
    if ema_now < price_now and delta < 0:
        return "bear"
    return "sideways"


def regime_multiplier(regime: Regime) -> float:
    """Map regime to size multiplier using env‑configured values."""
    if regime == "bull":
        return BULL_MULT
    if regime == "bear":
        return BEAR_MULT
    return SIDEWAYS_MULT
