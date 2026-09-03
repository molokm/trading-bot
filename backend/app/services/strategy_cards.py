"""Single source of truth for public strategy metrics and Telegram blurbs.

AI-only product mode: marketing copy and tracker summary describe
AI Discretionary 1H (not the retired Momentum/Impulse pair).
"""
from __future__ import annotations

BACKTEST_SUMMARY = {
    "note": (
        "AI Discretionary 1H — решения LLM + фильтры индикаторов (EMA/ADX/ROC) "
        "на таймфрейме 1H, монеты BTC/ETH/SOL/XRP, OKX SWAP. "
        "Прошлые результаты и демо-сессии не гарантируют будущую доходность."
    ),
    "periods": [
        {
            "label": "AI Discretionary 1H (live / adaptive)",
            "return_pct": 0.0,
            "max_dd_pct": 0.0,
            "cagr_pct": 0.0,
            "sharpe": 0.0,
        },
    ],
    "win_rate_backtest_pct": 0.0,
    "liquidations": 0,
    "ai_only": True,
}


def _ai_version() -> str:
    try:
        from .ai_strategy import STRATEGY_VERSION
        return STRATEGY_VERSION
    except Exception:
        return "v1.3"


def _ai_name() -> str:
    try:
        from .ai_strategy import STRATEGY_NAME
        return STRATEGY_NAME
    except Exception:
        return "AI Discretionary 1H"


def strategy_versions_line() -> str:
    return f"{_ai_name()} {_ai_version()}"


def cagr_range_str() -> str:
    """No fixed marketing CAGR for live AI — honest placeholder."""
    return "live (без фиксированного CAGR)"


def telegram_metrics_block(html: bool = True) -> str:
    b, e = ("<b>", "</b>") if html else ("", "")
    name = _ai_name()
    ver = _ai_version()
    return "\n".join([
        f"• {b}Как решает{e}: AI (LLM) + индикаторы, решения каждые ~2 мин",
        f"• {b}Что торгует{e}: BTC, ETH, SOL, XRP на OKX (USDT-SWAP)",
        f"• {b}Риск{e}: лимит на сделку, стопы, умеренное плечо (до ×2–3)",
        f"• {b}Ликвидации{e}: цель 0 при штатных настройках (не гарантия)",
        "• Результаты live — в мини-апе и на дашборде; прошлые ≠ будущие",
    ])


def telegram_profile_description() -> str:
    return (
        f"COPIX — AI-трейдер на OKX.\n"
        "AI анализирует рынок и торгует BTC, ETH, SOL, XRP — сам открывает, "
        "ведёт и закрывает позиции.\n"
        "Сигналы бесплатно в боте · Pro — автоторговля на вашем счёте.\n"
        "Не финансовый совет."
    )


def telegram_short_description() -> str:
    return (
        f"AI-трейдер на OKX: торгует BTC, ETH, SOL, XRP. "
        "Сигналы бесплатно · Pro — автоторговля на вашем счёте."
    )
