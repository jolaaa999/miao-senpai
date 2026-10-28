"""Phase 4 前置：行为快照测试（在拆代码**之前**写）。

目的：把 `_handle_chat_inner` + `llm.chat()` 的当前行为固化成可执行契约，
使 Phase 4 的结构迁移可以被机器验证为"行为等价"。

覆盖（按用户要求第三节）：
    普通聊天 / CARD / MUTE / DRAW / BROWSE / STICKER / VOICE / IMPRESSION / SEARCH
    以及第四节：4 套补救 retry

方法：用 fake LLM 客户端注入固定响应，断言
    - 消息构造（system 存在、history、user 内容）
    - LLM 调用次数
    - marker 解析结果
    - 正文不泄漏 marker
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dl_senpai.config import PluginConfig
from dl_senpai.llm import SenpaiLLM
from dl_senpai.observability.retry import NoWaitRetryPolicy


# ================================================================
# 测试脚手架
# ================================================================


def make_config(**overrides: Any) -> PluginConfig:
    """构造一个完全隔离的测试配置（不读 .env）。"""
    base: dict[str, Any] = dict(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="test-model",
        DL_SENPAI_TEMPERATURE=0.5,
        DL_SENPAI_MAX_TOKENS=100,
        DL_SENPAI_INTERRUPT_MAX_TOKENS=50,
        DL_SENPAI_REPLY_MAX_CHARS=500,
        DL_SENPAI_REPLY_HARD_MAX_CHARS=500,
        # 关闭会引入外部依赖的能力，保证快照只反映"文本与解析"行为
        DL_SENPAI_SEARCH_ENABLE=False,
        DL_SENPAI_DRAW_ENABLE=False,
        DL_SENPAI_BROWSER_ENABLE=False,
        DL_SENPAI_PERSON_MEMORY_ENABLE=False,
        DL_SENPAI_FEWSHOT_ENABLE=False,
    )
    base.update(overrides)
    return PluginConfig(**base)


class FakeLLM:
    """可编排的假 LLM：按序返回预设文本，记录每次调用的 messages。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def install(self, llm: SenpaiLLM) -> "FakeLLM":
        async def fake_create(**kwargs: Any) -> MagicMock:
            self.calls.append(kwargs)
            text = self.replies.pop(0) if self.replies else ""
            msg = MagicMock()
            msg.content = text
            choice = MagicMock()
            choice.message = msg
            resp = MagicMock()
            resp.choices = [choice]
            resp.usage = None
            return resp

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=fake_create)
        llm.use_client(client)
        return self

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def messages_at(self, index: int) -> list[dict[str, Any]]:
        return self.calls[index]["messages"]

    def system_at(self, index: int = 0) -> str:
        return self.messages_at(index)[0]["content"]

    def last_user_at(self, index: int = 0) -> Any:
        return self.messages_at(index)[-1]["content"]


def make_llm(config: PluginConfig, replies: list[str]) -> tuple[SenpaiLLM, FakeLLM]:
    llm = SenpaiLLM(config, retry=NoWaitRetryPolicy())
    fake = FakeLLM(replies).install(llm)
    return llm, fake


# ================================================================
# 1. 普通聊天
# ================================================================


