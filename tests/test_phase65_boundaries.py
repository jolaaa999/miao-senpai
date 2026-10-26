"""Phase 6.5：编排架构边界与模式隔离测试。

覆盖第三十/三十一/三十二/三十三/四十/四十一/四十二/四十七节：
    handlers boundary
    legacy boundary
    runtime boundary
    import cycles
    mode isolation（RUNTIME / LEGACY / SHADOW）
    no double side effects
    observability 字段
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"
AGENT = PKG / "agent"
HANDLERS = PKG / "handlers.py"
LEGACY = AGENT / "legacy_orchestrator.py"


def imports_of(path: Path) -> list[str]:
    """收集文件的 import 模块名 + 导入的符号名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            out.append(node.module or "")
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
    return out


def code_names(path: Path) -> set[str]:
    """真实代码里出现的名字与属性（不含 docstring）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docs.add(id(body[0].value))

    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
    return out


# ================================================================
# 1. handlers boundary（第三十/五十节）
# ================================================================


def test_handlers_does_not_import_llm_directly() -> None:
    """handlers 不得再直接持有 SenpaiLLM（编排已搬走）。"""
    names = code_names(HANDLERS)
    assert "get_llm" not in names, "handlers 仍直接使用 get_llm"
    assert "LLMError" not in names, "handlers 仍 import LLMError"


def test_handlers_does_not_import_tool_internals() -> None:
    """handlers 不得 import ToolExecutor / MarkerToolAdapter / ToolRegistry。"""
    imps = " ".join(imports_of(HANDLERS))
    for banned in ("ToolExecutor", "MarkerToolAdapter", "ToolRegistry"):
        assert banned not in imps, f"handlers import 了 {banned}"


def test_handlers_does_not_import_raw_memory_stores() -> None:
    """handlers 的**编排路径**不得直接 import 原始 memory store 工厂。

    第十四节限制的是 "Legacy orchestration"，不是整个 handlers.py。
    `get_affection_store` 仍被 affection / checkin / shop **命令**使用
    （那是平台业务命令，不是 Agent 编排），属合理例外。

    2026-10 注：旧编排实现已删除，这条断言的对象只剩 handlers 里的
    `_handle_chat_inner`（现在是一个显式抛错的桩）。
    """
    import dl_senpai.handlers as handlers

    src = inspect.getsource(handlers._handle_chat_inner)
    for banned in (
        "get_person_memory_store", "get_affection_store",
        "get_inner_state_store", "get_group_style_store",
        "MemoryFacade",
    ):
        assert banned not in src, f"编排路径仍访问 raw store: {banned}"


def test_handlers_does_not_parse_markers() -> None:
    """handlers 不得持有 marker parser。"""
    names = code_names(HANDLERS)
    for banned in (
        "parse_card_actions", "parse_mute_actions", "parse_draw_request",
        "parse_browse_request", "parse_sticker_request", "parse_voice_request",
        "parse_impression_updates", "parse_search_request",
    ):
        assert banned not in names, f"handlers 仍引用 parser {banned}"


def test_handlers_does_not_use_recovery_policy() -> None:
    names = code_names(HANDLERS)
    assert "RecoveryPolicy" not in names
    assert "build_recovery_policies" not in names


def test_handlers_has_no_system_prompt_assembly() -> None:
    names = code_names(HANDLERS)
    assert "build_system_prompt" not in names, "handlers 仍组装 system prompt"


# ================================================================
# 2. Legacy boundary（第三十一节）
# ================================================================


# ================================================================
# 3. Runtime boundary（第三十二/四十节）
# ================================================================


def test_runtime_does_not_depend_on_legacy_orchestrator() -> None:
    """RUNTIME 生产路径不得 import / invoke LegacyOrchestrator。"""
    offenders: list[str] = []
    for path in AGENT.rglob("*.py"):
        if path.name == "legacy_orchestrator.py":
            continue
        imps = imports_of(path)
        if any("legacy_orchestrator" in i for i in imps):
            offenders.append(path.name)
        if "LegacyOrchestrator" in imps:
            offenders.append(f"{path.name}(LegacyOrchestrator)")
    assert not offenders, f"Runtime 依赖了 LegacyOrchestrator: {offenders}"


def test_runtime_module_does_not_reference_legacy() -> None:
    """AgentRuntime 源码里不得出现 legacy 编排。"""
    src = (AGENT / "runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (
                body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docs.add(id(body[0].value))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert "LegacyOrchestrator" not in names
    assert "legacy_orchestrator" not in names


def test_agent_entry_is_thin() -> None:
    """第二十三节：AgentEntry 只负责 mode / orchestrator selection。"""
    src = (AGENT / "entry.py").read_text(encoding="utf-8")
    for banned in (
        "build_system_prompt", "PersonMemory", "Affection",
        "ToolExecutor(", "MarkerToolAdapter(", "PromptBuilder(",
    ):
        assert banned not in src, f"AgentEntry 侵入了不属于它的职责: {banned}"


# ================================================================
# 4. Import cycles（第三十三节）
# ================================================================


def test_no_module_level_cycle_handlers_legacy() -> None:
    """handlers 与旧编排之间不得有**模块级** import（历史上有过循环）。

    旧编排已删除，这条仍然要守：它是"handlers 不深入 Agent 内部"的
    结构信号。如果哪天有人又在 handlers 顶部 import 某个 Agent 模块，
    模块级循环会立刻回来。
    """
    handlers_tree = ast.parse(HANDLERS.read_text(encoding="utf-8"))
    for node in handlers_tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ""
            names = [a.name for a in node.names]
            assert "legacy_orchestrator" not in mod, (
                "handlers 在模块级 import 了 legacy_orchestrator（已不存在的模块）"
            )
            assert "LegacyOrchestrator" not in names


# ================================================================
# 5. Mode isolation（第四十一节）
# ================================================================


@pytest.mark.asyncio
async def test_runtime_survives_broken_legacy(monkeypatch) -> None:
    """配成 LEGACY、且 legacy handler 抛错时，仍必须给出回复（降级到 runtime）。

    这是删除 legacy_orchestrator 之后唯一的行为保险：
    以前靠"旧实现还在、只是坏了"，现在靠"坏得明明白白 + 稳定降级"。
    如果这条挂了，说明 `_run_legacy` 的降级分支被改坏了 —— 那时
    `DL_SENPAI_AGENT_MODE=legacy` 会变成"机器人完全不回话"。
    """
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "legacy")

    from dl_senpai.agent.context import AgentContext, AgentResult
    from dl_senpai.agent.entry import AgentEntry
    from dl_senpai.gateway import MessageContext

    class RT:
        executor = None

        async def run(self, *a: Any, **k: Any) -> Any:
            return AgentResult(text="runtime ok")

    async def broken_legacy(context: Any) -> None:
        raise RuntimeError("legacy_path_removed: 旧编排实现已删除")

    msg = MessageContext(
        message_type="group", group_id="900001", user_id="10001",
        session_id="group:900001", plain_text="你好",
    )
    ctx = AgentContext(session_id="group:900001", message_context=msg)
    entry = AgentEntry(runtime=RT(), legacy_handler=broken_legacy)
    outcome = await entry.handle(ctx)
    assert outcome.used_runtime is True
    assert outcome.result.text == "runtime ok"


@pytest.mark.asyncio
async def test_legacy_survives_runtime_breakage(monkeypatch) -> None:
    """LEGACY 下把 AgentRuntime 打成废的，legacy 仍工作。"""
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "legacy")

    from dl_senpai.agent.context import AgentContext
    from dl_senpai.agent.entry import AgentEntry
    from dl_senpai.gateway import MessageContext

    calls: list[str] = []

    async def legacy(ctx: AgentContext) -> Any:
        calls.append("legacy")
        return "L"

    class Broken:
        executor = None

        async def run(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("runtime broken")

    msg = MessageContext(
        message_type="group", group_id="900001", user_id="10001",
        session_id="group:900001", plain_text="你好",
    )
    ctx = AgentContext(session_id="group:900001", message_context=msg)
    outcome = await AgentEntry(runtime=Broken(), legacy_handler=legacy).handle(ctx)
    assert calls == ["legacy"]
    assert outcome.mode == "legacy"


@pytest.mark.asyncio
async def test_shadow_only_legacy_produces_effect(monkeypatch) -> None:
    """SHADOW：authoritative=Legacy，Runtime 只 dry-run。"""
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "shadow")

    from dl_senpai.agent.context import AgentContext
    from dl_senpai.agent.entry import AgentEntry
    from dl_senpai.agent.context import AgentResult
    from dl_senpai.gateway import MessageContext

    side_effects: list[str] = []

    async def legacy(ctx: AgentContext) -> Any:
        side_effects.append("legacy-effect")
        return "L"

    class Spy:
        executor = None
        dry_runs = 0

        async def run(self, *a: Any, **k: Any) -> Any:
            if k.get("dry_run"):
                self.dry_runs += 1
                # dry_run 不得产生副作用
                return AgentResult(text="shadow")
            side_effects.append("runtime-effect")
            return AgentResult(text="r")

    spy = Spy()
    msg = MessageContext(
        message_type="group", group_id="900001", user_id="10001",
        session_id="group:900001", plain_text="你好",
    )
    ctx = AgentContext(session_id="group:900001", message_context=msg)
    await AgentEntry(runtime=spy, legacy_handler=legacy).handle(ctx)

    assert spy.dry_runs == 1, "Runtime 应以 dry_run 调用"
    assert side_effects == ["legacy-effect"], (
        "SHADOW 下只有 authoritative（legacy）产生真实效果"
    )


# ================================================================
# 6. No double side effects（第四十二节）
# ================================================================


@pytest.mark.asyncio
async def test_no_double_side_effects_in_legacy_path() -> None:
    """同一 ToolCall 在 legacy 路径下也只执行一次（第四十二节）。

    使用**真实 ToolExecutor + ExecutionJournal**，只把工具实现的
    底层函数换成计数器 —— 这样验证的是真实的幂等链路，
    而不是测试替身自己的行为。
    """
    from unittest.mock import MagicMock

    from dl_senpai.agent.journal import ExecutionJournal
    from dl_senpai.agent.permissions import AllowAllPermissionService
    from dl_senpai.agent.tool_dispatch import (
        SharedToolDispatcher,
        tool_calls_from_mute_actions,
    )
    from dl_senpai.agent.tools.builtin import register_production_tools
    from dl_senpai.agent.tools.executor import ToolExecutor
    from dl_senpai.agent.tools.models import ToolResult
    from dl_senpai.agent.tools.registry import ToolRegistry

    executed: list[str] = []

    registry = ToolRegistry()
    register_production_tools(registry)
    for definition in registry.list():
        definition.executor = (
            lambda n: lambda ctx, call: executed.append(n) or ToolResult(
                tool_name=n, tool_call_id=call.id, success=True,
                data={"value": "ok"},
            )
        )(definition.name)

    journal = ExecutionJournal()
    executor = ToolExecutor(
        registry, permissions=AllowAllPermissionService(), journal=journal
    )
    dispatcher = SharedToolDispatcher(executor=executor, journal=journal)

    calls = tool_calls_from_mute_actions(
        [MagicMock(user_id=1, duration=60, kind="request")]
    )
    await dispatcher.dispatch(calls, None)
    await dispatcher.dispatch(calls, None)
    assert len(executed) == 1, f"mute 被执行了 {len(executed)} 次"


@pytest.mark.asyncio
async def test_double_execution_blocked_for_irreversible_tools() -> None:
    """逐个验证不可逆工具都只执行一次（第四十二节）。"""
    from unittest.mock import MagicMock

    from dl_senpai.agent.journal import ExecutionJournal
    from dl_senpai.agent.permissions import AllowAllPermissionService
    from dl_senpai.agent.tool_dispatch import (
        SharedToolDispatcher,
        tool_calls_from_card_actions,
        tool_calls_from_impressions,
    )
    from dl_senpai.agent.tools.builtin import register_production_tools
    from dl_senpai.agent.tools.executor import ToolExecutor
    from dl_senpai.agent.tools.models import ToolCall, ToolResult
    from dl_senpai.agent.tools.registry import ToolRegistry

    for tool, make_calls in (
        ("mute", lambda: [ToolCall(tool_name="mute", arguments={"user_id": 1, "duration": 60})]),
        ("card", lambda: tool_calls_from_card_actions(
            [MagicMock(user_id=1, card="x", kind="request")])),
        ("draw", lambda: [ToolCall(tool_name="draw", arguments={"prompt": "cat"})]),
        ("voice", lambda: [ToolCall(tool_name="voice", arguments={"override": "hi"})]),
        ("impression", lambda: tool_calls_from_impressions(
            [MagicMock(add=["爱猫"], remove=[], notes=None)])),
    ):
        executed: list[str] = []
        registry = ToolRegistry()
        register_production_tools(registry)
        for definition in registry.list():
            definition.executor = (
                lambda n: lambda ctx, call: executed.append(n) or ToolResult(
                    tool_name=n, tool_call_id=call.id, success=True,
                    data={"value": "ok"},
                )
            )(definition.name)

        journal = ExecutionJournal()
        executor = ToolExecutor(
            registry, permissions=AllowAllPermissionService(), journal=journal
        )
        dispatcher = SharedToolDispatcher(executor=executor, journal=journal)
        calls = make_calls()
        await dispatcher.dispatch(calls, None)
        await dispatcher.dispatch(calls, None)
        assert len(executed) == 1, f"{tool} 被执行了 {len(executed)} 次"


# ================================================================
# 7. Observability（第四十七节）
# ================================================================


def test_orchestrator_protocol_exists() -> None:
    from dl_senpai.agent.orchestrator import AgentOrchestrator, is_orchestrator

    assert is_orchestrator(type("X", (), {"run": lambda self: None})())

    # 唯一实现者：AgentRuntime 必须满足这层契约（旧实现已删除）。
    from dl_senpai.agent.runtime import AgentRuntime

    assert is_orchestrator(AgentRuntime())


def test_entry_logs_mode() -> None:
    """AgentEntry 必须记录 mode（便于区分 orchestrator）。"""
    src = (AGENT / "entry.py").read_text(encoding="utf-8")
    assert '"agent.entry"' in src or "'agent.entry'" in src
    assert "mode=" in src


def test_orchestrator_does_not_log_full_prompt() -> None:
    """第四十八节：不得记录完整 prompt。

    2026-10：断言对象从已删除的 legacy_orchestrator 换成生产路径的
    runtime + entry —— 守的东西没变，只是现在只剩这一条路径可守。
    """
    for path in (AGENT / "runtime.py", AGENT / "entry.py"):
        src = path.read_text(encoding="utf-8")
        for banned in (
            "logger.info(system_prompt", "logger.info(prompt",
            "logger.debug(prompt", "log_event(system_prompt=",
        ):
            assert banned not in src, f"{path.name} 记录了完整 prompt: {banned}"


# ================================================================
# 8. 结构性验收（第四十九/五十一节）
# ================================================================


def test_legacy_orchestrator_is_gone() -> None:
    """旧编排必须**不存在**。

    反过来的结构断言：2026-10 之前这里断言"LegacyOrchestrator 独立存在"，
    但它从来跑不起来（LegacyRunState 没接上），留着只是给回滚制造错觉。
    现在断言它消失，防止有人再塞一个"看起来能回滚"的假入口进来。
    """
    assert not LEGACY.exists(), "legacy_orchestrator.py 又回来了？旧路径已判定不可维护"
    assert not (AGENT / "legacy_state.py").exists()
    assert not (AGENT / "legacy_context.py").exists()
    with pytest.raises(ImportError):
        importlib.import_module("dl_senpai.agent.legacy_orchestrator")


def test_handlers_no_longer_contains_orchestration() -> None:
    """handlers.py 不再包含完整 Agent orchestration。"""
    src = HANDLERS.read_text(encoding="utf-8")
    lines = len(src.splitlines())
    assert lines < 1400, f"handlers.py 仍有 {lines} 行，编排可能未搬干净"
    # 主体必须不在 handlers
    assert "result.card_actions" not in src, "handlers 仍持有工具结果编排"


def test_forwarder_is_thin() -> None:
    """第八节：forwarding wrapper 只能做 argument adaptation。"""
    import dl_senpai.handlers as handlers

    src = inspect.getsource(handlers._handle_chat_inner)
    code_lines = [
        ln for ln in src.splitlines()
        if ln.strip() and not ln.strip().startswith("#") and '"""' not in ln
    ]
    assert len(code_lines) < 30, f"forwarder 太厚（{len(code_lines)} 行代码）"


def test_legacy_capabilities_still_complete() -> None:
    """第二十/二十一节：LEGACY 仍支持全部 8 种能力。"""
    from dl_senpai.agent.tools.builtin import build_production_tools
    from dl_senpai.agent.tools.executors import REAL_EXECUTORS

    tools = {t.name: t for t in build_production_tools()}
    assert set(tools) == set(REAL_EXECUTORS)
    for name, executor in REAL_EXECUTORS.items():
        assert tools[name].executor is executor
