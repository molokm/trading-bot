"""NLP signal extractor — parses text from Telegram/YouTube and extracts trade signals."""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from enum import Enum


class Side(Enum):
    LONG = "long"
    SHORT = "short"
    CLOSE = "close"
    UNKNOWN = "unknown"


@dataclass
class TradeSignal:
    source: str  # "telegram" or "youtube"
    source_url: str
    coin: str  # e.g. "BTC", "ETH"
    side: Side
    entry_price: Optional[float] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    confidence: float = 0.0  # 0..1
    raw_text: str = ""
    timestamp: str = ""
    is_exit: bool = False


# Coin patterns
COIN_MAP = {
    "btc": "BTC", "bitcoin": "BTC", "биткоин": "BTC", "биток": "BTC",
    "eth": "ETH", "ethereum": "ETH", "эфириум": "ETH", "эфир": "ETH",
    "sol": "SOL", "solana": "SOL",
    "xrp": "XRP", "ripple": "XRP",
    "doge": "DOGE", "dogecoin": "DOGE",
    "bnb": "BNB", "binance": "BNB",
    "ada": "ADA", "cardano": "ADA",
    "avax": "AVAX", "avalanche": "AVAX",
    "dot": "DOT", "polkadot": "DOT",
    "link": "LINK", "chainlink": "LINK",
    "matic": "MATIC", "polygon": "MATIC",
    "arb": "ARB", "arbitrum": "ARB",
    "op": "OP", "optimism": "OP",
    "apt": "APT", "aptos": "APT",
    "sui": "SUI",
    "hype": "HYPE",
}

# Long signals
LONG_PATTERNS = [
    r"(?:лонг|long|покупаю|покупаем|вхожу в лонг|открываю лонг|захожу в лонг|buy)",
    r"(?:будет расти|ожидается рост|цель вверх|upside)",
    r"(?:отскок|bounce|поддержка|support)",
    r"(?:ставлю на рост|ставит на рост|ставим на рост|жду рост)",
    r"(?:бычий|быки|bull)",
    r"(?:вырост|рост|вверх)",
]

# Short signals
SHORT_PATTERNS = [
    r"(?:шорт|short|продаю|продаем|вхожу в шорт|открываю шорт|захожу в шорт|sell)",
    r"(?:будет падать|ожидается падение|цель вниз|downside)",
    r"(?:сопротивление|resistance|откат|разворот)",
    r"(?:ставлю на падение|ставит на падение|ставим на падение|жду падение)",
    r"(?:медвежий|медведи|bear)",
    r"(?:снижени|падени|вниз|шорты|закрыл шорт|ловушк)",
]

# Close signals
CLOSE_PATTERNS = [
    r"(?:закрыл|закрыл позицию|close|закрываю|выход)",
    r"(?:фиксир|take profit|tp сработал|цель достигнута)",
]

# Price patterns
PRICE_PATTERNS = [
    r"(?:@|по цене|от|около|цена)\s*[:\s]?\s*(\d[\d\s]*[\d,\.])\s*(?:k|к|тыс)?",
    r"(\d[\d\s]*[\d,\.]+)\s*(?:k|к|тыс)\b",
    r"(?:SL|стоп|стоп-лосс|stop)\s*[:\s]?\s*(\d[\d\s]*[\d,\.]+)",
    r"(?:TP|тейк|тейк-профит|take profit|цель)\s*[:\s]?\s*(\d[\d\s]*[\d,\.]+)",
]


def _normalize_price(text: str) -> Optional[float]:
    """Normalize price string to float."""
    text = text.strip().replace(" ", "").replace(",", ".")
    multiplier = 1.0
    if text.endswith(("k", "к")):
        multiplier = 1000
        text = text[:-1]
    elif text.endswith("тыс"):
        multiplier = 1000
        text = text[:-3]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _extract_coin(text: str) -> str:
    """Extract coin name from text."""
    lower = text.lower()
    for pattern, symbol in COIN_MAP.items():
        if re.search(rf"\b{re.escape(pattern)}\b", lower):
            return symbol
    # Default to BTC if crypto context but no specific coin
    if any(w in lower for w in ["крипто", "crypto", "маркет", "market"]):
        return "BTC"
    return "BTC"


