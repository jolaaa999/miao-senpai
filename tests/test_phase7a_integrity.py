"""Phase 7A：架构保护 + 报告 CLI 测试。

覆盖第六十八节（Phase 7 不能让架构退化）与第四十二节（报告脚本）。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"
AGENT = PKG / "agent"
HANDLERS = PKG / "handlers.py"
LEGACY = AGENT / "legacy_orchestrator.py"


def code_names(path: Path) -> set[str]:
    """真实代码里出现的名字（**不含 docstring**）。"""
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
# 第六十八节：架构不能退化
# ================================================================


def test_handlers_still_has_no_orchestration_symbols() -> None:
    """handlers.py 不得重新出现 Agent 编排符号。

    注意两点：
      1. 必须检查**真实代码**，不看 docstring ——
         注释里会提到 ToolExecutor 来说明"禁止另建 LegacyToolExecutor"。
      2. `LegacyOrchestrator` 是**允许**的 —— 第二十四节要求保留
         22 行 forwarding wrapper，它只做 argument adaptation。
         禁止的是在 handlers 里重新实现编排（LLM/Tool/Parser/Recovery）。
    """
    names = code_names(HANDLERS)
    for banned in (
        "SenpaiLLM", "ToolExecutor", "MarkerToolAdapter",
        "RecoveryPolicy", "LegacyRunState",
    ):
        assert banned not in names, f"handlers 重新出现编排符号: {banned}"

    # LegacyOrchestrator 允许，但只能在 _handle_chat_inner 里做转发
    import inspect

    import dl_senpai.handlers as handlers

    fn_src = inspect.getsource(handlers._handle_chat_inner)
    code_lines = [
        ln for ln in fn_src.splitlines()
        if ln.strip() and not ln.strip().startswith("#") and '"""' not in ln
    ]
    assert len(code_lines) < 30, "forwarder 变厚了，可能在重新实现编排"


def test_single_tool_executor() -> None:
    """Tool executor 仍只有一套。"""
    from dl_senpai.agent.tools.builtin import build_production_tools
    from dl_senpai.agent.tools.executors import REAL_EXECUTORS

    tools = {t.name: t for t in build_production_tools()}
    assert set(tools) == set(REAL_EXECUTORS)
    for name, executor in REAL_EXECUTORS.items():
        assert tools[name].executor is executor


def test_single_marker_parser() -> None:
    """marker parser 仍只有一套（没有第二份 parse_* 调用者）。"""
    parsers = {
        "parse_card_actions", "parse_mute_actions", "parse_draw_request",
        "parse_browse_request", "parse_sticker_request", "parse_voice_request",
        "parse_impression_updates", "parse_search_request",
    }
    allowed_defs = {
        "group_card.py", "group_mute.py", "image_gen.py", "browser_agent.py",
        "stickers.py", "voice.py", "person_memory.py", "web_search.py",
        "builtin.py", "marker_adapter.py", "legacy_adapter.py",
    }
    offenders: list[str] = []
    for path in PKG.rglob("*.py"):
        if path.name in allowed_defs:
            continue
        names = code_names(path)
        for p in parsers & names:
            offenders.append(f"{path.name}: {p}")
    assert not offenders, f"出现了第二套 marker 解析: {offenders}"


def test_no_new_abstractions_introduced() -> None:
    """第六十七节：不得引入 ManagerFactory / CoordinatorKernel 之类抽象。"""
    banned = (
        "ManagerFactory", "CoordinatorKernel", "PipelineEngineV2",
        "OrchestrationKernel", "ServiceLocator",
    )
    for path in PKG.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for name in banned:
            assert name not in src, f"{path.name} 引入了 {name}"


def test_validation_is_not_an_architecture_layer() -> None:
    """Validation 不得被**业务决策层**依赖（只允许单向：业务 → validation）。

    否则它就变成架构层而不是观测层了。

    Phase 7A.1 明确允许的插桩点（都是"观测上报"，不做业务决策）：
        agent/entry.py          请求级 telemetry 绑定与落盘
        llm.py                  模型调用唯一 accounting boundary
        agent/tools/executor.py 工具终态采集
        agent/memory_facade.py  Memory 命中与作用域守卫
        reply_send.py           发送边界统计
        core/session/executor.py 队列指标

    禁止的是**核心业务逻辑**（router / commands / 各 memory store /
    persona / style 等）反过来依赖 validation 做判断。
    """
    allowed = {
        "entry.py",
        "llm.py",
        "reply_send.py",
        "executor.py",       # agent/tools/executor.py 与 core/session/executor.py
        "memory_facade.py",
        "telemetry.py",
    }
    violations: list[str] = []
    for path in PKG.rglob("*.py"):
        if "validation" in path.parts:
            continue
        if path.name in allowed:
            continue
        src = path.read_text(encoding="utf-8")
        if "from ..validation" in src or "from .validation" in src:
            violations.append(str(path.relative_to(PKG)))
    assert not violations, f"非插桩层依赖了 validation: {violations}"


def test_validation_does_not_make_business_decisions() -> None:
    """Validation 层不得被用来决定业务行为。

    判定：validation/ 里不应 import 业务编排模块（agent runtime /
    legacy orchestrator / handlers），否则它就成了业务依赖。
    """
    import ast

    banned_mods = {"runtime", "legacy_orchestrator", "handlers", "entry"}
    offenders: list[str] = []
    for path in (PKG / "validation").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = (node.module or "").rsplit(".", 1)[-1]
                if base in banned_mods:
                    offenders.append(f"{path.name} imports {node.module}")
    assert not offenders, f"validation 反向依赖业务层: {offenders}"


