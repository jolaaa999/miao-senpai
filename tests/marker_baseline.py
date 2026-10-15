"""Marker 容错闭合行为快照（Phase 4 基线）。

本文件记录 **Phase 4 之前** 8 种 marker 的真实解析行为。
它不是"理想行为"，而是"当前生产行为"——Phase 4 迁移必须逐字保持。

关键发现（探查得出，非推测）：

| marker | >>> | >> | ＞＞＞ | 】 | 备注 |
|---|---|---|---|---|---|
| CARD       | ✅ | ❌ | ❌ | ❌ | 严格要求 `>>>` |
| MUTE       | ✅ | ❌ | ❌ | ❌ | 严格要求 `>>>` |
| IMPRESSION | ✅ | ❌ | ❌ | ❌ | 严格要求 `>>>` |
| DRAW       | ✅ | ✅ | 未测 | ✅ | 宽容错 |
| BROWSE     | ✅ | ✅ | 未测 | 未测 | 宽容错 |
| VOICE      | ✅ | 未测 | 未测 | ✅ | 宽容错 |
| SEARCH     | ✅ | ✅ | ✅ | ✅ | 最宽 |
| STICKER    | 仅带关键词 | — | — | — | 裸 `<<<STICKER>>>` **不解析** |

这些差异是**历史调优的结果**，不能为了"统一"而抹平。
"""

from __future__ import annotations

# 每个 marker 的容错能力（True = 能解析）
TOLERANCE: dict[str, dict[str, bool]] = {
    "CARD": {">>>": True, ">>": False, "＞＞＞": False, "】": False},
    "MUTE": {">>>": True, ">>": False, "＞＞＞": False, "】": False},
    "IMPRESSION": {">>>": True, ">>": False, "＞＞＞": False, "】": False},
    "DRAW": {">>>": True, ">>": True, "】": True},
    "BROWSE": {">>>": True, ">>": True},
    "VOICE": {">>>": True, "】": True},
    "SEARCH": {">>>": True, ">>": True, "＞＞＞": True, "】": True},
}

# 裸 marker（无参数）的行为
BARE_MARKER: dict[str, bool] = {
    "CARD": False,        # 需要 JSON payload
    "MUTE": False,
    "DRAW": True,         # <<<DRAW>>> → force=True, prompt=">"
    "BROWSE": True,       # <<<BROWSE>>> → 1 task
    "STICKER": False,     # 裸 STICKER 不解析（需关键词）
    "VOICE": True,        # <<<VOICE>>> → force=True
    "IMPRESSION": False,
    "SEARCH": False,
}

# 损坏 payload 的行为：正文里的 marker 被剥掉，但不产生 action
BAD_PAYLOAD_STRIPS_TEXT: dict[str, bool] = {
    "CARD": True,         # '{bad json}' → text 剥掉 marker, actions=[]
    "MUTE": True,
    "IMPRESSION": True,
}

__all__ = ["TOLERANCE", "BARE_MARKER", "BAD_PAYLOAD_STRIPS_TEXT"]
