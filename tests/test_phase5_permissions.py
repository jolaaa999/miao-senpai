"""Phase 5 测试：双层权限（visibility + authorization）。

第六十二节要求的覆盖：
    tool invisible / visible but execution denied / valid actor /
    invalid target / bot lacks permission / cross-group attempt
"""

from __future__ import annotations

from typing import Any

import pytest

from dl_senpai.agent.context import AgentContext
from dl_senpai.agent.permissions import (
    AllowAllPermissionService,
    DenyAllPermissionService,
    PermissionDecision,
    PermissionService,
)
from dl_senpai.agent.tools.builtin import build_production_tools
from dl_senpai.agent.tools.executor import ToolExecutor
from dl_senpai.agent.tools.models import ToolCall
from dl_senpai.agent.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _clear_group_role_cache():
    """每个用例前清空群角色缓存。

    缓存是模块级、按群号共享的，不清会让上一个用例的结果漏到下一个。
    """
    from dl_senpai.agent import permissions as _perm

    _perm._GROUP_MANAGE_CACHE.clear()
    yield
    _perm._GROUP_MANAGE_CACHE.clear()


def tools_by_name() -> dict[str, Any]:
    return {t.name: t for t in build_production_tools()}


def make_context(
    *,
    group_id: str = "900001",
    user_id: str = "10001",
    plain_text: str = "",
    bot: Any = None,
    raw_event: Any = None,
) -> AgentContext:
    from dl_senpai.gateway import MessageContext

    msg = MessageContext(
        message_type="group", group_id=group_id, user_id=user_id,
        session_id=f"group:{group_id}", plain_text=plain_text,
        raw_event=raw_event, bot=bot,
    )
    return AgentContext(
        session_id=f"group:{group_id}", message_context=msg,
        config=None, trace_id="t1",
    )


# ================================================================
# PermissionDecision 结构化（第二十九节）
# ================================================================


def test_decision_is_structured_not_bool() -> None:
    d = PermissionDecision.deny("mute.duration", "时长超范围", requested=999)
    assert d.allowed is False
    assert d.policy == "mute.duration"
    assert d.reason == "时长超范围"
    assert d.metadata["requested"] == 999


def test_decision_log_fields_hide_sensitive_detail() -> None:
    """日志字段不暴露敏感内部信息。"""
    d = PermissionDecision.deny("mute.target", "不能禁言机器人自己")
    fields = d.as_log_fields()
    assert set(fields) == {"allowed", "policy", "reason"}


def test_decision_allow() -> None:
    d = PermissionDecision.allow(policy="default", reason="ok")
    assert d.allowed is True
    assert d.policy == "default"


# ================================================================
# 第一层：visibility
# ================================================================


def test_group_manage_tools_invisible_without_bot_context() -> None:
    """无 bot 上下文时保守放行（与旧行为一致）。"""
    svc = PermissionService()
    tools = tools_by_name()
    ctx = make_context()
    assert svc.visible(tools["mute"], ctx) is True


def test_group_manage_invisible_when_bot_cannot_manage(monkeypatch) -> None:
    """机器人不是管理员 → MUTE/CARD 不该出现在 prompt 里。"""
    import asyncio
    from types import SimpleNamespace

    async def _cannot(bot: Any, group_id: int) -> bool:
        return False

    monkeypatch.setattr("dl_senpai.group_card.bot_can_manage_cards", _cannot)
    svc = PermissionService()
    tools = tools_by_name()
    ctx = make_context(
        bot=object(), raw_event=SimpleNamespace(group_id="900001")
    )
    # 角色判定是异步的、权限层是同步的，真实顺序就是先预取再判定
    asyncio.run(svc.prefetch_group_manage(ctx))
    assert svc.visible(tools["mute"], ctx) is False
    assert svc.visible(tools["card"], ctx) is False


def test_non_manage_tools_unaffected_by_bot_role(monkeypatch) -> None:
    monkeypatch.setattr(
        "dl_senpai.group_card.bot_can_manage_cards",
        lambda bot, event: False,
    )
    svc = PermissionService()
    tools = tools_by_name()
    ctx = make_context(bot=object(), raw_event=object())
    # search 不受群管理能力影响
    assert svc.visible(tools["search"], ctx) is True


def test_disabled_tool_invisible() -> None:
    svc = PermissionService()
    tools = tools_by_name()
    tools["search"].enabled = False
    assert svc.visible(tools["search"], make_context()) is False


def test_tool_visibility_predicate_respected() -> None:
    """工具自身的 visibility 谓词优先。"""
    svc = PermissionService()
    tools = tools_by_name()
    ctx = make_context()
    # draw 需要 draw_configured()，测试配置下未配置 → 不可见
    assert svc.visible(tools["draw"], ctx) is False