def _count_pattern_matches(text: str, patterns: List[str]) -> int:
    """Count how many patterns match in text."""
    count = 0
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            count += 1
    return count


def extract_signals(
    text: str,
    source: str = "telegram",
    source_url: str = "",
    timestamp: str = "",
) -> List[TradeSignal]:
    """Extract trade signals from text using pattern matching."""
    signals = []
    lower = text.lower()

    # Determine side
    long_score = _count_pattern_matches(lower, LONG_PATTERNS)
    short_score = _count_pattern_matches(lower, SHORT_PATTERNS)
    close_score = _count_pattern_matches(lower, CLOSE_PATTERNS)

    # Skip non-trade texts
    if long_score == 0 and short_score == 0 and close_score == 0:
        return []

    # Determine primary side
    if close_score > 0 and long_score == 0 and short_score == 0:
        side = Side.CLOSE
    elif long_score > short_score:
        side = Side.LONG
    elif short_score > long_score:
        side = Side.SHORT
    elif long_score == short_score and long_score > 0:
        # Ambiguous — skip
        return []
    else:
        return []

    # Extract coin
    coin = _extract_coin(text)

    # Extract prices
    entry_price = None
    sl_price = None
    tp_price = None

    for pattern in PRICE_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            price = _normalize_price(m)
            if price and price > 0:
                if entry_price is None:
                    entry_price = price
                elif sl_price is None:
                    sl_price = price
                elif tp_price is None:
                    tp_price = price

    # Check for specific SL/TP patterns
    sl_match = re.search(r"(?:SL|стоп|стоп-лосс|stop)\s*[:\s]?\s*(\d[\d\s]*[\d,\.]+)", text, re.IGNORECASE)
    if sl_match:
        sl_price = _normalize_price(sl_match.group(1))

    tp_match = re.search(r"(?:TP|тейк|тейк-профит|take profit|цель)\s*[:\s]?\s*(\d[\d\s]*[\d,\.]+)", text, re.IGNORECASE)
    if tp_match:
        tp_price = _normalize_price(tp_match.group(1))

    # Calculate confidence
    confidence = min(1.0, (long_score + short_score + close_score) * 0.25)

    signal = TradeSignal(
        source=source,
        source_url=source_url,
        coin=coin,
        side=side,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        confidence=confidence,
        raw_text=text[:500],
        timestamp=timestamp,
        is_exit=(side == Side.CLOSE),
    )

    signals.append(signal)
    return signals


def test_extractor():
    """Test the signal extractor on sample texts."""
    test_cases = [
        "Летим на 65к$, господа.",
        "Открыл шорт ETH по текущей цене.",
        "Шорт по ETH прям-таки напрашивается от уровня 2131.",
        "🟢 BTC будет расти. Или ловушка?!",
        "Шортим. Ждем. Верим.",
        "Закрыл шорт по BTC, зафиксировал прибыль",
        "Лонг BTC по 63500, стоп 62800, тейк 65000",
        "Рынок зашевелился, буду открывать новые позиции",
        "Игла мировой экономики — цена барреля",
        "5 премьеров и не может принять бюджет",
    ]

    for text in test_cases:
        signals = extract_signals(text, source="test")
        if signals:
            s = signals[0]
            print(f"[{s.side.value:6}] {s.coin:4} conf={s.confidence:.2f} entry={s.entry_price} sl={s.sl_price} tp={s.tp_price}")
            print(f"  Text: {text[:80]}")
        else:
            print(f"[SKIP] {text[:80]}")
        print()


if __name__ == "__main__":
    test_extractor()
