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


def test_legacy_has_zero_handlers_references() -> None:
    """AST 级：legacy 不得以任何形式引用 handlers。

    检查：ImportFrom / Import / Name / Attribute / 字符串常量
    （后者能抓到 importlib.import_module("...handlers") 这类动态引用）。
    """
    tree = ast.parse(LEGACY.read_text(encoding="utf-8"))
    docs = _strip_docstring_ids(tree)
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "handlers" in (node.module or ""):
                offenders.append(f"L{node.lineno} from {node.module}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if "handlers" in a.name:
                    offenders.append(f"L{node.lineno} import {a.name}")
        elif isinstance(node, ast.Name) and node.id == "handlers":
            offenders.append(f"L{node.lineno} Name handlers")
        elif isinstance(node, ast.Attribute) and node.attr == "handlers":
            offenders.append(f"L{node.lineno} attr handlers")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docs:
                continue
            if node.value.strip().rstrip(".").endswith("handlers"):
                offenders.append(f"L{node.lineno} string {node.value!r}")

    assert not offenders, "legacy 仍引用 handlers:\n" + "\n".join(offenders)


def test_legacy_has_no_dynamic_import_of_handlers() -> None:
    """禁止 getattr / importlib 形式的动态 handlers 访问。"""
    src = LEGACY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if name in {"getattr", "import_module", "__import__"}:
                # 参数里不应出现 handlers
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        assert "handlers" not in arg.value, (
                            f"L{node.lineno} 动态 import 了 handlers"
                        )


def test_shared_helper_class_removed() -> None:
    """第二十节：_SharedHelper 默认目标 = removed。"""
    import dl_senpai.agent.legacy_orchestrator as mod

    assert not hasattr(mod, "_SharedHelper"), "_SharedHelper 仍存在"


def test_helper_ownership_is_explicit() -> None:
    """第六十四节：每个 helper 都能回答"现在属于哪个模块"。"""
    import dl_senpai.agent.legacy_orchestrator as mod

    expected = {
        "plain_text": "dl_senpai.gateway.platform",
        "sender_name": "dl_senpai.gateway.platform",
        "resolve_session": "dl_senpai.gateway.platform",
        "is_to_me": "dl_senpai.gateway.platform",
        "reply": "dl_senpai.gateway.platform",
        "feature_enabled": "dl_senpai.core.domain",
        "affection_scope": "dl_senpai.core.domain",
        "collect_stickers": "dl_senpai.core.domain",
        "finalize_affection_gain": "dl_senpai.core.domain",
    }
    for name, module in expected.items():
        fn = getattr(mod, name)
        assert fn.__module__ == module, f"{name} 归属错误: {fn.__module__}"


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
async def test_runtime_survives_legacy_breakage(monkeypatch) -> None:
    """第四十八节：RUNTIME 下 legacy 坏掉仍正常。"""
    import dl_senpai.agent.legacy_orchestrator as lo

    original = lo.LegacyOrchestrator.run

    async def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("legacy broken")

    lo.LegacyOrchestrator.run = boom
    try:
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
    finally:
        lo.LegacyOrchestrator.run = original


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


def test_run_is_no_longer_a_god_method() -> None:
    """第六十一节：run 不再是 778 行巨型方法。"""
    cls = _legacy_class()
    run = next(
        m for m in cls.body
        if isinstance(m, ast.AsyncFunctionDef) and m.name == "run"
    )
    loc = (run.end_lineno or run.lineno) - run.lineno + 1
    assert loc < 200, f"run() 仍有 {loc} 行，拆分未生效"


def test_run_branch_count_reduced() -> None:
    """第六十三节：分支数应显著下降（原为 211）。"""
    cls = _legacy_class()
    run = next(
        m for m in cls.body
        if isinstance(m, ast.AsyncFunctionDef) and m.name == "run"
    )
    n = 0
    for node in ast.walk(run):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
            n += 1
        elif isinstance(node, ast.BoolOp):
            n += len(node.values) - 1
        elif isinstance(node, ast.IfExp):
            n += 1
    assert n < 40, f"run() 分支数仍为 {n}"


def test_semantic_stages_exist() -> None:
    """第九/十节：存在有业务语义的阶段方法。

    判定：方法名必须是**动词短语**（prepare_/invoke_/process_/
    apply_/finalize_/decide_/validate_/resolve_/interpret_ 开头），
    且不得是 _step1 / _part_a 这类无语义命名。
    """
    cls = _legacy_class()
    methods = [
        m.name for m in cls.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not m.name.startswith("_")
    ]
    semantic_prefixes = (
        "prepare_", "invoke_", "process_", "apply_", "finalize_",
        "decide_", "validate_", "resolve_", "interpret_", "send_",
    )
    stages = [m for m in methods if m.startswith(semantic_prefixes)]
    assert len(stages) >= 8, f"语义阶段过少（{len(stages)}）: {methods}"

    # 禁止无语义命名
    for m in methods:
        for bad in ("_step", "_part_", "_phase"):
            assert bad not in m, f"存在无语义方法名: {m}"


def test_state_is_separate_from_context() -> None:
    """第八节：Context 与 State 分开，且 State 不是垃圾桶。"""
    from dl_senpai.agent.legacy_state import LegacyRunState

    state = LegacyRunState()
    fields = set(vars(state))

    # 第七节：禁止服务定位器
    for banned in ("services", "everything", "app", "globals", "kernel"):
        assert banned not in fields, f"LegacyRunState 含垃圾桶字段: {banned}"

    # 第六节：应包含核心中间状态
    for expected in ("chat_text", "intent_text", "history", "result", "reply"):
        assert expected in fields, f"LegacyRunState 缺少 {expected}"


def test_state_holds_no_service_references() -> None:
    """第七节：稳定依赖走 constructor，不进 State。"""
    import dl_senpai.agent.legacy_state as ls

    src = Path(ls.__file__).read_text(encoding="utf-8")
    for banned in ("bot:", "config:", "facade", "dispatcher:", "orchestrator:"):
        assert banned not in src, f"LegacyRunState 引用了服务: {banned}"


# ================================================================
# 5. Side effect boundary（第三十/三十一/六十六节）
# ================================================================


def test_legacy_tools_go_through_shared_dispatcher() -> None:
    """第六十六节：8 个 Tool 仍走 SharedToolDispatcher → ToolExecutor。"""
    src = LEGACY.read_text(encoding="utf-8")
    assert "SharedToolDispatcher" in src or "_dispatch_legacy_tool_calls" in src

    # 不得直接调具体工具的业务函数
    banned = (
        "apply_card_actions(", "apply_mute_actions(", "build_image_segment(",
        "run_browse_tasks(", "build_voice_segment(", "maybe_pick_sticker_reply(",
    )
    for b in banned:
        assert b not in src, f"legacy 直接执行了工具: {b}"


def test_legacy_memory_goes_through_facade() -> None:
    """第三十二节：Memory 只走 Facade。"""
    src = LEGACY.read_text(encoding="utf-8")
    for banned in (
        "get_person_memory_store", "get_affection_store",
        "get_inner_state_store", "get_group_style_store",
    ):
        assert banned not in src, f"legacy 绕过 Facade 访问 memory: {banned}"


def test_legacy_recovery_stays_in_policy() -> None:
    """第三十四节：不得重新引入内联 recovery。"""
    src = LEGACY.read_text(encoding="utf-8")
    assert "retry once" not in src
    assert "draw_retry_hint(" not in src
    assert "browse_retry_hint(" not in src


def test_legacy_permission_stays_in_executor() -> None:
    """第三十三节：不直接调权限规则。"""
    src = LEGACY.read_text(encoding="utf-8")
    # can_emit_mute 用于编排判断（是否进入禁言流程）属正常；
    # 但不得出现 filter/apply 这类"执行+授权"一体的调用
    for banned in ("apply_mute_actions(", "filter_mute_actions(bot"):
        assert banned not in src, f"legacy 直接做了权限+执行: {banned}"


# ================================================================
# 6. Observability（第三十七/三十八节）
# ================================================================


def test_legacy_logs_orchestrator_identity() -> None:
    src = LEGACY.read_text(encoding="utf-8")
    assert "legacy" in src
    # 应记录自身身份，便于与 runtime 区分
    assert "legacy." in src or "orchestrator" in src


def test_legacy_does_not_log_full_prompt() -> None:
    """第四十八节（Phase 6.5 延续）：不记录完整 prompt。"""
    src = LEGACY.read_text(encoding="utf-8")
    for banned in (
        "logger.info(system_prompt", "logger.info(prompt", "logger.debug(prompt",
        "logger.info(messages", "logger.debug(messages",
    ):
        assert banned not in src, f"记录了完整 prompt: {banned}"