@pytest.mark.asyncio
async def test_snapshot_plain_chat() -> None:
    """普通聊天：prompt 主要组成正确、1 次 LLM、无 tool、最终 reply 正确。"""
    cfg = make_config()
    llm, fake = make_llm(cfg, ["在呢～有什么事儿吗？"])

    result = await llm.chat(
        [{"role": "user", "content": "之前的话"}, {"role": "assistant", "content": "嗯"}],
        "你好",
        sender_name="小明",
    )

    # LLM 调用次数
    assert fake.call_count == 1

    # system prompt 含人设关键锚点
    system = fake.system_at(0)
    assert "学姐" in system
    assert "活人感" in system or "真人" in system
    assert "【当前时间】" in system

    # history 被带上
    roles = [m["role"] for m in fake.messages_at(0)]
    assert roles[0] == "system"
    assert roles[-1] == "user"
    assert {"role": "user", "content": "之前的话"} in fake.messages_at(0)

    # 当前输入前缀发送者名
    assert fake.last_user_at(0).startswith("[小明] 说：")

    # 无 tool 执行
    assert result.card_actions == []
    assert result.mute_actions == []
    assert result.draw_force is False
    assert result.browse_force is False
    assert result.sticker_force is False
    assert result.voice_force is False
    assert result.impression_updates == []

    # 最终 reply 无 marker 泄漏。
    # 2026-10-25 起闲聊会按句拆成多条气泡，正文里因此多了换行——
    # 断言只关心「文本内容没被 marker 污染」，把换行归一化掉再比。
    assert result.text.replace("\n", "") == "在呢～有什么事儿吗？"
    assert "<<<" not in result.text


@pytest.mark.asyncio
async def test_snapshot_interrupt_mode_is_shorter() -> None:
    """插嘴模式：max_tokens 用 interrupt_max_tokens，且 system 含插嘴说明。"""
    cfg = make_config()
    llm, fake = make_llm(cfg, ["接一句"])

    await llm.chat([], "随便说说", interrupt=True, sender_name="小明")

    assert fake.calls[0]["max_tokens"] == cfg.interrupt_max_tokens
    assert "插嘴" in fake.system_at(0)


# ================================================================
# 2. CARD
# ================================================================


@pytest.mark.asyncio
async def test_snapshot_card() -> None:
    """CARD：marker 可解析、正文不泄漏、参数原样。"""
    cfg = make_config()
    llm, fake = make_llm(
        cfg,
        ['改成小鱼干啦～\n<<<CARD {"user_id":123,"card":"小鱼干","kind":"request"}>>>'],
    )

    result = await llm.chat(
        [], "把我名片改成小鱼干", sender_name="小明",
        card_enable=True, card_candidates_prompt="【成员候选】\n123 小明",
    )

    assert fake.call_count == 1
    # marker 被解析
    assert len(result.card_actions) == 1
    action = result.card_actions[0]
    assert action.user_id == 123
    assert action.card == "小鱼干"
    assert action.kind == "request"
    # 正文不泄漏 marker
    assert "CARD" not in result.text
    assert "<<" not in result.text
    assert result.text == "改成小鱼干啦～"
    # system 含 CARD 说明
    assert "<<<CARD" in fake.system_at(0)


@pytest.mark.asyncio
async def test_snapshot_card_disabled_strips_marker() -> None:
    """未启用 CARD 时，marker 仍应被剥离（不让它泄漏给群友）。"""
    cfg = make_config()
    llm, _ = make_llm(
        cfg,
        ['好的\n<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>'],
    )
    result = await llm.chat([], "改名片", sender_name="小明", card_enable=False)
    assert result.card_actions == []
    assert "CARD" not in result.text


# ================================================================
# 3. MUTE
# ================================================================


@pytest.mark.asyncio
async def test_snapshot_mute() -> None:
    """MUTE：权限开关、参数解析、正文不泄漏。"""
    cfg = make_config()
    llm, fake = make_llm(
        cfg,
        ['行吧，安静一会儿\n<<<MUTE {"user_id":456,"duration":60,"kind":"request"}>>>'],
    )

    result = await llm.chat(
        [], "禁言他", sender_name="小明",
        mute_enable=True, card_candidates_prompt="【成员候选】\n456 小红",
    )

    assert fake.call_count == 1
    assert len(result.mute_actions) == 1
    m = result.mute_actions[0]
    assert m.user_id == 456
    assert m.duration == 60
    assert "MUTE" not in result.text
    assert "<<<" not in result.text
    # 未启用 CARD 时不应解析 CARD
    assert result.card_actions == []


