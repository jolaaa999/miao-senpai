"""长期记忆包：双层记忆架构的实现（参考《深入理解 AI Agent》第 3 章）。

上层是 cards（结构化事实，常驻上下文，提供概览），
下层是检索（按需从原始轨迹里取回细节）。
"""

from .cards import (
    CardStore,
    MemoryCard,
    build_cards_brief,
    card_importance,
    get_card_store,
    normalize_fact,
    reset_card_store,
)
from .extractor import build_extract_user_text, parse_cards, should_extract

__all__ = [
    "CardStore",
    "MemoryCard",
    "build_cards_brief",
    "build_extract_user_text",
    "card_importance",
    "get_card_store",
    "normalize_fact",
    "parse_cards",
    "reset_card_store",
    "should_extract",
]