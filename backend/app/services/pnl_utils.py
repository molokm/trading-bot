"""Shared PnL helpers for live strategies (OKX fills).

OKX typically returns fee as a *negative* number when the account pays the fee.
Older code did `gross - fee`, which *adds* fee when fee is negative. We normalize
to a positive cost and always subtract it from gross PnL.
"""
from __future__ import annotations
from typing import Any, Iterable, Optional, Tuple


def fee_cost(fee: Any) -> float:
    """Positive USDT cost of a fee field (handles signed OKX fees)."""
    try:
        return abs(float(fee or 0))
    except (TypeError, ValueError):
        return 0.0


def extract_fill_avg(
    fills: Optional[Iterable[dict]],
    fallback_px: float,
) -> Tuple[float, float, float]:
    """From order fill rows return (avg_px, fee_cost_total, filled_size).

    Prefers avgPx on a row when present; otherwise size-weighted fillPx.
    Fee is always returned as a positive cost.
    """
    rows = list(fills or [])
    if not rows:
        return float(fallback_px or 0), 0.0, 0.0

    # Single-row shortcut with avgPx
    if len(rows) == 1:
        r = rows[0]
        try:
            px = float(r.get("avgPx") or r.get("fillPx") or fallback_px or 0)
        except (TypeError, ValueError):
            px = float(fallback_px or 0)
        try:
            sz = abs(float(r.get("fillSz") or r.get("sz") or 0))
        except (TypeError, ValueError):
            sz = 0.0
        return px, fee_cost(r.get("fee")), sz

    notional = 0.0
    size = 0.0
    fees = 0.0
    for r in rows:
        try:
            px = float(r.get("avgPx") or r.get("fillPx") or 0)
        except (TypeError, ValueError):
            continue
        try:
            sz = abs(float(r.get("fillSz") or r.get("sz") or 0))
        except (TypeError, ValueError):
            sz = 0.0
        if px <= 0:
            continue
        if sz <= 0:
            sz = 1.0  # treat as one unit if size missing
        notional += px * sz
        size += sz
        fees += fee_cost(r.get("fee"))
    if size <= 0 or notional <= 0:
        r0 = rows[0]
        try:
            px = float(r0.get("avgPx") or r0.get("fillPx") or fallback_px or 0)
        except (TypeError, ValueError):
            px = float(fallback_px or 0)
        return px, fees or fee_cost(r0.get("fee")), 0.0
    return notional / size, fees, size


def close_pnl(
    side: str,
    size: float,
    entry_px: float,
    exit_px: float,
    fee: Any,
    ct_val: float,
) -> float:
    """Realized PnL in USDT for a close (full or partial).

    side: position side "long" | "short"
    fee: raw OKX fee (signed or unsigned) — always treated as cost
    ct_val: contract face value (coin units per contract)
    """
    try:
        size = float(size)
        entry_px = float(entry_px)
        exit_px = float(exit_px)
        ct = float(ct_val)
    except (TypeError, ValueError):
        return 0.0
    if size <= 0 or ct <= 0 or entry_px <= 0 or exit_px <= 0:
        # still apply fee if we have a degenerate row
        return -fee_cost(fee)
    if side == "long":
        gross = size * ct * (exit_px - entry_px)
    else:
        gross = size * ct * (entry_px - exit_px)
    return gross - fee_cost(fee)


def pnl_from_okx_fills(fills: Optional[Iterable[dict]], fallback_px: float = 0.0):
    """Prefer exchange-reported fillPnl + signed fee (matches OKX history closely).

    Returns (net_pnl, avg_px, fee_cost_total, filled_sz) or (None, avg, fee, sz)
    when fillPnl is unavailable.
    """
    rows = list(fills or [])
    if not rows:
        return None, float(fallback_px or 0), 0.0, 0.0

    has_pnl = any(
        (r.get("fillPnl") is not None and str(r.get("fillPnl")) != "")
        for r in rows
    )
    net = 0.0
    notional = 0.0
    size = 0.0
    fees = 0.0
    for r in rows:
        try:
            f = float(r.get("fee") or 0)
        except (TypeError, ValueError):
            f = 0.0
        fees += abs(f)
        if has_pnl:
            try:
                net += float(r.get("fillPnl") or 0)
            except (TypeError, ValueError):
                pass
            net += f  # signed OKX fee
        try:
            px = float(r.get("fillPx") or r.get("avgPx") or 0)
            sz = abs(float(r.get("fillSz") or r.get("sz") or 0))
        except (TypeError, ValueError):
            continue
        if px > 0 and sz > 0:
            notional += px * sz
            size += sz

    avg = (notional / size) if size > 0 else float(fallback_px or 0)
    if has_pnl:
        return net, avg, fees, size
    return None, avg, fees, size



def order_avg_from_details(order_row: dict, fallback_px: float = 0.0):
    """Parse a /trade/order row into (avg_px, fee_cost, acc_fill_sz)."""
    if not order_row:
        return float(fallback_px or 0), 0.0, 0.0
    try:
        avg = float(order_row.get("avgPx") or 0)
    except (TypeError, ValueError):
        avg = 0.0
    if avg <= 0:
        try:
            avg = float(order_row.get("px") or fallback_px or 0)
        except (TypeError, ValueError):
            avg = float(fallback_px or 0)
    try:
        sz = abs(float(order_row.get("accFillSz") or order_row.get("sz") or 0))
    except (TypeError, ValueError):
        sz = 0.0
    return avg, fee_cost(order_row.get("fee")), sz
