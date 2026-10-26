"""轻量服务注册表：让新插件不互相 import。

Phase 3 任务 F。设计目标只有一个：**把"谁提供能力"与"谁使用能力"解耦**。

    registry.register("llm", llm_instance, owner="core")
    llm = registry.get("llm")

刻意不做的事（用户明确要求"不要引入大型 DI 框架"）：
- 不做构造函数注入 / 类型解析 / 作用域生命周期
- 不做循环依赖检测（靠 owner 约定避免）

与 EventBus 的分工：
- ServiceRegistry：**同步调用**，需要返回值，需要确定性（"给我 LLM 客户端"）
- EventBus：**异步通知**，不关心有多少订阅者，不需要返回值（"消息收到了"）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ServiceEntry:
    name: str
    value: Any
    owner: str = ""
    # 是否允许被后续注册覆盖（默认允许，便于测试替换）
    replaceable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class ServiceRegistry:
    """具名服务容器。"""

    def __init__(self) -> None:
        self._services: dict[str, ServiceEntry] = {}

    def register(
        self,
        name: str,
        value: Any,
        *,
        owner: str = "",
        replaceable: bool = True,
        **metadata: Any,
    ) -> ServiceEntry:
        """注册服务。同名且不可替换时抛 `ValueError`。"""
        existing = self._services.get(name)
        if existing is not None and not existing.replaceable:
            raise ValueError(
                f"服务 {name!r} 已由 {existing.owner!r} 注册且不可替换"
            )
        entry = ServiceEntry(
            name=name, value=value, owner=owner, replaceable=replaceable,
            metadata=dict(metadata),
        )
        self._services[name] = entry
        return entry

    def get(self, name: str, default: Any = None) -> Any:
        entry = self._services.get(name)
        return entry.value if entry is not None else default

    def get_entry(self, name: str) -> ServiceEntry | None:
        return self._services.get(name)

    def require(self, name: str) -> Any:
        """取服务；不存在时抛 `KeyError`（用于启动期硬依赖）。"""
        entry = self._services.get(name)
        if entry is None:
            raise KeyError(f"未注册的服务: {name!r}")
        return entry.value

    def has(self, name: str) -> bool:
        return name in self._services

    def unregister(self, name: str, *, owner: str | None = None) -> bool:
        existing = self._services.get(name)
        if existing is None:
            return False
        if owner is not None and existing.owner != owner:
            return False
        del self._services[name]
        return True

    def unregister_owner(self, owner: str) -> int:
        """按归属者批量注销（插件 disable 时清理它提供的服务）。"""
        if not owner:
            return 0
        names = [n for n, e in self._services.items() if e.owner == owner]
        for n in names:
            del self._services[n]
        return len(names)

    def names(self) -> list[str]:
        return sorted(self._services)

    def __contains__(self, name: object) -> bool:
        return name in self._services

    def __len__(self) -> int:
        return len(self._services)

    def clear(self) -> None:
        self._services.clear()


# 全局单例
_services = ServiceRegistry()


def get_services() -> ServiceRegistry:
    return _services


# -- 服务名常量：避免各处拼错字符串 --

SERVICE_CONFIG = "config"
SERVICE_LLM = "llm"
SERVICE_COMMANDS = "commands"
SERVICE_ROUTER = "router"
SERVICE_EVENT_BUS = "event_bus"
SERVICE_SESSIONS = "sessions"
SERVICE_REPLY = "reply"
SERVICE_METRICS = "metrics"
# Phase 4
SERVICE_TOOLS = "tools"
SERVICE_AGENT = "agent"
# 扩展登记表：声明式装载/摘除（MCP 等外部能力来源走这里）
SERVICE_EXTENSIONS = "extensions"


__all__ = [
    "ServiceRegistry",
    "ServiceEntry",
    "get_services",
    "SERVICE_CONFIG",
    "SERVICE_LLM",
    "SERVICE_COMMANDS",
    "SERVICE_ROUTER",
    "SERVICE_EVENT_BUS",
    "SERVICE_SESSIONS",
    "SERVICE_REPLY",
    "SERVICE_METRICS",
    "SERVICE_TOOLS",
    "SERVICE_AGENT",
    "SERVICE_EXTENSIONS",
]
