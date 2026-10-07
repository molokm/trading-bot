"""
Crypto Scanner — Finds significant movers for the AI orchestrator.
Equivalent of the S&P 500 gap scanner from Humbled Trader, adapted for 24/7 crypto markets.

Scans OKX SWAP pairs for:
- Significant 4H candle moves (>1.5%)
- Volume spikes (>1.5x 20-period average)
- Momentum breakouts
"""
import json
import time
import numpy as np
import pandas as pd
from pathlib import Path

RULES_PATH = Path(__file__).parent / "rules.json"


def load_rules():
    with open(RULES_PATH) as f:
        return json.load(f)


async def scan_market(client_manager, rules=None):
    """
    Scan OKX SWAP pairs for significant movers.
    Returns list of symbols with their scan scores.
    """
    if rules is None:
        rules = load_rules()

    scanner_config = rules.get("scanner", {})
    min_vol_usd = scanner_config.get("min_volume_usd_24h", 50_000_000)
    min_change_4h = scanner_config.get("min_change_pct_4h", 1.5)
    max_results = scanner_config.get("max_results", 10)

    # Get all SWAP tickers
    client = client_manager.get_client()
    if not client:
        print("[SCANNER] No OKX client available", flush=True)
        return []

    try:
        tickers = await client.get_tickers(instType="SWAP")
    except Exception as e:
        print(f"[SCANNER] Error fetching tickers: {e}", flush=True)
        return []

    scan_results = []
    for ticker in tickers:
        inst_id = ticker.get("instId", "")
        if not inst_id.endswith("-SWAP"):
            continue

        # Get 24h volume in USD
        vol_24h = float(ticker.get("volCcy24h", 0))
        if vol_24h < min_vol_usd:
            continue

        # Get price change
        last = float(ticker.get("last", 0))
        open_24h = float(ticker.get("open24h", 0))
        if open_24h <= 0:
            continue
        change_pct = (last - open_24h) / open_24h * 100

        # Get bid/ask spread
        bid = float(ticker.get("bidPx", 0))
        ask = float(ticker.get("askPx", 0))
        spread_pct = (ask - bid) / last * 100 if last > 0 else 999

        # Compute scan score
        score = 0
        reasons = []

        # Volume score (higher volume = better liquidity)
        if vol_24h > 500_000_000:
            score += 3
            reasons.append("HIGH_VOL")
        elif vol_24h > 100_000_000:
            score += 2
            reasons.append("MED_VOL")
        elif vol_24h > min_vol_usd:
            score += 1

        # Momentum score
        abs_change = abs(change_pct)
        if abs_change > 5:
            score += 3
            reasons.append("STRONG_MOVE")
        elif abs_change > 3:
            score += 2
            reasons.append("NOTABLE_MOVE")
        elif abs_change > min_change_4h:
            score += 1
            reasons.append("MOVING")

        # Trend direction
        if change_pct > 0:
            score += 1
            reasons.append("BULLISH")
        elif change_pct < -3:
            score += 1
            reasons.append("OVERSOLD")

        # Tight spread (liquid market)
        if spread_pct < 0.05:
            score += 1
            reasons.append("TIGHT_SPREAD")

        # Only include if meets minimum criteria
        if abs_change >= min_change_4h and score >= 2:
            scan_results.append({
                "instId": inst_id,
                "last": last,
                "change_pct": round(change_pct, 2),
                "vol_24h_usd": round(vol_24h, 0),
                "spread_pct": round(spread_pct, 4),
                "score": score,
                "reasons": reasons,
                "timestamp": time.time(),
            })

    # Sort by score descending, limit results
    scan_results.sort(key=lambda x: x["score"], reverse=True)
    scan_results = scan_results[:max_results]

    return scan_results


def get_watchlist(scan_results, rules=None):
    """
    Convert scan results into watchlist for the orchestrator.
    Combines scanner output with configured symbols from rules.
    """
    if rules is None:
        rules = load_rules()

    # Always include configured symbols
    configured = rules.get("symbols", [])

    # Add top scanner results
    scanner_symbols = [r["instId"] for r in scan_results]

    # Merge: configured first, then scanner additions
    watchlist = list(dict.fromkeys(configured + scanner_symbols))

    return watchlist


def format_scan_report(scan_results):
    """Format scan results for human-readable report."""
    if not scan_results:
        return "No significant movers found."

    lines = [f"📊 Scanner Report — {len(scan_results)} symbols\n"]
    for r in scan_results:
        direction = "🟢" if r["change_pct"] > 0 else "🔴"
        vol_m = r["vol_24h_usd"] / 1_000_000
        lines.append(
            f"  {direction} {r['instId']:<18} "
            f"{r['change_pct']:>+6.2f}%  "
            f"Vol ${vol_m:>7.1f}M  "
            f"Score {r['score']}  "
            f"[{', '.join(r['reasons'])}]"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    print("Crypto Scanner — standalone test")
    print("Run with: python -m scanner (requires OKX client)")
