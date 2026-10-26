"""`llm.chat()` 出站后处理的**等价性harness**。

背景：`chat()` 曾是 625 行的单函数，混了开关解析、预取、消息组装、多轮调用、
接地重试、出站后处理、收尾七件事。要把它切开，就必须先有"改完行为一模一样"的机器判据，
否则任何一次搬迁都只能靠肉眼。

本文件锁定的是**最容易被搬坏的**那一段：出站后处理（安慰腔剥离 / 复盘腔收敛 /
反 AI 味 / 按句截断 / 拆泡）。这五道工序**顺序有依赖**，且各自的开关散落在配置里——
顺序错一个，产出就是"半截菜单"或"被截断的助手腔"。

写法：只通过公开入口 `chat()` 断言最终 `result.text`，
不直接调 `_polish_reply` 之类内部方法。这样无论后续怎么拆、拆成几个函数，
这条契约都仍然有效。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dl_senpai.config import PluginConfig
from dl_senpai.llm import SenpaiLLM
from dl_senpai.observability.retry import NoWaitRetryPolicy


def make_config(**overrides: Any) -> PluginConfig:
    """完全隔离的测试配置（不读 .env，见 conftest 的 TestConfigProvider）。

    能力开关全关：这里只测"文本怎么被处理"，不要被联网/生图/知识库的预取干扰。
    """
    base: dict[str, Any] = dict(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="test-model",
        DL_SENPAI_TEMPERATURE=0.5,
        DL_SENPAI_MAX_TOKENS=100,
        DL_SENPAI_INTERRUPT_MAX_TOKENS=50,
        DL_SENPAI_REPLY_MAX_CHARS=500,
        DL_SENPAI_REPLY_HARD_MAX_CHARS=500,
        DL_SENPAI_SEARCH_ENABLE=False,
        DL_SENPAI_DRAW_ENABLE=False,
        DL_SENPAI_BROWSER_ENABLE=False,
        DL_SENPAI_PERSON_MEMORY_ENABLE=False,
        DL_SENPAI_FEWSHOT_ENABLE=False,
        # 后处理四道工序全开：默认值就是全开，显式写出来是为了"改默认值会打到这条测试"
        DL_SENPAI_COMFORT_STRIP_ENABLE=True,
        DL_SENPAI_SELF_REVIEW_COLLAPSE_ENABLE=True,
        DL_SENPAI_HUMANIZE_ENABLE=True,
        DL_SENPAI_BUBBLE_MAX=1,
    )
    base.update(overrides)
    return PluginConfig(**base)


def install_fake(llm: SenpaiLLM, text: str) -> list[dict[str, Any]]:
    """注入固定回复，返回调用记录（用于断言"只调一次"）。"""
    calls: list[dict[str, Any]] = []

    async def fake_create(**kwargs: Any) -> MagicMock:
        calls.append(kwargs)
        message = MagicMock()
        message.content = text
        choice = MagicMock()
        choice.message = message
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = None
        return resp

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    llm.use_client(client)
    return calls


async def run_chat(
    reply: str,
    user_text: str,
    *overrides: Any,
    private_chat: bool = False,
) -> str:
    """跑一次完整的 chat()，返回出站正文。"""
    cfg = make_config(**dict(*overrides)) if overrides else make_config()
    llm = SenpaiLLM(cfg, retry=NoWaitRetryPolicy())
    calls = install_fake(llm, reply)
    result = await llm.chat([], user_text, sender_name="小明", private_chat=private_chat)
    assert len(calls) == 1, "这段测试只覆盖「一次调用就结束」的路径"
    return result.text


# ================================================================
# 逐工序的等价断言（期望值 = 重构前实测产出）
# ================================================================


@pytest.mark.asyncio
async def test_plain_reply_passes_through() -> None:
    """没有任何腔调问题时，正文必须**一个字符都不改**。

    这条看起来废话，但它守住的是"后处理不要自作聪明"——
    任何一道工序误判都会先在这里炸出来。
    """
    out = await run_chat("在呢～有什么事儿吗？", "你好")
    assert out == "在呢～有什么事儿吗？"


@pytest.mark.asyncio
async def test_comfort_clause_is_stripped_on_vent() -> None:
    """对方在吐槽 + 回复带安慰腔 → 只剪安慰小句，别的话留着。

    为什么不能整句砍：「别难过啦，我理解你的感受」和「先深呼吸一下」贴在一起，
    整句砍会把有用的一半一起扔了。
    """
    out = await run_chat(
        "别难过啦，我理解你的感受。其实呢，先深呼吸一下，一切都会好起来的，你要相信自己。",
        "烦死了，今天又被骂了",
    )
    assert out == "其实呢，先深呼吸一下，你要相信自己。"


@pytest.mark.asyncio
async def test_self_review_is_left_as_is_when_not_matched() -> None:
    """括号里的自我点评**不算**复盘腔：collapse 只认整段独白式复盘。

    钉住现状而不是钉住理想：改成"删掉所有括号"会误伤「（笑）」这类正常表达。
    """
    out = await run_chat("好啊（这样回应该没问题）", "晚上吃什么")
    assert out == "好啊（这样回应该没问题）"


@pytest.mark.asyncio
async def test_private_chat_uses_its_own_budget() -> None:
    """私聊预算比群聊宽：用群聊的 60 会把私聊压成客服腔。"""
    reply = "嗯嗯，我知道啦，你先忙你的，忙完了再来找我，我一直在的，不用着急，慢慢来就好。"
    out = await run_chat(reply, "我先去忙", private_chat=True)
    assert out == reply


@pytest.mark.asyncio
async def test_bubbles_are_shaped_when_enabled() -> None:
    """开拆泡时正文保留换行（出站按行拆成多条），不压成一句。"""
    out = await run_chat(
        "好呀\n那我们几点见？\n我提前十分钟到。",
        "约个时间",
        {"DL_SENPAI_BUBBLE_MAX": 3},
    )
    assert out == "好呀\n那我们几点见？\n我提前十分钟到。"


@pytest.mark.asyncio
async def test_ai_flavor_phrases_are_removed() -> None:
    """「总而言之/以上就是我的建议」这类助手腔必须剪掉，但内容保留。"""
    out = await run_chat(
        "总而言之，这个问题需要综合考虑各方面因素，以上就是我的建议。",
        "怎么办",
    )
    assert out == "这个问题需要综合考虑各方面因素，以上就是我的建议。"


@pytest.mark.asyncio
async def test_long_reply_exceeding_budget_is_cut() -> None:
    """超预算的长回复按句截断到预算内（这里是 60 字那一档的实际产出）。"""
    reply = (
        "这个嘛，我觉得还是看你自己怎么想吧，反正我觉得都行，你要是喜欢就买，不喜欢就算了，"
        "别纠结太久，纠结太久反而更难受，真的，相信我。"
    )
    out = await run_chat(reply, "随便")
    assert out == (
        "这个嘛，我觉得还是看你自己怎么想吧，反正我觉得都行，你要是喜欢就买，"
        "不喜欢就算了，别纠结太久，纠结太久反而更难受，真的"
    )


# ================================================================
# 拆分后的**归属**守卫（不是行数断言）
# ================================================================


def test_chat_no_longer_owns_these_responsibilities() -> None:
    """`chat()` 只做编排，不该再直接碰预取与后处理的细节。

    钉"职责归属"而不是"行数多少"：行数会随注释增减而抖动，
    但"chat() 里出现 grounding_prefetch"意味着预取又被塞回来了，这是实质退化。
    """
    import inspect

    from dl_senpai.llm import SenpaiLLM

    src = inspect.getsource(SenpaiLLM.chat)
    assert "grounding_prefetch" not in src
    assert "strip_comfort_clauses" not in src
    assert "humanize_text" not in src
    assert "_prepare_turn(" in src
    assert "_polish_reply(" in src


def test_prepare_turn_owns_capability_switches() -> None:
    """开关解析与预取必须留在 `_prepare_turn` 里（它是唯一决定"这轮能用什么"的地方）。"""
    import inspect

    from dl_senpai.llm import SenpaiLLM

    src = inspect.getsource(SenpaiLLM._prepare_turn)
    assert "search_allow_on_interrupt" in src   # 插嘴模式下逐项禁用
    assert "local_infer" in src                  # 本地小模型直接关掉联网
    assert "grounding_prefetch" in src
    assert "_TurnPrep(" in src
