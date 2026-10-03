"""Global risk gates (stage-3 + position-level).

Env-driven kill switch and soft limits checked before any place_order.
Daily PnL is fed from the dashboard PnL pipeline via update_daily_pnl().
Position-level limits: max open positions, per-symbol exposure, correlation groups.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Set


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


def _b(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


@dataclass
class RiskStatus:
    kill_switch: bool
    max_daily_loss_usd: float
    max_position_usd: float
    max_leverage: float
    max_open_positions: int
    max_positions_per_symbol: int
    correlation_groups: Dict[str, int]
    okx_demo: bool
    block_new_entries: bool
    reason: Optional[str] = None
    daily_pnl_usd: Optional[float] = None
    daily_pnl_updated_at: Optional[float] = None
    open_positions_count: int = 0
    positions_per_symbol: Dict[str, int] = field(default_factory=dict)
    correlation_exposure: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kill_switch": self.kill_switch,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_position_usd": self.max_position_usd,
            "max_leverage": self.max_leverage,
            "max_open_positions": self.max_open_positions,
            "max_positions_per_symbol": self.max_positions_per_symbol,
            "correlation_groups": self.correlation_groups,
            "okx_demo": self.okx_demo,
            "block_new_entries": self.block_new_entries,
            "reason": self.reason,
            "daily_pnl_usd": self.daily_pnl_usd,
            "daily_pnl_updated_at": self.daily_pnl_updated_at,
            "open_positions_count": self.open_positions_count,
            "positions_per_symbol": self.positions_per_symbol,
            "correlation_exposure": self.correlation_exposure,
        }


_runtime_kill: Optional[bool] = None
_daily_pnl: Optional[float] = None
_daily_pnl_ts: Optional[float] = None

# Position tracking (in-memory, updated by position manager)
_open_positions: Dict[str, dict] = {}  # coin -> {side, size, notional_usd, entry_ts}
_symbol_groups: Dict[str, Set[str]] = {
    "major": {"BTC", "ETH"},
    "defi": {"UNI", "AAVE", "COMP", "SUSHI", "CRV"},
    "layer1": {"SOL", "AVAX", "NEAR", "ATOM", "DOT", "ADA"},
    "meme": {"DOGE", "SHIB", "PEPE", "WIF", "BONK"},
    "stable": {"USDT", "USDC", "DAI", "FDUSD", "TUSD"},
}


def set_kill_switch(enabled: bool) -> None:
    global _runtime_kill
    _runtime_kill = bool(enabled)


def update_daily_pnl(value: float) -> None:
    """Called from /api/pnl pipeline so place_order can enforce daily loss."""
    global _daily_pnl, _daily_pnl_ts
    try:
        _daily_pnl = float(value)
        _daily_pnl_ts = time.time()
    except (TypeError, ValueError):
        pass


def get_cached_daily_pnl() -> Optional[float]:
    return _daily_pnl


def update_position(coin: str, side: str, size: float, notional_usd: float, entry_ts: float = None) -> None:
    """Update in-memory position tracking for risk checks."""
    global _open_positions
    if size <= 0:
        _open_positions.pop(coin, None)
    else:
        _open_positions[coin] = {
            "side": side.lower(),
            "size": float(size),
            "notional_usd": float(notional_usd),
            "entry_ts": entry_ts or time.time(),
        }


def get_open_positions() -> Dict[str, dict]:
    return dict(_open_positions)


def clear_positions() -> None:
    global _open_positions
    _open_positions.clear()


def get_correlation_group(coin: str) -> str:
    """Return correlation group for a coin."""
    for group, coins in _symbol_groups.items():
        if coin.upper() in coins:
            return group
    return "other"


def get_status(daily_pnl: Optional[float] = None) -> RiskStatus:
    env_kill = _b("RISK_KILL_SWITCH", False)
    kill = env_kill if _runtime_kill is None else _runtime_kill
    max_daily = _f("RISK_MAX_DAILY_LOSS_USD", 0.0)  # 0 = disabled
    max_pos = _f("RISK_MAX_POSITION_USD", 0.0)
    max_lev = _f("RISK_MAX_LEVERAGE", 0.0)
    max_open = _i("RISK_MAX_OPEN_POSITIONS", 10)  # 0 = disabled
    max_per_sym = _i("RISK_MAX_POSITIONS_PER_SYMBOL", 1)  # 0 = disabled
    demo = _b("OKX_DEMO", True)

    # Correlation group limits (env: RISK_CORR_MAJOR=2,RISK_CORR_DEFI=1,...)
    corr_groups: Dict[str, int] = {}
    for group in _symbol_groups.keys():
        env_val = os.getenv(f"RISK_CORR_{group.upper()}", "")
        if env_val:
            try:
                corr_groups[group] = int(env_val)
            except ValueError:
                pass

    pnl = daily_pnl if daily_pnl is not None else _daily_pnl

    reason = None
    block = False
    if kill:
        block = True
        reason = "kill_switch"
    elif max_daily > 0 and pnl is not None and pnl <= -abs(max_daily):
        block = True
        reason = f"max_daily_loss ({pnl:.2f} <= -{max_daily:.2f})"

    # Count current positions
    open_count = len(_open_positions)
    pos_per_sym: Dict[str, int] = {}
    corr_exposure: Dict[str, float] = {g: 0.0 for g in _symbol_groups.keys()}
    corr_exposure["other"] = 0.0

    for coin, pos in _open_positions.items():
        sym = coin.upper()
        pos_per_sym[sym] = pos_per_sym.get(sym, 0) + 1
        notional = pos.get("notional_usd", 0.0)
        group = get_correlation_group(coin)
        corr_exposure[group] = corr_exposure.get(group, 0.0) + notional

    return RiskStatus(
        kill_switch=kill,
        max_daily_loss_usd=max_daily,
        max_position_usd=max_pos,
        max_leverage=max_lev,
        max_open_positions=max_open,
        max_positions_per_symbol=max_per_sym,
        correlation_groups=corr_groups,
        okx_demo=demo,
        block_new_entries=block,
        reason=reason,
        daily_pnl_usd=pnl,
        daily_pnl_updated_at=_daily_pnl_ts,
        open_positions_count=open_count,
        positions_per_symbol=pos_per_sym,
        correlation_exposure=corr_exposure,
    )


def assert_can_open(
    *,
    coin: str = "",
    side: str = "",
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

    # Position count limits
    if st.max_open_positions > 0:
        if st.open_positions_count >= st.max_open_positions:
            raise RuntimeError(
                f"risk_guard max_open_positions: {st.open_positions_count} >= {st.max_open_positions}"
            )

    if st.max_positions_per_symbol > 0 and coin:
        sym = coin.upper()
        if st.positions_per_symbol.get(sym, 0) >= st.max_positions_per_symbol:
            raise RuntimeError(
                f"risk_guard max_positions_per_symbol({sym}): {st.positions_per_symbol.get(sym, 0)} >= {st.max_positions_per_symbol}"
            )

    # Correlation group limits
    if coin:
        group = get_correlation_group(coin)
        limit = st.correlation_groups.get(group, 0)
        if limit > 0:
            current_exposure = st.correlation_exposure.get(group, 0.0)
            new_exposure = current_exposure + (notional_usd or 0.0)
            if new_exposure > limit:
                raise RuntimeError(
                    f"risk_guard correlation_group({group}): {new_exposure:.2f} > {limit}"
                )