@pytest.mark.asyncio
async def test_snapshot_mute_disabled_ignores_marker() -> None:
    cfg = make_config()
    llm, _ = make_llm(
        cfg, ['好\n<<<MUTE {"user_id":1,"duration":60,"kind":"request"}>>>']
    )
    result = await llm.chat([], "禁言", sender_name="小明", mute_enable=False)
    assert result.mute_actions == []
    assert "MUTE" not in result.text


# ================================================================
# 4. DRAW
# ================================================================


@pytest.mark.asyncio
async def test_snapshot_draw() -> None:
    """DRAW：marker 解析出 prompt/force，正文不泄漏。"""
    cfg = make_config()
    llm, fake = make_llm(cfg, ["给你画一个\n<<<DRAW a cute cat>>>"])

    result = await llm.chat([], "画只猫", sender_name="小明", draw_enable=True)

    assert fake.call_count == 1
    assert result.draw_prompt == "a cute cat"
    assert result.draw_force is True
    assert "DRAW" not in result.text
    assert "<<<" not in result.text


@pytest.mark.asyncio
async def test_snapshot_draw_disabled_strips_marker() -> None:
    cfg = make_config()
    llm, _ = make_llm(cfg, ["好\n<<<DRAW a cat>>>"])
    result = await llm.chat([], "画图", sender_name="小明", draw_enable=False)
    assert result.draw_force is False
    assert "DRAW" not in result.text


# ================================================================
# 5. BROWSE
# ================================================================


@pytest.mark.asyncio
async def test_snapshot_browse() -> None:
    """BROWSE：任务解析、正文不泄漏。"""
    cfg = make_config()
    llm, fake = make_llm(cfg, ["去找找看\n<<<BROWSE pixiv 猫>>>"])

    result = await llm.chat([], "去pixiv找猫图", sender_name="小明", browser_enable=True)

    assert fake.call_count == 1
    assert len(result.browse_tasks) == 1
    assert result.browse_force is True
    assert "BROWSE" not in result.text


@pytest.mark.asyncio
async def test_snapshot_browse_disabled_strips_marker() -> None:
    cfg = make_config()
    llm, _ = make_llm(cfg, ["好\n<<<BROWSE pixiv 猫>>>"])
    result = await llm.chat([], "找图", sender_name="小明", browser_enable=False)
    assert result.browse_tasks == []
    assert "BROWSE" not in result.text


# ================================================================
# 6. STICKER
# ================================================================


@pytest.mark.asyncio
async def test_snapshot_sticker() -> None:
    """STICKER：query/force 解析、正文不泄漏、不重复。"""
    cfg = make_config()
    llm, fake = make_llm(cfg, ["哼～\n<<<STICKER 傲娇>>>"])

    result = await llm.chat([], "你好", sender_name="小明")

    assert fake.call_count == 1
    assert result.sticker_query == "傲娇"
    assert result.sticker_force is True
    assert "STICKER" not in result.text
    assert result.text == "哼～"


@pytest.mark.asyncio
async def test_snapshot_sticker_not_duplicated() -> None:
    """只有一个 STICKER marker 时不应产生第二次。"""
    cfg = make_config()
    llm, _ = make_llm(cfg, ["哼\n<<<STICKER 傲娇>>>"])
    result = await llm.chat([], "你好", sender_name="小明")
    assert result.text.count("STICKER") == 0


# ================================================================
# 7. VOICE
# ================================================================


@pytest.mark.asyncio
async def test_snapshot_voice_default() -> None:
    """VOICE：无参数 → force=True，override 为空。"""
    cfg = make_config()
    llm, fake = make_llm(cfg, ["在呢\n<<<VOICE>>>"])

    result = await llm.chat([], "你好", sender_name="小明")

    assert fake.call_count == 1
    assert result.voice_force is True
    assert result.voice_override == ""
    assert "VOICE" not in result.text


