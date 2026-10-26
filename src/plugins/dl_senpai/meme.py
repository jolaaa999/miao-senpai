"""梗密度：这一轮该不该玩梗、玩到什么程度。

来源：sakurawwwxh/qq-agent-plus 的 memeDensityPrompt（src/llm/humanize.js）。
它把「用梗」做成**每轮一贴的分档提示**，而不是人设里一句「可以接梗」——
人设里那种写法等于没有约束：要么一轮堆三个梗，要么正经答疑里也硬塞。

我们这边补两件事：
1. 分档 + 纪律（一轮最多一个、不解释、不复读、不贴合就不用）。
2. 自动定档：正经答疑不用梗、真倾诉不用梗、对方自己在用梗就接一句。

措辞上刻意不出现「他/对方/回一句」这类词：
实测提示词里一旦出现第三人称或祈使短语，模型会切换成「替用户起草回复」，
输出「回他：…」「给你几个版本」。这条教训在 tone.build_tone_hint 里也有。
"""

from __future__ import annotations

# 档位（配置里手改坏了按 auto 处理，不把整段提示词弄丢——原仓库同样的兜底）
MEME_LEVELS = ("auto", "off", "light", "normal", "heavy")

_HINTS = {
    "off": "【梗密度】这轮不用梗：听到梗只按意思理解，不接、不跟着复读。",
    "light": "【梗密度】克制的：接住刚冒出来的那个就好，不主动抛新的，一轮最多一个。",
    "normal": (
        "【梗密度】正常的：刚冒出来的梗可以接一句；话题贴合才主动用，"
        "一轮最多一个；不解释、不复读，不贴合就不用。"
    ),
    "heavy": "【梗密度】活跃的：闲聊里可以主动带梗，但别硬塞；一轮最多一个。",
}

# 对方在玩梗的信号。只为「接住对方已经在用的梗」判断，不作为要不要用梗的依据。
MEME_MARKERS = (
    "笑死",
    "绷不住",
    "崩不住",
    "破防",
    "离谱",
    "寄了",
    "乐死",
    "绝了",
    "无语",
    "难绷",
    "yyds",
    "666",
    "awsl",
    "蚌埠住",
    "太草了",
    "哈哈哈哈",
    "裂开",
    "栓Q",
)

# 正经答疑/严肃话题里不用梗。命中这些词就按「不贴合」处理。
_SERIOUS_MARKERS = (
    "怎么",
    "为什么",
    "如何",
    "报错",
    "代码",
    "配置",
    "配装",
    "步骤",
    "教程",
    "论文",
    "面试",
    "考试",
    "挂科",
    "生病",
    "医院",
    "分手",
)


def normalize_level(value: str | None) -> str:
    """配置值归一；认不出来当 auto。"""
    text = str(value or "").strip().lower()
    return text if text in MEME_LEVELS else "auto"


def has_meme_marker(text: str) -> bool:
    source = str(text or "")
    return any(marker in source for marker in MEME_MARKERS)


def resolve_level(
    configured: str | None,
    *,
    verdict: str = "normal",
    casual: bool = True,
    user_text: str = "",
) -> str:
    """定这一轮的档位。显式配了 off/light/normal/heavy 就照配置。"""
    level = normalize_level(configured)
    if level != "auto":
        return level

    # 真倾诉：这时候玩梗是没接住情绪
    if verdict == "distress":
        return "off"
    # 吐槽：可以顺着损一句，但别抛新梗
    if verdict == "vent":
        return "light"
    # 正经问题：不贴合，不用
    source = str(user_text or "")
    if any(marker in source for marker in _SERIOUS_MARKERS):
        return "off"
    # 对方自己在用梗：接住是本分
    if has_meme_marker(source):
        return "normal"
    # 随口闲聊：克制档，不主动抛
    return "light" if casual else "off"


def build_meme_hint(level: str) -> str:
    """渲染这一轮的一行提示；off 也返回一行，让模型明确知道这轮不用。"""
    return _HINTS.get(normalize_level(level), "")
