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
        f"• {b}Стратегия{e}: {name} {ver}",
        f"• {b}Таймфрейм{e}: 1H · монеты BTC, ETH, SOL, XRP",
        f"• {b}Решение{e}: LLM + индикаторы (вход/выход), риск на сделку ограничен",
        f"• {b}Плечо{e}: умеренное (до ×2–3), контроль позиций",
        f"• Ликвидаций в штатном режиме: {b}цель 0{e} (не гарантия)",
        "• Результаты live — в мини-апе и на дашборде; прошлые ≠ будущие",
    ])


def telegram_profile_description() -> str:
    return (
        f"COPIX — алгоритмический трейдер на OKX.\n"
        f"Единственная стратегия: {strategy_versions_line()}.\n"
        "1H, BTC/ETH/SOL/XRP, LLM + риск-фильтры.\n"
        "Сигналы бесплатно в боте · Pro — торговля на вашем счёте.\n"
        "Не финансовый совет."
    )


def telegram_short_description() -> str:
    return (
        f"{strategy_versions_line()} на OKX (1H). "
        "Сигналы бесплатно · Pro — автоторговля на вашем счёте."
    )
