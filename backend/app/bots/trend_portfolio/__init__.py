"""Daily trend portfolio — paper-only long-only breakout book.

See ``engine.py`` for the rules and ``config.py`` for the honest, validated
performance numbers.  This package never places an order.
"""
from app.bots.trend_portfolio.config import (
    DEFAULT_UNIVERSE,
    TrendPortfolioConfig,
)
from app.bots.trend_portfolio.engine import (
    Bar,
    Position,
    TrendPortfolio,
    backtest,
)

__all__ = [
    "Bar",
    "Position",
    "TrendPortfolio",
    "TrendPortfolioConfig",
    "DEFAULT_UNIVERSE",
    "backtest",
]
