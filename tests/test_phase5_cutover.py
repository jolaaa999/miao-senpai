"""Phase 5 测试：执行模式、Fallback 安全、Journal 幂等。

这是 Phase 5 最关键的一类测试（第六十/六十五条）：
**什么情况下允许 fallback，什么情况下禁止。**
"""

from __future__ import annotations

from typing import Any

import pytest

from dl_senpai.agent.context import AgentContext, AgentResult
from dl_senpai.agent.entry import AgentEntry, AgentOutcome
from dl_senpai.agent.guard import StopReason as GuardStopReason
from dl_senpai.agent.journal import (
    IRREVERSIBLE_TOOLS,
    ExecutionJournal,
    SideEffectStatus,
    is_irreversible,
)
from dl_senpai.agent.mode import (
    AgentExecutionMode,
    FALLBACK_ALLOWED_REASONS,
    StopReason,
    canary_groups,
    mode_for_group,
    resolve_mode,
    should_fallback,
)


# ================================================================
# 执行模式
# ================================================================


def test_default_mode_is_runtime() -> None:
    """Phase 5 切流目标：默认走新路径。"""
    assert resolve_mode() is AgentExecutionMode.RUNTIME


def test_mode_from_env(monkeypatch) -> None:
    for raw, expected in (
        ("legacy", AgentExecutionMode.LEGACY),
        ("shadow", AgentExecutionMode.SHADOW),
        ("canary", AgentExecutionMode.CANARY),
        ("runtime", AgentExecutionMode.RUNTIME),
    ):
        monkeypatch.setenv("DL_SENPAI_AGENT_MODE", raw)
        assert resolve_mode() is expected


def test_mode_alias_tolerance(monkeypatch) -> None:
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "off")
    assert resolve_mode() is AgentExecutionMode.LEGACY
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "on")
    assert resolve_mode() is AgentExecutionMode.RUNTIME


def test_phase4_shadow_env_still_works(monkeypatch) -> None:
    """兼容 Phase 4 的 DL_SENPAI_AGENT_SHADOW，避免用户配置突然失效。

    注意：必须用哨兵值而不是 delenv —— 删掉环境变量会让
    `config_env.get_env` 回退到开发机的 `.env`（可能写着 runtime）。
    """
    from dl_senpai.config_env import UNSET_SENTINEL

    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", UNSET_SENTINEL)
    monkeypatch.setenv("DL_SENPAI_AGENT_SHADOW", "1")
    assert resolve_mode() is AgentExecutionMode.SHADOW


def test_canary_uses_group_allowlist_not_random(monkeypatch) -> None:
    """第七节：canary 必须按群号切，不能随机。"""
    monkeypatch.setenv("DL_SENPAI_AGENT_RUNTIME_GROUPS", "111,222")
    assert canary_groups() == {"111", "222"}
    assert mode_for_group(AgentExecutionMode.CANARY, "111") is AgentExecutionMode.RUNTIME
    assert mode_for_group(AgentExecutionMode.CANARY, "333") is AgentExecutionMode.LEGACY
    # 同一群多次解析结果稳定（可复现）
    assert mode_for_group(AgentExecutionMode.CANARY, "111") is AgentExecutionMode.RUNTIME


def test_mode_for_group_passthrough() -> None:
    """非 CANARY 模式下 group 不影响结果。"""
    for mode in (
        AgentExecutionMode.LEGACY,
        AgentExecutionMode.SHADOW,
        AgentExecutionMode.RUNTIME,
    ):
        assert mode_for_group(mode, "anything") is mode


def test_canary_empty_allowlist_sends_all_to_legacy(monkeypatch) -> None:
    monkeypatch.delenv("DL_SENPAI_AGENT_RUNTIME_GROUPS", raising=False)
    assert mode_for_group(AgentExecutionMode.CANARY, "111") is AgentExecutionMode.LEGACY


# ================================================================
# StopReason 与 fallback 判定
# ================================================================


def test_stop_reason_enum_is_fixed() -> None:
    """第四十六节：固定枚举，不用任意字符串。"""
    values = {r.value for r in StopReason}
    for expected in (
        "completed", "max_steps", "max_tool_calls", "repeat_limit",
        "timeout", "token_budget", "model_error", "tool_error",
        "permission_denied", "cancelled",
    ):
        assert expected in values, f"缺少 StopReason.{expected}"


