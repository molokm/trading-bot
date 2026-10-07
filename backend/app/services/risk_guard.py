"""Global risk gates (stage-3 + position-level limits).

Env-driven kill switch and soft limits checked before any place_order.
Daily PnL is fed from the dashboard PnL pipeline via update_daily_pnl().
Position-level limits: max open positions, per-symbol limits, correlation caps.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.ai_strategy import AIStrategy


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


def _b(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    return raw in ("1", "true", "yes", "on")


# Asset class correlation groups for exposure limiting
CORRELATION_GROUPS = {
    "crypto_major": ["BTC", "ETH"],
    "crypto_alt": ["SOL", "XRP", "ADA", "DOT", "AVAX", "MATIC", "LINK", "UNI"],
    "crypto_meme": ["DOGE", "SHIB", "PEPE", "WIF", "BONK"],
    "defi": ["UNI", "AAVE", "COMP", "MKR", "CRV", "LDO"],
    "layer1": ["SOL", "AVAX", "ADA", "DOT", "NEAR", "ATOM"],
}


def _symbol_to_group(symbol: str) -> str:
    """Map symbol (e.g., BTC-USDT-SWAP) to correlation group."""
    base = symbol.split("-")[0].upper()
    for group, symbols in CORRELATION_GROUPS.items():
        if base in symbols:
            return group
    return "other"


@dataclass
class RiskStatus:
    kill_switch: bool
    max_daily_loss_usd: float
    max_position_usd: float
    max_leverage: float
    okx_demo: bool
    block_new_entries: bool
    reason: Optional[str] = None
    daily_pnl_usd: Optional[float] = None
    daily_pnl_updated_at: Optional[float] = None
    # Position-level limits
    max_open_positions: int = 0
    open_positions_count: int = 0
    max_positions_per_symbol: int = 0
    max_correlated_exposure_usd: float = 0.0
    current_correlated_exposure_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "kill_switch": self.kill_switch,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_position_usd": self.max_position_usd,
            "max_leverage": self.max_leverage,
            "okx_demo": self.okx_demo,
            "block_new_entries": self.block_new_entries,
            "reason": self.reason,
            "daily_pnl_usd": self.daily_pnl_usd,
            "daily_pnl_updated_at": self.daily_pnl_updated_at,
            "max_open_positions": self.max_open_positions,
            "open_positions_count": self.open_positions_count,
            "max_positions_per_symbol": self.max_positions_per_symbol,
            "max_correlated_exposure_usd": self.max_correlated_exposure_usd,
            "current_correlated_exposure_usd": self.current_correlated_exposure_usd,
        }


_runtime_kill: Optional[bool] = None
_daily_pnl: Optional[float] = None
_daily_pnl_ts: Optional[float] = None


def _count_open_positions(ai_bot: Optional["AIStrategy"]) -> Dict:
    """Count open positions by symbol and correlation group."""
    if not ai_bot:
        return {"total": 0, "by_symbol": {}, "by_group": {}}

    positions = getattr(ai_bot, "_positions", {}) or {}
    live_positions = getattr(ai_bot, "_live_positions", {}) or {}

    counts = {"total": 0, "by_symbol": {}, "by_group": {}}

    # Count demo positions
    for coin, pos in positions.items():
        size = float(getattr(pos, "size", 0) or 0)
        if size > 0:
            counts["total"] += 1
            counts["by_symbol"][coin] = counts["by_symbol"].get(coin, 0) + 1
            group = _symbol_to_group(getattr(pos, "inst_id", f"{coin}-USDT-SWAP"))
            counts["by_group"][group] = counts["by_group"].get(group, 0) + 1

    # Count live positions
    for coin, pos in live_positions.items():
        size = float(getattr(pos, "size", 0) or 0)
        if size > 0:
            counts["total"] += 1
            counts["by_symbol"][coin] = counts["by_symbol"].get(coin, 0) + 1
            group = _symbol_to_group(getattr(pos, "inst_id", f"{coin}-USDT-SWAP"))
            counts["by_group"][group] = counts["by_group"].get(group, 0) + 1

    return counts


def _calculate_correlated_exposure(ai_bot: Optional["AIStrategy"]) -> float:
    """Calculate total notional exposure for correlated assets."""
    if not ai_bot:
        return 0.0

    positions = getattr(ai_bot, "_positions", {}) or {}
    live_positions = getattr(ai_bot, "_live_positions", {}) or {}

    # Group notional by correlation group
    group_exposure: Dict[str, float] = {}

    for coin, pos in list(positions.items()) + list(live_positions.items()):
        size = float(getattr(pos, "size", 0) or 0)
        if size <= 0:
            continue
        entry = float(getattr(pos, "entry_price", 0) or 0)
        inst = getattr(pos, "inst_id", f"{coin}-USDT-SWAP")
        notional = size * entry
        group = _symbol_to_group(inst)
        group_exposure[group] = group_exposure.get(group, 0.0) + notional

    # Return max group exposure (most concentrated correlation risk)
    return max(group_exposure.values()) if group_exposure else 0.0


def get_status(daily_pnl: Optional[float] = None, ai_bot: Optional["AIStrategy"] = None) -> "RiskStatus":
    env_kill = _b("RISK_KILL_SWITCH", False)
    kill = env_kill if _runtime_kill is None else _runtime_kill
    max_daily = _f("RISK_MAX_DAILY_LOSS_USD", 0.0)  # 0 = disabled
    max_pos = _f("RISK_MAX_POSITION_USD", 0.0)
    max_lev = _f("RISK_MAX_LEVERAGE", 0.0)
    demo = _b("OKX_DEMO", True)

    # Position-level limits from env
    max_open_pos = int(_f("RISK_MAX_OPEN_POSITIONS", 0))  # 0 = unlimited
    max_per_symbol = int(_f("RISK_MAX_POS_PER_SYMBOL", 0))
    max_corr_exp = _f("RISK_MAX_CORRELATED_EXPOSURE_USD", 0.0)

    pnl = daily_pnl if daily_pnl is not None else _daily_pnl

    # Count current positions
    pos_counts = _count_open_positions(ai_bot) if ai_bot else {"total": 0, "by_symbol": {}, "by_group": {}}
    corr_exp = _calculate_correlated_exposure(ai_bot) if ai_bot else 0.0

    reason = None
    block = False

    if kill:
        block = True
        reason = "kill_switch"
    elif max_daily > 0 and pnl is not None and pnl <= -abs(max_daily):
        block = True
        reason = f"max_daily_loss ({pnl:.2f} <= -{max_daily:.2f})"

    # Position-level checks
    if max_open_pos > 0 and pos_counts["total"] >= max_open_pos:
        block = True
        reason = f"max_open_positions ({pos_counts['total']} >= {max_open_pos})"

    # Per-symbol limit (check both demo and live)
    if max_per_symbol > 0:
        for sym, cnt in pos_counts.get("by_symbol", {}).items():
            if cnt >= max_per_symbol:
                block = True
                reason = f"max_pos_per_symbol ({sym}: {cnt} >= {max_per_symbol})"
                break

    # Correlated exposure limit
    if max_corr_exp > 0 and corr_exp > max_corr_exp:
        block = True
        reason = f"max_correlated_exposure ({corr_exp:.2f} > {max_corr_exp:.2f})"

    return RiskStatus(
        kill_switch=kill,
        max_daily_loss_usd=_f("RISK_MAX_DAILY_LOSS_USD", 0.0),
        max_position_usd=_f("RISK_MAX_POSITION_USD", 0.0),
        max_leverage=_f("RISK_MAX_LEVERAGE", 0.0),
        okx_demo=_b("OKX_DEMO", True),
        block_new_entries=block,
        reason=reason,
        daily_pnl_usd=daily_pnl if daily_pnl is not None else _daily_pnl,
        daily_pnl_updated_at=_daily_pnl_ts,
        max_open_positions=int(_f("RISK_MAX_OPEN_POSITIONS", 0)),
        open_positions_count=pos_counts["total"],
        max_positions_per_symbol=int(_f("RISK_MAX_POS_PER_SYMBOL", 0)),
        max_correlated_exposure_usd=_f("RISK_MAX_CORRELATED_EXPOSURE_USD", 0.0),
        current_correlated_exposure_usd=corr_exp,
    )


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


def assert_can_open(
    *,
    notional_usd: Optional[float] = None,
    leverage: Optional[float] = None,
    daily_pnl: Optional[float] = None,
    is_reduce_only: bool = False,
    ai_bot: Optional["AIStrategy"] = None,
    symbol: Optional[str] = None,
) -> None:
    """Raise RuntimeError if a new risk-increasing order must be blocked.

    Reduce-only / close orders are always allowed (even under kill switch)
    so positions can be flattened.
    """
    if is_reduce_only:
        return

    st = get_status(daily_pnl=daily_pnl, ai_bot=ai_bot)
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

    # Per-symbol limit check
    if st.max_positions_per_symbol > 0 and ai_bot and symbol:
        pos_counts = _count_open_positions(ai_bot)
        current = pos_counts.get("by_symbol", {}).get(symbol, 0)
        if current >= st.max_positions_per_symbol:
            raise RuntimeError(
                f"risk_guard max_pos_per_symbol: {symbol} already has {current} >= {st.max_positions_per_symbol}"
            )

    # Correlated exposure limit
    if st.max_correlated_exposure_usd > 0 and ai_bot:
        corr_exp = _calculate_correlated_exposure(ai_bot)
        if notional_usd is not None:
            projected = corr_exp + notional_usd
            if projected > st.max_correlated_exposure_usd:
                raise RuntimeError(
                    f"risk_guard max_correlated_exposure: projected {projected:.2f} > {st.max_correlated_exposure_usd:.2f}"
                )