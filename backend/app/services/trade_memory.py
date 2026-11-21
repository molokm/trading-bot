"""Deterministic settlement memory for closed trades.

Every close is scored on three axes:
  * direction-adjusted move over the holding period,
  * alpha vs BTC buy & hold over the same period,
  * R-multiple (PnL / planned risk).

The score is written into the trade journal and reused by the LLM prompt
(`daily_lessons`, `journal_tail`) so the desk learns from settled outcomes,
not from raw PnL that may be pure market beta.

Pure functions only — no I/O, no exchange calls.
"""
from __future__ import annotations

from typing import Iterable

EDGE_ALPHA_PCT = 0.15  # |alpha| below this is treated as beta/noise


def settle_trade(
    *,
    side: str,
    entry: float,
    exit: float,
    pnl: float,
    risk_usd: float = 0.0,
    btc_entry: float = 0.0,
    btc_exit: float = 0.0,
) -> dict:
    """Return move_pct / btc_pct / alpha_pct / r_mult / verdict for one close."""
    try:
        entry = float(entry or 0)
        exit = float(exit or 0)
        pnl = float(pnl or 0)
        risk_usd = float(risk_usd or 0)
    except (TypeError, ValueError):
        return {"verdict": "unsettled"}

    if entry <= 0 or exit <= 0:
        return {"verdict": "unsettled"}

    move = (exit / entry - 1.0) * 100.0
    if str(side or "").lower() == "short":
        move = -move

    btc_pct = None
    alpha = None
    try:
        btc_entry = float(btc_entry or 0)
        btc_exit = float(btc_exit or 0)
        if btc_entry > 0 and btc_exit > 0:
            btc_pct = round((btc_exit / btc_entry - 1.0) * 100.0, 3)
            # Benchmark is long BTC: a short's beta is -btc_pct, not +btc_pct,
            # otherwise shorts in a red market look like pure alpha.
            beta_move = btc_pct if str(side or "").lower() != "short" else -btc_pct
            alpha = round(move - beta_move, 3)
    except (TypeError, ValueError):
        btc_pct = None
        alpha = None

    r_mult = round(pnl / risk_usd, 3) if risk_usd > 0 else None

    if alpha is None:
        verdict = "unsettled"
    elif pnl > 0 and alpha > EDGE_ALPHA_PCT:
        verdict = "edge"
    elif pnl > 0:
        verdict = "beta_win"
    elif alpha < -EDGE_ALPHA_PCT:
        verdict = "bad_thesis"
    else:
        verdict = "noise_loss"

    return {
        "move_pct": round(move, 3),
        "btc_pct": btc_pct,
        "alpha_pct": alpha,
        "r_mult": r_mult,
        "verdict": verdict,
    }


def build_lessons(journal: Iterable[dict], limit: int = 3) -> list[str]:
    """Derive up to `limit` actionable rules from the recent journal."""
    rows = [r for r in (journal or []) if isinstance(r, dict)][-30:]
    if len(rows) < 3:
        return []

    lessons: list[str] = []
    by_coin_side: dict[tuple, dict] = {}
    by_coin: dict = {}
    by_reason: dict = {}
    losses = 0
    beta_wins: dict[str, int] = {}

    for r in rows:
        coin = str(r.get("coin") or r.get("symbol") or "?").replace("-USDT-SWAP", "")
        side = str(r.get("side") or "?").lower()
        pnl = float(r.get("pnl") or 0)
        reason = str(r.get("reason") or "unknown")
        verdict = str(r.get("verdict") or "")
        alpha = r.get("alpha_pct")

        key = (coin, side)
        by_coin_side.setdefault(key, {"n": 0, "bad": 0, "pnl": 0.0})
        by_coin_side[key]["n"] += 1
        by_coin_side[key]["pnl"] += pnl
        if verdict in ("bad_thesis", "noise_loss"):
            by_coin_side[key]["bad"] += 1

        by_coin.setdefault(coin, {"pnl": 0.0, "n": 0})
        by_coin[coin]["pnl"] += pnl
        by_coin[coin]["n"] += 1

        if pnl <= 0:
            losses += 1
            by_reason[reason] = by_reason.get(reason, 0) + 1
        elif verdict == "beta_win":
            beta_wins[coin] = beta_wins.get(coin, 0) + 1

    # Side-specific anti-pattern with negative alpha
    bad = sorted(
        ((k, v) for k, v in by_coin_side.items() if v["bad"] >= 2),
        key=lambda kv: kv[1]["bad"],
        reverse=True,
    )
    if bad:
        (coin, side), v = bad[0]
        lessons.append(
            f"Avoid {coin} {side}: negative alpha on {v['bad']}/{v['n']} closes — "
            "no edge, not bad luck."
        )

    # Wins that were just BTC beta
    if beta_wins:
        coin, n = max(beta_wins.items(), key=lambda kv: kv[1])
        if n >= 2:
            lessons.append(
                f"{coin} wins are mostly BTC beta (alpha<{EDGE_ALPHA_PCT:+.2f}%) "
                f"x{n} — do not add size on it."
            )

    # Worst coin
    if by_coin:
        coin, v = min(by_coin.items(), key=lambda kv: kv[1]["pnl"])
        if v["pnl"] < 0 and v["n"] >= 2:
            lessons.append(
                f"Avoid weak setups on {coin} (recent sum PnL {v['pnl']:+.1f} "
                f"over {v['n']} trades)."
            )

    # Dominant loss exit path
    if by_reason:
        reason, n = max(by_reason.items(), key=lambda kv: kv[1])
        if n >= 2:
            lessons.append(
                f"Frequent loss exit={reason} x{n} — tighten entry or exit "
                "earlier on that path."
            )

    if losses >= 3 and len(rows) >= 5:
        lessons.append(
            "Recent losses cluster — prefer hold unless align>=0.7 and RR>=2; cut size."
        )
    if not lessons:
        lessons.append("No strong anti-pattern — keep requiring RR>=1.8 and bar-close only.")

    out: list[str] = []
    for line in lessons:
        if line not in out:
            out.append(line)
    return out[:limit]
