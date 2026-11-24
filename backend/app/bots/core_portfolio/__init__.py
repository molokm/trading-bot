"""Core portfolio — two independent paper books over one universe.

* ``daily`` — Donchian 40/20 + Chandelier 5xATR, long-only, BTC 200d gate.
* ``h6``    — Donchian 80/40 (6h) + Chandelier 3.5xATR, long-only, same gate.

Half of the capital sits in each book and the two never rebalance into each
other, exactly as validated.  See ``config.py`` for the honest numbers and
``engine6h.py`` for the causality contract.  This package never places an
order: it reads public candles and simulates.
"""
from app.bots.core_portfolio.config import CorePortfolioConfig
from app.bots.core_portfolio.engine6h import (
    Sleeve6,
    Trend6hBook,
    feed_chronological as feed_6h_chronological,
    gate_map,
)

__all__ = [
    "CorePortfolioConfig",
    "Sleeve6",
    "Trend6hBook",
    "feed_6h_chronological",
    "gate_map",
]
