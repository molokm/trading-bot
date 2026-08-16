"""Global risk gates (stage-3a).

Env-driven kill switch and soft limits checked before any place_order.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


def _b(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    return raw in ("1", "true", "yes", "on")


@dataclass
class RiskStatus:
    kill_switch: bool
    max_daily_loss_usd: float
    max_position_usd: float
    max_leverage: float
    okx_demo: bool
    block_new_entries: bool
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "kill_switch": self.kill_switch,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_position_usd": self.max_position_usd,
            "max_leverage": self.max_leverage,
            "okx_demo": self.okx_demo,
            "block_new_entries": self.block_new_entries,
            "reason": self.reason,
        }


# Runtime override (admin API); None = use env only
_runtime_kill: Optional[bool] = None


def set_kill_switch(enabled: bool) -> None:
    global _runtime_kill
    _runtime_kill = bool(enabled)


def get_status(daily_pnl: Optional[float] = None) -> RiskStatus:
    env_kill = _b("RISK_KILL_SWITCH", False)
    kill = env_kill if _runtime_kill is None else _runtime_kill
    max_daily = _f("RISK_MAX_DAILY_LOSS_USD", 0.0)  # 0 = disabled
    max_pos = _f("RISK_MAX_POSITION_USD", 0.0)
    max_lev = _f("RISK_MAX_LEVERAGE", 0.0)
    demo = _b("OKX_DEMO", True)

    reason = None
    block = False
    if kill:
        block = True
        reason = "kill_switch"
    elif max_daily > 0 and daily_pnl is not None and daily_pnl <= -abs(max_daily):
        block = True
        reason = f"max_daily_loss ({daily_pnl:.2f} <= -{max_daily:.2f})"

    return RiskStatus(
        kill_switch=kill,
        max_daily_loss_usd=max_daily,
        max_position_usd=max_pos,
        max_leverage=max_lev,
        okx_demo=demo,
        block_new_entries=block,
        reason=reason,
    )


def assert_can_open(
    *,
    notional_usd: Optional[float] = None,
    leverage: Optional[float] = None,
    daily_pnl: Optional[float] = None,
    is_reduce_only: bool = False,
) -> None:
    """Raise RuntimeError if a new risk-increasing order must be blocked.

    Reduce-only / close orders are always allowed (even under kill switch)
    so positions can be flattened.
    """
    if is_reduce_only:
        return
    st = get_status(daily_pnl=daily_pnl)
    if st.block_new_entries:
        raise RuntimeError(f"risk_guard blocked entry: {st.reason}")
    if st.max_position_usd > 0 and notional_usd is not None:
        if notional_usd > st.max_position_usd:
            raise RuntimeError(
                f"risk_guard max_position_usd: {notional_usd:.2f} > {st.max_position_usd:.2f}"
            )
    if st.max_leverage > 0 and leverage is not None:
        if leverage > st.max_leverage:
            raise RuntimeError(
                f"risk_guard max_leverage: {leverage} > {st.max_leverage}"
            )
