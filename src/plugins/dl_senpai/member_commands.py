"""群友指令：不带「喵」前缀，谁都能用。

为什么单独一个文件：这批指令的风险模型和管理指令完全不同——它们是
「你能对自己做的事」（改自己的名片）和「你能看的信息」（群文件、禁言名单、
自己被警告了几次）。混进 admin_commands 里，看代码的人会以为它们也要管理员身份，
以后想加一条群友指令就得先读一千行管理代码。

为什么全部**整句**匹配：「运势」「群文件」这种词日常聊天里很少单独出现，
但「算一下」「改名片」很容易成为半句话的前缀。每条正则都锚定整句，
宁可少认一种说法，也不能把正常聊天短路掉——被短路的用户永远等不到回复，
而且他不知道自己被拦了。

实现上大量复用 admin_commands_extra 里已有的 handler（_h_lots、_h_dice、
_h_calc、_h_groupinfo 等）。为什么不复制一份：同一件事有两份实现，
迟早会出现「喵groupinfo 对的、群信息 错的」这种没人能解释的差异。
"""

from __future__ import annotations

import re
from typing import Any

from .core.commands.models import Command, CommandMatch

#: 优先级与其它玩法指令一致（50）。群友指令之间互不重叠，同优先级即可。
PRIORITY_MEMBER = 50

#: 触发词 → 子命令。全部整句锚定，详见模块说明。
MEMBER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 抽签：「抽签 今天面试」带事项，不带也行
    ("lots", re.compile(r"^(?:抽签|求签|今日运势|运势)(?:\s+(?P<arg>.+))?$")),
    # 骰子：「骰子」「骰子 2d6」「roll 3d10」
    ("dice", re.compile(r"^(?:骰子|掷骰|roll)(?:\s+(?P<arg>[0-9dD\s]+))?$", re.IGNORECASE)),
    # 计算：只认纯算式。放宽成「算一下.+」会把「算一下他会来吗」当命令吞掉
    ("calc", re.compile(r"^(?:计算|算一下|算算)\s*(?P<arg>[0-9+\-*/().%\s×÷^]+)$")),
    # 看自己：不给查别人，避免变成窥探工具（要查别人找管理员）
    ("me", re.compile(r"^(?:我的信息|我是谁|我的档案|我的群资料)$")),
    ("groupinfo", re.compile(r"^(?:群信息|本群信息|群资料)$")),
    ("notices", re.compile(r"^(?:群公告|看公告|查公告)$")),
    ("files", re.compile(r"^(?:群文件|本群文件)$")),
    ("mutelist", re.compile(r"^(?:禁言名单|谁被禁言了|谁在禁言)$")),
    ("admins", re.compile(r"^(?:管理名单|本群管理|管理员名单)$")),
    ("mywarn", re.compile(r"^(?:我的警告|我的违规|警告记录)$")),
    # 改自己的名片：必须带一个空格，避免「改名片」三个字单独出现时误触
    ("card", re.compile(r"^(?:改名片|我要改名)\s+(?P<arg>.+)$")),
]


def _arg(match: CommandMatch) -> str:
    """取正则里的 arg 组（没写就是空串）。"""
    return (match.arg(0) or "").strip()


async def _handle_lots(ctx: Any, match: CommandMatch) -> None:
    from .admin_commands_extra import _h_lots

    await _h_lots(ctx, None, _arg(match))


async def _handle_dice(ctx: Any, match: CommandMatch) -> None:
    from .admin_commands_extra import _h_dice

    await _h_dice(ctx, None, _arg(match))


async def _handle_calc(ctx: Any, match: CommandMatch) -> None:
    from .admin_commands_extra import _h_calc

    await _h_calc(ctx, None, _arg(match))


async def _handle_me(ctx: Any, match: CommandMatch) -> None:
    """自己的群档案。只查自己：查别人是管理员的活。"""
    from .admin_commands import AdminArgs, _reply
    from .admin_commands_extra import _h_whois

    uid = str(getattr(ctx, "user_id", "") or "")
    if not uid.isdigit():
        await _reply(ctx, "拿不到你的 QQ 号，查不了呢。")
        return
    await _h_whois(ctx, AdminArgs(targets=(int(uid),)), "")


