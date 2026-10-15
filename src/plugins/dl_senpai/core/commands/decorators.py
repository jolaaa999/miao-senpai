"""命令注册装饰器。

用法::

    @command(
        name="checkin",
        namespace="checkin",
        patterns=[("checkin", _CHECKIN_PATTERN), ...],
        priority=50,
        consume=True,
    )
    async def handle_checkin(ctx, match):
        ...

装饰器只负责构造 `Command` 并登记到注册表，不绑定任何运行时状态，
因此可以在模块导入期安全使用。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from .models import Command, CommandGuard, CommandHandler
from .registry import CommandRegistry, get_registry


def command(
    *,
    name: str,
    patterns: Sequence[Any],
    namespace: str = "default",
    description: str = "",
    priority: int = 100,
    permission: str = "",
    consume: bool = True,
    owner: str = "",
    guard: CommandGuard | None = None,
    enabled: bool = True,
    registry: CommandRegistry | None = None,
) -> Callable[[CommandHandler], CommandHandler]:
    """把一个函数登记为命令处理器。

    返回原函数（不包装），因此装饰后的名字仍然是可直接调用的协程函数，
    现有测试与调用点不受影响。
    """

    def decorator(func: CommandHandler) -> CommandHandler:
        target = registry or get_registry()
        target.register(
            Command(
                name=name,
                patterns=patterns,
                handler=func,
                namespace=namespace,
                description=description,
                priority=priority,
                permission=permission,
                consume=consume,
                owner=owner,
                guard=guard,
                enabled=enabled,
            )
        )
        # 挂一个反向引用，方便测试与诊断代码从函数找回命令定义
        try:
            setattr(func, "__dl_command__", name)  # noqa: B010
        except Exception:  # noqa: BLE001
            pass
        return func

    return decorator


def register_command(cmd: Command, registry: CommandRegistry | None = None) -> Command:
    """命令式注册（不想用装饰器时使用）。"""
    return (registry or get_registry()).register(cmd)


__all__ = ["command", "register_command"]
