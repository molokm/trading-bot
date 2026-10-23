#!/usr/bin/env python3
"""Aggregate existing BT JSON artifacts and print Stage-5 evaluation summary.

Does not re-download market data. Run full engines separately (see STAGE5_EVAL.md).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = [
    ROOT / "external/backtests/macd_donchian_validation_bt_result.json",
    ROOT / "external/scripts/honest_macd_donchian_final.json",
    ROOT / "external/scripts/honest_macd_donchian_be_final_10coins.json",
    ROOT / "external/scripts/honest_macd_donchian_partial_final.json",
]


def load(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:
        return {"_error": str(e), "_path": str(p)}


def row(d: dict, path: Path) -> str:
    if not d:
        return f"  MISSING  {path}"
    if d.get("_error"):
        return f"  ERROR    {path.name}: {d['_error']}"
    name = d.get("strategy") or d.get("name") or path.name
    cagr = d.get("cagr_pct", d.get("cagr", "—"))
    dd = d.get("max_drawdown_pct", d.get("max_dd_pct", "—"))
    sh = d.get("sharpe", "—")
    wr = d.get("win_rate", d.get("win_rate_pct", "—"))
    n = d.get("closed_trades", d.get("trades", "—"))
    costs = d.get("costs", "")
    return (
        f"  {name[:48]:48s}  CAGR={cagr!s:>8}  MaxDD={dd!s:>8}  "
        f"Sharpe={sh!s:>6}  WR={wr!s:>6}  n={n!s:>5}  {costs}"
    )


def main():
    print("=== Stage 5 evaluation report (artifact scan) ===\n")
    print("Protocol: external/STAGE5_EVAL.md")
    print("Full-window metrics after parameter sweeps are NOT pure OOS.\n")
    print("Artifacts:")
    for p in CANDIDATES:
        print(row(load(p), p))
    print("\nChecklist:")
    for line in [
        "[ ] Re-run Backtrader engines with current live params",
        "[ ] Inspect OOS windows (wfo_oos_compare / walkforward_v3)",
        "[ ] Confirm costs 0.10% + 0.05% in engine output",
        "[ ] Compare live demo trades vs BT rules for 30d",
        "[ ] Set RISK_MAX_DAILY_LOSS_USD before LIVE",
    ]:
        print(" ", line)
    print("\nDone.")


if __name__ == "__main__":
    main()
