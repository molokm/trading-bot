"""Strategy card / Telegram text helpers — AI-only product."""

BOT_CARDS = []
BACKTEST_SUMMARY = {}


def telegram_metrics_block(html: bool = False) -> str:
    if html:
        return (
            "<b>AI Discretionary 1H</b>\n"
            "LLM-бот: вход/выход по сигналам нейросети на 1H.\n"
            "Монеты: BTC · ETH · SOL · XRP"
        )
    return (
        "AI Discretionary 1H — LLM на 1H, BTC/ETH/SOL/XRP"
    )


def telegram_profile_description() -> str:
    return (
        "COPIX — торговый терминал с AI Discretionary 1H. "
        "Бот анализирует рынок и открывает/закрывает позиции на OKX."
    )


def telegram_short_description() -> str:
    return "AI-бот для OKX: Discretionary 1H"


def strategy_versions_line() -> str:
    return "AI Discretionary 1H"


def cagr_range_str() -> str:
    return "—"
