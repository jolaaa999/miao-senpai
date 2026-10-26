"""Extension：扩展的**声明式**登记单元。

解决的问题（第一阶段的真实痛点，不是"架构漂亮"）：

    现在要加一个能力，得同时改 4 个地方——
    core/runtime.py 的 _register_builtin_observers / _register_builtin_routes
    / _register_builtin_plugins，以及 _build_agent_layer 里的一串 register_*。
    每处的 owner 字符串还要手写一致，写错就等于"清理时摘不掉"。

本模块给出一个统一的**声明口**：一个 Extension 声明自己贡献哪些
Command / Tool / Route / Observer，由 ExtensionRegistry 一次性装进
CommandRegistry / ToolRegistry / Router，并按 owner=extension.id
一次性摘除。

**刻意不做什么**（对齐"不要为了统一接口把所有扩展做成同一种对象"）：

- 不发明统一的 ExtensionHandler 基类。Command / Tool / Route / Observer
  各自保持原有形状与语义，Extension 只是"把它们列在一起的一张单子"。
- 不接管生命周期（load/enable/disable/reload 仍是 PluginRuntime 的职责）。
  Extension 只回答"装什么、装给谁"，不回答"什么时候该活着"。
- 不在本轮把既有内置能力迁移进来（那要改 owner 语义，而 owner 被
  20+ 个测试当作契约盯着）。既有注册保持原样，新扩展走这条路，
  迁移列为后续项。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..observability.logging import log_event


@dataclass
class Extension:
    """一次扩展声明。

    Attributes:
        id: 唯一标识，同时作为四类注册项的 **owner**（清理靠它）。
        name: 人类可读名称（控制台/日志用）。
        description: 一句话说明它提供什么。
        source: 来源种类（builtin / mcp / plugin / user），写进日志。
        commands: Command 对象列表（已构造，不要在这里编译正则）。
        tools: ToolDefinition 对象列表。
        routes: Route 对象列表。
        observers: Observer 对象列表。
    """

    id: str
    name: str = ""
    description: str = ""
    source: str = "builtin"
    commands: Sequence[Any] = field(default_factory=tuple)
    tools: Sequence[Any] = field(default_factory=tuple)
    routes: Sequence[Any] = field(default_factory=tuple)
    observers: Sequence[Any] = field(default_factory=tuple)

    def counts(self) -> dict[str, int]:
        return {
            "commands": len(self.commands),
            "tools": len(self.tools),
            "routes": len(self.routes),
            "observers": len(self.observers),
        }


class ExtensionRegistry:
    """扩展登记表：把 Extension 装进 Runtime，并支持按 id 整体摘除。

    构造时绑定 Runtime；但不把它放进 repr —— 否则 Runtime 的 repr
    会经由本对象递归回自己，任何一次异常打印都会 RecursionError。
    """

    def __init__(self, runtime: Any = None) -> None:
        self._runtime = runtime
        self._extensions: dict[str, Extension] = {}

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"ExtensionRegistry(ids={sorted(self._extensions)})"

    # -- 登记 --

    def apply(self, extension: Extension) -> Extension:
        """装载一个扩展（幂等：同 id 重复装载会先摘除旧的）。

        owner 只在**为空**时回填，不覆盖扩展自己声明的 owner ——
        有些工具（例如高风险层）刻意保留自己的 owner，
        强行改成扩展 id 会让既有的 unregister_owner 失效。
        """
        if not extension.id:
            raise ValueError("Extension.id 不能为空（它是清理用的 owner）")
        if extension.id in self._extensions:
            self.remove(extension.id)

        runtime = self._runtime
        commands = getattr(runtime, "commands", None)
        tools = getattr(runtime, "tools", None)
        router = getattr(runtime, "router", None)

        for command in extension.commands:
            if commands is None:
                break
            if not getattr(command, "owner", ""):
                command.owner = extension.id
            commands.register(command)

        for tool in extension.tools:
            if tools is None:
                break
            if not getattr(tool, "owner", ""):
                tool.owner = extension.id
            tools.register(tool)

        for route in extension.routes:
            if router is None:
                break
            if not getattr(route, "owner", ""):
                route.owner = extension.id
            router.register(route)

        for observer in extension.observers:
            if router is None:
                break
            # Observer 没有 register 入口，用 observe() 重建一份等价实例；
            # 这样不必为了本模块去改 Router 的公开 API。
            router.observe(
                observer.name,
                observer.handler,
                matcher=observer.matcher,
                priority=observer.priority,
                owner=getattr(observer, "owner", "") or extension.id,
            )

        self._extensions[extension.id] = extension
        log_event(
            "extension.applied",
            extension=extension.id,
            source=extension.source,
            **extension.counts(),
        )
        return extension

    def remove(self, extension_id: str) -> dict[str, int]:
        """按 id 摘除扩展贡献的全部注册项，返回各类数量。

        用各注册表自带的 unregister_owner 而不是逐个名字删除：
        owner 是唯一可靠的归属依据，逐个删会漏掉扩展后来动态注册的项。

        注意 routes 与 observers 都归 Router 管，它的 unregister_owner
        一次就把两类都摘掉，因此统计里合并成一项 router。
        """
        extension = self._extensions.pop(extension_id, None)
        removed = {"commands": 0, "tools": 0, "router": 0}
        runtime = self._runtime
        for key, holder in (
            ("commands", getattr(runtime, "commands", None)),
            ("tools", getattr(runtime, "tools", None)),
            ("router", getattr(runtime, "router", None)),
        ):
            if holder is None:
                continue
            unregister = getattr(holder, "unregister_owner", None)
            if unregister is None:
                continue
            try:
                removed[key] = int(unregister(extension_id) or 0)
                # Router 的 unregister_owner **只清 routes**，观察者另有入口
                # （unregister_observer_owner）。只调前者会留下"停用后仍在采集"
                # 的幽灵观察者，必须两个都调。
                observer_owner = getattr(holder, "unregister_observer_owner", None)
                if observer_owner is not None:
                    removed[key] += int(observer_owner(extension_id) or 0)
            except Exception as exc:  # noqa: BLE001 —— 清理失败不该炸调用方
                log_event(
                    "extension.remove_failed",
                    level="warning",
                    extension=extension_id,
                    kind=key,
                    error=type(exc).__name__,
                )
        if extension is not None:
            log_event(
                "extension.removed",
                extension=extension_id,
                source=extension.source,
                **{f"removed_{k}": v for k, v in removed.items()},
            )
        return removed

    # -- 查询 --

    def get(self, extension_id: str) -> Extension | None:
        return self._extensions.get(extension_id)

    def list(self) -> list[Extension]:
        return list(self._extensions.values())

    def ids(self) -> list[str]:
        return sorted(self._extensions)

    def describe(self) -> list[dict[str, Any]]:
        """给控制台/诊断用的扁平描述（不含任何用户数据）。"""
        return [
            {
                "id": ext.id,
                "name": ext.name,
                "source": ext.source,
                **ext.counts(),
            }
            for ext in self._extensions.values()
        ]

    def __len__(self) -> int:
        return len(self._extensions)

    def __contains__(self, extension_id: object) -> bool:
        return extension_id in self._extensions


__all__ = ["Extension", "ExtensionRegistry"]