@pytest.mark.parametrize("reason", ["model_error", "infra_error"])
def test_fallback_allowed_for_infra_failures(reason: str) -> None:
    assert should_fallback(reason, side_effect_committed=False) is True


@pytest.mark.parametrize("reason", [
    "max_steps", "max_tool_calls", "repeat_limit",
    "token_budget", "timeout", "permission_denied",
    "tool_error", "cancelled", "completed", "no_tool_call",
])
def test_fallback_forbidden_for_normal_stops(reason: str) -> None:
    """第十一/十二节：这些是护栏正常工作，不是故障。

    对它们做 fallback 等于绕过护栏再跑一遍，可能重复副作用。
    """
    assert should_fallback(reason, side_effect_committed=False) is False


def test_fallback_forbidden_after_side_effect() -> None:
    """第十三节最高风险点：已发生副作用则**任何原因**都禁止 fallback。"""
    for reason in list(FALLBACK_ALLOWED_REASONS):
        assert should_fallback(reason, side_effect_committed=True) is False


def test_fallback_allowed_set_is_minimal() -> None:
    assert FALLBACK_ALLOWED_REASONS == {"model_error", "infra_error"}


# ================================================================
# ExecutionJournal
# ================================================================


def test_journal_blocks_fallback_after_irreversible_tool() -> None:
    j = ExecutionJournal()
    assert j.fallback_allowed is True
    j.start("c1", "mute")
    assert j.side_effect_committed is True
    assert j.fallback_allowed is False


def test_journal_allows_fallback_after_readonly_tool() -> None:
    j = ExecutionJournal()
    j.start("c1", "search")
    j.complete("c1")
    assert j.side_effect_committed is False
    assert j.fallback_allowed is True


@pytest.mark.parametrize("tool", sorted(IRREVERSIBLE_TOOLS))
def test_all_irreversible_tools_block_fallback(tool: str) -> None:
    j = ExecutionJournal()
    j.start("c", tool)
    assert j.fallback_allowed is False, f"{tool} 未阻断 fallback"


@pytest.mark.parametrize("tool", ["search", "browse"])
def test_readonly_tools_do_not_block(tool: str) -> None:
    assert is_irreversible(tool) is False


def test_journal_idempotency() -> None:
    """第十五节：同一 tool_call_id 不得执行两次。"""
    j = ExecutionJournal()
    assert j.already_executed("c1") is False
    assert j.mark_executed("c1") is True
    assert j.already_executed("c1") is True
    assert j.mark_executed("c1") is False


def test_journal_start_marks_executed() -> None:
    j = ExecutionJournal()
    j.start("c1", "search")
    assert j.already_executed("c1") is True


def test_journal_failed_side_effect_still_blocks() -> None:
    """即使工具失败，只要开始执行过就要阻断 fallback。

    原因：失败可能是"请求已发出但对端未响应"，
    再跑一次 legacy 有真实重复风险。保守优先。
    """
    j = ExecutionJournal()
    j.start("c1", "mute")
    j.complete("c1", status=SideEffectStatus.FAILED)
    assert j.fallback_allowed is False


def test_journal_skipped_does_not_block() -> None:
    """权限拒绝 = 未真正执行，不阻断 fallback。"""
    j = ExecutionJournal()
    j.start("c1", "mute", side_effect=False)
    j.complete("c1", status=SideEffectStatus.SKIPPED)
    assert j.fallback_allowed is True


def test_journal_dry_run_does_not_block() -> None:
    """shadow 模式不产生副作用，不阻断。"""
    j = ExecutionJournal()
    j.start("c1", "mute")
    j.complete("c1", status=SideEffectStatus.DRY_RUN)
    # start 已经标记为不可逆并处于非终态之外的 COMPLETED 之外的 DRY_RUN
    # DRY_RUN 不在 side_effect_committed 的判定集合里
    assert j.side_effect_committed is False


def test_journal_summary() -> None:
    j = ExecutionJournal()
    j.start("c1", "search")
    j.complete("c1")
    s = j.summary()
    assert s["entries"] == 1
    assert s["fallback_allowed"] is True
    assert s["tools"] == ["search"]


