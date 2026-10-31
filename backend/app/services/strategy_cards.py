"""Strategy card / Telegram text helpers — AI-only product."""

BOT_CARDS = []
BACKTEST_SUMMARY = {}

_AI_DESC = (
    "AI Discretionary 1H v1.10 — защитный режим: "
    "BTC · ETH · SOL · XRP, высокий порог входа, ранний трейл и безубыток, риск ~1.5% на сделку."
)


def telegram_metrics_block(html: bool = False) -> str:
    if html:
        return (
            "<b>AI Discretionary 1H</b> · v1.10 defensive\n"
            "Защитный режим: высокий порог входа, ранний трейл, лимит дня −2%.\n"
            "Монеты: <b>BTC · ETH · SOL · XRP</b>"
        )
    return _AI_DESC


def telegram_profile_description() -> str:
    return (
        "COPIX — AI Discretionary 1H (v1.10). "
        "Торгует BTC, ETH, SOL, XRP на OKX: вход по ИИ, стопы и трейл автоматически."
    )


def telegram_short_description() -> str:
    return "AI Discretionary 1H · BTC ETH SOL XRP"


def strategy_versions_line() -> str:
    return "AI Discretionary 1H v1.10"


def cagr_range_str() -> str:
    return "—"
