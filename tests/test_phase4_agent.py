"""Phase 4 测试：Tool Registry / Marker Adapter / Executor / Agent Loop / Guard。

覆盖用户要求（第五十二/五十三/五十四/五十五条）的全部条目。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from dl_senpai.agent.context import AgentContext, AgentResult
from dl_senpai.agent.guard import AgentGuard, GuardConfig, StopReason
from dl_senpai.agent.model_gateway import StubModelGateway
from dl_senpai.agent.runtime import AgentRuntime
from dl_senpai.agent.tools.builtin import build_production_tools, register_production_tools
from dl_senpai.agent.tools.dummy_tool import register_dummy_tool
from dl_senpai.agent.tools.executor import ToolExecutor
from dl_senpai.agent.tools.marker_adapter import MarkerToolAdapter, NativeToolAdapter
from dl_senpai.agent.tools.models import Continuation, ToolCall, ToolCallSource, ToolResult
from dl_senpai.agent.tools.registry import ToolDefinition, ToolRegistry


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_production_tools(reg)
    return reg


@pytest.fixture
def adapter(registry: ToolRegistry) -> MarkerToolAdapter:
    return MarkerToolAdapter(registry)


def ctx(**kw: Any) -> AgentContext:
    base = dict(session_id="group:1", trace_id="t1", trigger_reason="mention")
    base.update(kw)
    return AgentContext(**base)


# ================================================================
# Tool Registry（第五十二条）
# ================================================================


def test_registry_has_all_production_tools(registry: ToolRegistry) -> None:
    # 原名叫 test_registry_has_8_production_tools；2026-10-14 加了 group_read，
    # 名字里的数字会变成假话，所以去掉数字而不是改成 9。
    assert len(registry) == 9
    assert set(registry.names()) == {
        "card", "mute", "impression", "search", "draw", "browse", "sticker",
        "voice", "group_read"
    }


def test_registry_markers_complete(registry: ToolRegistry) -> None:
    assert set(registry.markers()) == {
        "CARD", "MUTE", "IMPRESSION", "SEARCH", "DRAW", "BROWSE", "STICKER",
        "VOICE", "GROUP"
    }


def test_register_and_get() -> None:
    reg = ToolRegistry()
    tool = ToolDefinition(name="x", description="d")
    reg.register(tool)
    assert reg.get("x") is tool
    assert reg.get("missing") is None


def test_register_replaces_same_name() -> None:
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="x", description="first"))
    reg.register(ToolDefinition(name="x", description="second"))
    assert len(reg) == 1
    assert reg.get("x").description == "second"


def test_unregister() -> None:
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="x"))
    assert reg.unregister("x") is True
    assert reg.unregister("x") is False


def test_unregister_respects_owner() -> None:
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="x", owner="a"))
    assert reg.unregister("x", owner="b") is False
    assert reg.unregister("x", owner="a") is True


def test_unregister_owner_bulk() -> None:
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="a", owner="p"))
    reg.register(ToolDefinition(name="b", owner="p"))
    reg.register(ToolDefinition(name="c", owner="other"))
    assert reg.unregister_owner("p") == 2
    assert reg.names() == ["c"]


def test_enable_disable() -> None:
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="x"))
    assert reg.disable("x") is True
    assert reg.visible_tools(ctx()) == []
    assert reg.enable("x") is True
    assert len(reg.visible_tools(ctx())) == 1


def test_enable_unknown_returns_false() -> None:
    reg = ToolRegistry()
    assert reg.enable("nope") is False
    assert reg.disable("nope") is False


def test_visible_tools_filters_by_predicate() -> None:
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="yes", visibility=lambda c: True))
    reg.register(ToolDefinition(name="no", visibility=lambda c: False))
    names = [t.name for t in reg.visible_tools(ctx())]
    assert names == ["yes"]


def test_visible_tools_predicate_exception_means_invisible() -> None:
    """可见性谓词异常时必须保守隐藏（宁少给不错给）。"""
    def boom(_c: Any) -> bool:
        raise RuntimeError("boom")

    reg = ToolRegistry()
    reg.register(ToolDefinition(name="bad", visibility=boom))
    assert reg.visible_tools(ctx()) == []


def test_visible_tools_sorted_by_priority() -> None:
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="late", priority=90))
    reg.register(ToolDefinition(name="early", priority=10))
    assert [t.name for t in reg.visible_tools(ctx())] == ["early", "late"]


def test_disabled_tool_excluded_from_markers() -> None:
    reg = ToolRegistry()
    register_production_tools(reg)
    reg.disable("draw")
    assert "DRAW" not in reg.markers()


# ================================================================
# Marker Adapter（第五十三条）
# ================================================================


def test_parse_card_marker(adapter: MarkerToolAdapter) -> None:
    out = adapter.parse_response('好\n<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>')
    assert "CARD" not in out.text
    call = out.first("card")
    assert call is not None
    assert call.arguments["user_id"] == 1
    assert call.arguments["card"] == "x"
    assert call.source is ToolCallSource.MARKER


def test_parse_mute_marker(adapter: MarkerToolAdapter) -> None:
    out = adapter.parse_response('好\n<<<MUTE {"user_id":1,"duration":60,"kind":"request"}>>>')
    call = out.first("mute")
    assert call is not None
    assert call.arguments["duration"] == 60


def test_parse_search_marker(adapter: MarkerToolAdapter) -> None:
    out = adapter.parse_response("<<<SEARCH 天气>>>")
    call = out.first("search")
    assert call is not None
    assert call.arguments["queries"] == ["天气"]
    assert out.searched is True


def test_parse_draw_marker(adapter: MarkerToolAdapter) -> None:
    out = adapter.parse_response("图\n<<<DRAW a cat>>>")
    call = out.first("draw")
    assert call is not None
    assert call.arguments["prompt"] == "a cat"


def test_parse_voice_marker(adapter: MarkerToolAdapter) -> None:
    out = adapter.parse_response("在\n<<<VOICE 晚安>>>")
    call = out.first("voice")
    assert call is not None
    assert call.arguments["override"] == "晚安"


def test_parse_sticker_marker(adapter: MarkerToolAdapter) -> None:
    out = adapter.parse_response("哼\n<<<STICKER 傲娇>>>")
    call = out.first("sticker")
    assert call is not None
    assert call.arguments["query"] == "傲娇"


def test_parse_impression_marker(adapter: MarkerToolAdapter) -> None:
    out = adapter.parse_response('<<<IMPRESSION {"add":["爱猫"],"remove":[],"notes":null}>>>')
    call = out.first("impression")
    assert call is not None
    assert "爱猫" in call.arguments["add"]


def test_parse_browse_marker(adapter: MarkerToolAdapter) -> None:
    out = adapter.parse_response("找\n<<<BROWSE pixiv 猫>>>")
    call = out.first("browse")
    assert call is not None
    assert len(call.arguments["tasks"]) == 1


def test_parse_no_marker(adapter: MarkerToolAdapter) -> None:
    out = adapter.parse_response("就是普通聊天")
    assert out.has_calls is False
    assert out.text == "就是普通聊天"


def test_parse_multiple_markers_stable_order(adapter: MarkerToolAdapter) -> None:
    """多 marker 时解析顺序必须稳定（第五十四条）。"""
    text = (
        "好呀\n"
        '<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>\n'
        "<<<VOICE 晚安>>>\n"
        "<<<STICKER 得意>>>"
    )
    first = adapter.parse_response(text)
    second = adapter.parse_response(text)
    assert [c.tool_name for c in first.calls] == [c.tool_name for c in second.calls]
    # CARD(priority 10) → VOICE(70) → STICKER(80)
    assert [c.tool_name for c in first.calls] == ["card", "voice", "sticker"]
    assert "<<" not in first.text


def test_parse_tolerance_card_strict(adapter: MarkerToolAdapter) -> None:
    """CARD 只认 `>>>` —— 这是历史行为，必须保持。"""
    ok = adapter.parse_response('<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>')
    assert ok.first("card") is not None
    not_ok = adapter.parse_response('<<<CARD {"user_id":1,"card":"x","kind":"request"}>>')
    assert not_ok.first("card") is None


def test_parse_tolerance_search_wide(adapter: MarkerToolAdapter) -> None:
    """SEARCH 容错最宽：>>> / >> / ＞＞＞ / 】 全部可解析。"""
    for closer in (">>>", ">>", "＞＞＞", "】】", "】"):
        out = adapter.parse_response(f"<<<SEARCH 天气{closer}")
        assert out.first("search") is not None, f"closer={closer} 未被识别"


def test_parse_tolerance_draw(adapter: MarkerToolAdapter) -> None:
    for closer in (">>>", ">>", "】"):
        out = adapter.parse_response(f"图\n<<<DRAW a cat{closer}")
        assert out.first("draw") is not None, f"closer={closer} 未被识别"


def test_parse_tolerance_voice(adapter: MarkerToolAdapter) -> None:
    for closer in (">>>", "】"):
        out = adapter.parse_response(f"在\n<<<VOICE 晚安{closer}")
        assert out.first("voice") is not None, f"closer={closer} 未被识别"


def test_parse_bad_json_no_crash(adapter: MarkerToolAdapter) -> None:
    out = adapter.parse_response("好\n<<<CARD {bad json}>>>")
    assert out.first("card") is None
    assert "CARD" not in out.text


def test_parse_sticker_bare_not_recognized(adapter: MarkerToolAdapter) -> None:
    """裸 <<<STICKER>>> 不解析（与现有行为一致）。"""
    out = adapter.parse_response("哼\n<<<STICKER>>>")
    assert out.first("sticker") is None


def test_parse_parser_exception_isolated(registry: ToolRegistry) -> None:
    """单个 parser 抛异常不能影响其他工具解析。"""
    def boom(_t: str):
        raise RuntimeError("parser boom")

    registry.register(
        ToolDefinition(name="bad", marker="BAD", parser=boom, priority=1)
    )
    adapter = MarkerToolAdapter(registry)
    out = adapter.parse_response("<<<SEARCH 天气>>>")
    assert out.first("search") is not None


# ================================================================
# Tool Executor（第五十二条）
# ================================================================


@pytest.mark.asyncio
async def test_executor_runs_tool(registry: ToolRegistry) -> None:
    ex = ToolExecutor(registry)

    async def run(_ctx: Any, call: ToolCall) -> ToolResult:
        return ToolResult(tool_name=call.tool_name, success=True, output="done")

    registry.register(ToolDefinition(name="t", executor=run))
    result = await ex.execute(ToolCall(tool_name="t"), ctx())
    assert result.success is True
    assert result.output == "done"


@pytest.mark.asyncio
async def test_executor_unknown_tool(registry: ToolRegistry) -> None:
    ex = ToolExecutor(registry)
    result = await ex.execute(ToolCall(tool_name="nope"), ctx())
    assert result.success is False
    assert result.error_type == "unknown_tool"


@pytest.mark.asyncio
async def test_executor_disabled_tool(registry: ToolRegistry) -> None:
    ex = ToolExecutor(registry)
    registry.register(ToolDefinition(name="t", executor=lambda c, k: None))
    registry.disable("t")
    result = await ex.execute(ToolCall(tool_name="t"), ctx())
    assert result.error_type == "disabled"


@pytest.mark.asyncio
async def test_executor_permission_deny(registry: ToolRegistry) -> None:
    class DenyPerm:
        def check(self, *, tool, call, context):
            return False, "no permission"

    ex = ToolExecutor(registry, permissions=DenyPerm())
    registry.register(
        ToolDefinition(name="t", permission="x", executor=lambda c, k: None)
    )
    result = await ex.execute(ToolCall(tool_name="t"), ctx())
    assert result.success is False
    assert result.error_type == "permission_denied"


@pytest.mark.asyncio
async def test_executor_permission_service_exception_denies(registry: ToolRegistry) -> None:
    class BoomPerm:
        def check(self, *, tool, call, context):
            raise RuntimeError("perm boom")

    ex = ToolExecutor(registry, permissions=BoomPerm())
    registry.register(
        ToolDefinition(name="t", permission="x", executor=lambda c, k: None)
    )
    result = await ex.execute(ToolCall(tool_name="t"), ctx())
    assert result.success is False


@pytest.mark.asyncio
async def test_executor_timeout(registry: ToolRegistry) -> None:
    async def slow(_c: Any, _k: ToolCall) -> ToolResult:
        await asyncio.sleep(5)
        return ToolResult(tool_name="t", success=True)

    ex = ToolExecutor(registry)
    registry.register(ToolDefinition(name="t", executor=slow, timeout=0.05))
    result = await ex.execute(ToolCall(tool_name="t"), ctx())
    assert result.success is False
    assert result.error_type == "timeout"


@pytest.mark.asyncio
async def test_executor_failure_isolated(registry: ToolRegistry) -> None:
    def boom(_c: Any, _k: ToolCall) -> ToolResult:
        raise ValueError("exec boom")

    ex = ToolExecutor(registry)
    registry.register(ToolDefinition(name="t", executor=boom))
    result = await ex.execute(ToolCall(tool_name="t"), ctx())
    assert result.success is False
    assert result.error_type == "ValueError"


@pytest.mark.asyncio
async def test_executor_rate_limit(registry: ToolRegistry) -> None:
    ex = ToolExecutor(registry)
    registry.register(
        ToolDefinition(name="t", executor=lambda c, k: None, rate_limit=60.0)
    )
    first = await ex.execute(ToolCall(tool_name="t"), ctx())
    assert first.success is True
    second = await ex.execute(ToolCall(tool_name="t"), ctx())
    assert second.success is False
    assert second.error_type == "rate_limited"


@pytest.mark.asyncio
async def test_executor_dry_run_no_side_effect(registry: ToolRegistry) -> None:
    """dry_run 只报告"本应执行"，不真正执行。"""
    called: list[str] = []

    def real(_c: Any, _k: ToolCall) -> ToolResult:
        called.append("executed")
        return ToolResult(tool_name="t", success=True)

    ex = ToolExecutor(registry)
    registry.register(ToolDefinition(name="t", executor=real))
    result = await ex.execute(ToolCall(tool_name="t"), ctx(), dry_run=True)
    assert result.would_execute is True
    assert called == []


@pytest.mark.asyncio
async def test_executor_normalizes_legacy_returns(registry: ToolRegistry) -> None:
    """legacy 返回值（bool/str/dict）必须被归一化为 ToolResult。"""
    ex = ToolExecutor(registry)
    reg = ToolRegistry()

    reg.register(ToolDefinition(name="b", executor=lambda c, k: True))
    reg.register(ToolDefinition(name="s", executor=lambda c, k: "text"))
    reg.register(ToolDefinition(name="d", executor=lambda c, k: {"output": "o"}))
    reg.register(ToolDefinition(name="n", executor=lambda c, k: None))
    reg.register(ToolDefinition(name="f", executor=lambda c, k: False))
    ex2 = ToolExecutor(reg)

    assert (await ex2.execute(ToolCall(tool_name="b"))).success is True
    assert (await ex2.execute(ToolCall(tool_name="s"))).output == "text"
    assert (await ex2.execute(ToolCall(tool_name="d"))).output == "o"
    assert (await ex2.execute(ToolCall(tool_name="n"))).success is True
    assert (await ex2.execute(ToolCall(tool_name="f"))).success is False


def test_executor_source_has_no_tool_branching() -> None:
    """硬性约束：Executor 内部不得出现具体工具名分支（第二十三条）。"""
    import inspect

    src = inspect.getsource(ToolExecutor)
    for tool in ("card", "mute", "draw", "browse", "sticker", "voice", "impression"):
        assert f'tool_name == "{tool}"' not in src
        assert f"tool_name == '{tool}'" not in src


# ================================================================
# Agent Loop（第五十五条）
# ================================================================


def make_runtime(registry: ToolRegistry, replies: list[str]) -> tuple[AgentRuntime, StubModelGateway]:
    gateway = StubModelGateway(replies)
    rt = AgentRuntime(registry=registry, gateway=gateway)
    return rt, gateway


@pytest.mark.asyncio
async def test_loop_zero_tools(registry: ToolRegistry) -> None:
    rt, gw = make_runtime(registry, ["你好呀～"])
    result = await rt.run(ctx(), "你好")
    assert result.text == "你好呀～"
    assert result.tool_calls == []
    assert result.steps == 1
    assert result.stop_reason is StopReason.NO_TOOL_CALL
    assert len(gw.requests) == 1


@pytest.mark.asyncio
async def test_loop_one_side_effect_tool(registry: ToolRegistry) -> None:
    """副作用工具：执行后不回模型，直接结束。"""
    rt, gw = make_runtime(
        registry,
        ['好\n<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>'],
    )
    result = await rt.run(ctx(), "改名片")
    assert result.was_called("card")
    assert result.steps == 1
    assert len(gw.requests) == 1, "副作用工具不应触发第二次模型调用"
    assert "CARD" not in result.text


@pytest.mark.asyncio
async def test_loop_tool_then_model_continuation(registry: ToolRegistry) -> None:
    """RETURN_TO_MODEL 工具：执行后回填结果，再次调用模型。"""
    rt, gw = make_runtime(registry, ["<<<SEARCH 天气>>>", "今天晴～"])
    # 给 search 一个真实执行器（生产注册的只是占位）
    registry.register(
        ToolDefinition(
            name="search",
            marker="SEARCH",
            parser=registry.get("search").parser,
            executor=lambda c, k: ToolResult(
                tool_name="search", success=True, output="【联网检索结果】今天晴"
            ),
            continuation=Continuation.RETURN_TO_MODEL,
            priority=40,
        )
    )
    result = await rt.run(ctx(), "天气如何")
    assert len(gw.requests) == 2, "RETURN_TO_MODEL 应触发第二次模型调用"
    assert result.text == "今天晴～"
    assert result.steps == 2
    assert result.searched is True

    # 第二轮 messages 应含工具结果
    second = gw.requests[1].messages
    joined = " ".join(str(m.get("content", "")) for m in second)
    assert "联网检索结果" in joined


@pytest.mark.asyncio
async def test_loop_max_steps(registry: ToolRegistry) -> None:
    """永不停止的模型：max_steps 必须终止循环。"""
    rt, gw = make_runtime(registry, ["<<<SEARCH a>>>"] * 10)
    registry.register(
        ToolDefinition(
            name="search", marker="SEARCH",
            parser=registry.get("search").parser,
            executor=lambda c, k: ToolResult(
                tool_name="search", success=True, output=f"r{id(k)}"
            ),
            continuation=Continuation.RETURN_TO_MODEL,
            priority=40,
        )
    )
    result = await rt.run(ctx(), "x", guard=AgentGuard(config=GuardConfig(max_steps=3)))
    assert result.steps <= 3
    assert result.stop_reason in (StopReason.MAX_STEPS, StopReason.REPEAT_LIMIT)


@pytest.mark.asyncio
async def test_loop_same_tool_repeat_detected(registry: ToolRegistry) -> None:
    """同一工具+同一参数重复调用必须被终止（第三十四条）。"""
    rt, _ = make_runtime(registry, ["<<<SEARCH abc>>>"] * 10)
    registry.register(
        ToolDefinition(
            name="search", marker="SEARCH",
            parser=registry.get("search").parser,
            executor=lambda c, k: ToolResult(
                tool_name="search", success=True, output="same"
            ),
            continuation=Continuation.RETURN_TO_MODEL,
            priority=40,
        )
    )
    guard = AgentGuard(config=GuardConfig(max_steps=10, same_tool_repeat_limit=2))
    result = await rt.run(ctx(), "x", guard=guard)
    assert result.stop_reason is StopReason.REPEAT_LIMIT


@pytest.mark.asyncio
async def test_loop_timeout(registry: ToolRegistry) -> None:
    """整体超时必须终止。"""
    async def slow(_c: Any, _k: ToolCall) -> ToolResult:
        await asyncio.sleep(0.3)
        return ToolResult(tool_name="search", success=True, output="late")

    rt, _ = make_runtime(registry, ["<<<SEARCH a>>>"] * 10)
    registry.register(
        ToolDefinition(
            name="search", marker="SEARCH",
            parser=registry.get("search").parser,
            executor=slow,
            continuation=Continuation.RETURN_TO_MODEL,
            priority=40,
        )
    )
    guard = AgentGuard(config=GuardConfig(timeout=0.1, max_steps=10))
    result = await rt.run(ctx(), "x", guard=guard)
    assert result.stop_reason is StopReason.TIMEOUT


@pytest.mark.asyncio
async def test_loop_token_budget(registry: ToolRegistry) -> None:
    """token 预算耗尽必须终止。"""
    class UsageGateway(StubModelGateway):
        async def chat(self, request, context=None):
            resp = await super().chat(request, context)
            resp.usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
            return resp

    rt = AgentRuntime(registry=registry, gateway=UsageGateway(["<<<SEARCH a>>>"] * 5))
    registry.register(
        ToolDefinition(
            name="search", marker="SEARCH",
            parser=registry.get("search").parser,
            executor=lambda c, k: ToolResult(tool_name="search", success=True, output="o"),
            continuation=Continuation.RETURN_TO_MODEL,
            priority=40,
        )
    )
    guard = AgentGuard(config=GuardConfig(max_steps=10, token_budget=200))
    result = await rt.run(ctx(), "x", guard=guard)
    assert result.stop_reason is StopReason.TOKEN_BUDGET
    assert result.token_usage.total_tokens == 300


@pytest.mark.asyncio
async def test_loop_tool_error_does_not_crash(registry: ToolRegistry) -> None:
    def boom(_c: Any, _k: ToolCall) -> ToolResult:
        raise RuntimeError("tool down")

    rt, _ = make_runtime(registry, ["<<<DUMMY x>>>", "好的"])
    register_dummy_tool(registry)
    registry.get("dummy").executor = boom
    result = await rt.run(ctx(), "x")
    # 工具失败必须被记录，但不能让整轮崩溃
    assert result.tool_calls
    failed = [r for r in result.tool_results if not r.success]
    assert failed


@pytest.mark.asyncio
async def test_loop_model_error(registry: ToolRegistry) -> None:
    class ErrGateway(StubModelGateway):
        async def chat(self, request, context=None):
            from dl_senpai.agent.model_gateway import ModelResponse
            return ModelResponse(error="RuntimeError: network down")

    rt = AgentRuntime(registry=registry, gateway=ErrGateway())
    result = await rt.run(ctx(), "x")
    assert result.stop_reason is StopReason.ERROR
    assert "network down" in result.metadata.get("error", "")


@pytest.mark.asyncio
async def test_loop_dry_run_no_side_effects(registry: ToolRegistry) -> None:
    """dry_run 模式下不得产生任何真实副作用（第五十八条）。"""
    called: list[str] = []

    rt, _ = make_runtime(registry, ["<<<DUMMY x>>>"])
    register_dummy_tool(registry)
    registry.get("dummy").executor = lambda c, k: called.append("real") or ToolResult(
        tool_name="dummy", success=True
    )
    result = await rt.run(ctx(), "x", dry_run=True)
    assert called == []
    assert result.tool_results[0].would_execute is True


# ================================================================
# AgentGuard
# ================================================================


def test_guard_step_limit() -> None:
    g = AgentGuard(config=GuardConfig(max_steps=2))
    g.begin_step()
    assert g.check_step() is None
    g.begin_step()
    assert g.check_step() is StopReason.MAX_STEPS


def test_guard_tool_call_limit() -> None:
    g = AgentGuard(config=GuardConfig(max_tool_calls=1))
    assert g.check_tool_call("a") is None
    g.record_tool_call("a")
    assert g.check_tool_call("b") is StopReason.MAX_TOOL_CALLS


def test_guard_repeat_limit() -> None:
    g = AgentGuard(config=GuardConfig(same_tool_repeat_limit=2))
    g.record_tool_call("fp")
    assert g.check_tool_call("fp") is None
    g.record_tool_call("fp")
    assert g.check_tool_call("fp") is StopReason.REPEAT_LIMIT


def test_guard_different_fingerprints_independent() -> None:
    g = AgentGuard(config=GuardConfig(same_tool_repeat_limit=1))
    g.record_tool_call("a")
    assert g.check_tool_call("b") is None
    assert g.check_tool_call("a") is StopReason.REPEAT_LIMIT


def test_token_usage_unknown_stays_none() -> None:
    """provider 不返回 usage 时必须保持 None，不估算（第三十五条）。"""
    from dl_senpai.agent.guard import TokenUsage

    u = TokenUsage()
    u.add(None, None, None)
    assert u.total_tokens is None
    assert u.known is False


def test_token_usage_accumulates() -> None:
    from dl_senpai.agent.guard import TokenUsage

    u = TokenUsage()
    u.add(10, 5, 15)
    u.add(20, 10, 30)
    assert u.total_tokens == 45
    assert u.calls == 2


def test_token_usage_derives_only_when_both_known() -> None:
    from dl_senpai.agent.guard import TokenUsage

    u = TokenUsage()
    u.add(10, None, None)
    assert u.total_tokens is None, "缺分量时不得推导"
    u.add(5, 5, None)
    assert u.total_tokens == 10


# ================================================================
# DummyTool 扩展性验收（第四十五条）
# ================================================================


def test_dummy_tool_registerable_without_core_changes(registry: ToolRegistry) -> None:
    """只注册 ToolDefinition 即可生成 prompt / 解析 / 执行。"""
    before = len(registry)
    register_dummy_tool(registry)
    assert len(registry) == before + 1

    tool = registry.get("dummy")
    assert tool is not None
    assert tool.visible_tools_ok() if hasattr(tool, "visible_tools_ok") else True
    assert tool.is_visible(ctx()) is True
    assert tool.prompt_text
    assert "DUMMY" in tool.prompt_text

    # 解析
    adapter = MarkerToolAdapter(registry)
    out = adapter.parse_response("好\n<<<DUMMY hello>>>")
    call = out.first("dummy")
    assert call is not None
    assert call.arguments["value"] == "hello"
    assert "DUMMY" not in out.text


def test_dummy_tool_prompt_rendering(registry: ToolRegistry) -> None:
    """DummyTool 的 prompt_text 能被渲染出来（注册表驱动）。"""
    register_dummy_tool(registry)
    adapter = MarkerToolAdapter(registry)
    rendered = adapter.render_tools([registry.get("dummy")])
    assert "DUMMY" in rendered
    assert "<<<DUMMY" in rendered


@pytest.mark.asyncio
async def test_dummy_tool_executes_in_loop(registry: ToolRegistry) -> None:
    """DummyTool 在 Agent 循环中可执行，且不改 runtime。"""
    register_dummy_tool(registry)
    rt, gw = make_runtime(registry, ["<<<DUMMY hi>>>", "收到"])
    result = await rt.run(ctx(), "x")
    assert result.was_called("dummy")
    assert len(gw.requests) == 2  # RETURN_TO_MODEL


def test_dummy_tool_owner_cleanup(registry: ToolRegistry) -> None:
    register_dummy_tool(registry)
    assert registry.unregister_owner("test_dummy") == 1
    assert registry.get("dummy") is None


# ================================================================
# Native Adapter 扩展点（第四十六条）
# ================================================================


def test_native_adapter_is_stub_not_production(registry: ToolRegistry) -> None:
    """NativeToolAdapter 只做形状转换，不参与生产路径。"""
    native = NativeToolAdapter(registry)
    out = native.parse_response("普通文本")
    assert out.text == "普通文本"
    assert out.calls == []


def test_native_adapter_converts_tool_calls(registry: ToolRegistry) -> None:
    native = NativeToolAdapter(registry)
    raw = [{"id": "c1", "function": {"name": "search", "arguments": '{"queries":["x"]}'}}]
    calls = native.parse_native_calls(raw)
    assert len(calls) == 1
    assert calls[0].tool_name == "search"
    assert calls[0].arguments["queries"] == ["x"]
    assert calls[0].source is ToolCallSource.NATIVE


def test_native_adapter_renders_no_prompt_text(registry: ToolRegistry) -> None:
    native = NativeToolAdapter(registry)
    assert native.render_tools(registry.list()) == ""


def test_native_tool_schemas() -> None:
    native = NativeToolAdapter(ToolRegistry())
    schemas = native.tool_schemas(build_production_tools())
    assert len(schemas) == 9
    assert all(s["type"] == "function" for s in schemas)
    names = {s["function"]["name"] for s in schemas}
    assert "search" in names