def test_journal_entry_duration() -> None:
    j = ExecutionJournal()
    entry = j.start("c1", "search")
    j.complete("c1")
    assert entry.duration_ms >= 0


# ================================================================
# AgentEntry：四种模式
# ================================================================


def make_context(mode: str = "runtime", monkeypatch=None) -> AgentContext:
    from dl_senpai.gateway import MessageContext

    if monkeypatch is not None:
        monkeypatch.setenv("DL_SENPAI_AGENT_MODE", mode)
    msg = MessageContext(
        message_type="group", group_id="900001", user_id="10001",
        session_id="group:900001", plain_text="你好",
    )
    return AgentContext(
        session_id="group:900001", message_context=msg,
        config=None, trace_id="t1",
    )


class FakeRuntime:
    """可控的假 AgentRuntime。"""

    def __init__(self, result: AgentResult | None = None, error: Exception | None = None):
        self._result = result or AgentResult(text="新路径回复")
        self._error = error
        self.run_count = 0
        self.dry_run_count = 0
        self.executor = _FakeExecutorProxy()

    async def run(self, context, user_message="", *, dry_run=False, guard=None, journal=None):
        self.run_count += 1
        if dry_run:
            self.dry_run_count += 1
            return AgentResult(text="[shadow]")
        if self._error is not None:
            raise self._error
        if journal is not None:
            self.executor._journal = journal
        return self._result


class _FakeExecutorProxy:
    def __init__(self) -> None:
        self._journal = None

    def set_journal(self, journal) -> None:
        self._journal = journal


def _legacy_spy(calls: list[str], result: Any = "legacy-result"):
    async def _handler(context):
        calls.append("legacy")
        return result

    return _handler


@pytest.mark.asyncio
async def test_entry_legacy_mode_only_runs_legacy(monkeypatch) -> None:
    calls: list[str] = []
    rt = FakeRuntime()
    entry = AgentEntry(runtime=rt, legacy_handler=_legacy_spy(calls))
    ctx = make_context("legacy", monkeypatch)

    outcome = await entry.handle(ctx)
    assert calls == ["legacy"]
    assert rt.run_count == 0
    assert outcome.used_runtime is False
    assert outcome.mode == "legacy"


@pytest.mark.asyncio
async def test_entry_runtime_mode_uses_runtime(monkeypatch) -> None:
    calls: list[str] = []
    rt = FakeRuntime()
    entry = AgentEntry(runtime=rt, legacy_handler=_legacy_spy(calls))
    ctx = make_context("runtime", monkeypatch)

    outcome = await entry.handle(ctx)
    assert rt.run_count == 1
    assert calls == [], "正常路径不应调用 legacy"
    assert outcome.used_runtime is True
    assert outcome.fallback_used is False


@pytest.mark.asyncio
async def test_entry_shadow_runs_legacy_and_computes(monkeypatch) -> None:
    """SHADOW：legacy 真正执行，runtime 只算不做。"""
    calls: list[str] = []
    rt = FakeRuntime()
    entry = AgentEntry(runtime=rt, legacy_handler=_legacy_spy(calls))
    ctx = make_context("shadow", monkeypatch)

    outcome = await entry.handle(ctx)
    assert calls == ["legacy"]
    assert rt.dry_run_count == 1
    assert rt.run_count == 1
    assert outcome.mode == "shadow"


@pytest.mark.asyncio
async def test_entry_canary_routes_by_group(monkeypatch) -> None:
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "canary")
    monkeypatch.setenv("DL_SENPAI_AGENT_RUNTIME_GROUPS", "900001")
    calls: list[str] = []
    rt = FakeRuntime()
    entry = AgentEntry(runtime=rt, legacy_handler=_legacy_spy(calls))
    ctx = make_context()

    outcome = await entry.handle(ctx)
    assert rt.run_count == 1, "白名单群应走新路径"
    assert outcome.used_runtime is True


@pytest.mark.asyncio
async def test_entry_canary_non_listed_group_uses_legacy(monkeypatch) -> None:
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "canary")
    monkeypatch.setenv("DL_SENPAI_AGENT_RUNTIME_GROUPS", "888888")
    calls: list[str] = []
    rt = FakeRuntime()
    entry = AgentEntry(runtime=rt, legacy_handler=_legacy_spy(calls))
    ctx = make_context()

    outcome = await entry.handle(ctx)
    assert calls == ["legacy"]
    assert rt.run_count == 0