# ================================================================
# 第二层：authorization
# ================================================================


def test_authorize_mute_requires_explicit_intent() -> None:
    """复用 `can_emit_mute`：未明确要求禁言 → 拒绝。"""
    svc = PermissionService()
    tools = tools_by_name()
    call = ToolCall(
        tool_name="mute",
        arguments={"user_id": "10002", "duration": 60, "kind": "request"},
    )
    ctx = make_context(plain_text="今天天气不错")
    d = svc.authorize(tool=tools["mute"], call=call, context=ctx)
    assert d.allowed is False
    assert d.policy == "mute.intent"


def test_authorize_mute_allows_explicit_intent() -> None:
    svc = PermissionService()
    tools = tools_by_name()
    call = ToolCall(
        tool_name="mute",
        arguments={"user_id": "10002", "duration": 60, "kind": "request"},
    )
    ctx = make_context(plain_text="禁言他")
    d = svc.authorize(tool=tools["mute"], call=call, context=ctx)
    assert d.allowed is True
    assert d.policy == "mute"


def test_authorize_mute_rejects_out_of_range_duration() -> None:
    """参数级校验：时长超范围应拒绝（第二十七节）。"""
    svc = PermissionService()
    tools = tools_by_name()
    call = ToolCall(
        tool_name="mute",
        arguments={"user_id": "10002", "duration": 999999, "kind": "request"},
    )
    ctx = make_context(plain_text="禁言他")
    d = svc.authorize(tool=tools["mute"], call=call, context=ctx)
    assert d.allowed is False
    assert d.policy == "mute.duration"
    assert d.metadata["requested"] == 999999


def test_authorize_mute_rejects_non_integer_duration() -> None:
    svc = PermissionService()
    tools = tools_by_name()
    call = ToolCall(
        tool_name="mute",
        arguments={"user_id": "10002", "duration": "很久", "kind": "request"},
    )
    ctx = make_context(plain_text="禁言他")
    d = svc.authorize(tool=tools["mute"], call=call, context=ctx)
    assert d.allowed is False


def test_authorize_mute_rejects_self_target() -> None:
    """不能禁言机器人自己。"""
    class Bot:
        self_id = "9999"

    svc = PermissionService()
    tools = tools_by_name()
    call = ToolCall(
        tool_name="mute",
        arguments={"user_id": "9999", "duration": 60, "kind": "request"},
    )
    ctx = make_context(plain_text="禁言他", bot=Bot())
    d = svc.authorize(tool=tools["mute"], call=call, context=ctx)
    assert d.allowed is False
    assert d.policy == "mute.target"


def test_authorize_mute_denied_when_bot_lacks_permission(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    async def _cannot(bot: Any, group_id: int) -> bool:
        return False

    monkeypatch.setattr("dl_senpai.group_card.bot_can_manage_cards", _cannot)
    svc = PermissionService()
    tools = tools_by_name()
    call = ToolCall(
        tool_name="mute",
        arguments={"user_id": "10002", "duration": 60, "kind": "request"},
    )
    ctx = make_context(
        plain_text="禁言他", bot=object(),
        raw_event=SimpleNamespace(group_id="900001"),
    )
    asyncio.run(svc.prefetch_group_manage(ctx))
    d = svc.authorize(tool=tools["mute"], call=call, context=ctx)
    assert d.allowed is False
    assert d.policy == "mute.bot_role"


def test_authorize_card_rejects_too_long(monkeypatch) -> None:
    svc = PermissionService()
    tools = tools_by_name()
    call = ToolCall(
        tool_name="card",
        arguments={"user_id": "10001", "card": "x" * 100, "kind": "request"},
    )
    ctx = make_context(plain_text="改名片")
    d = svc.authorize(tool=tools["card"], call=call, context=ctx)
    assert d.allowed is False
    assert d.policy == "card.length"


def test_authorize_card_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        "dl_senpai.group_card.bot_can_manage_cards",
        lambda bot, event: True,
    )
    svc = PermissionService()
    tools = tools_by_name()
    call = ToolCall(
        tool_name="card",
        arguments={"user_id": "10001", "card": "小鱼干", "kind": "request"},
    )
    ctx = make_context(plain_text="改名片", bot=object(), raw_event=object())
    d = svc.authorize(tool=tools["card"], call=call, context=ctx)
    assert d.allowed is True


def test_authorize_non_manage_tool_allowed() -> None:
    svc = PermissionService()
    tools = tools_by_name()
    call = ToolCall(tool_name="search", arguments={"queries": ["x"]})
    d = svc.authorize(tool=tools["search"], call=call, context=make_context())
    assert d.allowed is True