@pytest.mark.asyncio
async def test_snapshot_voice_with_override() -> None:
    """VOICE：带内容 → override 为指定文本。"""
    cfg = make_config()
    llm, _ = make_llm(cfg, ["好\n<<<VOICE 晚安啦笨蛋>>>"])
    result = await llm.chat([], "说晚安", sender_name="小明")
    assert result.voice_force is True
    assert result.voice_override == "晚安啦笨蛋"
    assert "VOICE" not in result.text


# ================================================================
# 8. IMPRESSION
# ================================================================


@pytest.mark.asyncio
async def test_snapshot_impression() -> None:
    """IMPRESSION：写入画像的 update 被解析出来。"""
    cfg = make_config()
    llm, fake = make_llm(
        cfg,
        ['记住了～\n<<<IMPRESSION {"add":["爱猫"],"remove":[],"notes":"养了两只猫"}>>>'],
    )

    result = await llm.chat(
        [], "我叫小明，养了两只猫", sender_name="小明", person_memory_enable=True
    )

    assert fake.call_count == 1
    assert len(result.impression_updates) == 1
    upd = result.impression_updates[0]
    assert "爱猫" in upd.add
    assert upd.notes == "养了两只猫"
    assert "IMPRESSION" not in result.text


@pytest.mark.asyncio
async def test_snapshot_impression_disabled_strips() -> None:
    """未启用画像时 marker 应被剥离且不产生 update。"""
    cfg = make_config()
    llm, _ = make_llm(
        cfg, ['好\n<<<IMPRESSION {"add":["x"],"remove":[],"notes":null}>>>']
    )
    result = await llm.chat([], "你好", sender_name="小明", person_memory_enable=False)
    assert result.impression_updates == []
    assert "IMPRESSION" not in result.text


# ================================================================
# 9. SEARCH（tool → result → model continuation）
# ================================================================


@pytest.mark.asyncio
async def test_snapshot_search_loop() -> None:
    """SEARCH：第一轮出 marker → 检索 → 结果回填 → 第二轮 LLM。

    这是 Phase 4 Agent Loop 最重要的一条链路样板。
    """
    cfg = make_config(
        DL_SENPAI_SEARCH_ENABLE=True,
        DL_SENPAI_SEARCH_AUTO=False,
        DL_SENPAI_SEARCH_MAX_ROUNDS=2,
    )
    llm, fake = make_llm(
        cfg,
        [
            "<<<SEARCH 今日天气>>>",
            "今天晴，20 度左右～",
        ],
    )

    searcher = MagicMock()
    searcher.search_many = AsyncMock(return_value="【联网检索结果】\n今天晴 20 度")

    import dl_senpai.llm as llm_mod

    orig = llm_mod.searcher_from_config
    llm_mod.searcher_from_config = lambda _cfg: searcher
    try:
        result = await llm.chat(
            [], "今天天气怎么样", sender_name="小明", search_enable=True
        )
    finally:
        llm_mod.searcher_from_config = orig

    # 两次 LLM 调用：第一次出 marker，第二次据结果作答
    assert fake.call_count == 2
    assert result.searched is True
    assert result.text == "今天晴，20 度左右～"
    assert "SEARCH" not in result.text

    # 第二轮 messages 里应含检索结果回填
    second = fake.messages_at(1)
    joined = " ".join(
        m["content"] if isinstance(m["content"], str) else str(m["content"])
        for m in second
    )
    assert "联网检索结果" in joined
    # 检索被真的调用
    assert searcher.search_many.await_count == 1


@pytest.mark.asyncio
async def test_snapshot_search_no_marker_single_call() -> None:
    """未出 marker 时只调一次 LLM。"""
    cfg = make_config(DL_SENPAI_SEARCH_ENABLE=True, DL_SENPAI_SEARCH_AUTO=False)
    llm, fake = make_llm(cfg, ["直接回答就好"])
    result = await llm.chat([], "你好", sender_name="小明", search_enable=True)
    assert fake.call_count == 1
    assert result.searched is False


