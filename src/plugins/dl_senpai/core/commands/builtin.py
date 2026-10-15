"""内置命令表：把现有 matcher 的正则**逐字搬进** Command Registry。

关键约束（Phase 3 第五节）：正则必须逐字复用，不"优化"、不重新解释。
因此本模块**只 import 各业务模块已有的模式常量**，不复制字符串：

    from ...checkin import _CMD_PATTERNS       # 签到
    from ...shop import _OPEN_PATTERNS, ...    # 商店
    from ...affection import _CMD_PATTERNS     # 好感度

这样即使将来业务模块调整正则，注册表也自动跟随——单一事实来源。

handler 目前是**薄封装**：调用现有的 `handle_*` 业务函数。
真正的业务实现留在原模块（Phase 3 明确不迁移业务实现）。
"""

from __future__ import annotations

from typing import Any

from ...observability.logging import log_event
from .models import Command, CommandMatch
from .registry import CommandRegistry

# 优先级：数值小者先匹配。
# 这是**语义优先级**，不是把 NoneBot 的 priority 数字照搬过来：
#   verify 是会话门禁，必须最先（它决定新人能否留在群里）
#   其余业务命令之间互不重叠，同优先级即可
PRIORITY_GATE = 10
PRIORITY_COMMAND = 50

# 命名空间（按业务域分组，支持按域批量启停）
NS_VERIFY = "verify"
NS_RESET = "reset"
NS_CHECKIN = "checkin"
NS_SHOP = "shop"
NS_AFFECTION = "affection"


def _pattern_list(pairs: Any) -> list[tuple[str, Any]]:
    """把 (action, regex) 列表规整成 Command.patterns 需要的形状。"""
    return [(str(action), pattern) for action, pattern in pairs]


def build_builtin_commands() -> list[Command]:
    """构造内置命令定义列表（不注册）。

    拆成"构造"与"注册"两步是为了让测试能拿到干净的命令定义做断言，
    而不受全局注册表当前状态影响。
    """
    from ...affection import _CMD_PATTERNS as AFFECTION_PATTERNS
    from ...checkin import _CMD_PATTERNS as CHECKIN_PATTERNS
    from ...shop import (
        _BUY_PATTERN,
        _HELP_PATTERN,
        _OPEN_PATTERNS,
        _WAREHOUSE_PATTERNS,
        _WEAR_PATTERN,
    )

    # -- 签到 / 好感度：共用一次匹配（两者正则互不重叠，但同属"卡片类"命令）--
    async def handle_checkin_command(ctx: Any, match: CommandMatch) -> None:
        from ...handlers import dispatch_checkin_command

        await dispatch_checkin_command(ctx, match)

    # -- 商店 --
    async def handle_shop_command(ctx: Any, match: CommandMatch) -> None:
        from ...handlers import dispatch_shop_command

        await dispatch_shop_command(ctx, match)

    # -- 新人认证（会话门禁）--
    async def handle_verify_command(ctx: Any, match: CommandMatch) -> None:
        from ...handlers import dispatch_verify_command

        await dispatch_verify_command(ctx, match)

    # -- 重置（清理被污染的本群上下文）--
    async def handle_reset_command(ctx: Any, match: CommandMatch) -> None:
        from ...reset_cmd import handle_reset_command as impl

        await impl(ctx, match)

    shop_patterns: list[tuple[str, Any]] = [
        *[("open", p) for p in _OPEN_PATTERNS],
        *[("warehouse", p) for p in _WAREHOUSE_PATTERNS],
        ("wear", _WEAR_PATTERN),
        ("buy", _BUY_PATTERN),
        ("help", _HELP_PATTERN),
    ]

    return [
        Command(
            name="verify",
            namespace=NS_VERIFY,
            patterns=[("verify", _VERIFY_PATTERN)],
            handler=handle_verify_command,
            description="新人进群认证（会话门禁）",
            priority=PRIORITY_GATE,
            consume=True,
            owner="builtin",
            guard=_verify_guard,
        ),
        Command(
            name="checkin",
            namespace=NS_CHECKIN,
            patterns=_pattern_list(CHECKIN_PATTERNS),
            handler=handle_checkin_command,
            description="签到 / 积分 / 排行 / 每日任务",
            priority=PRIORITY_COMMAND,
            consume=True,
            owner="builtin",
            guard=_checkin_guard,
        ),
        Command(
            name="affection",
            namespace=NS_AFFECTION,
            patterns=_pattern_list(AFFECTION_PATTERNS),
            handler=handle_checkin_command,
            description="好感度查询 / 排行 / 帮助",
            priority=PRIORITY_COMMAND,
            consume=True,
            owner="builtin",
            guard=_checkin_guard,
        ),
        Command(
            name="reset",
            namespace=NS_RESET,
            # 严格匹配整个「reset喵」：写宽了（比如只要含 reset）会把
            # 正常聊天里的英文单词也当成命令短路掉，用户永远收不到回复。
            # 参数部分只允许中文层级词或空白，其余一律走用法提示。
            patterns=[("reset", _RESET_PATTERN)],
            handler=handle_reset_command,
            description="重置本群上下文（历史 / 卡片 / 风格）",
            priority=PRIORITY_COMMAND,
            consume=True,
            owner="builtin",
        ),
        Command(
            name="shop",
            namespace=NS_SHOP,
            patterns=shop_patterns,
            handler=handle_shop_command,
            description="积分商店 / 仓库 / 购买 / 穿戴",
            priority=PRIORITY_COMMAND,
            consume=True,
            owner="builtin",
            guard=_shop_guard,
        ),
    ]


# 认证消息的语法：具体判定在 guard 里（需要 pending 状态），
# 这里只做"看起来像认证词"的正则，与 group_verify.looks_like_verify_message 等价。
import re as _re  # noqa: E402

_VERIFY_PATTERN = _re.compile(r"^(认证|验证|新人认证)$")

# 「reset喵」必须整体匹配。捕获组是可选参数（历史/卡片/风格/全部），
# 空参数等价于只清对话历史 —— 这是最常用也最轻的一档。
_RESET_PATTERN = _re.compile(
    r"^\s*reset\s*喵\s*(历史|对话|记忆|卡片|记忆卡|风格|群风格|全部|所有)?\s*$",
    _re.IGNORECASE,
)


def _verify_guard(ctx: Any) -> bool:
    from ...handlers import guard_verify

    return guard_verify(ctx)


def _checkin_guard(ctx: Any) -> bool:
    from ...handlers import guard_checkin

    return guard_checkin(ctx)


def _shop_guard(ctx: Any) -> bool:
    from ...handlers import guard_shop

    return guard_shop(ctx)


def register_builtin_commands(registry: CommandRegistry, owner: str = "builtin") -> list[Command]:
    """把内置命令注册进给定注册表；返回注册的命令列表。

    Args:
        owner: 归属者 id。默认为 `"builtin"`；`Runtime` 会传入
               `builtin_commands`，让 PluginRuntime 能按插件批量停用。
    """
    commands = build_builtin_commands()
    for cmd in commands:
        if owner:
            cmd.owner = owner
        registry.register(cmd)
    log_event(
        "command.registry_ready",
        count=len(commands),
        names=[c.name for c in commands],
        namespaces=registry.namespaces(),
        owner=owner,
    )
    return commands


__all__ = [
    "build_builtin_commands",
    "register_builtin_commands",
    "PRIORITY_GATE",
    "PRIORITY_COMMAND",
    "NS_VERIFY",
    "NS_CHECKIN",
    "NS_SHOP",
    "NS_AFFECTION",
]
