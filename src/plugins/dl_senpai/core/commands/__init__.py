"""命令注册中心。

    from ..core.commands import Command, CommandRegistry, get_registry

命令表的**实际填充**在 `core/commands/builtin.py`——那里逐字引用
checkin.py / shop.py / affection.py 的现有正则，保证行为等价。
"""

from __future__ import annotations

from .decorators import command, register_command
from .models import Command, CommandGuard, CommandHandler, CommandMatch
from .registry import CommandInvocation, CommandRegistry, get_registry

__all__ = [
    "Command",
    "CommandMatch",
    "CommandHandler",
    "CommandGuard",
    "CommandRegistry",
    "CommandInvocation",
    "get_registry",
    "command",
    "register_command",
]