# ================================================================
# 10. 多 marker 混合（稳定性）
# ================================================================


@pytest.mark.asyncio
async def test_snapshot_multiple_markers() -> None:
    """同一回复出现多个 marker 时的解析顺序必须稳定。"""
    cfg = make_config()
    llm, _ = make_llm(
        cfg,
        [
            "好呀\n"
            '<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>\n'
            "<<<VOICE 晚安>>>\n"
            "<<<STICKER 得意>>>"
        ],
    )

    result = await llm.chat([], "改名片然后说晚安", sender_name="小明", card_enable=True)

    assert len(result.card_actions) == 1
    assert result.voice_override == "晚安"
    assert result.voice_force is True
    assert result.sticker_query == "得意"
    assert result.sticker_force is True
    for marker in ("CARD", "VOICE", "STICKER", "<<"):
        assert marker not in result.text


# ================================================================
# 11. 补救 retry 快照（第四节）
# ================================================================


@pytest.mark.asyncio
async def test_snapshot_card_recovery_retry() -> None:
    """CARD 补救：第一次没 marker → 第二次补上。

    这里验证的是 **llm.py 之外** 的补救路径所需的前提：
    即"两次调用可被区分"。handlers 层的补救分支在
    test_phase4_recovery.py 中单独验证。
    """
    cfg = make_config()
    llm, fake = make_llm(
        cfg,
        ["已经帮你改好啦", '好的\n<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>'],
    )

    first = await llm.chat([], "改名片", sender_name="小明", card_enable=True)
    assert first.card_actions == []  # 第一次没 marker
    assert fake.call_count == 1

    hist2 = [
        *[],
        {"role": "user", "content": "[小明] 说：改名片"},
        {"role": "assistant", "content": first.text},
    ]
    second = await llm.chat(
        hist2, "请补上 CARD 指令行", sender_name="系统", card_enable=True
    )
    assert fake.call_count == 2
    assert len(second.card_actions) == 1


@pytest.mark.asyncio
async def test_snapshot_draw_recovery_retry() -> None:
    """DRAW 补救：第二次返回 marker。"""
    cfg = make_config()
    llm, fake = make_llm(cfg, ["画好啦", "<<<DRAW a cat>>>"])

    first = await llm.chat([], "画只猫", sender_name="小明", draw_enable=True)
    assert first.draw_force is False

    second = await llm.chat(
        [{"role": "user", "content": "x"}, {"role": "assistant", "content": first.text}],
        "请用 <<<DRAW ...>>> 执行",
        sender_name="系统",
        draw_enable=True,
    )
    assert fake.call_count == 2
    assert second.draw_force is True
    assert second.draw_prompt == "a cat"


# ================================================================
# 12. 本地 3B 降级路径（不能退化）
# ================================================================


@pytest.mark.asyncio
async def test_snapshot_local_infer_limits() -> None:
    """本地推理：砍历史、压 system、限额 max_tokens。"""
    cfg = make_config(
        OPENAI_BASE_URL="http://127.0.0.1:8001/v1",
        DL_SENPAI_MAX_TOKENS=1600,
    )
    llm, fake = make_llm(cfg, ["短回复"])

    long_history = []
    for i in range(20):
        long_history.append({"role": "user", "content": f"u{i}"})
        long_history.append({"role": "assistant", "content": f"a{i}"})

    await llm.chat(long_history, "你好", sender_name="小明")

    messages = fake.messages_at(0)
    # 总消息数被裁剪（system + ≤4 条历史 + 当前）
    assert len(messages) <= 6
    # system 被压成短风格提示（远小于完整人设）
    assert len(messages[0]["content"]) <= 800
    # max_tokens 被压到 96
    assert fake.calls[0]["max_tokens"] <= 96
