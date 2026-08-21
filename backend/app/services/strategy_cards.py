"""Single source of truth for public strategy card metrics and Telegram blurbs.

Update BACKTEST_SUMMARY when strategy versions / BT numbers change — UI tracker
(`/api/tracker`) and Telegram marketing copy both read from here so they stay aligned.
"""
from __future__ import annotations

BACKTEST_SUMMARY = {
    "note": (
        "Результаты бэктестов на реальных свечах OKX (нативные 1D, 10 монет, 2023–2026). "
        "Не гарантия будущей доходности. Full-sample после тюнинга ≠ чистый OOS; "
        "см. external/STAGE5_EVAL.md."
    ),
    "periods": [
        {
            "label": "Momentum Rotation v6.2 2023–2026",
            "return_pct": 572.8,
            "max_dd_pct": 42.4,
            "cagr_pct": 78.5,
            "sharpe": 1.45,
        },
        {
            "label": "Impulse 1D v4 2023–2026",
            "return_pct": 555.0,
            "max_dd_pct": 36.5,
            "cagr_pct": 77.3,
            "sharpe": 1.41,
        },
        {
            "label": "Портфель 50/50 2023–2026",
            "return_pct": 383.9,
            "max_dd_pct": 36.2,
            "cagr_pct": 61.6,
            "sharpe": 1.60,
        },
    ],
    "win_rate_backtest_pct": 55.0,
    "liquidations": 0,
}


def _period(name_substr: str):
    for p in BACKTEST_SUMMARY["periods"]:
        if name_substr.lower() in p["label"].lower():
            return p
    return None


def strategy_versions_line() -> str:
    try:
        from .rotation_strategy import STRATEGY_VERSION as mom_v
        from .impulse_strategy import STRATEGY_VERSION as imp_v
        return f"Momentum Rotation {mom_v} + Impulse 1D {imp_v}"
    except Exception:
        return "Momentum Rotation + Impulse 1D"


def cagr_range_str() -> str:
    cagrs = [p["cagr_pct"] for p in BACKTEST_SUMMARY["periods"] if "Портфель" not in p["label"]]
    if not cagrs:
        return "~60%"
    lo, hi = min(cagrs), max(cagrs)
    if abs(lo - hi) < 1:
        return f"~{lo:.0f}%"
    return f"~{lo:.0f}–{hi:.0f}%"


def telegram_metrics_block(html: bool = True) -> str:
    mom = _period("Momentum")
    imp = _period("Impulse")
    port = _period("Портфель")
    wr = BACKTEST_SUMMARY.get("win_rate_backtest_pct", 55)
    liq = BACKTEST_SUMMARY.get("liquidations", 0)
    b, e = ("<b>", "</b>") if html else ("", "")
    lines = []
    if mom:
        lines.append(
            f"• {b}Momentum{e}: CAGR ~{mom['cagr_pct']:.0f}% / год, "
            f"MaxDD −{mom['max_dd_pct']:.0f}%, Sharpe ~{mom['sharpe']:.2f} "
            f"(full ~+{mom['return_pct']:.0f}%)"
        )
    if imp:
        lines.append(
            f"• {b}Impulse{e}: CAGR ~{imp['cagr_pct']:.0f}% / год, "
            f"MaxDD −{imp['max_dd_pct']:.0f}%, Sharpe ~{imp['sharpe']:.2f} "
            f"(full ~+{imp['return_pct']:.0f}%)"
        )
    if port:
        lines.append(
            f"• {b}Портфель 50/50{e}: CAGR ~{port['cagr_pct']:.0f}% / год, "
            f"MaxDD −{port['max_dd_pct']:.0f}%"
        )
    lines.append(f"• Win rate (ориентир) ~{wr:.0f}%, ликвидаций в BT: {b}{liq}{e}")
    lines.append("• Full-sample после тюнинга ≠ чистый OOS; прошлые результаты ≠ гарантия")
    return "\n".join(lines)


def telegram_profile_description() -> str:
    return (
        f"Алгоритмический трейдер: {strategy_versions_line()} на OKX.\n"
        f"Дневные стратегии, риск-менеджмент, 0 ликвидаций в BT.\n"
        f"Ориентиры full-sample: Momentum/Impulse CAGR {cagr_range_str()} в год "
        f"(см. карточки в мини-апе).\n"
        "Не финансовый совет."
    )


def telegram_short_description() -> str:
    return (
        f"{strategy_versions_line()}. "
        f"Бэктест CAGR {cagr_range_str()} (full-sample, не чистый OOS)."
    )
