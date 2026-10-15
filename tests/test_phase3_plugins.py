"""Phase 3：真实 Plugin 生命周期与 handlers 职责收敛验证。

与 test_phase3_registry.py 的区别：那里的插件是测试构造的假插件；
这里验证的是**真实内置能力**（签到/商店/表情采集）的 owner 归属与清理。
"""

from __future__ import annotations

import pytest

from dl_senpai.core.plugins.builtin import PLUGIN_COMMANDS, PLUGIN_OBSERVERS
from dl_senpai.core.runtime import Runtime


@pytest.fixture
def runtime() -> Runtime:
    return Runtime().build()


def test_builtin_plugins_registered(runtime: Runtime) -> None:
    ids = runtime.plugins.ids()
    assert PLUGIN_COMMANDS in ids
    assert PLUGIN_OBSERVERS in ids


def test_builtin_commands_plugin_owns_real_commands(runtime: Runtime) -> None:
    """内置命令插件必须持有**真实的** Command 对象，否则 disable 清理不到。"""
    plugin = runtime.plugins.get(PLUGIN_COMMANDS)
    assert plugin is not None
    assert len(plugin.commands) == 5
    names = {c.name for c in plugin.commands}
    assert names == {"verify", "checkin", "affection", "reset", "shop"}


def test_builtin_commands_owner_is_plugin_id(runtime: Runtime) -> None:
    for cmd in runtime.commands.list():
        assert cmd.owner == PLUGIN_COMMANDS


def test_builtin_observers_owned_by_observer_plugin(runtime: Runtime) -> None:
    for obs in runtime.router.observers():
        assert obs.owner == PLUGIN_OBSERVERS


def test_disable_builtin_commands_removes_all(runtime: Runtime) -> None:
    """真实能力验证：停用插件 → 内置命令全部消失。"""
    assert len(runtime.commands) == 5
    assert runtime.plugins.disable(PLUGIN_COMMANDS) is True
    assert len(runtime.commands) == 0


def test_disable_commands_does_not_touch_observers(runtime: Runtime) -> None:
    """停用命令插件不能影响观察者插件（owner 隔离）。

    断言写成「停用前后观察者集合不变」，而不是钉死数量：观察者是会随功能
    增加的（最近聊天流水就是新加的一个），钉死数字等于每加一个观察者都要
    回来改测试，却测不出任何真实回归。
    """
    before = {obs.name for obs in runtime.router.observers()}
    assert before, "观察者不该为空"
    runtime.plugins.disable(PLUGIN_COMMANDS)
    assert {obs.name for obs in runtime.router.observers()} == before


def test_reenable_restores_commands(runtime: Runtime) -> None:
    runtime.plugins.disable(PLUGIN_COMMANDS)
    runtime.plugins.enable(PLUGIN_COMMANDS)
    assert len(runtime.commands) == 5


def test_repeated_reload_does_not_duplicate(runtime: Runtime) -> None:
    """核心验收：reload 多次后命令数不变。"""
    for _ in range(5):
        runtime.plugins.reload(PLUGIN_COMMANDS)
    assert len(runtime.commands) == 5


def test_reload_disabled_stays_disabled(runtime: Runtime) -> None:
    runtime.plugins.disable(PLUGIN_COMMANDS)
    runtime.plugins.reload(PLUGIN_COMMANDS)
    assert len(runtime.commands) == 0


def test_build_is_idempotent() -> None:
    rt = Runtime()
    rt.build()
    first = len(rt.commands)
    rt.build()  # 再次 build 不应重复注册
    assert len(rt.commands) == first


def test_runtime_services_registered(runtime: Runtime) -> None:
    for name in ("config", "commands", "router", "event_bus", "sessions"):
        assert runtime.services.has(name), f"缺少服务 {name}"


def test_config_service_is_late_bound(runtime: Runtime) -> None:
    """config 服务注册为函数，每次取都能拿到最新配置（支持 reload）。"""
    provider = runtime.services.get("config")
    assert callable(provider)
    cfg = provider()
    assert cfg is not None


# ================================================================
# handlers.py 职责收敛验证
# ================================================================


def test_no_active_nonebot_matchers_for_business_logic() -> None:
    """验证 7 个旧 matcher 已停用：模块里不应再有活跃的 on_message 注册。

    只允许 gateway 的两个 matcher。
    """
    import dl_senpai.handlers as h

    # gateway matcher 必须存在
    assert h.gateway_message_matcher is not None
    assert h.gateway_notice_matcher is not None

    # 旧 matcher 变量应已被注释掉（不存在于模块命名空间）
    for legacy in (
        "verify_matcher",
        "checkin_matcher",
        "shop_matcher",
        "private_matcher",
        "mention_matcher",
        "interrupt_matcher",
        "sticker_collector",
        "welcome_matcher",
        "leave_matcher",
    ):
        assert not hasattr(h, legacy), f"{legacy} 仍处于激活状态"


def test_legacy_handlers_still_callable() -> None:
    """业务实现必须保留（Router 适配层依赖它们）。"""
    import dl_senpai.handlers as h

    for fn in (
        "handle_checkin",
        "handle_shop",
        "handle_group_verify",
        "handle_group_increase",
        "handle_group_decrease",
        "_handle_chat",
    ):
        assert hasattr(h, fn), f"业务函数 {fn} 丢失"


def test_dispatch_adapters_exist() -> None:
    import dl_senpai.handlers as h

    for fn in (
        "dispatch_checkin_command",
        "dispatch_shop_command",
        "dispatch_verify_command",
        "observe_stickers",
        "observe_notice_welcome",
        "observe_notice_leave",
        "guard_checkin",
        "guard_shop",
        "guard_verify",
    ):
        assert hasattr(h, fn), f"适配层函数 {fn} 缺失"


def test_handlers_no_longer_decides_routing() -> None:
    """handlers.py 不应再包含 priority 数字驱动的 matcher 注册。

    这是 Phase 3 的核心目标：handlers 不再回答"这条消息该谁处理"。
    """
    import inspect

    import dl_senpai.handlers as h

    source = inspect.getsource(h)
    # 允许注释里提到，但不应有活跃的 on_notice(priority=5, ...) 之类
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "on_notice(priority=5" not in stripped
        assert "on_message(priority=100" not in stripped
        assert "on_message(priority=50" not in stripped
        assert "on_message(priority=10" not in stripped
        assert "on_message(priority=8" not in stripped
