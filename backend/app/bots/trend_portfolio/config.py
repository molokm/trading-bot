"""Configuration for the daily trend portfolio (paper-only).

Every knob is off/neutral by default and the whole module is gated by a single
env switch, ``TREND_PORTFOLIO_ENABLE``.  Nothing here can place an order: the
module has no exchange write path at all (see ``engine.py`` docstring).

Research basis (45 majors, UTC daily bars, 2019-01 → 2026-11, 7.9y):
    long-only Donchian40 / exit20 / Chandelier 5xATR,
    risk 32% of a 1/45 sleeve (= 0.71% of the account per trade),
    sizing base = equity smoothed with a 365-day half-life.

HONEST performance (single account, this engine, re-measured 2026-11-26 on
Binance Vision, 0.16%/side).  Drawdown is **mark-to-market** — the equity an
account actually lives through, including open positions:

  BTC 200-day gate ON (the shipped profile):
       risk 0.16/sleeve (0.36%):  CAGR +17.5%, maxDD 21.4%, Sharpe 1.01
       risk 0.20/sleeve (0.44%):  CAGR +20.9%, maxDD 24.3%, Sharpe 1.03
       risk 0.32/sleeve (0.71%):  CAGR +29.7%, maxDD 31.4%, Sharpe 1.07
       risk 0.48/sleeve (1.07%):  CAGR +39.0%, maxDD 38.6%, Sharpe 1.09
       risk 0.64/sleeve (1.42%):  CAGR +46.3%, maxDD 44.0%, Sharpe 1.10
  Gate OFF at risk 0.64:          CAGR +45.0%, maxDD 49.5%, Sharpe 1.03

  Drawdown brake (new entries sized by 0.5 once the book is 20% below its
  mark-to-market high-water mark, released below 10%):
       risk 0.64 + brake:        CAGR +35.5%, maxDD 32.4%, Sharpe 1.04
       risk 0.48 + brake 15%:    CAGR +30.9%, maxDD 26.8%, Sharpe 1.09
       risk 0.32 + brake:        CAGR +23.0%, maxDD 25.1%, Sharpe 0.98
  The brake does not resize open positions, so it dampens the *recovery*, not
  the first slide: it buys ~11pp of drawdown for ~11pp of CAGR.

  The gate is a risk reducer: it costs ~1pp of CAGR and buys ~5pp of drawdown.
  Its plateau is MA 180-240; MA ≤160 or ≥260 degrades DD to 24-28% (at 0.32).

WHY THE OLD TABLE WAS WRONG: earlier versions measured drawdown on the
*realized* curve, which only moves when a trade closes.  On 2024-12-08 the book
was worth $280k mark vs $159k realized; it then gave that back by 2025-09-25
(291 days, -44%) and the realized curve reported only -23.8%.  At the shipped
risk 0.64 the honest drawdown was therefore ~44%, not ~22%, and the account sat
more than 25% below its high-water mark on 38% of all days.  To keep the
DD<25% rule, risk per sleeve must stay at or below 0.20.

Every variant still earns most of its return in one year (2023-2024), the
universe is today's 45 survivors (survivorship bias inflates the result), and
2025 was flat-to-negative.  Do not quote 20-25% CAGR as a given.
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

    # ── market data ──────────────────────────────────────────────────────
    # Venue for the daily bars: "binance_vision" (default), "binance" or "okx".
    # The 5.5-year validation used UTC-bucketed Binance days; OKX buckets its
    # 1D candles on UTC+8 and identical rules score visibly worse there
    # (2023-08→2026-11: CAGR 14% / Sharpe 0.57 on OKX vs 24% / 0.85 on
    # Binance), so the venue is part of the strategy, not an implementation
    # detail.  "binance_vision" is the public mirror of the same UTC klines:
    # it serves all 45 coins from cloud hosts where api.binance.com answers
    # 451 and OKX rate-limits the shared IP after ~26 requests.  The runner
    # falls back along a venue chain inside a cycle, but a stable book should
    # stay on one venue.
    source: str = "binance_vision"

    # ── signal (validated plateau, not a grid peak) ──────────────────────
    entry_lookback: int = 40       # close > max(high[-40:])
    exit_lookback: int = 20        # close < min(low[-20:])  → flat next open
    chandelier_mult: float = 5.0   # trailing stop = peak_close - 5*ATR
    atr_period: int = 14
    # Symmetric book: also go short on a break of the 40-bar LOW.
    # Off for crypto (shorts lost money: Sharpe -1.10); on for cross-asset,
    # where crisis alpha lives on the short side (2008, 2022).
    allow_short: bool = False

    # Market-regime gate: only take a breakout when the regime asset trades
    # above its own N-bar mean (below it, for shorts).  Empty = no gate.
    # BTC = the market beta of the whole 45-coin book, so the gate is a cheap
    # "don't buy breakouts into a bear market" switch.
    regime_symbol: str = ""
    regime_ma: int = 200

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

    # ── portfolio drawdown brake ─────────────────────────────────────────
    # 0 = off (the default: a new feature must never change a running book).
    # When mark-to-market equity sits ``brake_start_pct`` below its high-water
    # mark, every NEW entry is sized by ``brake_mult``; the brake releases once
    # the drawdown is back below ``brake_recover_pct`` (default: half of the
    # start).  Open positions are not resized — this throttles new risk, it
    # does not dump the book at the bottom.  Measured on 2019-2026 at 0.64:
    # 20%/0.5 turns CAGR 46.3% / DD 44.0% into 35.5% / 32.4%, but Sharpe falls
    # 1.10 -> 1.04 and only 4/7 folds stay positive (vs 5/7 unbraked).  It is
    # a drawdown/CAGR trade, not an improvement — hence off by default.
    brake_start_pct: float = 0.0
    brake_mult: float = 0.5
    brake_recover_pct: float = 0.0

    # ── shadow mirror (planning only, no order path) ─────────────────────
    # "off"    — no mirror work at all (default);
    # "shadow" — translate open/close events into the exact OKX USDT-M SWAP
    #            orders a LIVE mirror would send, and record them.  Nothing is
    #            sent: this module has no client and no credentials.
    # "live"   — actually place the orders.  Requires ALL of: an explicit DB
    #            flag (trend_mirror_live_enabled=1), a coin whitelist, a
    #            per-order cap, a total cap and a daily order cap.  Fail-closed.
    mirror_mode: str = "off"
    # Equity the mirror would run with.  0 = mirror the book 1:1 ($10k).
    # The order is scaled by mirror_equity / book.capital.
    mirror_equity: float = 0.0
    # Hard per-order notional cap (USD), 0 = no cap (not allowed in live).
    mirror_max_notional: float = 0.0
    # Micro-live: only these coins may be mirrored.  Empty = none (live is
    # rejected), so going live is always a deliberate, named choice.
    mirror_coins: Tuple[str, ...] = ()
    # Safety rails for live: max orders per UTC day and max total notional.
    mirror_daily_order_cap: int = 20
    mirror_max_total_notional: float = 0.0

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
    bars_per_year: int = 365          # 252 for exchange-traded assets

    @property
    def risk_per_trade_pct(self) -> float:
        """Risk of one trade as a fraction of the whole account."""
        return self.risk_per_sleeve / max(len(self.universe), 1)

    @property
    def state_bars(self) -> int:
        """Bars kept per sleeve in the state file.

        Must cover the longest window the engine reads *after* a restore:
        the regime MA (200), the entry lookback and the ATR period.  Storing
        only ``warmup_bars`` used to leave the gate blind for ~110 days
        after a state restore (200 closes needed, 90 stored).
        """
        return max(
            self.warmup_bars,
            self.regime_ma + 1 if self.regime_symbol else 0,
            self.entry_lookback + 2,
            self.atr_period + 2,
        )

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
        if self.regime_symbol and self.regime_symbol not in self.universe:
            raise ValueError("trend_portfolio: regime_symbol must be in the universe")
        if self.regime_symbol and self.regime_ma < 5:
            raise ValueError("trend_portfolio: regime_ma too small")
        if self.warmup_bars <= max(self.entry_lookback, self.atr_period):
            raise ValueError("trend_portfolio: warmup_bars too small")
        if self.source not in ("okx", "binance", "binance_vision"):
            raise ValueError(
                "trend_portfolio: source must be 'binance_vision', 'binance' or 'okx'"
            )
        if not (0.0 <= self.brake_start_pct < 100.0):
            raise ValueError("trend_portfolio: brake_start_pct must be [0, 100)")
        if not (0.0 < self.brake_mult <= 1.0):
            raise ValueError("trend_portfolio: brake_mult must be (0, 1]")
        if self.brake_start_pct and not (0.0 <= self.brake_recover_pct < self.brake_start_pct):
            raise ValueError(
                "trend_portfolio: brake_recover_pct must be [0, brake_start_pct)"
            )
        if self.mirror_mode not in ("off", "shadow", "live"):
            raise ValueError("trend_portfolio: mirror_mode must be off/shadow/live")
        if self.mirror_equity < 0 or self.mirror_max_notional < 0:
            raise ValueError("trend_portfolio: mirror equity/cap must be >= 0")
        if self.mirror_mode == "live":
            if not self.mirror_coins:
                raise ValueError("trend_portfolio: live mirror needs mirror_coins")
            unknown = [c for c in self.mirror_coins if c not in self.universe]
            if unknown:
                raise ValueError(f"trend_portfolio: mirror_coins not in universe: {unknown}")
            if self.mirror_equity <= 0 or self.mirror_max_notional <= 0:
                raise ValueError("trend_portfolio: live mirror needs equity and a per-order cap")
            if self.mirror_max_total_notional < self.mirror_max_notional:
                raise ValueError("trend_portfolio: live mirror total cap must be >= per-order cap")
            if self.mirror_daily_order_cap <= 0:
                raise ValueError("trend_portfolio: live mirror needs a daily order cap")

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
            brake_start_pct=_env_float("TREND_PORTFOLIO_BRAKE_START", 0.0),
            brake_mult=_env_float("TREND_PORTFOLIO_BRAKE_MULT", 0.5),
            brake_recover_pct=_env_float("TREND_PORTFOLIO_BRAKE_RECOVER", 0.0),
            mirror_mode=(os.getenv("TREND_MIRROR_MODE", "off") or "off").strip().lower(),
            mirror_equity=_env_float("TREND_MIRROR_EQUITY", 0.0),
            mirror_max_notional=_env_float("TREND_MIRROR_MAX_NOTIONAL", 0.0),
            mirror_coins=tuple(
                c.strip().upper()
                for c in (os.getenv("TREND_MIRROR_COINS", "") or "").split(",")
                if c.strip()
            ),
            mirror_daily_order_cap=_env_int("TREND_MIRROR_DAILY_CAP", 20),
            mirror_max_total_notional=_env_float("TREND_MIRROR_MAX_TOTAL", 0.0),
            regime_symbol=(os.getenv("TREND_PORTFOLIO_REGIME", "") or "").strip().upper(),
            regime_ma=_env_int("TREND_PORTFOLIO_REGIME_MA", 200),
            poll_interval_sec=_env_int("TREND_PORTFOLIO_POLL", 3600),
            source=(os.getenv("TREND_PORTFOLIO_SOURCE", "binance_vision") or "binance_vision").strip().lower(),
        )
        cfg.validate()
        return cfg