# ================================================================
# Fallback 安全（Phase 5.5 更新：RUNTIME 不再 per-request fallback）
# ================================================================
#
# Phase 5 的行为：runtime 失败 → 自动跑完整 legacy。
# Phase 5.5 的行为：runtime 失败 → 记录 error + 安全文案，**不跑 legacy**。
#
# 理由（第十六~二十一节）：per-request fallback 意味着永远维护两套
# prompt / parsing / recovery / memory orchestration。
# 部署级回滚改为 `DL_SENPAI_AGENT_MODE=legacy`。


@pytest.mark.asyncio
async def test_no_fallback_on_infra_failure(monkeypatch) -> None:
    """runtime 崩溃 → **不**自动跑 legacy，只标记未处理。"""
    calls: list[str] = []
    rt = FakeRuntime(error=RuntimeError("prompt builder crashed"))
    entry = AgentEntry(runtime=rt, legacy_handler=_legacy_spy(calls))
    ctx = make_context("runtime", monkeypatch)

    outcome = await entry.handle(ctx)
    assert calls == [], "RUNTIME 模式不得 per-request fallback 到 legacy"
    assert outcome.fallback_used is False
    assert outcome.handled is False
    assert outcome.stop_reason == "infra_error"
    assert entry.need_safe_fallback(outcome) is True


@pytest.mark.asyncio
async def test_no_fallback_after_mute_side_effect(monkeypatch) -> None:
    """已禁言后崩溃 → 禁止 fallback（否则禁言两次）。"""
    calls: list[str] = []

    class MuteThenCrash(FakeRuntime):
        async def run(self, context, user_message="", *, dry_run=False, guard=None, journal=None):
            self.run_count += 1
            if journal is not None:
                self.executor._journal = journal
                journal.start("c1", "mute")
                journal.complete("c1")
            raise RuntimeError("response pipeline crashed")

    rt = MuteThenCrash()
    entry = AgentEntry(runtime=rt, legacy_handler=_legacy_spy(calls))
    ctx = make_context("runtime", monkeypatch)

    outcome = await entry.handle(ctx)
    assert calls == [], "已发生副作用时绝不能 fallback"
    assert outcome.fallback_used is False
    assert outcome.handled is False


@pytest.mark.asyncio
async def test_no_fallback_after_card_side_effect(monkeypatch) -> None:
    calls: list[str] = []

    class CardThenCrash(FakeRuntime):
        async def run(self, context, user_message="", *, dry_run=False, guard=None, journal=None):
            self.run_count += 1
            if journal is not None:
                journal.start("c1", "card")
                journal.complete("c1")
            raise RuntimeError("boom")

    entry = AgentEntry(runtime=CardThenCrash(), legacy_handler=_legacy_spy(calls))
    outcome = await entry.handle(make_context("runtime", monkeypatch))
    assert calls == []
    assert outcome.fallback_used is False


@pytest.mark.asyncio
async def test_no_fallback_after_impression_write(monkeypatch) -> None:
    calls: list[str] = []

    class ImpressionThenCrash(FakeRuntime):
        async def run(self, context, user_message="", *, dry_run=False, guard=None, journal=None):
            self.run_count += 1
            if journal is not None:
                journal.start("c1", "impression")
                journal.complete("c1")
            raise RuntimeError("boom")

    entry = AgentEntry(runtime=ImpressionThenCrash(), legacy_handler=_legacy_spy(calls))
    outcome = await entry.handle(make_context("runtime", monkeypatch))
    assert calls == []
    assert outcome.fallback_used is False


@pytest.mark.asyncio
async def test_no_fallback_after_readonly_search_crash(monkeypatch) -> None:
    """Phase 5.5：即使只读工具（无副作用），RUNTIME 也不再 fallback。

    保守化是刻意的：与其维护两套 orchestration，不如返回安全文案。
    """
    calls: list[str] = []

    class SearchThenCrash(FakeRuntime):
        async def run(self, context, user_message="", *, dry_run=False, guard=None, journal=None):
            self.run_count += 1
            if journal is not None:
                journal.start("c1", "search")
                journal.complete("c1")
            raise RuntimeError("boom")

    entry = AgentEntry(runtime=SearchThenCrash(), legacy_handler=_legacy_spy(calls))
    outcome = await entry.handle(make_context("runtime", monkeypatch))
    assert calls == [], "只读工具后崩溃也不再 fallback（Phase 5.5 收敛）"
    assert outcome.fallback_used is False


