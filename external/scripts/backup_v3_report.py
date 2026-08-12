#!/usr/bin/env python3
"""Honest backup report for the LIVE rotation config (min_roc=2.0, adx=22).
Runs the honest engine over the full window, prints per-year returns,
and saves a full snapshot to JSON (equity curve, trades, config, yearly).
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from honest_backtest_3y import (
    V3_LIVE, load_data, load_funding, run_strategy, print_report, summarize,
)

OUT = os.path.join(os.path.dirname(__file__), "backup_v3_minroc2_results.json")


def yearly_detail(r):
    """Year-by-year: return %, equity, trades count in that year."""
    yearly = r.get("yearly", [])
    lines = []
    for y in yearly:
        yret = y["return_pct"]
        lines.append(f"    {y['year']}: {yret:+.1f}%   eq=${y['equity']:,.0f}")
    return lines


async def main():
    print("=" * 72)
    print("HONEST BACKUP — live rotation v3 (min_roc=2.0, adx_min=22)")
    print("Rules: signal@T close → entry@T+1 open | stop vs H/L | fee 0.10% + slip 0.05% | funding @8h")
    print("=" * 72, flush=True)

    data = await load_data(force_refresh="--refresh" in sys.argv)
    funding = await load_funding(force_refresh="--refresh" in sys.argv)

    r = run_strategy(data, V3_LIVE, funding, return_trades=True)
    print_report(r)

    print("\n" + "=" * 72)
    print("YEARLY RETURN (честный, по закрытым свечам)")
    print("=" * 72)
    for line in yearly_detail(r):
        print(line)

    # Year-by-year closed-trade stats
    closed = [t for t in r.get("all_trades", []) if t.get("closed")]
    by_year = {}
    for t in closed:
        y = t["date"][:4]
        by_year.setdefault(y, []).append(t)
    print("\nYearly trade stats:")
    for y in sorted(by_year):
        ts = by_year[y]
        wins = sum(1 for t in ts if t["pnl"] > 0)
        total = sum(t["pnl"] for t in ts)
        print(f"    {y}: {len(ts)} closed trades | WR {wins/len(ts)*100:.1f}% | net PnL ${total:+,.2f}")

    # Save snapshot
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": r["config"],
        "summary": {k: v for k, v in r.items() if k not in ("equity_curve", "recent_trades", "config", "all_trades")},
        "yearly": r["yearly"],
        "yearly_trade_stats": {
            y: {"trades": len(ts), "win_rate_pct": round(sum(1 for t in ts if t["pnl"] > 0) / len(ts) * 100, 1),
                "net_pnl": round(sum(t["pnl"] for t in ts), 2)}
            for y, ts in sorted(by_year.items())
        },
        "equity_curve": r["equity_curve"],
        "all_trades": r["all_trades"],
    }
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved → {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