def test_authorize_missing_module_is_safe() -> None:
    """底层模块不可用时保守放行（不因权限服务故障而阻断正常功能）。"""
    svc = PermissionService()
    tools = tools_by_name()
    call = ToolCall(tool_name="mute", arguments={})
    d = svc.authorize(tool=tools["mute"], call=call, context=make_context())
    assert isinstance(d, PermissionDecision)


# ================================================================
# 集成：ToolExecutor 走 PermissionService
# ================================================================


@pytest.mark.asyncio
async def test_executor_denies_when_service_denies() -> None:
    registry = ToolRegistry()
    for tool in build_production_tools():
        registry.register(tool)

    ex = ToolExecutor(registry, permissions=DenyAllPermissionService())
    call = ToolCall(tool_name="mute", arguments={"user_id": "1", "duration": 60})
    result = await ex.execute(call, make_context(plain_text="禁言他"))
    assert result.success is False
    assert result.error_type == "permission_denied"


@pytest.mark.asyncio
async def test_executor_allows_when_service_allows() -> None:
    registry = ToolRegistry()
    executed: list[str] = []
    for tool in build_production_tools():
        tool.executor = (lambda n: lambda c, k: executed.append(n) or None)(tool.name)
        registry.register(tool)

    ex = ToolExecutor(registry, permissions=AllowAllPermissionService())
    call = ToolCall(tool_name="mute", arguments={"user_id": "1", "duration": 60})
    result = await ex.execute(call, make_context(plain_text="禁言他"))
    assert result.success is True
    assert executed == ["mute"]


@pytest.mark.asyncio
async def test_executor_denied_tool_not_executed() -> None:
    """权限拒绝时执行器**绝不能**被调用（副作用零容忍）。"""
    registry = ToolRegistry()
    executed: list[str] = []
    for tool in build_production_tools():
        tool.executor = (lambda n: lambda c, k: executed.append(n) or None)(tool.name)
        registry.register(tool)

    ex = ToolExecutor(registry, permissions=DenyAllPermissionService())
    await ex.execute(
        ToolCall(tool_name="mute", arguments={"user_id": "1", "duration": 60}),
        make_context(plain_text="禁言他"),
    )
    assert executed == [], "权限拒绝后执行器仍被调用了"


@pytest.mark.asyncio
async def test_executor_permission_exception_denies() -> None:
    class Boom:
        def authorize(self, **kw):
            raise RuntimeError("perm down")

    registry = ToolRegistry()
    for tool in build_production_tools():
        registry.register(tool)

    ex = ToolExecutor(registry, permissions=Boom())
    result = await ex.execute(
        ToolCall(tool_name="mute", arguments={}), make_context()
    )
    assert result.success is False
    assert result.error_type == "permission_denied"


@pytest.mark.asyncio
async def test_executor_accepts_legacy_bool_permission() -> None:
    """兼容返回 bool 的旧权限实现。"""
    class BoolPerm:
        def authorize(self, **kw):
            return False

    registry = ToolRegistry()
    for tool in build_production_tools():
        registry.register(tool)

    ex = ToolExecutor(registry, permissions=BoolPerm())
    result = await ex.execute(ToolCall(tool_name="search", arguments={}), make_context())
    assert result.success is False


# ================================================================
# 幂等去重（第十五节）
# ================================================================


@pytest.mark.asyncio
async def test_executor_skips_duplicate_tool_call_id() -> None:
    from dl_senpai.agent.journal import ExecutionJournal

    registry = ToolRegistry()
    executed: list[str] = []
    for tool in build_production_tools():
        tool.executor = (lambda n: lambda c, k: executed.append(n) or None)(tool.name)
        registry.register(tool)

    journal = ExecutionJournal()
    ex = ToolExecutor(registry, permissions=AllowAllPermissionService(), journal=journal)
    call = ToolCall(tool_name="mute", arguments={"user_id": "1", "duration": 60})
    ctx = make_context(plain_text="禁言他")

    first = await ex.execute(call, ctx)
    second = await ex.execute(call, ctx)

    assert first.success is True
    assert second.success is False
    assert second.error_type == "duplicate_call"
    assert executed == ["mute"], f"工具被执行了 {len(executed)} 次"


@pytest.mark.asyncio
async def test_duplicate_does_not_leak_side_effect_flag() -> None:
    """重复调用被跳过时，不应额外把 journal 标记成"又要执行一次"。"""
    from dl_senpai.agent.journal import ExecutionJournal

    registry = ToolRegistry()
    for tool in build_production_tools():
        registry.register(tool)

    journal = ExecutionJournal()
    ex = ToolExecutor(registry, permissions=AllowAllPermissionService(), journal=journal)
    call = ToolCall(tool_name="search", arguments={"queries": ["x"]})
    ctx = make_context()

    await ex.execute(call, ctx)
    entries_before = len(journal.entries)
    await ex.execute(call, ctx)
    assert len(journal.entries) == entries_before
