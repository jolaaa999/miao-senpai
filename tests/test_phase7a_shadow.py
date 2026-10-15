"""Phase 7A：Shadow 安全与 Validation 采集的端到端测试。

覆盖：
    第五/四节  —— Runtime Shadow 零副作用（第九节要求证明）
    第六节     —— ValidationRecorder 接入真实 shadow 路径
    第四十节   —— shadow no side effect
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dl_senpai.agent.context import AgentContext, AgentResult
from dl_senpai.agent.entry import AgentEntry
from dl_senpai.gateway import MessageContext
from dl_senpai.validation import ValidationStore, reset_recorder
from dl_senpai.validation import recorder as recorder_mod


def make_context(**kw: Any) -> AgentContext:
    base = dict(
        message_type="group", group_id="12345", user_id="67890",
        session_id="group:12345", plain_text="你好呀",
    )
    base.update(kw)
    msg = MessageContext(**base)
    return AgentContext(
        session_id=msg.session_id,
        message_context=msg,
        trigger_reason="mention",
        trace_id="tr-test",
    )


class _SideEffectTracker:
    """记录所有被触碰的副作用入口。"""

    def __init__(self) -> None:
        self.effects: list[str] = []
        self.tool_executions: list[str] = []


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """把 recorder 指向临时目录，并启用。"""
    store = ValidationStore(tmp_path / "v", enabled=True)
    recorder = recorder_mod.ValidationRecorder(store)
    monkeypatch.setattr(recorder_mod, "_recorder", recorder)
    # get_recorder 是模块级函数，entry 里 `from ..validation import get_recorder`
    # 会绑定到 validation 包的导出，因此两边都要替换
    import dl_senpai.validation as vpkg

    monkeypatch.setattr(vpkg, "get_recorder", lambda: recorder)
    yield store, recorder
    reset_recorder()


@pytest.mark.asyncio
async def test_shadow_runtime_produces_zero_side_effects(wired, monkeypatch) -> None:
    """第五节：Runtime 在 SHADOW 下只能计算，不得产生任何副作用。

    用真实 ToolExecutor + 计数 executor 验证：
        dry_run=True 时，工具**不被执行**。
    """
    from dl_senpai.agent.tools.builtin import register_production_tools
    from dl_senpai.agent.tools.executor import ToolExecutor
    from dl_senpai.agent.tools.models import ToolCall, ToolResult
    from dl_senpai.agent.tools.registry import ToolRegistry
    from dl_senpai.agent.permissions import AllowAllPermissionService

    executed: list[str] = []
    registry = ToolRegistry()
    register_production_tools(registry)
    for definition in registry.list():
        definition.executor = (
            lambda n: lambda ctx, call: executed.append(n) or ToolResult(
                tool_name=n, tool_call_id=call.id, success=True,
                data={"value": n},
            )
        )(definition.name)

    executor = ToolExecutor(
        registry, permissions=AllowAllPermissionService()
    )
    call = ToolCall(tool_name="mute", arguments={"user_id": 1, "duration": 60})

    # dry_run=True → 不得执行
    result = await executor.execute(call, make_context(), dry_run=True)
    assert executed == [], f"dry_run 下执行了工具: {executed}"
    assert result is not None

    # dry_run=False → 正常执行（对照，证明计数有效）
    await executor.execute(call, make_context(), dry_run=False)
    assert executed == ["mute"], "对照组未执行，说明计数逻辑本身有问题"


@pytest.mark.asyncio
async def test_shadow_records_validation_data(wired) -> None:
    """第六节：SHADOW 路径必须产出 Validation 记录。"""
    store, recorder = wired

    async def legacy_handler(ctx: AgentContext) -> Any:
        from dl_senpai.llm import ChatResult

        return ChatResult(text="legacy 回复")

    class Runtime:
        executor = None

        async def run(self, ctx, **kw):
            from dl_senpai.agent.tools.models import ToolCall

            result = AgentResult(text="runtime 回复")
            result.tool_calls = [
                ToolCall(tool_name="search", arguments={"queries": ["x"]})
            ]
            result.stop_reason = None
            return result

    monkeypatch_env = None
    import os

    os.environ["DL_SENPAI_AGENT_MODE"] = "shadow"
    try:
        entry = AgentEntry(runtime=Runtime(), legacy_handler=legacy_handler)
        await entry.handle(make_context())
    finally:
        os.environ.pop("DL_SENPAI_AGENT_MODE", None)

    rows = store.read()
    assert len(rows) == 1, f"未产出验证记录: {rows}"
    row = rows[0]
    assert row["execution_mode"] == "shadow"
    assert row["legacy"] is not None
    assert row["runtime"] is not None
    # 行为级数据存在
    assert "search" in row["runtime"]["tool_calls"]
    assert row["latency"]["total"] >= 0


@pytest.mark.asyncio
async def test_shadow_validation_has_no_plaintext(wired) -> None:
    """第七/八/九节：落盘内容不得含真实 id 或用户原文。"""
    store, recorder = wired

    async def legacy_handler(ctx: AgentContext) -> Any:
        from dl_senpai.llm import ChatResult

        return ChatResult(text="我不会出现在盘上")

    class Runtime:
        executor = None

        async def run(self, ctx, **kw):
            return AgentResult(text="我也不会出现")

    import os

    os.environ["DL_SENPAI_AGENT_MODE"] = "shadow"
    try:
        entry = AgentEntry(runtime=Runtime(), legacy_handler=legacy_handler)
        await entry.handle(
            make_context(
                group_id="998877665", user_id="112233445",
                session_id="group:998877665",
                plain_text="我的手机号是13800138000",
            )
        )
    finally:
        os.environ.pop("DL_SENPAI_AGENT_MODE", None)

    files = list(store.directory.glob("*.jsonl"))
    assert files
    raw = files[0].read_text(encoding="utf-8")
    for secret in (
        "998877665", "112233445", "13800138000",
        "我的手机号是", "我不会出现在盘上", "我也不会出现",
    ):
        assert secret not in raw, f"落盘内容泄漏了 {secret!r}"

    row = json.loads(raw.strip())
    assert row["group_hash"].startswith("g_")
    assert row["message"]["length"] > 0
    assert "text" not in row["message"]


@pytest.mark.asyncio
async def test_validation_failure_does_not_break_reply(wired, monkeypatch) -> None:
    """观测失败绝不影响业务（与 observability 同一条原则）。"""
    store, recorder = wired

    def boom(*a, **k):
        raise RuntimeError("validation disk exploded")

    monkeypatch.setattr(recorder, "record_request", boom)

    async def legacy_handler(ctx: AgentContext) -> Any:
        from dl_senpai.llm import ChatResult

        return ChatResult(text="正常回复")

    class Runtime:
        executor = None

        async def run(self, ctx, **kw):
            return AgentResult(text="runtime")

    import os

    os.environ["DL_SENPAI_AGENT_MODE"] = "shadow"
    try:
        entry = AgentEntry(runtime=Runtime(), legacy_handler=legacy_handler)
        outcome = await entry.handle(make_context())
    finally:
        os.environ.pop("DL_SENPAI_AGENT_MODE", None)

    # 回复流程照常完成
    assert outcome.handled is True


@pytest.mark.asyncio
async def test_validation_disabled_by_default(tmp_path, monkeypatch) -> None:
    """默认不落盘（避免开发机产生垃圾）。

    用哨兵值而非 delenv —— 见 config_env.UNSET_SENTINEL 的说明。
    """
    from dl_senpai.config_env import UNSET_SENTINEL

    monkeypatch.setenv("DL_SENPAI_VALIDATION", UNSET_SENTINEL)
    store = ValidationStore(tmp_path / "v")
    assert store.enabled is False

    recorder = recorder_mod.ValidationRecorder(store)
    monkeypatch.setattr(recorder_mod, "_recorder", recorder)
    import dl_senpai.validation as vpkg

    monkeypatch.setattr(vpkg, "get_recorder", lambda: recorder)

    async def legacy_handler(ctx: AgentContext) -> Any:
        from dl_senpai.llm import ChatResult

        return ChatResult(text="x")

    class Runtime:
        executor = None

        async def run(self, ctx, **kw):
            return AgentResult(text="y")

    import os

    os.environ["DL_SENPAI_AGENT_MODE"] = "shadow"
    try:
        entry = AgentEntry(runtime=Runtime(), legacy_handler=legacy_handler)
        await entry.handle(make_context())
    finally:
        os.environ.pop("DL_SENPAI_AGENT_MODE", None)

    assert not (tmp_path / "v").exists() or not list((tmp_path / "v").glob("*.jsonl"))
