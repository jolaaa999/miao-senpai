"""Phase 4 测试：RecoveryPolicy / ResponsePipeline / Agent Shadow。

重点证明：
- 4 套补救 prompt 文本**逐字未变**（与 handlers.py 现有字面量一致）
- ResponsePipeline 复用现有 reply_send / style，不重写
- Agent Shadow 绝不产生副作用
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dl_senpai.agent.context import AgentContext, AgentResult
from dl_senpai.agent.recovery import (
    CARD_RETRY_PROMPT,
    MUTE_RETRY_PROMPT,
    RecoveryPolicy,
    build_recovery_policies,
)
from dl_senpai.agent.response import ResponseOutcome, ResponsePipeline, ResponsePlan
from dl_senpai.agent.shadow import AgentShadowRecord, _legacy_tools_of, shadow_enabled
from dl_senpai.agent.tools.models import ToolCall, ToolResult


# ================================================================
# RecoveryPolicy
# ================================================================


def test_recovery_policies_cover_four_tools() -> None:
    policies = build_recovery_policies()
    assert set(policies) == {"card", "mute", "draw", "browse"}


def test_card_retry_prompt_matches_legacy_verbatim() -> None:
    """CARD 补救 prompt 必须与 handlers.py 现有字面量逐字一致。"""
    assert CARD_RETRY_PROMPT == (
        "你刚才只口头说改了名片，但没写 <<<CARD ...>>>，所以实际没改成。"
        "请重新简短回复，并在末尾补上指令行；user_id 必须从成员候选抄，"
        'kind 用 request。格式：<<<CARD {"user_id":数字,"card":"新名片","kind":"request"}>>>'
    )
    assert '<<<CARD {"user_id":数字,"card":"新名片","kind":"request"}>>>' in CARD_RETRY_PROMPT


def test_mute_retry_prompt_matches_legacy_verbatim() -> None:
    assert MUTE_RETRY_PROMPT == (
        "对方在要求禁言。请简短回复一两句（别问心事、别说对方发不了消息），"
        "并在末尾补上指令行；user_id 必须从成员候选抄（禁言我=sender 的 user_id）。"
        '格式：<<<MUTE {"user_id":数字,"duration":60,"kind":"request"}>>>'
    )


def test_recovery_trigger_when_intent_but_no_call() -> None:
    policy = RecoveryPolicy(
        tool_name="card",
        intent_predicate=lambda t: "名片" in t,
        retry_prompt="p",
    )
    assert policy.should_retry(
        intent_text="把我名片改成小鱼干", already_called=False
    ) is True


def test_recovery_skipped_when_already_called() -> None:
    policy = RecoveryPolicy(
        tool_name="card", intent_predicate=lambda t: True, retry_prompt="p"
    )
    assert policy.should_retry(intent_text="x", already_called=True) is False


def test_recovery_skipped_on_interrupt() -> None:
    policy = RecoveryPolicy(
        tool_name="card", intent_predicate=lambda t: True, retry_prompt="p"
    )
    assert policy.should_retry(
        intent_text="x", already_called=False, interrupt=True
    ) is False


def test_recovery_respects_max_retries() -> None:
    policy = RecoveryPolicy(
        tool_name="card", intent_predicate=lambda t: True, retry_prompt="p",
        max_retries=1,
    )
    assert policy.should_retry(
        intent_text="x", already_called=False, retries_so_far=0
    ) is True
    assert policy.should_retry(
        intent_text="x", already_called=False, retries_so_far=1
    ) is False


def test_recovery_no_intent_no_retry() -> None:
    policy = RecoveryPolicy(
        tool_name="card", intent_predicate=lambda t: False, retry_prompt="p"
    )
    assert policy.should_retry(intent_text="随便聊聊", already_called=False) is False


def test_recovery_predicate_exception_is_safe() -> None:
    def boom(_t: str) -> bool:
        raise RuntimeError("boom")

    policy = RecoveryPolicy(tool_name="card", intent_predicate=boom, retry_prompt="p")
    assert policy.should_retry(intent_text="x", already_called=False) is False


def test_recovery_builds_history_like_legacy() -> None:
    """补救历史形状必须与重构前 hist2 一致。"""
    policy = RecoveryPolicy(
        tool_name="card", intent_predicate=lambda t: True, retry_prompt="p"
    )
    history = [{"role": "user", "content": "旧"}]
    hist2 = policy.build_history(history, "小明", "改名片", "已经改好啦")
    assert hist2[0] == {"role": "user", "content": "旧"}
    assert hist2[-2] == {"role": "user", "content": "小明：改名片"}
    assert hist2[-1] == {"role": "assistant", "content": "已经改好啦"}


def test_card_policy_uses_legacy_intent_predicate() -> None:
    """card 策略必须复用 group_card.looks_like_card_request（不重新实现）。"""
    from dl_senpai.group_card import looks_like_card_request

    policies = build_recovery_policies()
    assert policies["card"].intent_predicate is looks_like_card_request


def test_draw_browse_policy_prompts_are_intent_aware() -> None:
    """DRAW/BROWSE 的补救 prompt 必须通过 prompt_builder 动态生成。

    Phase 5.5 决策矩阵发现：它们依赖用户意图（browse 会区分
    pixiv/taobao/screenshot 并带具体关键词），不能提前固化成字符串。
    """
    policies = build_recovery_policies()
    for tool in ("draw", "browse"):
        policy = policies[tool]
        assert policy.prompt_builder is not None, f"{tool} 缺少 prompt_builder"
        prompt = policy.build_retry_prompt("给我画只猫")
        assert prompt.strip()
        assert "<<" in prompt


# ================================================================
# ResponsePipeline
# ================================================================


def ctx(**kw: Any) -> AgentContext:
    base = dict(session_id="group:1", trace_id="t1")
    base.update(kw)
    return AgentContext(**base)


def test_pipeline_build_plan_polishes_text() -> None:
    pipeline = ResponsePipeline()
    result = AgentResult(text="你好**加粗**")
    plan = pipeline.build_plan(result, ctx())
    # style.polish_reply 会去掉 markdown 加粗
    assert "**" not in plan.text


def test_pipeline_plan_collects_media() -> None:
    pipeline = ResponsePipeline()
    context = ctx(extra={
        "sticker_segment": object(),
        "voice_segment": object(),
        "image_segment": object(),
    })
    plan = pipeline.build_plan(AgentResult(text="x"), context)
    assert set(plan.media) == {"sticker", "voice", "image"}


def test_pipeline_plan_no_media() -> None:
    pipeline = ResponsePipeline()
    plan = pipeline.build_plan(AgentResult(text="x"), ctx())
    assert plan.media == []


@pytest.mark.asyncio
async def test_pipeline_send_without_bot_returns_error() -> None:
    """没有 bot/event 时必须返回明确错误，而不是抛异常。"""
    pipeline = ResponsePipeline()
    outcome = await pipeline.send(ResponsePlan(text="x"), ctx())
    assert outcome.sent is False
    assert outcome.error == "missing_bot_or_event"


@pytest.mark.asyncio
async def test_pipeline_send_calls_injected_sender() -> None:
    sender = AsyncMock()
    pipeline = ResponsePipeline(sender=sender)

    context = ctx(
        message_context=MagicMock(bot=object(), raw_event=object()),
    )
    outcome = await pipeline.send(ResponsePlan(text="你好"), context)

    assert outcome.sent is True
    assert sender.await_count == 1
    # 位置参数：bot, event, text
    assert sender.await_args.args[2] == "你好"


@pytest.mark.asyncio
async def test_pipeline_send_failure_isolated() -> None:
    sender = AsyncMock(side_effect=RuntimeError("send down"))
    pipeline = ResponsePipeline(sender=sender)
    context = ctx(message_context=MagicMock(bot=object(), raw_event=object()))
    outcome = await pipeline.send(ResponsePlan(text="x"), context)
    assert outcome.sent is False
    assert "send down" in outcome.error


@pytest.mark.asyncio
async def test_pipeline_dry_run_does_not_send() -> None:
    sender = AsyncMock()
    pipeline = ResponsePipeline(sender=sender)
    context = ctx(message_context=MagicMock(bot=object(), raw_event=object()))
    outcome = await pipeline.deliver(AgentResult(text="x"), context, dry_run=True)
    assert sender.await_count == 0
    assert outcome.error == "dry_run"


@pytest.mark.asyncio
async def test_pipeline_passes_media_to_sender() -> None:
    sender = AsyncMock()
    pipeline = ResponsePipeline(sender=sender)
    sticker, voice = object(), object()
    context = ctx(
        message_context=MagicMock(bot=object(), raw_event=object()),
        extra={"sticker_segment": sticker, "voice_segment": voice},
    )
    await pipeline.send(ResponsePlan(text="x", sticker=sticker, voice=voice), context)
    kwargs = sender.await_args.kwargs
    assert kwargs["sticker"] is sticker
    assert kwargs["voice"] is voice


def test_pipeline_does_not_reimplement_style() -> None:
    """必须复用 style.polish_reply，而不是自己写净化逻辑。"""
    import inspect

    from dl_senpai.agent import response as response_mod

    src = inspect.getsource(response_mod)
    assert "from ..style import polish_reply" in src
    # 不应出现自建的正则净化
    assert "_MD_BOLD_RE" not in src


# ================================================================
# Agent Shadow
# ================================================================


def test_agent_shadow_disabled_by_default() -> None:
    assert shadow_enabled() is False


def test_agent_shadow_env(monkeypatch) -> None:
    monkeypatch.setenv("DL_SENPAI_AGENT_SHADOW", "1")
    assert shadow_enabled() is True


def test_legacy_tools_mapping() -> None:
    """旧 ChatResult 的 8 个字段能反向映射成工具名。"""
    legacy = MagicMock()
    legacy.card_actions = [object()]
    legacy.mute_actions = []
    legacy.impression_updates = []
    legacy.searched = True
    legacy.draw_force = False
    legacy.browse_force = False
    legacy.voice_force = False
    legacy.sticker_force = True
    tools = _legacy_tools_of(legacy)
    assert set(tools) == {"card", "search", "sticker"}


@pytest.mark.asyncio
async def test_shadow_disabled_returns_none() -> None:
    from dl_senpai.agent.shadow import shadow_compare

    rt = MagicMock()
    assert await shadow_compare(rt, ctx()) is None


@pytest.mark.asyncio
async def test_shadow_never_sends_or_executes(monkeypatch) -> None:
    """Shadow 绝不允许产生副作用：不执行工具、不发送。"""
    monkeypatch.setenv("DL_SENPAI_AGENT_SHADOW", "1")

    from dl_senpai.agent.guard import GuardConfig
    from dl_senpai.agent.model_gateway import StubModelGateway
    from dl_senpai.agent.runtime import AgentRuntime
    from dl_senpai.agent.shadow import shadow_compare
    from dl_senpai.agent.tools.builtin import register_production_tools
    from dl_senpai.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    register_production_tools(registry)

    executed: list[str] = []
    for tool in registry.list():
        tool.executor = (lambda name: lambda c, k: executed.append(name))(tool.name)

    gateway = StubModelGateway(
        ['<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>']
    )
    rt = AgentRuntime(
        registry=registry, gateway=gateway,
        guard_config=GuardConfig(max_steps=2),
    )

    record = await shadow_compare(rt, ctx())
    assert record is not None
    assert executed == [], "shadow 绝不能真正执行工具"


@pytest.mark.asyncio
async def test_shadow_compares_visible_tools(monkeypatch) -> None:
    monkeypatch.setenv("DL_SENPAI_AGENT_SHADOW", "1")

    from dl_senpai.agent.model_gateway import StubModelGateway
    from dl_senpai.agent.runtime import AgentRuntime
    from dl_senpai.agent.shadow import shadow_compare
    from dl_senpai.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    rt = AgentRuntime(registry=registry, gateway=StubModelGateway(["hi"]))
    record = await shadow_compare(rt, ctx(), legacy_visible=[])
    assert record.match is True


@pytest.mark.asyncio
async def test_shadow_mismatch_recorded(monkeypatch) -> None:
    monkeypatch.setenv("DL_SENPAI_AGENT_SHADOW", "1")

    from dl_senpai.agent.model_gateway import StubModelGateway
    from dl_senpai.agent.runtime import AgentRuntime
    from dl_senpai.agent.shadow import shadow_compare
    from dl_senpai.agent.tools.builtin import register_production_tools
    from dl_senpai.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    register_production_tools(registry)
    rt = AgentRuntime(registry=registry, gateway=StubModelGateway(["hi"]))

    # 旧路径说只有 card 可用，新路径算出 8 个 → 应记 mismatch
    record = await shadow_compare(rt, ctx(), legacy_visible=["card"])
    assert record.match is False
    assert record.legacy_visible == ["card"]


@pytest.mark.asyncio
async def test_shadow_failure_isolated(monkeypatch) -> None:
    """shadow 内部异常绝不能向上传播。"""
    monkeypatch.setenv("DL_SENPAI_AGENT_SHADOW", "1")

    from dl_senpai.agent.shadow import shadow_compare

    class Boom:
        async def run(self, *a, **k):
            raise RuntimeError("shadow boom")

    assert await shadow_compare(Boom(), ctx()) is None


def test_shadow_record_log_fields() -> None:
    rec = AgentShadowRecord(
        session_id="group:1", legacy_visible=["card"], new_visible=["card"],
        match=True,
    )
    fields = rec.as_log_fields()
    assert fields["match"] is True
    assert fields["session_id"] == "group:1"
