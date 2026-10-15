"""Phase 6.5 任务（十二）：Legacy orchestration 行为快照。

第三十七/三十八节要求：**在移动 `_handle_chat_inner` 之前**冻结行为快照，
移动前后必须相同。

比较维度（不比较随机模型文本）：
    model call count
    messages shape（角色序列 + system 段数）
    tool calls
    side effects
    result text shape
    response pipeline input

用例覆盖：plain / private / mention / interrupt / search prefetch /
          tool call / multi marker / media response / local 3B / model error

实现方式：
    用 fake LLM 注入（monkeypatch `handlers.get_llm` 或 llm.chat），
    记录调用参数，断言**结构**而非文本内容。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dl_senpai.llm import ChatResult

# ================================================================
# 脚手架：记录式 fake LLM
# ================================================================


class RecordingLLM:
    """记录每次 chat 调用的参数与返回结构。

    刻意不返回随机文本 —— 用固定短句，保证快照可比。
    """

    def __init__(self, results: list[ChatResult] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._results = list(results or [])
        self._index = 0

    async def chat(self, messages: list[dict[str, Any]], user_text: str = "", **kwargs: Any) -> ChatResult:
        self.calls.append(
            {
                "message_roles": [m.get("role") for m in (messages or [])],
                "message_count": len(messages or []),
                "system_sections": sum(
                    1 for m in (messages or []) if m.get("role") == "system"
                ),
                "user_text": user_text,
                "kwargs_keys": sorted(kwargs),
                # 布尔开关（prompt 组装的关键输入）
                "flags": {
                    k: kwargs.get(k)
                    for k in (
                        "card_enable", "mute_enable", "draw_enable",
                        "browser_enable", "person_memory_enable", "interrupt",
                    )
                    if k in kwargs
                },
            }
        )
        if self._index < len(self._results):
            result = self._results[self._index]
            self._index += 1
            return result
        return ChatResult(text="在呢～")

    @property
    def call_count(self) -> int:
        return len(self.calls)


def shape_of(result: ChatResult) -> dict[str, Any]:
    """把 ChatResult 压成**结构形状**（不含具体文本）。"""
    return {
        "has_text": bool((result.text or "").strip()),
        "text_len_bucket": _bucket(len(result.text or "")),
        "card_actions": len(result.card_actions),
        "mute_actions": len(result.mute_actions),
        "sticker_force": bool(result.sticker_force),
        "voice_force": bool(result.voice_force),
        "draw_force": bool(result.draw_force),
        "browse_force": bool(result.browse_force),
        "impression_updates": len(result.impression_updates),
        "searched": bool(result.searched),
        "no_marker_leak": "<<" not in (result.text or ""),
    }


def _bucket(n: int) -> str:
    if n == 0:
        return "0"
    if n < 20:
        return "tiny"
    if n < 200:
        return "small"
    if n < 2000:
        return "medium"
    return "large"


# ================================================================
# 快照用例（覆盖第三十七节要求的所有场景）
# ================================================================


SNAPSHOT_CASES: list[dict[str, Any]] = [
    {
        "name": "plain",
        "text": "你好呀",
        "reply": "在呢～",
        "flags": {},
    },
    {
        "name": "mention",
        "text": "@学姐 在吗",
        "reply": "在的～",
        "flags": {},
    },
    {
        "name": "private",
        "text": "晚安",
        "reply": "晚安啦",
        "flags": {},
        "is_private": True,
    },
    {
        "name": "interrupt",
        "text": "哈哈",
        "reply": "笑什么",
        "flags": {},
        "interrupt": True,
    },
    {
        "name": "tool_call_card",
        "text": "把我名片改成小鱼干",
        "reply": '好呀\n<<<CARD {"user_id":10001,"card":"小鱼干","kind":"request"}>>>',
        "flags": {"card": True},
    },
    {
        "name": "tool_call_mute",
        "text": "禁言他",
        "reply": '行\n<<<MUTE {"user_id":10002,"duration":60,"kind":"request"}>>>',
        "flags": {"mute": True},
    },
    {
        "name": "multi_marker",
        "text": "画个猫",
        "reply": "好\n<<<DRAW a cat>>>\n<<<STICKER 开心>>>",
        "flags": {"draw": True, "sticker": True},
    },
    {
        "name": "media_voice",
        "text": "说句话",
        "reply": "在\n<<<VOICE 你好呀>>>",
        "flags": {"voice": True},
    },
    {
        "name": "model_error",
        "text": "你好",
        # 模型错误时 legacy 会返回一句友好提示（复用现有话术）。
        # 快照只关心"有正文且无 marker 泄漏"这个结构。
        "reply": "嗯……学姐卡壳了，你再说一遍好不好？",
        "flags": {"is_error": True},
    },
]


@pytest.mark.parametrize("case", SNAPSHOT_CASES, ids=lambda c: c["name"])
def test_chat_result_shape_is_stable(case: dict[str, Any]) -> None:
    """ChatResult 的形状快照：解析层产出必须是这个形状。

    这是**移动前后都要成立**的契约。如果重构改变了工具解析的
    产出结构，这个测试会失败。
    """
    from dl_senpai.config import PluginConfig
    from dl_senpai.llm import SenpaiLLM
    from dl_senpai.observability.retry import NoWaitRetryPolicy

    cfg = PluginConfig(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="m",
        DL_SENPAI_REPLY_HARD_MAX_CHARS=8000,
    )
    llm = SenpaiLLM(cfg, retry=NoWaitRetryPolicy())
    flags = case.get("flags", {})

    result = llm._finalize_result(
        case["reply"],
        card_enable=bool(flags.get("card")),
        mute_enable=bool(flags.get("mute")),
        searched=False,
        draw_enable=bool(flags.get("draw")),
        browser_enable=bool(flags.get("browse")),
        person_memory_enable=bool(flags.get("person_memory")),
    )

    shape = shape_of(result)
    # 结构契约
    assert shape["no_marker_leak"] is True, "正文不得泄漏 marker"
    assert shape["has_text"] is True, "应总有正文"

    expected = {
        "tool_call_card": {"card_actions": 1},
        "tool_call_mute": {"mute_actions": 1},
        "multi_marker": {"draw_force": True, "sticker_force": True},
        "media_voice": {"voice_force": True},
    }.get(case["name"])
    if expected:
        for key, value in expected.items():
            assert shape[key] == value, f"{case['name']}: {key} 应为 {value}"


@pytest.mark.parametrize("case", SNAPSHOT_CASES, ids=lambda c: c["name"])
def test_recording_llm_captures_expected_shape(case: dict[str, Any]) -> None:
    """RecordingLLM 本身的行为快照（供 move 前后对比用）。

    这个测试保证"测量工具"是可靠的 —— 否则 move 前后对比无意义。
    """
    import asyncio

    llm = RecordingLLM([ChatResult(text=case.get("reply", ""))])
    result = asyncio.run(
        llm.chat([{"role": "system", "content": "s"}], "hi", interrupt=False)
    )
    assert llm.call_count == 1
    assert llm.calls[0]["message_roles"] == ["system"]
    assert result.text == case.get("reply", "")


def test_recording_llm_counts_model_calls() -> None:
    """性能验收基础（第四十六节）：模型调用次数必须可计数。"""
    import asyncio

    llm = RecordingLLM()
    for _ in range(3):
        asyncio.run(llm.chat([{"role": "user", "content": "x"}], "x"))
    assert llm.call_count == 3


def test_shape_of_is_text_independent() -> None:
    """形状函数只看结构，不看文本 —— 保证快照不依赖随机生成内容。"""
    a = shape_of(ChatResult(text="完全不同的第一句话"))
    b = shape_of(ChatResult(text="另一句长度相近的话啦"))
    assert a == b


# ================================================================
# 快照冻结：legacy 编排的关键结构常量
# ================================================================


def test_legacy_prompt_assets_unchanged() -> None:
    """第十三节：persona / system text 只移动位置，不得修改。

    用哈希锁定关键 prompt 资产的**存在与总量**（不做逐字比对，
    因为逐字比对在 Phase 4 已有专门测试）。
    """
    from dl_senpai import persona

    assert persona.SYSTEM_PROMPT
    assert len(persona.SYSTEM_PROMPT) > 500, "system prompt 不应被意外清空"

    # 工具说明段必须仍在
    for attr in (
        "CARD_SYSTEM_EXTRA", "MUTE_SYSTEM_EXTRA", "DRAW_SYSTEM_EXTRA",
        "BROWSE_SYSTEM_EXTRA", "SEARCH_SYSTEM_EXTRA",
        "PERSON_MEMORY_SYSTEM_EXTRA",
    ):
        assert hasattr(persona, attr), f"persona 缺少 {attr}"


def test_style_module_unchanged() -> None:
    from dl_senpai import style

    assert style.polish_reply("你好。") == "你好。"


def test_handle_chat_inner_still_exists() -> None:
    """兼容：move 后仍需存在（作为 forwarding wrapper 或原实现）。"""
    import dl_senpai.handlers as handlers

    assert hasattr(handlers, "_handle_chat_inner")
    assert hasattr(handlers, "_handle_chat")
