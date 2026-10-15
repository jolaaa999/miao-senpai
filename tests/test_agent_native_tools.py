"""原生 function calling 接入测试（含一个历史 bug 的回归）。

背景：`SenpaiLLM._finalize_result` 会把 `<<<MARKER>>>` 从正文里剥掉再返回
`ChatResult`；而 `LegacyModelGateway` 过去只把 `text` 交给 `AgentRuntime`，
导致 runtime 再解析时一个工具都拿不到 —— 也就是说 marker 模式下工具从未
真正执行过。这组测试锁住修复后的行为，并覆盖新接入的原生 tool_calls。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from dl_senpai.agent.context import AgentContext
from dl_senpai.agent.model_gateway import (
    LegacyModelGateway,
    ModelRequest,
    ModelResponse,
    StubModelGateway,
)
from dl_senpai.agent.runtime import AgentRuntime
from dl_senpai.agent.tools.builtin import register_production_tools
from dl_senpai.agent.tools.marker_adapter import NativeToolAdapter, ParseOutput
from dl_senpai.agent.tools.models import ToolCall
from dl_senpai.agent.tools.registry import ToolRegistry
from dl_senpai.config import get_config
from dl_senpai.llm import LLMError, SenpaiLLM, _Completion

NL = chr(10)
CARD_MARKER = '<<<CARD {"user_id":1,"card":"新名片","kind":"request"}>>>'


def _ctx(**kw: Any) -> AgentContext:
    base = dict(session_id="group:1", trace_id="t1", trigger_reason="mention")
    base.update(kw)
    return AgentContext(**base)


def _fake_client(message: Any, sink: list[dict[str, Any]] | None = None) -> Any:
    """最小 OpenAI 客户端替身：记录请求参数并返回预设 message。"""

    class _Completions:
        async def create(self, **kwargs: Any) -> Any:
            if sink is not None:
                sink.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)], usage=None
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))


def _llm_with_client(message: Any, sink=None) -> SenpaiLLM:
    llm = SenpaiLLM(get_config())
    client = _fake_client(message, sink)
    llm._resolve_client = lambda: (client, "relay", "deepseek-v4.1-flash")
    return llm


# ================================================================
# 回归：网关必须把工具调用带回给 runtime
# ================================================================


def test_finalize_result_exposes_marker_calls() -> None:
    """解析出的 marker 调用必须挂到 ChatResult.tool_calls 上。

    这是修复的核心：过去 ChatResult 只暴露 8 个 legacy 字段（card_actions
    等），网关拿不到通用 calls，正文里的 marker 又被剥掉了，于是 runtime
    什么也解析不到。card 只在 card_enable=True 时才解析，所以要显式打开。
    """
    llm = SenpaiLLM(get_config())
    res = llm._finalize_result(
        "好的" + NL + CARD_MARKER, card_enable=True, mute_enable=False, searched=False
    )
    assert "CARD" not in res.text
    assert [c.tool_name for c in res.tool_calls] == ["card"]
    assert res.tool_calls[0].arguments["card"] == "新名片"


@pytest.mark.asyncio
async def test_gateway_propagates_tool_calls() -> None:
    """网关必须把 ChatResult.tool_calls 透传成 ModelResponse.tool_calls。"""
    from dl_senpai.agent.tools.models import ToolCallSource
    from dl_senpai.llm import ChatResult

    llm = SenpaiLLM(get_config())
    card_call = ToolCall(
        tool_name="card",
        arguments={"user_id": 1, "card": "新名片"},
        source=ToolCallSource.MARKER,
    )

    async def fake_chat(*args: Any, **kwargs: Any) -> Any:
        return ChatResult(text="好的", tool_calls=[card_call])

    llm.chat = fake_chat  # type: ignore[assignment]
    gw = LegacyModelGateway(llm)
    ctx = SimpleNamespace(config=get_config(), actor="小明")
    resp = await gw.chat(
        ModelRequest(system_prompt="你是学姐", user_text="改名片", history=[]), ctx
    )

    assert resp.text == "好的"
    assert [c.tool_name for c in resp.tool_calls] == ["card"]


@pytest.mark.asyncio
async def test_gateway_propagates_native_calls_on_empty_content() -> None:
    """原生调用通常 content 为空，此时不能抛"空回复"，且 calls 要带出来。"""
    llm = SenpaiLLM(get_config())
    raw = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="search", arguments='{"queries": ["武汉天气"]}'),
    )

    async def fake_complete(messages, *, max_tokens, timeout, tools=None):
        return _Completion(text="", tool_calls=[raw])

    llm._complete_once = fake_complete
    gw = LegacyModelGateway(llm)
    ctx = SimpleNamespace(config=get_config(), actor="小明")
    resp = await gw.chat(ModelRequest(system_prompt="s", user_text="天气", history=[]), ctx)

    assert [c.tool_name for c in resp.tool_calls] == ["search"]
    assert resp.tool_calls[0].arguments["queries"] == ["武汉天气"]


# ================================================================
# runtime：优先使用结构化 calls，空时才回落解析正文
# ================================================================


def test_parse_model_response_prefers_structured_calls() -> None:
    rt = AgentRuntime(registry=ToolRegistry(), gateway=StubModelGateway())
    call = ToolCall(tool_name="draw", arguments={"prompt": "猫"})
    parsed = rt._parse_model_response(ModelResponse(text="画画", tool_calls=[call]), _ctx())
    assert parsed.calls == [call]


def test_parse_model_response_falls_back_to_text() -> None:
    """tool_calls 为空时必须回落到解析正文，保持对 Stub/旧网关的兼容。"""
    rt = AgentRuntime(registry=ToolRegistry(), gateway=StubModelGateway())
    seen: list[str] = []

    class _Spy:
        def parse_response(self, text: str, context: Any = None) -> ParseOutput:
            seen.append(text)
            return ParseOutput(text=text, calls=[])

    rt._adapter = _Spy()  # type: ignore[assignment]
    rt._parse_model_response(ModelResponse(text="hello"), _ctx())
    assert seen == ["hello"]


# ================================================================
# 原生 schema：默认关闭 + 形状必须合法
# ================================================================


def test_native_tools_disabled_by_default() -> None:
    cfg = get_config()
    assert getattr(cfg, "agent_native_tools", None) is False


def test_native_tool_schemas_are_valid_json_schema() -> None:
    """schema 必须是标准 JSON Schema，否则兼容接口会判 400。"""
    reg = ToolRegistry()
    register_production_tools(reg)
    schemas = NativeToolAdapter.tool_schemas([reg.get(n) for n in reg.names()])
    assert schemas, "至少要能生成 schema"
    for item in schemas:
        assert item["type"] == "function"
        fn = item["function"]
        assert fn["name"] and isinstance(fn["description"], str)
        params = fn["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        for pname, pnode in params["properties"].items():
            assert "type" in pnode, f"{fn['name']}.{pname} 缺少 type"


def test_native_schemas_empty_when_disabled() -> None:
    rt = AgentRuntime(registry=ToolRegistry(), gateway=StubModelGateway())

    class _Cfg:
        agent_native_tools = False

    assert rt._native_tool_schemas(_ctx(config=_Cfg())) == []


# ================================================================
# _complete_once：返回值形状、tools 透传、空回复守卫
# ================================================================


@pytest.mark.asyncio
async def test_complete_once_returns_completion_and_sends_tools() -> None:
    sink: list[dict[str, Any]] = []
    message = SimpleNamespace(
        content="",
        tool_calls=[
            SimpleNamespace(id="c1", function=SimpleNamespace(name="search", arguments="{}"))
        ],
    )
    llm = _llm_with_client(message, sink)
    schemas = [
        {
            "type": "function",
            "function": {"name": "search", "parameters": {"type": "object", "properties": {}}},
        }
    ]

    comp = await llm._complete_once(
        [{"role": "user", "content": "hi"}], max_tokens=16, timeout=5.0, tools=schemas
    )

    assert comp.text == ""
    assert len(comp.tool_calls) == 1
    assert sink[0]["tools"] == schemas
    assert sink[0]["tool_choice"] == "auto"
    # 带 tools 时不得使用流式（流式不会聚合分片的 tool_calls）
    assert sink[0].get("stream") is None


@pytest.mark.asyncio
async def test_omitting_tools_keeps_request_unchanged() -> None:
    """不传 tools 时请求体不能多出 tools/tool_choice 字段。"""
    sink: list[dict[str, Any]] = []
    llm = _llm_with_client(SimpleNamespace(content="你好", tool_calls=None), sink)
    comp = await llm._complete_once(
        [{"role": "user", "content": "hi"}], max_tokens=16, timeout=5.0
    )
    assert comp.text == "你好"
    assert "tools" not in sink[0]


@pytest.mark.asyncio
async def test_empty_content_without_tool_calls_still_raises() -> None:
    """空回复守卫不能因为原生调用而被整体放宽。"""
    llm = _llm_with_client(SimpleNamespace(content="", tool_calls=None))
    with pytest.raises(LLMError):
        await llm._complete_once(
            [{"role": "user", "content": "hi"}], max_tokens=16, timeout=5.0
        )