def test_trigger_algorithm_unchanged() -> None:
    """第五十六节：trigger.py 是长期调优资产，无真实证据不得改算法。

    注意：trigger.py **确实**有未提交改动，但那是 Phase 2 的
    `BoundedTTLMap` 容量治理（`_last` / `_armed` 从无界 dict 换成有界）。
    本节禁止的是**改算法**（概率 / 关键词倍率 / 冷却语义），
    因此这里检查算法相关的行是否被动过，而不是检查文件是否 clean。
    """
    out = subprocess.run(
        ["git", "diff", "--", "src/plugins/dl_senpai/trigger.py"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    changed_lines = [
        ln for ln in out.stdout.splitlines()
        if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
    ]
    # 算法关键词：这些行不应出现在 diff 里
    algorithmic = ("interrupt_prob", "keyword", "multiplier", "prob_mult")
    offenders = [
        ln for ln in changed_lines
        if any(k in ln for k in algorithmic)
    ]
    assert not offenders, (
        "trigger.py 的算法被改动（Phase 7A 无真实数据不应调参）:\n"
        + "\n".join(offenders[:5])
    )


def test_trigger_capacity_governance_is_present() -> None:
    """Phase 2 的容量治理应保留（说明改动是治理而非算法）。"""
    src = (PKG / "trigger.py").read_text(encoding="utf-8")
    assert "BoundedTTLMap" in src, "Phase 2 的容量治理被回退了"


def test_tool_confirmation_not_enabled() -> None:
    """第五十八节：requires_confirmation 保持未启用。"""
    from dl_senpai.agent.tools.builtin import build_production_tools

    for tool in build_production_tools():
        assert getattr(tool, "requires_confirmation", False) is False


# ================================================================
# 报告 CLI（第四十二节）
# ================================================================


def run_report(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/validation_report.py", *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )


def test_report_runs_with_no_data(tmp_path) -> None:
    """没有数据时如实显示 0，不伪造示例。"""
    import os

    env = os.environ.copy()
    env["DL_SENPAI_VALIDATION_DIR"] = str(tmp_path / "empty")
    proc = subprocess.run(
        [sys.executable, "scripts/validation_report.py", "--window", "24h"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )
    assert proc.returncode == 0
    assert "Requests:" in proc.stdout
    assert "real traffic samples = 0" in proc.stdout


def test_report_window_parsing() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import validation_report as vr

    assert vr.parse_window("24h") == 86400
    assert vr.parse_window("7d") == 86400 * 7
    assert vr.parse_window("1h") == 3600
    assert vr.parse_window("30d") == 86400 * 30
    assert vr.parse_window("15m") == 900


def test_report_no_hardcoded_numbers() -> None:
    """第四十三节：报告不得硬编码示例数字。"""
    src = (ROOT / "scripts" / "validation_report.py").read_text(encoding="utf-8")
    for banned in ("1254", "99.2", "5.2s", "8.7s"):
        assert banned not in src, f"报告硬编码了示例数字: {banned}"


def test_report_renders_sample_data(tmp_path) -> None:
    """有数据时应渲染出各分节。"""
    import os

    sys.path.insert(0, str(ROOT / "src" / "plugins"))
    from dl_senpai.validation import (
        BehaviorSnapshot,
        ValidationRecorder,
        ValidationStore,
    )

    store = ValidationStore(tmp_path / "v", enabled=True)
    rec = ValidationRecorder(store)
    rec.record_request(
        trace_id="t1", user_id="10001", group_id="55555",
        execution_mode="shadow", trigger_reason="mention", user_text="hi",
        legacy=BehaviorSnapshot(tool_calls=["search"], stop_reason="finished", ok=True),
        runtime=BehaviorSnapshot(tool_calls=["search"], stop_reason="finished",
                                 ok=True, model_call_count=1, total_tokens=500),
        latency={"total": 3.0, "queue_wait": 0.5, "model": 2.0},
        memory={"history_count": 6},
        send={"send_success": 1},
        model="m1",
    )

    env = os.environ.copy()
    env["DL_SENPAI_VALIDATION_DIR"] = str(tmp_path / "v")
    proc = subprocess.run(
        [sys.executable, "scripts/validation_report.py", "--window", "24h"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )
    assert proc.returncode == 0
    out = proc.stdout
    assert "Requests:" in out
    assert "Mismatches by severity" in out
    assert "Latency (ms)" in out
    assert "Tokens" in out
    assert "Tool calls" in out
    assert "Session queue" in out
    assert "QQ send" in out
    assert "real traffic samples = 1" in out
    # 匿名 id 出现在分群里
    assert "g_" in out
    # 明文不得出现
    assert "55555" not in out
    assert "10001" not in out


def test_report_cleanup_mode(tmp_path) -> None:
    import os

    sys.path.insert(0, str(ROOT / "src" / "plugins"))
    from dl_senpai.validation import ValidationRecorder, ValidationStore

    store = ValidationStore(tmp_path / "v", enabled=True, retention_days=7)
    rec = ValidationRecorder(store)
    store.append(rec.build_record(user_text="hi", user_id="1"))

    files = list((tmp_path / "v").glob("*.jsonl"))
    import time

    old = time.time() - 86400 * 30
    os.utime(files[0], (old, old))

    env = os.environ.copy()
    env["DL_SENPAI_VALIDATION_DIR"] = str(tmp_path / "v")
    proc = subprocess.run(
        [sys.executable, "scripts/validation_report.py", "--cleanup"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )
    assert proc.returncode == 0
    assert "removed" in proc.stdout
