"""配置运行时：缓存 + 版本 + 变更事件。

职责边界（三项，不多做）：

1. **缓存**：进程内只保留一个 `PluginConfig` 实例，避免每条消息重复读盘；
2. **版本**：每次成功 reload 递增 `config_version`，让依赖方能判断"配置变了"；
3. **事件**：reload 后发布 `config.changed`，让订阅方（Phase 3 起的模块）
   有机会响应热更新。

本阶段**不**把现有模块迁移到热更新——它们仍然在下次 `get_config()` 时拿到
新配置即可（因为 `get_config()` 返回的就是新缓存）。事件机制先建好，
Phase 3 再逐步让需要的模块订阅。
"""

from __future__ import annotations

import threading
from typing import Any

from .providers import ConfigProvider, TestConfigProvider, default_provider
from .events import ConfigChangedCallback, notify_config_changed

# 延迟 import 避免 config/ ↔ config.py 的循环依赖
_CONFIG_CLS: Any = None


def _config_cls() -> Any:
    global _CONFIG_CLS
    if _CONFIG_CLS is None:
        from ..config import PluginConfig

        _CONFIG_CLS = PluginConfig
    return _CONFIG_CLS


class ConfigRuntime:
    """承载当前配置实例、版本号与变更通知。"""

    def __init__(self, provider: ConfigProvider | None = None) -> None:
        self._lock = threading.RLock()
        self._config: Any = None
        self._version: int = 0
        self._provider: ConfigProvider | None = provider
        self._subscribers: list[ConfigChangedCallback] = []

    # -- Provider --

    @property
    def provider(self) -> ConfigProvider:
        if self._provider is not None:
            return self._provider
        return default_provider()

    def set_provider(self, provider: ConfigProvider | None) -> None:
        """切换配置来源并强制重建缓存。

        测试用：`runtime.set_provider(TestConfigProvider())`。
        """
        with self._lock:
            self._provider = provider
            cls = _config_cls()
            # 让 PluginConfig 的 settings_customise_sources 看到同一个 provider
            cls.use_config_provider(provider if provider is not None else default_provider())
            self._config = None
        self.reload()

    # -- 读 --

    def get(self) -> Any:
        """取缓存的配置实例；首次调用时构建。"""
        with self._lock:
            if self._config is None:
                self._config = self._build()
            return self._config

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    # -- 重建 --

    def _build(self) -> Any:
        cls = _config_cls()
        provider = self.provider
        # 确保 PluginConfig 的 source 定制逻辑与本 runtime 的 provider 一致
        cls.use_config_provider(provider)
        config = cls(**provider.overrides)
        self._version += 1
        return config

    def reload(self) -> Any:
        """丢弃缓存并重新构建配置；返回新实例。

        版本号一定递增，即使内容没变——版本表达的是"发生过一次重载"，
        而不是"内容有差异"。订阅方如果关心内容变化，自行比较字段。
        """
        with self._lock:
            old_version = self._version
            self._config = None
            config = self.get()
            new_version = self._version
        if new_version != old_version:
            notify_config_changed(self._subscribers, old_version, new_version)
        return config

    # -- 订阅 --

    def on_changed(self, callback: ConfigChangedCallback) -> ConfigChangedCallback:
        """注册配置变更回调；返回 callback 便于取消。"""
        with self._lock:
            self._subscribers.append(callback)
        return callback

    def off_changed(self, callback: ConfigChangedCallback) -> bool:
        with self._lock:
            try:
                self._subscribers.remove(callback)
                return True
            except ValueError:
                return False

    def reset(self) -> None:
        """清空缓存与订阅，仅供测试。"""
        with self._lock:
            self._config = None
            self._version = 0
            self._subscribers.clear()


# 全局单例：现有 47 个模块通过 config.get_config() 间接使用
_runtime = ConfigRuntime()


def get_runtime() -> ConfigRuntime:
    return _runtime


_shadow_warned = False


def _warn_dotenv_shadowing() -> None:
    """两个 dotenv 里有同名键时告警一次。

    为什么值得专门写：加载顺序是 (.env, .env.dev)，后读的 .env.dev **覆盖**前面。
    于是「我在 .env 里改了这项，怎么没生效」会有一次找很久的排查——
    实际值来自 .env.dev。这一条今天就踩了两次。
    不改加载顺序（.env.dev 作为本地覆盖是合理设计），只是把它说出来。
    """
    global _shadow_warned
    if _shadow_warned:
        return
    _shadow_warned = True
    try:
        from pathlib import Path

        from ..observability.logging import log_event

        def _keys(path: Path) -> set[str]:
            if not path.is_file():
                return set()
            out: set[str] = set()
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                out.add(line.split("=", 1)[0].strip())
            return out

        base = Path(".env")
        dev = Path(".env.dev")
        shadowed = sorted(_keys(base) & _keys(dev))
        if shadowed:
            log_event(
                "config.dotenv_shadowed",
                count=len(shadowed),
                keys=",".join(shadowed[:12]),
                hint=".env.dev 覆盖 .env",
            )
    except Exception:  # noqa: BLE001 — 告警失败绝不能影响取配置
        pass


def get_config() -> Any:
    """取缓存配置（现有代码的主入口）。"""
    _warn_dotenv_shadowing()
    return _runtime.get()


def reload_config() -> Any:
    """热重载配置；返回新实例并触发 config.changed。"""
    return _runtime.reload()


def config_version() -> int:
    return _runtime.version


def set_config_provider(provider: ConfigProvider | None) -> None:
    """切换配置来源（测试隔离用）。"""
    _runtime.set_provider(provider)


def use_test_provider(**overrides: Any) -> ConfigRuntime:
    """切到测试来源的便捷入口。返回 runtime 便于继续操作。"""
    _runtime.set_provider(TestConfigProvider(overrides))
    return _runtime


__all__ = [
    "ConfigRuntime",
    "get_runtime",
    "get_config",
    "reload_config",
    "config_version",
    "set_config_provider",
    "use_test_provider",
]
