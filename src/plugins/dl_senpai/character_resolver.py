"""判断「这句话是不是在要一个已知 IP 角色」，并把名字变成实体。

为什么必须单独一层：生图链路上真正致命的不是画质，而是**身份丢失**。
「画一张艾雅法拉」如果在进入提示词之前就被当成普通文本处理，模型只能凭记忆
猜设定，最后出来的是一张「泛二次元羊角少女」——五官、发色、服装结构全都不对，
用户一眼认不出是谁。

这一层只做判断，不做编造：
- 认不出来就返回 unknown，把原话原样交给普通生图链路（绝不猜发型、猜服装）；
- 「小羊」这类弱别名必须同时出现作品名才认，否则「画一只小羊」会变成艾雅法拉；
- 用户明说「原创」时直接放弃识别，哪怕句子里带了角色名。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .character_registry import (
    CharacterMatch,
    CharacterProfile,
    CharacterRegistry,
    get_character_registry,
)

# 用户明确要原创角色时的信号词。命中就不做角色识别——
# 「画一个原创的白发猫娘」不该被任何角色档案接管。
_ORIGINAL_RE = re.compile(r"原创|自创|自己设计|我的角色|\boc\b", re.IGNORECASE)


@dataclass(frozen=True)
class CharacterResolution:
    """一次角色识别的结果。

    kind 只有两种取值：known_character / unknown。
    调用方按它决定走角色管线还是普通生图管线。
    """

    kind: str
    profile: CharacterProfile | None = None
    confidence: float = 0.0
    matched: str = ""
    reason: str = ""
    match: CharacterMatch | None = None

    @property
    def is_known(self) -> bool:
        return self.kind == "known_character" and self.profile is not None


def resolve_character(
    user_request: str,
    *,
    draft_prompt: str = "",
    config: Any = None,
    registry: CharacterRegistry | None = None,
) -> CharacterResolution:
    """从用户这句话（以及模型写的草稿）里认出角色。

    为什么要连草稿一起看：用户可能只说「画那个银发的」，而名字只出现在
    模型写的提示词里；反过来也常见。两边都扫一遍才不漏。
    """
    if not bool(getattr(config, "character_enable", True)):
        return CharacterResolution(kind="unknown", reason="disabled")

    if _ORIGINAL_RE.search(user_request or ""):
        # 明说要原创：即使句子里出现角色名也不接管（用户是在刻意绕开 IP）
        return CharacterResolution(kind="unknown", reason="original_requested")

    reg = registry if registry is not None else get_character_registry(config)
    match = reg.find_in_text(user_request or "", draft_prompt or "")
    if match is None:
        return CharacterResolution(kind="unknown", reason="no_match")

    threshold = float(getattr(config, "character_min_confidence", 0.8) or 0.8)
    if match.confidence < threshold:
        return CharacterResolution(
            kind="unknown",
            confidence=match.confidence,
            matched=match.matched_alias,
            reason=f"low_confidence:{match.confidence:.2f}",
            match=match,
        )

    return CharacterResolution(
        kind="known_character",
        profile=match.profile,
        confidence=match.confidence,
        matched=match.matched_alias,
        reason=match.reason(),
        match=match,
    )


__all__ = ["CharacterResolution", "resolve_character"]
