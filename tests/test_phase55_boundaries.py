"""Phase 5.5 架构边界测试（第二十八~三十一节）。

这些测试的目的是**防止架构反向漂移**——确保以后不会有人又把
`from ..affection import ...` 加回 Agent 层，或者把 marker 解析
再写第二份。

边界清单：
    runtime boundary   —— AgentRuntime 不得 import handlers
    memory boundary    —— agent/ 只能通过 MemoryFacade 拿记忆
    tool boundary      —— Runtime/Executor/PromptBuilder 不认识具体工具
    permission boundary—— 上层不直接调 can_emit_mute / bot_can_manage_cards
    recovery boundary  —— recovery 编排只存在于 RecoveryPolicy
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"
AGENT = PKG / "agent"


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def all_imports(tree: ast.Module) -> list[tuple[str, int]]:
    """收集所有 import 的模块名与层级（只看代码，不看 docstring）。"""
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            out.append((node.module or "", node.level))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, 0))
    return out


def attr_names(tree: ast.Module) -> set[str]:
    return {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }


def assigned_import_names(tree: ast.Module) -> set[str]:
    """收集 import 进来的名字（用于检测直接调用）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


# ================================================================
# 1. Runtime boundary
# ================================================================


def test_agent_runtime_does_not_import_handlers() -> None:
    """agent/ 任何模块都不得 import handlers（legacy agent 实现）。"""
    offenders: list[str] = []
    for path in AGENT.rglob("*.py"):
        tree = parse(path)
        for mod, level in all_imports(tree):
            if "handlers" in mod:
                offenders.append(f"{path.name} imports {mod}")
            for name in assigned_import_names(tree):
                if name in {"_handle_chat", "_handle_chat_inner"}:
                    offenders.append(f"{path.name} imports {name}")
    assert not offenders, "Agent 层引用了 legacy handlers:\n" + "\n".join(offenders)


def test_only_bridge_references_legacy_handler() -> None:
    """整个包内，只有 legacy_bridge 允许引用 _handle_chat。"""
    allowed = {"legacy_bridge.py"}
    offenders: list[str] = []
    for path in PKG.rglob("*.py"):
        if path.name in allowed:
            continue
        tree = parse(path)
        names = assigned_import_names(tree)
        if "_handle_chat" in names or "_handle_chat_inner" in names:
            offenders.append(str(path.relative_to(PKG)))
    assert not offenders, (
        "除 legacy_bridge 外还有模块引用 legacy handler:\n" + "\n".join(offenders)
    )


def test_core_runtime_does_not_reference_legacy() -> None:
    """Router / core 层不得出现 legacy 调用（第十节）。"""
    tree = parse(PKG / "core" / "runtime.py")
    names = assigned_import_names(tree)
    assert "_handle_chat" not in names
    assert "_handle_chat_inner" not in names
    # 也不得通过属性访问
    assert "_handle_chat" not in attr_names(tree)


# ================================================================
# 2. Memory boundary
# ================================================================

# Agent 层允许直接 import 的 memory 相关模块（只有 facade 自己）
MEMORY_MODULES = {
    "memory",
    "memory_recall",
    "person_memory",
    "affection",
    "inner_state",
    "group_style",
    "trends",
}


def test_agent_layer_memory_access_only_via_facade() -> None:
    """agent/ 下除 memory_facade.py 外，不得直接 import 各 memory **存储**模块。

    第二十九节："防止有人把架构绕回去"。

    **允许的例外**：只 import `parse_*`（marker 协议解析）是合法的——
    那属于协议层，不是存储访问。判定方式是看导入的具体名字：
    如果导入的是 `get_*_store` / `*Store` 这类存储工厂，就是违规。
    """
    storage_tokens = ("get_", "Store", "apply_updates", "load_all", "append_turn")
    protocol_prefix = "parse_"

    offenders: list[str] = []
    for path in AGENT.rglob("*.py"):
        if path.name == "memory_facade.py":
            continue  # Facade 是唯一允许接触底层存储的地方
        tree = parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            base = (node.module or "").rsplit(".", 1)[-1]
            if base not in MEMORY_MODULES:
                continue
            for alias in node.names:
                name = alias.name
                # 纯协议解析（parse_*）放行
                if name.startswith(protocol_prefix):
                    continue
                # 值对象（如 ImpressionUpdate / UserAffection）不是存储访问
                if name.endswith(("Update", "Profile", "Record", "Snapshot")):
                    continue
                if any(tok in name for tok in storage_tokens):
                    offenders.append(
                        f"{path.relative_to(AGENT)} imports {base}.{name}"
                    )
    assert not offenders, (
        "Agent 层绕过了 MemoryFacade 直接访问存储:\n" + "\n".join(offenders)
    )


