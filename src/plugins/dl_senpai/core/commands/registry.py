"""Command Registry：命令的唯一注册中心。

职责：
    register / unregister / unregister_owner / match / list
    enable / disable

匹配语义（Phase 3 任务 C 第七节）：

    精确/正则命中
    ↓
    按 priority 排序（跨 namespace 全局排序）
    ↓
    执行第一个 guard 通过的命令
    ↓
    consume=True → 停止后续 stage

`unregister_owner` 是 Plugin Runtime `disable()` 能"彻底注销"的基础——
没有它，插件 reload 会导致重复注册。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .models import Command, CommandMatch


@dataclass
class CommandInvocation:
    """一次成功的命令匹配（命令 + 匹配详情）。"""

    command: Command
    match: CommandMatch

    @property
    def name(self) -> str:
        return self.command.name

    @property
    def namespace(self) -> str:
        return self.command.namespace

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "command": self.command.name,
            "namespace": self.command.namespace,
            "action": self.match.action,
            "priority": self.command.priority,
            "owner": self.command.owner or None,
        }


class CommandRegistry:
    """命令注册表。

    并发假设：注册/注销只在启动或插件 reload 时发生，匹配在消息热路径上。
    因此 `match()` 不加锁——它只读、且注册表在稳定期不再变动。
    这与项目现有的"单进程 asyncio + 同步 Store"模型一致。
    """

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        # 排序缓存：注册表变动时失效
        self._ordered: list[Command] | None = None

    # -- 注册 --

    def register(self, command: Command) -> Command:
        """注册命令；同名则替换（防 reload 重复注册）。"""
        command.compile()
        self._commands[command.name] = command
        self._ordered = None
        return command

    def unregister(self, name: str, *, owner: str | None = None) -> bool:
        """按名字注销；owner 传入时校验归属，防止插件 A 卸掉插件 B 的命令。"""
        existing = self._commands.get(name)
        if existing is None:
            return False
        if owner is not None and existing.owner != owner:
            return False
        del self._commands[name]
        self._ordered = None
        return True

    def unregister_owner(self, owner: str) -> int:
        """按归属者批量注销；返回注销数量。"""
        if not owner:
            return 0
        names = [n for n, c in self._commands.items() if c.owner == owner]
        for n in names:
            del self._commands[n]
        if names:
            self._ordered = None
        return len(names)

    # -- 启停 --

    def enable(self, name: str) -> bool:
        cmd = self._commands.get(name)
        if cmd is None:
            return False
        cmd.enabled = True
        self._ordered = None
        return True

    def disable(self, name: str) -> bool:
        cmd = self._commands.get(name)
        if cmd is None:
            return False
        cmd.enabled = False
        self._ordered = None
        return True

    def set_owner_enabled(self, owner: str, enabled: bool) -> int:
        """按归属者批量启停。"""
        count = 0
        for cmd in self._commands.values():
            if cmd.owner == owner:
                cmd.enabled = enabled
                count += 1
        if count:
            self._ordered = None
        return count

    # -- 查询 --

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def list(self, *, namespace: str | None = None) -> list[Command]:
        """按 priority 排序返回命令表（只读快照）。"""
        items = self._sorted()
        if namespace is not None:
            return [c for c in items if c.namespace == namespace]
        return list(items)

    def namespaces(self) -> list[str]:
        return sorted({c.namespace for c in self._commands.values()})

    def __len__(self) -> int:
        return len(self._commands)

    def __contains__(self, name: object) -> bool:
        return name in self._commands

    # -- 匹配 --

    def match(self, text: str, ctx: Any = None) -> CommandInvocation | None:
        """匹配命令。

        Args:
            text: 已归一化的纯文本。
            ctx: 可选上下文；传入时会执行命令的 `guard`。

        返回第一个「正则命中且 guard 通过」的命令。guard 不通过时**继续
        尝试后续命令**——这一点很重要：签到与商店的正则互不重叠，但
        「认证」等命令可能在错误群里命中同一段文本，必须能落到下一个候选。
        """
        for command in self._sorted():
            if not command.enabled:
                continue
            matched = command.match(text)
            if matched is None:
                continue
            if command.guard is not None and ctx is not None:
                try:
                    if not command.guard(ctx):
                        continue
                except Exception:  # noqa: BLE001 — guard 异常视为不通过
                    continue
            return CommandInvocation(command=command, match=matched)
        return None

    # -- 内部 --

    def _sorted(self) -> list[Command]:
        if self._ordered is None:
            # priority 小者先；同优先级按 name 保证确定性顺序
            self._ordered = sorted(
                self._commands.values(), key=lambda c: (c.priority, c.name)
            )
        return self._ordered

    def clear(self) -> None:
        self._commands.clear()
        self._ordered = None


# 全局单例：现有 matcher 与新 Router 共用同一张命令表
_registry = CommandRegistry()


def get_registry() -> CommandRegistry:
    return _registry


__all__ = ["CommandRegistry", "CommandInvocation", "get_registry"]