@pytest.mark.asyncio
async def test_no_fallback_for_model_error_with_side_effect(monkeypatch) -> None:
    """model_error + 副作用 → 禁止 fallback。"""
    calls: list[str] = []

    class Result(FakeRuntime):
        async def run(self, context, user_message="", *, dry_run=False, guard=None, journal=None):
            self.run_count += 1
            if journal is not None:
                journal.start("c1", "mute")
                journal.complete("c1")
            r = AgentResult(text="部分回复")
            r.stop_reason = StopReason.MODEL_ERROR
            return r

    entry = AgentEntry(runtime=Result(), legacy_handler=_legacy_spy(calls))
    outcome = await entry.handle(make_context("runtime", monkeypatch))
    assert calls == []
    assert outcome.fallback_used is False


@pytest.mark.asyncio
async def test_no_fallback_when_runtime_unavailable(monkeypatch) -> None:
    """Agent 层构建失败 → 返回 error，**不**偷偷跑 legacy。"""
    calls: list[str] = []
    entry = AgentEntry(runtime=None, legacy_handler=_legacy_spy(calls))
    outcome = await entry.handle(make_context("runtime", monkeypatch))
    assert calls == [], "RUNTIME 下 runtime 不可用也不得 fallback"
    assert outcome.error == "runtime_unavailable"
    assert outcome.handled is False
    assert entry.need_safe_fallback(outcome) is True


@pytest.mark.asyncio
async def test_legacy_mode_still_works(monkeypatch) -> None:
    """LEGACY 模式是显式部署级回滚，必须继续可用（第二十节）。"""
    calls: list[str] = []
    entry = AgentEntry(runtime=FakeRuntime(), legacy_handler=_legacy_spy(calls))
    outcome = await entry.handle(make_context("legacy", monkeypatch))
    assert calls == ["legacy"]
    assert outcome.mode == "legacy"


@pytest.mark.asyncio
async def test_safe_error_text_reuses_existing_wording() -> None:
    """安全文案必须复用现有错误话术，不新写人格文案（第十八节）。"""
    text = AgentEntry.safe_error_text(None)
    assert text
    # 与 llm._friendly_api_error 的兜底分支一致
    from dl_senpai.llm import _friendly_api_error

    assert text == _friendly_api_error(Exception("x"))


@pytest.mark.asyncio
async def test_outcome_log_fields() -> None:
    o = AgentOutcome(mode="runtime", used_runtime=True, stop_reason="completed")
    fields = o.as_log_fields()
    assert fields["mode"] == "runtime"
    assert fields["used_runtime"] is True


# ================================================================
# Router 不得知道 legacy 细节（第十节）
# ================================================================


def test_router_does_not_reference_legacy_handler() -> None:
    """Router / core 层不得出现 `_handle_chat_inner` 等 legacy 细节的**调用**。

    只检查代码行，不检查注释/文档字符串——注释里提到 legacy 是合法的
    （需要解释 fallback 行为），但代码里调用它就违反了第十节。
    """
    import inspect

    from dl_senpai.core import runtime as core_runtime

    src = inspect.getsource(core_runtime)
    code_lines = [
        line for line in src.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    # 抹掉 docstring 后再检查（docstring 里说明 legacy 是允许的）
    import ast

    tree = ast.parse(inspect.getsource(core_runtime))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            doc = ast.get_docstring(node)
            if doc:
                code = code.replace(doc, "")
    assert "_handle_chat" not in code, "core 层不应调用 legacy handler"


def test_legacy_details_confined_to_entry_and_bridge() -> None:
    """legacy 调用只允许出现在 legacy_bridge（通过 AgentEntry 注入）。"""
    import inspect

    from dl_senpai import legacy_bridge

    src = inspect.getsource(legacy_bridge)
    # bridge 负责构造 legacy_handler —— 这是允许的唯一边界
    assert "_handle_chat" in src
    # 但 bridge 不得自己决定"是否 fallback"（那是 AgentEntry 的职责）
    assert "fallback_allowed" not in src
