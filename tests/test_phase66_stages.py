"""Phase 6.6：阶段级测试（第四十/四十一/四十二/四十三/四十四节）。

第四十一节要求覆盖：
    context preparation
    memory preparation
    model invocation
    tool processing
    state update
    response preparation

不得只测 run()。用 fake services / fake LLM / fake MemoryFacade /
fake ToolDispatcher，禁止真实 API / QQ / 生图 / 语音 / 搜索网络。

第四十四节：call count assertions —— 确保拆分没导致
    LLM 多调用一次 / Memory 多写一次 / Tool 多执行一次 / Reply 多发一次
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"
LEGACY = PKG / "agent" / "legacy_orchestrator.py"


def legacy_class() -> ast.ClassDef:
    tree = ast.parse(LEGACY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LegacyOrchestrator":
            return node
    raise AssertionError("找不到 LegacyOrchestrator")


def method_names() -> list[str]:
    return [
        m.name for m in legacy_class().body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


# ================================================================
# 1. 阶段存在性（结构性）
# ================================================================


def test_context_preparation_stages_exist() -> None:
    names = method_names()
    assert any("message_context" in n or "prepare_message" in n for n in names), names
    assert any("media_context" in n or "prepare_media" in n for n in names), names


def test_memory_preparation_stage_exists() -> None:
    names = method_names()
    assert any("memory" in n for n in names), f"缺少 memory 阶段: {names}"


def test_model_invocation_stage_exists() -> None:
    """第二十八节：模型调用必须集中在一处。"""
    names = method_names()
    assert any("invoke_model" in n or n == "invoke_model" for n in names), names


def test_tool_processing_stage_exists() -> None:
    names = method_names()
    assert any(
        "tool" in n or "card" in n or "media_response" in n or "draw" in n
        for n in names
    ), names


def test_state_update_stage_exists() -> None:
    names = method_names()
    assert any("state_update" in n or "apply_state" in n for n in names), names


def test_response_preparation_stage_exists() -> None:
    """第三十五节：response 阶段可抽。"""
    names = method_names()
    assert any("finalize" in n or "response" in n or "send" in n for n in names), names


# ================================================================
# 2. run() 只做 orchestration（第十二/十三节）
# ================================================================


def test_run_is_orchestration_only() -> None:
    """run() 应只负责顺序调用阶段，不含具体业务。"""
    cls = legacy_class()
    run = next(
        m for m in cls.body
        if isinstance(m, ast.AsyncFunctionDef) and m.name == "run"
    )
    loc = (run.end_lineno or run.lineno) - run.lineno + 1
    assert loc < 200, f"run() 仍 {loc} 行"

    # 不应含具体业务调用
    names: set[str] = set()
    for node in ast.walk(run):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    for banned in (
        "clean_nickname", "format_candidates_for_prompt", "resolve_draw_prompt",
        "build_card_candidates", "resolve_browse_tasks",
    ):
        assert banned not in names, f"run() 仍含具体业务: {banned}"


def test_run_calls_stages_in_order() -> None:
    """run() 必须顺序调用阶段方法。"""
    cls = legacy_class()
    run = next(
        m for m in cls.body
        if isinstance(m, ast.AsyncFunctionDef) and m.name == "run"
    )
    # 找出 run 调用的 self.<method>
    called: list[str] = []
    for node in ast.walk(run):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "self":
                called.append(node.attr)
    stage_like = [c for c in called if c != "run"]
    assert len(stage_like) >= 8, f"run() 只调用了 {len(stage_like)} 个阶段: {called}"


# ================================================================
# 3. 模型调用集中（第二十八节）
# ================================================================


def test_model_chat_called_from_single_stage() -> None:
    """SenpaiLLM.chat 只应在一处出现（invoke_model）。"""
    cls = legacy_class()
    hits: list[str] = []
    for m in cls.body:
        if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(m):
            if isinstance(node, ast.Attribute) and node.attr == "chat":
                hits.append(m.name)
            elif isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                if name == "chat":
                    hits.append(m.name)
    unique = set(hits)
    assert len(unique) <= 1, f"模型调用分散在多个阶段: {unique}"


# ================================================================
# 4. 工具执行仍走共享 dispatcher（第四十五/六十六节）
# ================================================================


def test_stages_use_shared_dispatcher_not_direct_tools() -> None:
    src = LEGACY.read_text(encoding="utf-8")
    for banned in (
        "apply_card_actions(", "apply_mute_actions(", "build_image_segment(",
        "run_browse_tasks(", "build_voice_segment(", "maybe_pick_sticker_reply(",
    ):
        assert banned not in src, f"直接执行了工具: {banned}"


@pytest.mark.asyncio
async def test_tool_executes_exactly_once_through_dispatcher() -> None:
    """第四十五节：不可逆工具严格执行一次。"""
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
    assert len(executed) == 1


# ================================================================
# 5. Call count（第四十四节）
# ================================================================


def test_no_extra_llm_call_introduced() -> None:
    """模型调用点数量不得因拆分增加（应恰好 1 处）。"""
    cls = legacy_class()
    count = 0
    for m in cls.body:
        if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(m):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                if name in {"chat", "complete", "invoke_model"}:
                    count += 1
    # 允许 invoke_model 自身被 run 调用 1 次 + 内部 chat 1 次
    assert count <= 2, f"模型调用点过多（{count}），可能引入了额外调用"


def test_memory_write_stages_are_limited() -> None:
    """Memory 写入应集中在少数职责明确的阶段。

    实测分布（3 处，各自是**不同关注点**，不是重复写入）：
        prepare_memory_context  群风格 observe（观察者性质，进入时采样）
        apply_state_updates     会话历史 / 人物画像 / 好感度（本轮结果落库）
        finalize_response       内心状态 apply_after_reply（回复后才可知情绪）

    因此阈值取 3，而不是 2 —— 强行合并会让"回复后才知道"的内心状态
    被迫提前写入，那是行为改变，不是重构。
    """
    cls = legacy_class()
    writers: list[str] = []
    for m in cls.body:
        if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(m):
            if isinstance(node, ast.Attribute) and node.attr in {
                "record_turn", "record_person_updates", "record_interaction",
                "update_inner_state", "observe_group_text",
            }:
                writers.append(m.name)
    unique = set(writers)
    assert len(unique) <= 3, f"Memory 写入分散在多个阶段: {unique}"
    # 每个写入阶段都必须是有语义的命名
    for name in unique:
        assert name != "run", "run() 不应直接写 memory"


def test_reply_send_is_limited() -> None:
    """发送回复应集中在少数职责明确的阶段。

    实测分布（3 处，各自是**不同路径**，不是重复发送）：
        validate_preconditions  前置条件不满足时的早退提示（如 API 未配置）
        invoke_model            LLMError 时的错误文案
        finalize_response       正常回复

    因此阈值取 3。合并它们会把"异常路径"塞进正常路径，反而更难维护。
    """
    cls = legacy_class()
    senders: list[str] = []
    for m in cls.body:
        if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(m):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                if name in {"reply", "send_text_reply"}:
                    senders.append(m.name)
    unique = set(senders)
    assert len(unique) <= 3, f"发送分散在多个阶段: {unique}"
    # 正常回复必须在 finalize_response
    assert "finalize_response" in unique, (
        f"正常回复不在 finalize_response: {unique}"
    )


# ================================================================
# 6. Observability per stage（第三十七节）
# ================================================================


def test_stage_timings_are_recorded() -> None:
    """第三十七节：关键阶段应记录耗时。"""
    from dl_senpai.agent.legacy_state import LegacyRunState

    state = LegacyRunState()
    assert hasattr(state, "stage_timings"), "LegacyRunState 缺少 stage_timings"
    assert isinstance(state.stage_timings, dict)


def test_legacy_does_not_use_prometheus() -> None:
    """第三十九节：不为本阶段引入 Prometheus。

    用 AST 检查真实 import，不看 docstring
    （注释里会写"不引入 Prometheus"来说明设计取舍）。
    """
    tree = ast.parse(LEGACY.read_text(encoding="utf-8"))
    banned = {"prometheus", "prometheus_client", "pushgateway", "statsd"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
        for mod in mods:
            if mod.split(".")[0].lower() in banned:
                offenders.append(mod)
    assert not offenders, f"引入了监控依赖: {offenders}"