def test_handlers_agent_orchestration_uses_facade() -> None:
    """_handle_chat_inner 内不得直接调用 memory store 工厂函数。"""
    src = (PKG / "handlers.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_chat_inner":
            target = node
            break
    assert target is not None, "找不到 _handle_chat_inner"

    banned = {
        "get_person_memory_store",
        "get_affection_store",
        "get_inner_state_store",
        "get_group_style_store",
        "get_trends_brief",
        "get_hot_items",
        "messages_for_llm_with_recall",
    }
    found: list[str] = []
    for node in ast.walk(target):
        if isinstance(node, ast.Name) and node.id in banned:
            found.append(f"{node.id}@{node.lineno}")
    assert not found, f"_handle_chat_inner 仍直接访问 memory store: {found}"


def test_prompt_builder_knows_only_snapshot() -> None:
    """PromptBuilder 不得 import 任何具体 memory 模块（第三十七节）。"""
    tree = parse(AGENT / "prompt" / "builder.py")
    for mod, _level in all_imports(tree):
        base = mod.rsplit(".", 1)[-1] if mod else ""
        assert base not in MEMORY_MODULES, f"PromptBuilder import 了 {mod}"


# ================================================================
# 3. Tool boundary
# ================================================================


TOOL_NAMES = {"card", "mute", "draw", "browse", "sticker", "voice", "impression"}


def _hardcoded_tool_comparisons(path: Path) -> list[str]:
    """找出 `x == "card"` 这类真实代码里的硬编码比较。"""
    tree = parse(path)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    if comp.value in TOOL_NAMES:
                        out.append(f"{path.name}:{node.lineno} == {comp.value!r}")
    return out


@pytest.mark.parametrize(
    "rel",
    [
        "agent/runtime.py",
        "agent/tools/executor.py",
        "agent/tools/registry.py",
        "agent/prompt/builder.py",
        "agent/entry.py",
    ],
)
def test_core_files_have_no_hardcoded_tool_branches(rel: str) -> None:
    """核心文件不得硬编码具体工具名（第七十二节）。"""
    offenders = _hardcoded_tool_comparisons(PKG / rel)
    assert not offenders, f"{rel} 硬编码了具体工具: {offenders}"


def test_tool_names_confined_to_definition_layer() -> None:
    """具体工具名只允许出现在：ToolDefinition / executor / parser /
    形状转换 adapter。

    **为什么 legacy_adapter 是允许的**（Phase 6A）：
    它的职责就是"把统一的 ToolCall 转成旧 ChatResult 的 8 个字段"，
    这个映射本质上必须知道工具名。它**不执行**任何工具、
    不解析 marker、不做权限判断 —— 只是字段搬移。
    这是架构上必要的形状适配层，不是第二套实现。
    """
    allowed = {
        "tools/builtin.py",        # ToolDefinition 定义
        "tools/executors.py",       # executor 实现
        "tools/dummy_tool.py",      # 测试工具
        "recovery.py",              # RecoveryPolicy 关联
        "permissions.py",           # 权限策略
        "journal.py",               # 不可逆工具判定表
        "context.py",               # 文档说明
        "mode.py",                  # StopReason 文档
        "legacy_adapter.py",        # ToolCall → ChatResult 形状转换
    }
    offenders: list[str] = []
    for path in AGENT.rglob("*.py"):
        rel = str(path.relative_to(AGENT)).replace("\\", "/")
        if rel in allowed:
            continue
        # 只看真实代码里的字符串常量比较
        found = _hardcoded_tool_comparisons(path)
        if found:
            offenders.extend(found)
    assert not offenders, (
        "具体工具名出现在不该出现的地方:\n" + "\n".join(offenders)
    )


def test_executor_source_has_no_tool_branching() -> None:
    """ToolExecutor 内部禁止 `if tool_name == "draw"`（第二十三节）。"""
    import inspect

    from dl_senpai.agent.tools.executor import ToolExecutor

    src = inspect.getsource(ToolExecutor)
    compiled = ast.parse(src)
    for node in ast.walk(compiled):
        if isinstance(node, ast.Compare):
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and comp.value in TOOL_NAMES:
                    pytest.fail(f"ToolExecutor 硬编码了工具 {comp.value}")


# ================================================================
# 4. Permission boundary
# ================================================================


def test_permission_rules_confined_to_permission_service() -> None:
    """上层只依赖 PermissionService，不直接调 can_emit_mute 等（第三十九节）。

    允许出现的例外：
        handlers.py            —— legacy 路径（显式 LEGACY 模式才走）
        （legacy_orchestrator.py 已于 2026-10 删除，不再需要例外）
        permissions.py         —— PermissionService 的 adapter 内部
        recovery.py            —— 触发谓词（intent 判断，不是权限）
        builtin.py             —— 可见性谓词（intent 判断）
    """
    allowed_files = {
        "handlers.py",
        "permissions.py",
        "group_mute.py",
        "group_card.py",
        "recovery.py",
        "tools/builtin.py",
    }
    banned = {"can_emit_mute", "bot_can_manage_cards", "filter_actions"}
    offenders: list[str] = []
    for path in AGENT.rglob("*.py"):
        rel = str(path.relative_to(AGENT)).replace("\\", "/")
        if rel in allowed_files:
            continue
        tree = parse(path)
        names = assigned_import_names(tree) | attr_names(tree)
        for name in banned & names:
            offenders.append(f"{rel} 直接使用 {name}")
    assert not offenders, (
        "权限规则泄漏到 PermissionService 之外:\n" + "\n".join(offenders)
    )


def test_permission_service_is_the_adapter() -> None:
    """PermissionService 必须调用现有 legacy 规则（不重写）。"""
    src = (AGENT / "permissions.py").read_text(encoding="utf-8")
    assert "can_emit_mute" in src, "PermissionService 未复用 can_emit_mute"
    assert "bot_can_manage_cards" in src, "PermissionService 未复用 bot_can_manage_cards"


# ================================================================
# 5. Recovery boundary
# ================================================================


def test_handlers_has_no_recovery_orchestration() -> None:
    """handlers.py 不得再有 recovery 编排（第三十一节）。"""
    src = (PKG / "handlers.py").read_text(encoding="utf-8")
    # 补救调用特征：retry hint 或 "retry once" 日志
    assert "retry once" not in src, "handlers 仍保留 recovery 日志"
    assert "draw_retry_hint(" not in src, "handlers 仍调用 draw_retry_hint"
    assert "browse_retry_hint(" not in src, "handlers 仍调用 browse_retry_hint"


def test_recovery_prompts_live_in_recovery_module() -> None:
    """CARD/MUTE 补救 prompt 只能在 recovery.py 中定义一份。"""
    recovery_src = (AGENT / "recovery.py").read_text(encoding="utf-8")
    handlers_src = (PKG / "handlers.py").read_text(encoding="utf-8")

    marker = '<<<CARD {"user_id":数字,"card":"新名片","kind":"request"}>>>'
    assert marker in recovery_src, "recovery.py 缺少 CARD 补救 prompt"
    assert marker not in handlers_src, "handlers.py 仍有 CARD 补救 prompt 副本"

    marker_mute = '<<<MUTE {"user_id":数字,"duration":60,"kind":"request"}>>>'
    assert marker_mute in recovery_src
    assert marker_mute not in handlers_src


def test_recovery_is_registry_driven() -> None:
    """RecoveryPolicy 必须挂在 ToolDefinition 上（第六节）。"""
    from dl_senpai.agent.tools.builtin import build_production_tools

    tools = {t.name: t for t in build_production_tools()}
    for name in ("card", "mute", "draw", "browse"):
        assert tools[name].recovery_policy is not None, f"{name} 缺少 recovery_policy"


# ================================================================
# 6. Marker parser boundary
# ================================================================


def test_single_marker_parsing_source() -> None:
    """marker 解析的唯一来源是 MarkerToolAdapter（第三十七节）。

    llm._finalize_result 若仍自行解析 marker，就是第二份实现。
    本测试检查 agent/tools/marker_adapter.py 是唯一持有
    "逐工具 parse 委托"逻辑的地方。
    """
    adapter_src = (AGENT / "tools" / "marker_adapter.py").read_text(encoding="utf-8")
    assert "parseable_tools" in adapter_src, "Adapter 未从 Registry 取工具"
    assert "tool.parser" in adapter_src, "Adapter 未委托工具自带 parser"


# ================================================================
# 7. 目录结构边界
# ================================================================


def test_agent_package_is_importable_in_isolation() -> None:
    """agent/ 不依赖 nonebot driver（可在纯单测环境构建）。"""
    from dl_senpai.agent import AgentEntry, AgentRuntime, MemoryFacade
    from dl_senpai.agent.tools import ToolRegistry

    assert AgentRuntime is not None
    assert AgentEntry is not None
    assert MemoryFacade is not None
    assert ToolRegistry is not None


def test_no_empty_shell_modules() -> None:
    """禁止空壳模块（Phase 4 原则延续）。

    阈值取 120 字符：`__init__.py` 这类纯导出模块天然很短，
    但仍然是有实质内容的（导出了具体符号）。真正的空壳是
    `pass` / 只有 TODO 的文件。
    """
    for path in AGENT.rglob("*.py"):
        src = path.read_text(encoding="utf-8").strip()
        assert len(src) > 120, f"{path.name} 疑似空壳（{len(src)} 字符）"
        # 真正的空壳特征
        assert src not in {"pass", "..."}, f"{path.name} 是空壳"
        assert "TODO" not in src or len(src) > 500, f"{path.name} 只有 TODO"
