"""RUNTIME 模式下的媒体投递。

回归的 bug：draw / sticker / voice / browse 的媒体段只放在
`ToolResult.data["value"]` 里，而 `ResponsePipeline.build_plan` 原先只读
`context.extra["image_segment" ...]`。extra 的唯一写者是 legacy
orchestrator，legacy 路径早已不可运行 —— 于是模型画好了图、
日志里 success=True，用户却只收到一句纯文字（「商店生图好好的，
学姐生图没办法」就是这个差异）。

这里把这些行为逐条钉住。
"""

from __future__ import annotations

from typing import Any

import pytest

from dl_senpai.agent.context import AgentContext, AgentResult
from dl_senpai.agent.response import ResponsePipeline, ResponsePlan
from dl_senpai.agent.tools.models import ToolCall, ToolResult


def ctx(**kw: Any) -> AgentContext:
    base: dict[str, Any] = dict(session_id="group:1", trace_id="t1")
    base.update(kw)
    return AgentContext(**base)


def tool_ok(name: str, value: Any) -> ToolResult:
    return ToolResult(tool_name=name, success=True, data={"value": value})


# ================================================================
# build_plan：从工具结果取媒体
# ================================================================


def test_plan_takes_draw_image_from_tool_results() -> None:
    segment = object()
    result = AgentResult(text="画好啦", tool_results=[tool_ok("draw", segment)])

    plan = ResponsePipeline().build_plan(result, ctx())

    assert plan.image is segment
    assert plan.media == ["image"]


def test_plan_takes_sticker_and_voice_from_tool_results() -> None:
    sticker, voice = object(), object()
    result = AgentResult(
        text="嗯",
        tool_results=[tool_ok("sticker", sticker), tool_ok("voice", voice)],
    )

    plan = ResponsePipeline().build_plan(result, ctx())

    assert plan.sticker is sticker
    assert plan.voice is voice
    assert set(plan.media) == {"sticker", "voice"}


def test_plan_extra_segment_still_wins() -> None:
    """legacy 兼容分支显式写 extra，语义必须保持不变。"""
    explicit, from_tool = object(), object()
    result = AgentResult(text="x", tool_results=[tool_ok("draw", from_tool)])

    plan = ResponsePipeline().build_plan(
        result, ctx(extra={"image_segment": explicit})
    )

    assert plan.image is explicit
    assert plan.extra_images == []


def test_plan_browse_multiple_images() -> None:
    shots = [object(), object(), object()]
    result = AgentResult(text="逛到了", tool_results=[tool_ok("browse", shots)])

    plan = ResponsePipeline().build_plan(result, ctx())

    # 第一张跟正文同一条消息，其余补发
    assert plan.image is shots[0]
    assert plan.extra_images == shots[1:]
    assert plan.media == ["image"]


def test_plan_draw_wins_over_browse() -> None:
    draw_seg, shot = object(), object()
    result = AgentResult(
        text="x",
        tool_results=[tool_ok("draw", draw_seg), tool_ok("browse", [shot])],
    )

    plan = ResponsePipeline().build_plan(result, ctx())

    assert plan.image is draw_seg
    assert plan.extra_images == [shot]


def test_plan_ignores_failed_tool_results() -> None:
    """失败的 draw 不能把 value=None 当成图去发。"""
    result = AgentResult(
        text="画不出来",
        tool_results=[
            ToolResult(
                tool_name="draw",
                success=False,
                error="no_image",
                data={"value": None},
            )
        ],
    )

    plan = ResponsePipeline().build_plan(result, ctx())

    assert plan.image is None
    assert plan.media == []


# ================================================================
# send：额外图片补发
# ================================================================


@pytest.mark.asyncio
async def test_send_delivers_extra_images(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    import dl_senpai.reply_send as reply_send

    retry = AsyncMock(return_value=True)
    monkeypatch.setattr(reply_send, "_send_image_with_retry", retry)

    sender = AsyncMock()
    pipeline = ResponsePipeline(sender=sender)
    context = ctx(message_context=MagicMock(bot=object(), raw_event=object()))

    main, extra_a, extra_b = object(), object(), object()
    plan = ResponsePlan(
        text="逛到了", image=main, extra_images=[extra_a, extra_b]
    )
    outcome = await pipeline.send(plan, context)

    assert outcome.sent is True
    # 正文与第一张图走同一条消息
    assert sender.await_args.kwargs["image"] is main
    # 其余图片各发一次
    assert retry.await_count == 2
    assert [c.args[2] for c in retry.await_args_list] == [extra_a, extra_b]


@pytest.mark.asyncio
async def test_send_no_extra_images_does_not_touch_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    import dl_senpai.reply_send as reply_send

    retry = AsyncMock(return_value=True)
    monkeypatch.setattr(reply_send, "_send_image_with_retry", retry)

    sender = AsyncMock()
    pipeline = ResponsePipeline(sender=sender)
    context = ctx(message_context=MagicMock(bot=object(), raw_event=object()))

    await pipeline.send(ResponsePlan(text="只有文字"), context)

    assert retry.await_count == 0


# ================================================================
# runtime：执行工具前补上 extra["reply_text"]
# ================================================================


class _FakeGateway:
    """只发一次带 draw 调用的响应。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, request: Any, context: Any = None) -> Any:
        from dl_senpai.agent.model_gateway import ModelResponse

        self.calls += 1
        return ModelResponse(
            text="这就画～",
            tool_calls=[ToolCall(tool_name="draw", arguments={"prompt": "猫娘"})],
        )


class _FakeExecutor:
    """记录执行时 extra 里的 reply_text，返回一张假图。"""

    def __init__(self) -> None:
        self.seen_reply_text: list[str] = []
        self.image = object()

    def set_journal(self, journal: Any) -> None:
        return None

    async def execute(self, call: Any, context: Any, dry_run: bool = False) -> Any:
        self.seen_reply_text.append(context.extra.get("reply_text", ""))
        return ToolResult(
            tool_name=call.tool_name, success=True, data={"value": self.image}
        )


@pytest.mark.asyncio
async def test_runtime_injects_reply_text_before_tool_execution() -> None:
    """voice 念这段、sticker / draw 用它匹配；空了就等于媒体全废。"""
    from dl_senpai.agent.runtime import AgentRuntime
    from dl_senpai.agent.tools.registry import ToolRegistry

    executor = _FakeExecutor()
    runtime = AgentRuntime(
        registry=ToolRegistry(), gateway=_FakeGateway(), executor=executor
    )

    context = ctx()
    result = await runtime.run(context, "画张猫娘")

    assert executor.seen_reply_text == ["这就画～"]
    assert context.extra["reply_text"] == "这就画～"
    # 端到端：图片最终能被 ResponsePipeline 取到
    plan = ResponsePipeline().build_plan(result, context)
    assert plan.image is executor.image