async def _handle_groupinfo(ctx: Any, match: CommandMatch) -> None:
    from .admin_commands_extra import _h_groupinfo

    await _h_groupinfo(ctx, None, "")


async def _handle_notices(ctx: Any, match: CommandMatch) -> None:
    from .admin_commands_extra import _h_notices

    await _h_notices(ctx, None, "")


async def _handle_files(ctx: Any, match: CommandMatch) -> None:
    from .admin_commands_extra import _h_files

    await _h_files(ctx, None, "")


async def _handle_mutelist(ctx: Any, match: CommandMatch) -> None:
    """谁在禁言里。对群友公开：知道自己还剩多久，比被闷着好。"""
    from .admin_commands_extra import _h_mutelist

    await _h_mutelist(ctx, None, "")


async def _handle_admins(ctx: Any, match: CommandMatch) -> None:
    from .admin_commands_extra import _h_admins

    await _h_admins(ctx, None, "")


async def _handle_mywarn(ctx: Any, match: CommandMatch) -> None:
    """看自己被警告了几次、为什么。

    为什么要开放给本人：警告是个账本，账本不给本人看，被禁言时就只剩吵架。
    """
    from .admin_commands import _config_of, _int_gid, _reply
    from .admin_commands_extra import WARN_LIMIT, _warn_store

    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里看呢。")
        return
    uid = str(getattr(ctx, "user_id", "") or "")
    record = _warn_store(_config_of(ctx)).get(str(gid), uid)
    count = int((record or {}).get("count") or 0)
    if not count:
        await _reply(ctx, "你身上没有警告记录。")
        return
    lines = [f"你有 {count} 次警告（满 {WARN_LIMIT} 次禁言）。"]
    reasons = (record or {}).get("reasons")
    if isinstance(reasons, list) and reasons:
        lines.append("原因：" + "；".join(str(item) for item in reasons[-3:]))
    last = (record or {}).get("last")
    if last:
        lines.append(f"最近一次：{last}")
    await _reply(ctx, "\n".join(lines))


async def _handle_card(ctx: Any, match: CommandMatch) -> None:
    """改自己的群名片。群设置关了「允许成员改名片」时会失败，要说清原因在哪。"""
    from .admin_commands import _bot_event, _call, _int_gid, _reply

    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "改名片只能在群里用呢。")
        return
    uid = str(getattr(ctx, "user_id", "") or "")
    if not uid.isdigit():
        await _reply(ctx, "拿不到你的 QQ 号，改不了呢。")
        return
    name = _arg(match)[:30]
    if not name:
        await _reply(ctx, "要写成「改名片 新名字」。")
        return
    bot, _event = _bot_event(ctx)
    ok, err = await _call(bot, "set_group_card", group_id=gid, user_id=int(uid), card=name)
    if ok:
        await _reply(ctx, f"名片改成「{name}」了。")
        return
    await _reply(ctx, f"改不动：{err}\n（群可能关了「允许成员改名片」，那就得找管理员。）")


_HANDLERS: dict[str, Any] = {
    "lots": _handle_lots,
    "dice": _handle_dice,
    "calc": _handle_calc,
    "me": _handle_me,
    "groupinfo": _handle_groupinfo,
    "notices": _handle_notices,
    "files": _handle_files,
    "mutelist": _handle_mutelist,
    "admins": _handle_admins,
    "mywarn": _handle_mywarn,
    "card": _handle_card,
}


async def handle_member_command(ctx: Any, match: CommandMatch) -> None:
    """群友指令总入口：按 match.action 分派。"""
    handler = _HANDLERS.get(match.action)
    if handler is None:  # 正则和 handler 表脱节时不要静默无反应
        from .admin_commands import _reply

        await _reply(ctx, "这条指令我还没接上，回头让主人修一下。")
        return
    await handler(ctx, match)


def build_member_commands() -> list[Command]:
    """构造群友指令（不注册）。交给 builtin 一起注册，owner 会被统一改写。"""
    return [
        Command(
            name="member",
            namespace="member",
            patterns=list(MEMBER_PATTERNS),
            handler=handle_member_command,
            description="群友指令：抽签、骰子、计算、群信息、改名片",
            priority=PRIORITY_MEMBER,
            consume=True,
            owner="builtin",
        )
    ]


__all__ = [
    "MEMBER_PATTERNS",
    "build_member_commands",
    "handle_member_command",
]
