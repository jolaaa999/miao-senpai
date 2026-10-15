"""配置基础设施：来源隔离 + 缓存 + 版本 + 变更事件。

对外入口::

    from ..config import PluginConfig, get_config      # 保持原有写法不变
    from .config import get_config, reload_config      # 新写法

包内结构：

    providers.py  —— ProductionConfigProvider / TestConfigProvider
    runtime.py    —— ConfigRuntime（缓存 / version / reload）
    events.py     —— config.changed 通知协议

注意：**密钥仍然只来自 environment / .env**，本层没有引入 yaml / DB / JSON
作为密钥来源。
"""

from __future__ import annotations

from .events import ConfigChangedCallback, notify_config_changed, publish_to_bus
from .providers import (
    ConfigProvider,
    ProductionConfigProvider,
    TestConfigProvider,
    default_provider,
    is_test_mode,
)
from .runtime import (
    ConfigRuntime,
    config_version,
    get_config,
    get_runtime,
    reload_config,
    set_config_provider,
    use_test_provider,
)

__all__ = [
    "ConfigProvider",
    "ProductionConfigProvider",
    "TestConfigProvider",
    "default_provider",
    "is_test_mode",
    "ConfigRuntime",
    "get_config",
    "get_runtime",
    "reload_config",
    "config_version",
    "set_config_provider",
    "use_test_provider",
    "ConfigChangedCallback",
    "notify_config_changed",
    "publish_to_bus",
]
