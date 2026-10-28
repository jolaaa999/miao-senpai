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

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .models import Command, CommandMatch, CommandPermissionDenied

# 「喵 + 分隔符」前缀：喵/指令、喵 # 指令 都会被归一化成「分隔符 + 指令」。
# 为什么允许中间有空格：手机输入法和拼音候选上屏经常多一个空格，
# 「喵 / 人设」和「喵/人设」是同一个意图，没必要让用户为了格式重打一遍。
_PREFIX_ALIAS_RE = re.compile(r"^\s*喵\s*([/／#＃!！])\s*")

# 「喵」开头的输入只允许命中管理员命令（Command.permission == PERMISSION_ADMIN）。
# 这条规则是 2026-10-28 用户定的：带喵字的是给管理员用的，
# 签到/好感度这类玩法谁都能用但**不认喵前缀**。
# 做成 registry 层的硬约束而不是各命令正则的巧合：否则哪天给签到加上
# 「/签到」的支持，「喵/签到」就会突然变成所有人可用的玩法指令。
_MAO_PREFIX_RE = re.compile(r"^\s*喵")

#: Command.permission 取这个值 = 管理员专属指令。
PERMISSION_ADMIN = "admin"

# 全角 → 半角。只映射前缀里可能用到的四个符号，别的字符原样保留。
_FULLWIDTH_PREFIX: dict[str, str] = {"／": "/", "＃": "#", "！": "!"}


def normalize_command_text(text: str) -> str:
    """把「喵/指令」归一化成「/指令」，并把全角分隔符转成半角。

    为什么在 Registry 入口做，而不是给每条命令的正则各加一遍前缀：
    中文指令的正则分散在 builtin.py / personas.py / admin.py 里，
    每处各写一遍「喵/」很快就会出现「喵/人设能用、喵/撤回不能」。
    在这里统一剥一次，等于给所有命令白送一种写法。

    注意只剥「喵 + 分隔符」，单独的「喵」保持原样：
    「喵禁言」这类无斜杠写法由各命令自己的正则决定认不认。
    """
    raw = (text or "").strip()
    m = _PREFIX_ALIAS_RE.match(raw)
    if m is not None:
        raw = m.group(1) + raw[m.end():].strip()
    if not raw:
        return raw
    # 全角分隔符统一成半角：输入法打出来的「／人设」和「/人设」是同一个意图，
    # 不统一的话每加一条命令都要把两种斜杠各测一遍。
    return _FULLWIDTH_PREFIX.get(raw[0], raw[0]) + raw[1:]


def is_admin_prefixed(text: str) -> bool:
    """这条输入是不是「喵」开头的管理员指令写法。

    判断用**原文**（归一化之前）：归一化会把喵剥掉，剥完就分不出
    「喵/禁言」（管理员专属）和「/签到」（如果将来支持，就是玩法）了。
    """
    return bool(_MAO_PREFIX_RE.match(text or ""))


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
        # 先看原文有没有「喵」前缀，再看归一化后的文本：
        # 喵开头的输入只许命中管理员指令（见 is_admin_prefixed 里的说明）。
        admin_only = is_admin_prefixed(text)
        # 「喵/指令」→「/指令」：对齐中文指令的统一格式，同时保留旧写法。
        text = normalize_command_text(text)
        for command in self._sorted():
            if not command.enabled:
                continue
            # 带喵字的输入只走管理员指令：签到/商店/好感度这类玩法不认喵前缀。
            # 带喵字的输入只走管理员指令：签到/商店/好感度这类玩法不认喵前缀。
            # 例外是 help（open_admin_prefix）：群友也要能用「喵help」查自己能做什么。
            if (
                admin_only
                and command.permission != PERMISSION_ADMIN
                and not command.open_admin_prefix
            ):
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


__all__ = [
    "CommandRegistry",
    "CommandInvocation",
    "get_registry",
    "normalize_command_text",
    "is_admin_prefixed",
    "PERMISSION_ADMIN",
]
