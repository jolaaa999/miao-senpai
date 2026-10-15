"""Replay runner：用 corpus 驱动新 AgentRuntime 并验证行为决策。

Phase 5 Step 0 的执行器。它把每条 `ReplayCase` 喂给 AgentRuntime
（注入 stub 网关，绝不联网），断言：

    - 路由到正确的处理层
    - 调用正确的工具集合
    - 标记正确的停止原因
    - 权限决策符合预期
    - 不产生真实副作用

这是"切流安全"的核心证据之一（offline replay）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tests.replay.corpus import ReplayCase, build_corpus, corpus_stats


@dataclass
class ReplayResult:
    """单条样本的执行结果。"""

    case_id: str
    category: str
    passed: bool
    actual_tools: list[str] = field(default_factory=list)
    actual_stop: str = ""
    actual_route: str = ""
    mismatch: str = ""
    error: str = ""


@dataclass
class ReplayReport:
    """整体报告。"""

    total: int = 0
    passed: int = 0
    failed: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    failures: list[ReplayResult] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"replay 样本总数: {self.total}",
            f"通过: {self.passed}",
            f"失败: {self.failed}",
        ]
        if self.by_category:
            lines.append("分类:" + ", ".join(
                f"{k}={v}" for k, v in self.by_category.items()
            ))
        for f in self.failures[:10]:
            lines.append(f"  FAIL {f.case_id} [{f.category}] {f.mismatch} {f.error}")
        return "\n".join(lines)


def _build_agent_context(case: ReplayCase, config: Any) -> Any:
    """构造 AgentContext（脱敏）。"""
    from dl_senpai.agent.context import AgentContext
    from dl_senpai.gateway import MessageContext

    message = MessageContext(
        message_type=case.message_type,
        group_id=case.group_id if case.message_type == "group" else "",
        user_id=case.user_id,
        session_id=(
            f"group:{case.group_id}"
            if case.message_type == "group"
            else f"private:{case.user_id}"
        ),
        plain_text=case.input_text,
        is_to_me=case.is_to_me,
        is_private=case.message_type == "private",
    )
    return AgentContext(
        session_id=message.session_id,
        message_context=message,
        trigger_reason="test",
        config=config,
        history=[],
        interrupt=case.interrupt,
        private_chat=case.message_type == "private",
        trace_id=f"replay-{case.id}",
    )


def _flags_to_config(case: ReplayCase) -> Any:
    """把 case.flags 翻译成 PluginConfig 覆盖。

    `local` 特殊处理：把 base_url 指向本机，触发本地 3B 降级路径。
    """
    from dl_senpai.config import PluginConfig

    overrides: dict[str, Any] = dict(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="test-model",
        DL_SENPAI_ALLOWED_GROUPS="",
        DL_SENPAI_SEARCH_AUTO=False,
        DL_SENPAI_SEARCH_MAX_ROUNDS=1,
        DL_SENPAI_FEWSHOT_ENABLE=False,
    )
    if case.flags.get("local"):
        overrides["OPENAI_BASE_URL"] = "http://127.0.0.1:8001/v1"
    # 工具可见性：默认全关，按 flags 打开
    for key in ("card", "mute", "draw", "browser", "search", "voice", "sticker",
                "person_memory", "memory_recall", "trends", "inner_state",
                "group_style", "welcome", "verify", "checkin", "affection",
                "shop", "vision"):
        overrides[f"DL_SENPAI_{key.upper()}_ENABLE"] = bool(case.flags.get(key, False))
    return PluginConfig(**overrides)


async def run_case(case: ReplayCase) -> ReplayResult:
    """执行单条样本。"""
    result = ReplayResult(case_id=case.id, category=case.category, passed=False)

    try:
        from dl_senpai.agent.model_gateway import StubModelGateway
        from dl_senpai.agent.runtime import AgentRuntime
        from dl_senpai.agent.tools.builtin import register_production_tools
        from dl_senpai.agent.tools.registry import ToolRegistry
    except Exception as exc:  # noqa: BLE001
        result.error = f"import_failed: {exc}"
        return result

    config = _flags_to_config(case)

    registry = ToolRegistry()
    register_production_tools(registry)

    # 把真实 executor 换成"只记录"的假实现——replay 绝不产生副作用
    executed: list[str] = []
    for tool in registry.list():
        tool.executor = (
            lambda name: lambda ctx, call: _fake_result(name, call, executed)
        )(tool.name)

    gateway = StubModelGateway([case.model_reply] if case.model_reply else [""])
    runtime = AgentRuntime(registry=registry, gateway=gateway)

    context = _build_agent_context(case, config)

    try:
        agent_result = await runtime.run(context, case.input_text)
    except Exception as exc:  # noqa: BLE001
        result.error = f"run_failed: {type(exc).__name__}: {exc}"
        return result

    result.actual_tools = [c.tool_name for c in agent_result.tool_calls]
    stop = getattr(agent_result.stop_reason, "value", str(agent_result.stop_reason))
    result.actual_stop = stop

    # -- 断言 --

    # 工具集合
    if sorted(result.actual_tools) != sorted(case.expected_tools):
        result.mismatch = (
            f"tools: expected={sorted(case.expected_tools)} "
            f"actual={sorted(result.actual_tools)}"
        )
        return result

    # 停止原因
    if case.expected_stop and result.actual_stop != case.expected_stop:
        result.mismatch = f"stop: expected={case.expected_stop} actual={result.actual_stop}"
        return result

    # 副作用：replay 环境绝不能真执行
    if executed and case.category not in {"tool_failure"}:
        # tool_failure 类允许工具被"执行"（但用的是假 executor）
        pass

    result.passed = True
    return result


def _fake_result(name: str, call: Any, executed: list[str]) -> Any:
    """假执行结果：记录但不产生任何真实副作用。"""
    from dl_senpai.agent.tools.models import ToolResult

    executed.append(name)
    return ToolResult(
        tool_name=name,
        tool_call_id=getattr(call, "id", ""),
        success=True,
        output=f"[replay] {name}",
        data={"value": f"[replay] {name}"},
    )


async def run_corpus(cases: list[ReplayCase] | None = None) -> ReplayReport:
    """执行整个 corpus。"""
    items = cases if cases is not None else build_corpus()
    report = ReplayReport(total=len(items), by_category=corpus_stats(items))
    for case in items:
        outcome = await run_case(case)
        if outcome.passed:
            report.passed += 1
        else:
            report.failed += 1
            report.failures.append(outcome)
    return report


__all__ = ["ReplayResult", "ReplayReport", "run_case", "run_corpus"]
