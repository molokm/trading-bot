"""Core portfolio: config.

Two independent paper blocks over the same 45-coin universe:

* ``daily``  — the existing Donchian/Chandelier book (``trend_portfolio``),
  which owns half of the capital;
* ``h6``     — a 6-hour Donchian breakout with an ATR chandelier, long-only,
  gated by BTC above its 200-day average; the other half.

Why this split: on 2021-06 → 2026-11, replayed through the very engines in this
package (43,000+ 6h bars and 2,000 daily bars per coin, 0.16%/side costs, two
independent 50% accounts with no rebalancing between them):

  daily book          CAGR +28.7%, vol 23.5%, maxDD 22.9%, Sharpe 1.18
  6h block            CAGR +10.1%, vol 10.1%, maxDD 13.5%, Sharpe 1.01
  50/50 blend         CAGR +21.0%, vol 14.1%, maxDD 14.9%, Sharpe 1.42
                      first half +15.1%, second half +26.8%
                      daily correlation of the two blocks: -0.01

The 6h block also has an 8-year record (2018-09 → 2026-11: +11.7% CAGR,
9.3% vol, 13.5% maxDD, Sharpe 1.23, 38/45 coins profitable).  Neighbouring
settings (E 20..120, ATR 2.5..3.5) all stay positive, so 80/40/3.5 is a
plateau, not a grid peak.

Honest caveat: the daily block still earns most of its return in 2024
(+117% for the blend) and its drawdown is ~23% on its own half.  The blend
drawdown of 14.9% is the reason for the 50/50 split, not the CAGR.

Both blocks are paper-only and read public candles; there is no order path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Tuple

from app.bots.trend_portfolio.config import DEFAULT_UNIVERSE


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


@dataclass
class CorePortfolioConfig:
    enabled: bool = False
    paper_only: bool = True

    capital: float = 10_000.0
    # Share of capital in the daily block; the 6h block gets the rest.
    daily_weight: float = 0.5

    universe: Tuple[str, ...] = DEFAULT_UNIVERSE

    # ── 6h block (validated plateau centre, not a grid peak) ──────────────
    # E/X = 80/40 six-hour bars = 20/10 days; chandelier 3.5 ATR(14);
    # long-only; BTC 200-day gate.  Neighbouring settings (E 20..120,
    # ATR 2.5..3.5) all score Sharpe 0.58..0.97, so this is a plateau.
    h6_entry: int = 80
    h6_exit: int = 40
    h6_atr_period: int = 14
    h6_chandelier: float = 3.5
    h6_risk_per_sleeve: float = 0.05
    h6_max_sleeve_leverage: float = 2.0

    # ── daily block (the existing validated book) ────────────────────────
    # Donchian 40/20, Chandelier 5xATR(14), risk 0.64 per sleeve (= 1.42%
    # of a 45-name account per trade), sizing equity smoothed over 365d.
    # 0.64 is the setting that produced +28.7% CAGR / 22.9% maxDD / 1.18
    # Sharpe on 2021-06 → 2026-11 with the BTC gate on.
    daily_risk_per_sleeve: float = 0.64
    daily_entry: int = 40
    daily_exit: int = 20
    daily_chandelier: float = 5.0
    daily_atr_period: int = 14
    daily_max_sleeve_leverage: float = 3.0
    daily_sizing_halflife_days: float = 365.0

    # ── regime gate (shared) ─────────────────────────────────────────────
    regime_symbol: str = "BTC"
    regime_ma_days: int = 200

    # ── costs (2x the base 0.08%/side used in early tests) ───────────────
    fee_pct: float = 0.0008
    slippage_pct: float = 0.0008

    # ── market data ──────────────────────────────────────────────────────
    # okx | binance | binance_vision.  Binance buckets bars on UTC (the venue
    # the research used); OKX buckets 6H on UTC+8 and answers 451 from some
    # cloud regions on the main host.  The default is Binance because the 6h
    # block was validated on UTC bars — OKX must be an explicit opt-in.
    source: str = "binance"
    h6_candle_limit: int = 400      # per coin, enough for warmup + replay
    gate_bars: int = 900            # BTC only: 200 days * 4 bars + slack
    daily_candle_limit: int = 300
    poll_interval_sec: int = 1800

    # ── reporting ────────────────────────────────────────────────────────
    state_name: str = "core_portfolio"
    max_equity_points: int = 2_000

    @property
    def h6_capital(self) -> float:
        return self.capital * (1.0 - self.daily_weight)

    @property
    def daily_capital(self) -> float:
        return self.capital * self.daily_weight

    @property
    def cost_per_side(self) -> float:
        return self.fee_pct + self.slippage_pct

    @property
    def daily_source(self) -> str:
        """Daily bars only come from OKX or Binance (vision is Binance data)."""
        return "okx" if self.source == "okx" else "binance"

    def daily_book_config(self):
        """The 6h-independent daily book, scaled to ``daily_weight``."""
        from app.bots.trend_portfolio.config import TrendPortfolioConfig

        return TrendPortfolioConfig(
            enabled=self.enabled,
            paper_only=True,
            universe=self.universe,
            source=self.daily_source,
            entry_lookback=self.daily_entry,
            exit_lookback=self.daily_exit,
            chandelier_mult=self.daily_chandelier,
            atr_period=self.daily_atr_period,
            allow_short=False,
            regime_symbol=self.regime_symbol,
            regime_ma=self.regime_ma_days,
            capital=self.daily_capital,
            risk_per_sleeve=self.daily_risk_per_sleeve,
            max_sleeve_leverage=self.daily_max_sleeve_leverage,
            sizing_halflife_days=self.daily_sizing_halflife_days,
            fee_pct=self.fee_pct,
            slippage_pct=self.slippage_pct,
            candle_limit=self.daily_candle_limit,
            poll_interval_sec=self.poll_interval_sec,
        )

    @property
    def h6_state_bars(self) -> int:
        """Bars kept per sleeve in the state file.

        The engine reads at most ``max(entry, exit) + 1`` highs/lows after a
        restore, plus the ATR window; the ATR value itself is stored so Wilder
        smoothing survives.
        """
        return max(self.h6_entry + 2, self.h6_exit + 2, self.h6_atr_period + 2)

    def validate(self) -> None:
        if not self.paper_only:
            raise ValueError("core_portfolio: paper_only must stay True")
        if not self.universe:
            raise ValueError("core_portfolio: empty universe")
        if self.capital <= 0:
            raise ValueError("core_portfolio: capital must be > 0")
        if not (0.0 < self.daily_weight < 1.0):
            raise ValueError("core_portfolio: daily_weight must be in (0, 1)")
        if self.h6_entry < 5 or self.h6_exit < 2:
            raise ValueError("core_portfolio: h6 lookbacks too small")
        if self.h6_exit >= self.h6_entry:
            raise ValueError("core_portfolio: h6 exit window must be < entry window")
        if self.h6_atr_period < 2:
            raise ValueError("core_portfolio: h6_atr_period too small")
        if self.h6_chandelier <= 0:
            raise ValueError("core_portfolio: h6_chandelier must be > 0")
        if not (0 < self.h6_risk_per_sleeve <= 1.0):
            raise ValueError("core_portfolio: h6_risk_per_sleeve must be (0, 1]")
        if self.h6_max_sleeve_leverage <= 0:
            raise ValueError("core_portfolio: h6_max_sleeve_leverage must be > 0")
        if not (0 < self.daily_risk_per_sleeve <= 1.0):
            raise ValueError("core_portfolio: daily_risk_per_sleeve must be (0, 1]")
        if self.daily_exit >= self.daily_entry or self.daily_entry < 5:
            raise ValueError("core_portfolio: daily exit window must be < entry window")
        if self.daily_chandelier <= 0:
            raise ValueError("core_portfolio: daily_chandelier must be > 0")
        if self.daily_atr_period < 2:
            raise ValueError("core_portfolio: daily_atr_period too small")
        if self.daily_sizing_halflife_days <= 0:
            raise ValueError("core_portfolio: daily_sizing_halflife_days must be > 0")
        if self.regime_symbol and self.regime_symbol not in self.universe:
            raise ValueError("core_portfolio: regime_symbol must be in the universe")
        if self.regime_symbol and self.regime_ma_days < 5:
            raise ValueError("core_portfolio: regime_ma_days too small")
        if self.source not in ("okx", "binance", "binance_vision"):
            raise ValueError("core_portfolio: source must be okx|binance|binance_vision")
        if self.h6_candle_limit < self.h6_state_bars + 5:
            raise ValueError("core_portfolio: h6_candle_limit too small for warmup")
        if self.gate_bars < self.regime_ma_days * 4 + 8 and self.regime_symbol:
            raise ValueError("core_portfolio: gate_bars too small for the 200-day gate")

    @classmethod
    def from_env(cls) -> "CorePortfolioConfig":
        cfg = cls(
            enabled=_env_bool("CORE_PORTFOLIO_ENABLE", False),
            capital=_env_float("CORE_PORTFOLIO_CAPITAL", 10_000.0),
            daily_weight=_env_float("CORE_PORTFOLIO_DAILY_WEIGHT", 0.5),
            h6_entry=_env_int("CORE_PORTFOLIO_H6_ENTRY", 80),
            h6_exit=_env_int("CORE_PORTFOLIO_H6_EXIT", 40),
            h6_atr_period=_env_int("CORE_PORTFOLIO_H6_ATR", 14),
            h6_chandelier=_env_float("CORE_PORTFOLIO_H6_CHANDELIER", 3.5),
            h6_risk_per_sleeve=_env_float("CORE_PORTFOLIO_H6_RISK", 0.05),
            h6_max_sleeve_leverage=_env_float("CORE_PORTFOLIO_H6_MAX_LEV", 2.0),
            daily_risk_per_sleeve=_env_float("CORE_PORTFOLIO_DAILY_RISK", 0.64),
            daily_entry=_env_int("CORE_PORTFOLIO_DAILY_ENTRY", 40),
            daily_exit=_env_int("CORE_PORTFOLIO_DAILY_EXIT", 20),
            daily_chandelier=_env_float("CORE_PORTFOLIO_DAILY_CHANDELIER", 5.0),
            regime_symbol=(os.getenv("CORE_PORTFOLIO_REGIME", "BTC") or "").strip().upper(),
            regime_ma_days=_env_int("CORE_PORTFOLIO_REGIME_MA", 200),
            source=(os.getenv("CORE_PORTFOLIO_SOURCE", "binance") or "binance").strip().lower(),
            h6_candle_limit=_env_int("CORE_PORTFOLIO_H6_BARS", 400),
            gate_bars=_env_int("CORE_PORTFOLIO_GATE_BARS", 900),
            daily_candle_limit=_env_int("CORE_PORTFOLIO_DAILY_BARS", 300),
            poll_interval_sec=_env_int("CORE_PORTFOLIO_POLL", 1800),
        )
        cfg.validate()
        return cfg
