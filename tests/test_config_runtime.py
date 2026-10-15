"""配置基础设施测试：隔离、缓存、版本、变更事件。

对应 Phase 2 任务 A 与 Bug 1（P0）。这些测试是「单测绝不打真实网络」
这一硬约束的守门人。
"""

from __future__ import annotations

import pytest

from dl_senpai.config import PluginConfig
from dl_senpai.config_runtime import providers
from dl_senpai.config_runtime.runtime import (
    ConfigRuntime,
    get_runtime,
    reload_config,
)


# ---------------------------------------------------------------- 隔离


def test_explicit_kwargs_win_over_env_file() -> None:
    """显式参数不被 .env 覆盖 —— Bug 1 的核心断言。

    重构前：`PluginConfig(OPENAI_API_KEY="sk-test")` 会被真实 `.env` 覆盖，
    拿到生产中转 Key。这是 13 个测试失败与"单测真实计费"的根因。
    """
    cfg = PluginConfig(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="test-model",
    )
    assert cfg.openai_api_key == "sk-test"
    assert cfg.openai_base_url == "https://example.com/v1"
    assert cfg.openai_model == "test-model"
    assert cfg.active_api_key() == "sk-test"
    assert cfg.active_base_url() == "https://example.com/v1"
    assert cfg.active_model() == "test-model"


def test_test_provider_does_not_read_production_secret() -> None:
    """测试来源拿不到真实 secret。

    真实 `.env` 里的 key 形如 `sk-<64位>`，这里断言拿到的既不是它，
    也不包含真实中转域名。
    """
    cfg = PluginConfig()
    assert "airelay.buzz" not in cfg.active_base_url()
    assert cfg.active_base_url() != "https://airelay.buzz/v1"
    # 没有真实 key 时 api_configured 必须为假，而不是"意外为真"
    bare = PluginConfig(OPENAI_API_KEY="", OPENAI_BASE_URL="", OPENAI_MODEL="")
    assert bare.api_configured() is False


def test_test_provider_ignores_speak_style_file() -> None:
    """测试环境不读 speak_styles/styles.json。

    该文件把 active_id 指向本地微调端点 `127.0.0.1:8001` 并带独立 api_key，
    是 Phase 1 审计发现的**第二个**污染源。
    """
    cfg = PluginConfig(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="test-model",
    )
    assert cfg.active_speak_style_id() == "senpai_default"
    assert cfg.active_base_url() == "https://example.com/v1"
    assert cfg.active_api_key() == "sk-test"
    assert cfg.draw_api_key_resolved() == "sk-test"
    assert cfg.draw_base_url_resolved() == "https://example.com/v1"


def test_production_provider_declares_env_files() -> None:
    """生产来源必须仍然读 .env / .env.dev（不能为了测试把生产也关了）。"""
    prod = providers.ProductionConfigProvider()
    assert prod.dotenv_files() == (".env", ".env.dev")
    assert prod.read_os_environ is True


def test_test_provider_declares_no_sources() -> None:
    test_provider = providers.TestConfigProvider()
    assert test_provider.dotenv_files() == ()
    assert test_provider.read_os_environ is False


def test_provider_overrides_applied() -> None:
    runtime = ConfigRuntime(providers.TestConfigProvider({"OPENAI_MODEL": "forced"}))
    cfg = runtime.get()
    assert cfg.openai_model == "forced"


# ---------------------------------------------------------------- 缓存


def test_config_is_cached_between_calls() -> None:
    """同一个 runtime 连续两次 get() 返回同一对象（不再重复读盘）。"""
    runtime = ConfigRuntime(providers.TestConfigProvider())
    first = runtime.get()
    second = runtime.get()
    assert first is second


def test_global_get_config_is_cached() -> None:
    """模块级 get_config() 也走缓存。"""
    from dl_senpai.config import get_config

    assert get_config() is get_config()


# ---------------------------------------------------------------- 版本


def test_version_increases_on_reload() -> None:
    runtime = ConfigRuntime(providers.TestConfigProvider())
    runtime.get()
    v1 = runtime.version
    runtime.reload()
    v2 = runtime.version
    assert v2 > v1


def test_reload_rebuilds_instance() -> None:
    runtime = ConfigRuntime(providers.TestConfigProvider())
    before = runtime.get()
    after = runtime.reload()
    assert before is not after


def test_global_config_version_changes() -> None:
    from dl_senpai.config_runtime.runtime import config_version

    before = config_version()
    reload_config()
    assert config_version() > before


# ---------------------------------------------------------------- 事件


def test_config_changed_notifies_subscribers() -> None:
    runtime = ConfigRuntime(providers.TestConfigProvider())
    runtime.get()
    seen: list[tuple[int, int]] = []
    runtime.on_changed(lambda old, new: seen.append((old, new)))
    runtime.reload()
    assert len(seen) == 1
    old, new = seen[0]
    assert new > old


def test_config_changed_handler_exception_isolated() -> None:
    """一个订阅者抛异常，不能影响其他订阅者，也不能让 reload 失败。"""
    runtime = ConfigRuntime(providers.TestConfigProvider())
    runtime.get()
    good: list[int] = []

    def boom(old: int, new: int) -> None:
        raise RuntimeError("bad subscriber")

    runtime.on_changed(boom)
    runtime.on_changed(lambda old, new: good.append(new))
    runtime.reload()  # 不应抛异常
    assert len(good) == 1


def test_unsubscribe_config_changed() -> None:
    runtime = ConfigRuntime(providers.TestConfigProvider())
    runtime.get()
    seen: list[int] = []

    def cb(old: int, new: int) -> None:
        seen.append(new)

    runtime.on_changed(cb)
    assert runtime.off_changed(cb) is True
    runtime.reload()
    assert seen == []


def test_config_changed_bridges_to_event_bus() -> None:
    """config.changed 能发送到 EventBus（Phase 3 接线基础）。"""
    from dl_senpai.config_runtime.events import publish_to_bus
    from dl_senpai.core import EventBus, EventTypes

    bus = EventBus()
    received: list[tuple[int, int]] = []
    bus.subscribe(
        EventTypes.CONFIG_CHANGED,
        lambda e: received.append((e.get("old_version"), e.get("new_version"))),
    )
    delivered = publish_to_bus(bus, 1, 2)
    assert delivered == 1
    assert received == [(1, 2)]


# ---------------------------------------------------------------- 密钥来源


def test_secrets_only_from_environment_not_files() -> None:
    """密钥只能来自 environment / .env，本阶段未引入 yaml/DB/JSON 来源。

    这里断言的是"Provider 层没有偷偷加文件来源"：生产 Provider 只声明
    .env 系列文件，测试 Provider 一个文件都不读。
    """
    prod = providers.ProductionConfigProvider()
    assert all(name.endswith(".env") or ".env." in name for name in prod.dotenv_files())
    assert providers.TestConfigProvider().dotenv_files() == ()
