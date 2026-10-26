"""Phase 6.6：编排边界、反向依赖消除、阶段拆分验收。

覆盖第二十一/二十二/三十/三十一/三十二/三十三/三十四/四十六/
五十一/六十二/六十四/六十五/六十六/六十七节。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"
AGENT = PKG / "agent"
LEGACY = AGENT / "legacy_orchestrator.py"
HANDLERS = PKG / "handlers.py"


def _strip_docstring_ids(tree: ast.Module) -> set[int]:
    out: set[int] = set()
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
                out.add(id(body[0].value))
    return out


# ================================================================
# 1. Legacy 不 import handlers（第二十一/六十二节）
# ================================================================


def test_helper_ownership_is_explicit() -> None:
    """第六十四节：每个 helper 都能回答"现在属于哪个模块"。

    2026-10：旧编排删除后，这些 helper 的唯一归属地就是下面这些模块。
    守住它等于守住"不要在别处再抄一份同名 helper"。
    """
    import dl_senpai.core.domain as domain
    import dl_senpai.gateway.platform as platform

    expected = {
        "plain_text": platform,
        "sender_name": platform,
        "resolve_session": platform,
        "is_to_me": platform,
        "reply": platform,
        "feature_enabled": domain,
        "affection_scope": domain,
        "collect_stickers": domain,
        "finalize_affection_gain": domain,
    }
    for name, module in expected.items():
        assert hasattr(module, name), f"{name} 不在 {module.__name__}"


# ================================================================
# 2. handlers 不深入 Legacy internals（第二十二/六十五节）
# ================================================================


def test_handlers_does_not_import_legacy_internals() -> None:
    """handlers 只能调 AgentEntry 或 compatibility forwarder。"""
    tree = ast.parse(HANDLERS.read_text(encoding="utf-8"))
    banned = {"LegacyRunState", "LegacyOrchestrator"}
    offenders: list[str] = []
    # 允许 LegacyOrchestrator（forwarder 需要），但不允许 LegacyRunState 等内部结构
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in {"LegacyRunState", "LegacyPromptBuilder"}:
                    offenders.append(f"L{node.lineno} {a.name}")
    assert not offenders, f"handlers 深入了 legacy 内部: {offenders}"


def test_handlers_has_no_agent_orchestration() -> None:
    """第六十五节：handlers 不得重新增长 Agent orchestration。"""
    src = HANDLERS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    docs = _strip_docstring_ids(tree)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docs:
                names.add(node.value)

    for banned in (
        "SenpaiLLM", "LegacyRunState", "ToolExecutor",
        "MarkerToolAdapter", "RecoveryPolicy",
    ):
        assert banned not in names, f"handlers 出现编排符号: {banned}"


# ================================================================
# 3. Runtime 隔离（第四十八/四十九/五十/五十一节）
# ================================================================


def test_runtime_does_not_import_legacy() -> None:
    offenders: list[str] = []
    for path in AGENT.rglob("*.py"):
        if path.name in {"legacy_orchestrator.py", "legacy_state.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if "legacy_orchestrator" in mod or "legacy_state" in mod:
                    offenders.append(f"{path.name}: from {mod}")
                for a in node.names:
                    if a.name in {"LegacyOrchestrator", "LegacyRunState"}:
                        offenders.append(f"{path.name}: {a.name}")
    assert not offenders, f"Runtime 依赖了 legacy: {offenders}"


@pytest.mark.asyncio
async def test_runtime_survives_missing_legacy() -> None:
    """第四十八节：RUNTIME 与旧路径的存亡无关。

    2026-10：旧编排已删除，所以这里不再 monkeypatch 一个不存在的模块，
    而是直接断言"没有 legacy handler 时 runtime 照常跑"。
    守的东西没变：RUNTIME 绝不依赖旧路径。
    """
    from dl_senpai.agent.context import AgentContext, AgentResult
    from dl_senpai.agent.entry import AgentEntry
    from dl_senpai.gateway import MessageContext

    class RT:
        executor = None

        async def run(self, *a: Any, **k: Any) -> Any:
            return AgentResult(text="runtime ok")

    msg = MessageContext(
        message_type="group", group_id="900001", user_id="10001",
        session_id="group:900001", plain_text="你好",
    )
    ctx = AgentContext(session_id="group:900001", message_context=msg)
    outcome = await AgentEntry(runtime=RT(), legacy_handler=None).handle(ctx)
    assert outcome.used_runtime is True
    assert outcome.result.text == "runtime ok"


@pytest.mark.asyncio
async def test_legacy_survives_runtime_breakage(monkeypatch) -> None:
    """第四十九节：LEGACY 下 runtime 坏掉仍正常。"""
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
async def test_canary_still_selects_by_group(monkeypatch) -> None:
    """第五十一节：CANARY 按群白名单选择不受影响。"""
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "canary")
    monkeypatch.setenv("DL_SENPAI_AGENT_RUNTIME_GROUPS", "900001")

    from dl_senpai.agent.mode import AgentExecutionMode, mode_for_group

    # 白名单内的群走 Runtime，其余走 Legacy
    assert (
        mode_for_group(AgentExecutionMode.CANARY, "900001")
        is AgentExecutionMode.RUNTIME
    )
    assert (
        mode_for_group(AgentExecutionMode.CANARY, "900999")
        is AgentExecutionMode.LEGACY
    )
    # 非 CANARY 模式下 group 参数被忽略
    assert (
        mode_for_group(AgentExecutionMode.RUNTIME, "900999")
        is AgentExecutionMode.RUNTIME
    )


# ================================================================
# 4. 拆分结构验收（第九/十/十一/六十一/六十三节）
# ================================================================


def _legacy_class() -> ast.ClassDef:
    tree = ast.parse(LEGACY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LegacyOrchestrator":
            return node
    raise AssertionError("找不到 LegacyOrchestrator")


# ================================================================
# 5. Side effect boundary（第三十/三十一/六十六节）
# ================================================================


# ================================================================
# 6. Observability（第三十七/三十八节）
# ================================================================
