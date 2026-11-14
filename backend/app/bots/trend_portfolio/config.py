"""Configuration for the daily trend portfolio (paper-only).

Every knob is off/neutral by default and the whole module is gated by a single
env switch, ``TREND_PORTFOLIO_ENABLE``.  Nothing here can place an order: the
module has no exchange write path at all (see ``engine.py`` docstring).

Research basis (45 majors, Binance daily 2021-05 → 2026-11, 5.5y):
    long-only Donchian40 / exit20 / Chandelier 5xATR,
    risk 32% of a 1/45 sleeve (= 0.71% of the account per trade),
    sizing base = equity smoothed with a 365-day half-life.

HONEST performance (single account, this engine — validated 2026-11-14):
    CAGR +13.6%, maxDD 30.2%, Sharpe 1.02
    per year: 2021 +30, 2022 -19, 2023 -8, 2024 +150, 2025 +14, 2026 -5
    WITHOUT 2024: +1.3%/yr.  Block bootstrap: median +11.7%, p10 -2.4%,
    p90 +33.1%, P(5y loss) 17%, median maxDD 20.3%.

Do not quote 20-25% for this module: the earlier 21.6% came from a
per-sleeve-rebalanced model that a single exchange account cannot reproduce.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Tuple

# The universe the backtest was measured on.  Kept explicit (not "top-N by
# volume") so that a live run holds exactly what was validated.
DEFAULT_UNIVERSE: Tuple[str, ...] = (
    "BTC", "ETH", "BNB", "XRP", "DOGE", "ADA", "SOL", "DOT", "LINK", "LTC",
    "BCH", "AVAX", "TRX", "ETC", "XLM", "ALGO", "VET", "FIL", "ATOM", "ICP",
    "NEAR", "UNI", "AAVE", "MKR", "CRV", "SAND", "MANA", "AXS", "EOS", "NEO",
    "IOTA", "THETA", "GRT", "QNT", "APT", "ARB", "SUI", "INJ", "SEI", "TIA",
    "OP", "PEPE", "SHIB", "WAVES", "ZEC",
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


@dataclass
class TrendPortfolioConfig:
    """All parameters of the long-only daily breakout book."""

    # ── master switch ────────────────────────────────────────────────────
    enabled: bool = False          # TREND_PORTFOLIO_ENABLE (default OFF)

    # ── safety invariants ────────────────────────────────────────────────
    # This module can only ever simulate.  Kept as a field (not a constant) so
    # that a test can assert it is True and refuse to start otherwise.
    paper_only: bool = True

    # ── universe ─────────────────────────────────────────────────────────
    universe: Tuple[str, ...] = DEFAULT_UNIVERSE

    # ── signal (validated plateau, not a grid peak) ──────────────────────
    entry_lookback: int = 40       # close > max(high[-40:])
    exit_lookback: int = 20        # close < min(low[-20:])  → flat next open
    chandelier_mult: float = 5.0   # trailing stop = peak_close - 5*ATR
    atr_period: int = 14

    # ── sizing / risk ────────────────────────────────────────────────────
    capital: float = 10_000.0      # paper starting equity

    @property
    def sleeve_equity(self) -> float:
        return self.capital / max(len(self.universe), 1)

    # risk per trade as a fraction of ONE sleeve (1/N of the account).
    # 0.32 / 45 = 0.71% of the account per trade.
    risk_per_sleeve: float = 0.32
    max_sleeve_leverage: float = 3.0   # notional cap per sleeve (rarely binds)

    # Sizing base = account equity smoothed with this half-life (days).
    # Trend returns are year-clustered (+150% in 2024, -19% in 2022), so sizing
    # straight off realised equity systematically over-bets right after a good
    # year. Smoothing keeps Sharpe at ~1.0 instead of bleeding it to 0.88.
    sizing_halflife_days: float = 365.0

    # ── costs (conservative vs OKX retail) ───────────────────────────────
    fee_pct: float = 0.0008            # per side; OKX perp maker is 0.0002
    slippage_pct: float = 0.0002       # per side
    funding_pct_day: float = 0.00005   # 0.005%/day long perp ≈ 1.8%/yr

    # ── runtime ──────────────────────────────────────────────────────────
    bar: str = "1D"
    candle_limit: int = 300            # OKX /market/candles max per request
    warmup_bars: int = 90              # need > entry_lookback + atr_period
    poll_interval_sec: int = 3600      # check once an hour; acts on bar close

    # ── reporting ────────────────────────────────────────────────────────
    state_name: str = "trend_portfolio"
    max_equity_points: int = 2_000

    @property
    def risk_per_trade_pct(self) -> float:
        """Risk of one trade as a fraction of the whole account."""
        return self.risk_per_sleeve / max(len(self.universe), 1)

    def validate(self) -> None:
        if not self.paper_only:
            raise ValueError("trend_portfolio: paper_only must stay True")
        if not self.universe:
            raise ValueError("trend_portfolio: empty universe")
        if self.entry_lookback < 5 or self.exit_lookback < 2:
            raise ValueError("trend_portfolio: lookbacks too small")
        if self.exit_lookback >= self.entry_lookback:
            raise ValueError("trend_portfolio: exit window must be < entry window")
        if not (0 < self.risk_per_sleeve <= 1.0):
            raise ValueError("trend_portfolio: risk_per_sleeve must be (0, 1]")
        if self.chandelier_mult <= 0:
            raise ValueError("trend_portfolio: chandelier_mult must be > 0")
        if self.sizing_halflife_days <= 0:
            raise ValueError("trend_portfolio: sizing_halflife_days must be > 0")
        if self.capital <= 0:
            raise ValueError("trend_portfolio: capital must be > 0")
        if self.warmup_bars <= max(self.entry_lookback, self.atr_period):
            raise ValueError("trend_portfolio: warmup_bars too small")

    @classmethod
    def from_env(cls) -> "TrendPortfolioConfig":
        cfg = cls(
            enabled=_env_bool("TREND_PORTFOLIO_ENABLE", False),
            capital=_env_float("TREND_PORTFOLIO_CAPITAL", 10_000.0),
            risk_per_sleeve=_env_float("TREND_PORTFOLIO_RISK", 0.32),
            entry_lookback=_env_int("TREND_PORTFOLIO_ENTRY", 40),
            exit_lookback=_env_int("TREND_PORTFOLIO_EXIT", 20),
            chandelier_mult=_env_float("TREND_PORTFOLIO_CHANDELIER", 5.0),
            max_sleeve_leverage=_env_float("TREND_PORTFOLIO_MAX_LEV", 3.0),
            sizing_halflife_days=_env_float("TREND_PORTFOLIO_SIZING_HALFLIFE", 365.0),
            poll_interval_sec=_env_int("TREND_PORTFOLIO_POLL", 3600),
        )
        cfg.validate()
        return cfg
