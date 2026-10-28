"""管理指令扩展：群管增强 + 风控 + 运维自检。

为什么单独一个文件：admin_commands.py 已经把「解析 + 17 个 handler + 帮助文案」
塞进 900 行，再往里堆 24 个 handler 就没人敢改了。这里只放新的一批，
通过 ALIASES_EXTRA / DESCRIPTIONS_EXTRA / HANDLERS_EXTRA 合并回主表 ——
对外仍然是同一套「喵/指令」，管理员不需要知道它被拆成了几个文件。

这批指令的共同点：**大多数是「看一眼」而不是「动一下」**。
所以每个 handler 都要能在协议端不提供某个接口时给出人话解释，
而不是静静地什么都不做（管理指令最糟的失败方式是「没反应」）。
"""

from __future__ import annotations

import ast
import operator
import re
import random
import shutil
import time
from pathlib import Path
from typing import Any

from .observability.logging import log_event

# 复用主模块的解析与回复工具：这些函数已经处理了前缀、目标解析、
# 「协议端不支持」的措辞，重写一遍只会让两边的行为慢慢分叉。
from .admin_commands import (
    _bot_event,
    _call,
    _config_of,
    _fmt_duration,
    _group_id,
    _int_gid,
    _raw_text,
    _reply,
    _self_id,
    resolve_targets,
)

# 警告累计到这个次数就自动禁言。为什么是 3：翻聊天记录里管理员的习惯，
# 三次是「提醒两次、第三次动手」的常见节奏；再少容易误伤，再多等于不痛。
WARN_LIMIT = 3
# 满三次警告的自动禁言时长（2026-10-28 主人指定）：1 天 23 小时 59 分。
# 拆成 23 小时 + 59 分 + 1 天 写，比直接抄 172740 更能看出这是多久。
WARN_AUTO_MUTE_SECONDS = 23 * 3600 + 59 * 60 + 86400  # 1 天 23 小时 59 分 = 172740 秒

# 一次性能撤回多少条：Too many 撤回 = 撤到别人的消息上，协议端也扛不住。
MAX_RECALL_RECENT = 20
DEFAULT_RECALL_RECENT = 3


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


async def _api(bot: Any, api: str, **kwargs: Any) -> tuple[bool, Any, str]:
    """调一个 OneBot 接口，返回 (是否成功, 数据, 失败原因)。

    为什么不直接用 admin_commands._call：那个只返回成功/失败（够发消息用），
    这批指令要拿返回值（成员列表、公告条目、群信息），必须把数据带回来。

    接口有两种暴露方式：Bot 上的方法（get_group_member_list）和只能走
    call_api 的私有接口（_get_group_notice）。两种都试，省得每处各写一遍。
    """
    fn = getattr(bot, api, None)
    if fn is not None:
        try:
            result = fn(**kwargs)
            if hasattr(result, "__await__"):
                result = await result
            return True, result, ""
        except Exception as exc:  # noqa: BLE001 — 失败原因要给管理员看
            return False, None, _api_error(exc)
    try:
        result = await bot.call_api(api, **kwargs)
        return True, result, ""
    except Exception as exc:  # noqa: BLE001
        return False, None, _api_error(exc)


def _api_error(exc: Exception) -> str:
    """把协议端异常翻译成一句能照着做的话。"""
    name = type(exc).__name__
    text = str(exc)
    if "permission" in text.lower() or "权限" in text:
        return f"接口被拒（{name}）：我可能不是群管理员。"
    if "not found" in text.lower() or "不存在" in text:
        return f"接口说找不到对象（{name}）：群号/QQ 号是不是不对？"
    return f"接口调不通（{name}）。可能是协议端没实现这个接口。"


def _as_list(value: Any) -> list[Any]:
    """协议端返回值可能是裸列表，也可能包一层 data。"""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        inner = value.get("data")
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict):
            return inner.get("list") if isinstance(inner.get("list"), list) else []
    return []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pick(source: Any, *keys: str) -> str:
    data = _dict(source)
    for key in keys:
        value = data.get(key)
        if value is None or value == "":
            continue
        return str(value)
    return ""


def _fmt_ts(value: Any) -> str:
    """Unix 时间戳 → 人看的日期；无效值返回空串（而不是 1970 年）。"""
    try:
        stamp = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))
    except (OSError, ValueError, OverflowError):
        return ""


def _role_zh(role: str) -> str:
    return {"owner": "群主", "admin": "管理员", "member": "成员"}.get(role, role or "成员")


def _mute_until(member: Any) -> int:
    """成员被禁言到什么时候（Unix 秒）；0 = 没被禁言。

    字段名在不同协议端不一样，三个都认：NapCat 用 shut_up_timestamp，
    go-cqhttp 用 ban_expire_time。都不返回时只能当「没禁言」——
    这是个会骗人的默认值，所以「喵/禁言列表」在拿不到列表权限时会明说。
    """
    data = _dict(member)
    for key in ("shut_up_timestamp", "ban_expire_time", "mute_until", "ban_until"):
        try:
            value = int(data.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


async def _member_info(bot: Any, gid: int, uid: int) -> tuple[dict[str, Any], str]:
    ok, data, err = await _api(
        bot, "get_group_member_info", group_id=gid, user_id=uid, no_cache=True
    )
    return (_dict(data), "") if ok else ({}, err)


async def _member_list(bot: Any, gid: int) -> tuple[list[dict[str, Any]], str]:
    """拿全量成员表。失败时返回原因——「查不到」和「没人」必须能分开。"""
    ok, data, err = await _api(bot, "get_group_member_list", group_id=gid)
    if not ok:
        return [], err
    members = [item for item in _as_list(data) if isinstance(item, dict)]
    return members, ""


def _uid_of(member: dict[str, Any]) -> int:
    try:
        return int(member.get("user_id") or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 群管：看一眼
# ---------------------------------------------------------------------------


async def _h_whois(ctx: Any, args: Any, raw: str) -> None:
    """喵/查 @某人：一个人的档案（角色、进群多久、名片、是否被禁言）。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里查呢。")
        return
    targets, why = resolve_targets(ctx, args)
    if not targets:
        await _reply(ctx, why)
        return
    bot, _event = _bot_event(ctx)
    lines: list[str] = []
    for uid in targets[:5]:
        info, err = await _member_info(bot, gid, uid)
        if not info:
            lines.append(f"{uid}：查不到（{err}）")
            continue
        name = _pick(info, "card") or _pick(info, "nickname") or str(uid)
        nick = _pick(info, "nickname")
        parts = [f"{name}（{uid}）", _role_zh(_pick(info, "role"))]
        if nick and nick != name:
            parts.append(f"昵称 {nick}")
        joined = _fmt_ts(info.get("join_time"))
        if joined:
            parts.append(f"入群 {joined}")
        level = _pick(info, "level")
        if level:
            parts.append(f"等级 {level}")
        title = _pick(info, "title")
        if title:
            parts.append(f"头衔 {title}")
        until = _mute_until(info)
        if until:
            left = max(0, until - int(time.time()))
            parts.append(f"禁言中（剩 {_fmt_duration(left)}）" if left else "禁言已到期")
        lines.append(" / ".join(parts))
    await _reply(ctx, "\n".join(lines))


async def _h_members(ctx: Any, args: Any, raw: str) -> None:
    """喵/成员 [页码]：成员列表，按角色与入群时间排。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "成员列表只能在群里看呢。")
        return
    bot, _event = _bot_event(ctx)
    members, err = await _member_list(bot, gid)
    if not members:
        await _reply(ctx, f"拿不到成员列表：{err or '协议端返回了空列表'}。")
        return
    # 群主 → 管理员 → 成员，各自按入群时间从早到晚：管理员通常先看谁在里面
    order = {"owner": 0, "admin": 1, "member": 2}
    members.sort(key=lambda m: (order.get(_pick(m, "role"), 3), int(m.get("join_time") or 0)))
    page_size = 20
    page = args.duration if args.duration and args.duration > 0 else 1
    total_pages = max(1, (len(members) + page_size - 1) // page_size)
    page = min(page, total_pages)
    chunk = members[(page - 1) * page_size : page * page_size]
    lines = [f"本群 {len(members)} 人（第 {page}/{total_pages} 页）："]
    for member in chunk:
        uid = _uid_of(member)
        name = _pick(member, "card") or _pick(member, "nickname") or str(uid)
        role = _role_zh(_pick(member, "role"))
        lines.append(f"{uid} {name}" + ("" if role == "成员" else f" [{role}]"))
    await _reply(ctx, "\n".join(lines))


async def _h_admins(ctx: Any, args: Any, raw: str) -> None:
    """喵/管理：本群的管理员名单（含群主）。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里看呢。")
        return
    bot, _event = _bot_event(ctx)
    members, err = await _member_list(bot, gid)
    if not members:
        await _reply(ctx, f"拿不到成员列表：{err or '协议端返回了空列表'}。")
        return
    owners = [m for m in members if _pick(m, "role") == "owner"]
    admins = [m for m in members if _pick(m, "role") == "admin"]
    lines: list[str] = []
    for label, group in (("群主", owners), ("管理员", admins)):
        if not group:
            continue
        lines.append(f"{label}（{len(group)}）：")
        for member in group:
            uid = _uid_of(member)
            name = _pick(member, "card") or _pick(member, "nickname") or str(uid)
            lines.append(f"  {uid} {name}")
    if not lines:
        await _reply(ctx, "这个群里没有群主和管理员？（协议端没返回 role 字段）")
        return
    await _reply(ctx, "\n".join(lines))


async def _h_mutelist(ctx: Any, args: Any, raw: str) -> None:
    """喵/禁言列表：谁还在禁言里、还剩多久。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "禁言列表只能在群里看呢。")
        return
    bot, _event = _bot_event(ctx)
    members, err = await _member_list(bot, gid)
    if not members:
        await _reply(ctx, f"拿不到成员列表：{err or '协议端返回了空列表'}。")
        return
    now = int(time.time())
    muted = []
    for member in members:
        until = _mute_until(member)
        if until > now:
            muted.append((member, until))
    if not muted:
        # 「没有」和「查不了」是两件事：协议端一个禁言字段都不返回时，
        # 说「没人被禁言」就是撒谎。
        has_field = any(
            key in member
            for member in members
            for key in ("shut_up_timestamp", "ban_expire_time", "mute_until", "ban_until")
        )
        if not has_field:
            await _reply(ctx, "这个协议端不返回禁言截止时间，所以我数不出来谁在被禁言。")
            return
        await _reply(ctx, "现在没人被禁言。")
        return
    muted.sort(key=lambda pair: pair[1])
    lines = [f"正在禁言 {len(muted)} 人："]
    for member, until in muted[:30]:
        uid = _uid_of(member)
        name = _pick(member, "card") or _pick(member, "nickname") or str(uid)
        lines.append(f"  {uid} {name} 剩 {_fmt_duration(max(0, until - now))}")
    if len(muted) > 30:
        lines.append(f"（还有 {len(muted) - 30} 人没列出来）")
    await _reply(ctx, "\n".join(lines))


async def _h_groupinfo(ctx: Any, args: Any, raw: str) -> None:
    """喵/群信息：群名、人数、群主、我自己的角色。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里用呢。")
        return
    bot, _event = _bot_event(ctx)
    ok, data, err = await _api(bot, "get_group_info", group_id=gid)
    info = _dict(data) if ok else {}
    lines: list[str] = []
    if info:
        name = _pick(info, "group_name")
        if name:
            lines.append(f"群名：{name}")
        count = _pick(info, "member_count")
        cap = _pick(info, "max_member_count")
        if count or cap:
            lines.append(f"人数：{count or '?'}" + (f" / {cap}" if cap else ""))
        created = _fmt_ts(info.get("group_create_time"))
        if created:
            lines.append(f"建群：{created}")
        level = _pick(info, "group_level")
        if level:
            lines.append(f"群等级：{level}")
    else:
        lines.append(f"群信息拿不到：{err}")
    me = _self_id(ctx)
    if me.isdigit():
        mine, _err2 = await _member_info(bot, gid, int(me))
        if mine:
            lines.append(f"我在群里的身份：{_role_zh(_pick(mine, 'role'))}")
    await _reply(ctx, "\n".join(lines) or "什么都没查到。")


async def _h_notices(ctx: Any, args: Any, raw: str) -> None:
    """喵/公告列表：群公告（复用群信息工具，不重复实现渲染）。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里用呢。")
        return
    bot, _event = _bot_event(ctx)
    from .group_info import fetch_notices, render_notices

    notices = await fetch_notices(bot, gid)
    if not notices:
        await _reply(ctx, "这个群还没有公告（或者协议端不给我看公告）。")
        return
    text = render_notices(notices)
    # render_notices 是给模型看的（可能是几千字），群里发要截断
    await _reply(ctx, text[:1200] + ("\n…（太长，剩下的我没念完）" if len(text) > 1200 else ""))


async def _h_notice_del(ctx: Any, args: Any, raw: str) -> None:
    """喵/撤回公告 [序号]：删掉群公告。不给序号默认删最新的那条。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里用呢。")
        return
    bot, _event = _bot_event(ctx)
    from .group_info import fetch_notices

    notices = await fetch_notices(bot, gid)
    if not notices:
        await _reply(ctx, "没有公告可撤。")
        return
    index = args.duration if args.duration and args.duration > 0 else 1
    if index > len(notices):
        await _reply(ctx, f"只有 {len(notices)} 条公告，没有第 {index} 条。")
        return
    target = _dict(notices[index - 1])
    notice_id = _pick(target, "notice_id", "id", "fid")
    if not notice_id:
        await _reply(ctx, "这条公告没带 id，协议端不让我删它。")
        return
    ok, _data, err = await _api(
        bot, "_delete_group_notice", group_id=str(gid), notice_id=notice_id
    )
    if not ok:
        await _reply(ctx, f"没删掉：{err}")
        return
    log_event("admin_command.notice_deleted", session_id=_group_id(ctx), actor=_self_id(ctx))
    await _reply(ctx, f"第 {index} 条公告撤掉了。")


async def _h_files(ctx: Any, args: Any, raw: str) -> None:
    """喵/文件：群文件列表。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里用呢。")
        return
    bot, _event = _bot_event(ctx)
    from .group_info import fetch_files, render_files

    payload = await fetch_files(bot, gid)
    if not payload:
        await _reply(ctx, "拿不到群文件列表。")
        return
    text = render_files(payload)
    if not text:
        await _reply(ctx, "群文件是空的。")
        return
    await _reply(ctx, text[:1200] + ("\n…（太长，剩下的我没念完）" if len(text) > 1200 else ""))


async def _h_essence(ctx: Any, args: Any, raw: str) -> None:
    """喵/精华：群精华消息列表。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里用呢。")
        return
    bot, _event = _bot_event(ctx)
    from .group_info import fetch_essence, render_essence

    items = await fetch_essence(bot, gid)
    if not items:
        await _reply(ctx, "这个群还没有精华消息（或者协议端不给我看）。")
        return
    text = render_essence(items)
    await _reply(ctx, text[:1200] + ("\n…（太长，剩下的我没念完）" if len(text) > 1200 else ""))


async def _h_albums(ctx: Any, args: Any, raw: str) -> None:
    """喵/相册：群相册列表。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里用呢。")
        return
    bot, _event = _bot_event(ctx)
    from .group_info import fetch_albums, render_albums

    albums = await fetch_albums(bot, gid)
    if not albums:
        await _reply(ctx, "这个群还没有相册（或者协议端不给我看）。")
        return
    text = render_albums(albums)
    await _reply(ctx, text[:1200] + ("\n…（太长，剩下的我没念完）" if len(text) > 1200 else ""))


async def _h_recall_recent(ctx: Any, args: Any, raw: str) -> None:
    """喵/撤回最近 [N]：把她自己最近说的 N 条撤掉（默认 3 条）。

    为什么单独一条指令而不是让「喵/撤回 5」智能判断：撤回 QQ 号和撤回条数
    都是数字，混在一起迟早撤错东西。想撤单条用「喵/撤回」（引用那条）。
    """
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里用呢。")
        return
    count = args.duration if args.duration and args.duration > 0 else DEFAULT_RECALL_RECENT
    count = max(1, min(MAX_RECALL_RECENT, int(count)))
    bot, _event = _bot_event(ctx)
    me = _self_id(ctx)
    # 多取一些历史：中间夹着别人的消息，只取 count 条很可能一条自己的都没有
    ok, data, err = await _api(bot, "get_group_msg_history", group_id=gid, count=count * 4)
    if not ok:
        await _reply(ctx, f"拿不到聊天记录：{err}")
        return
    mine = [
        _pick(item, "message_id")
        for item in _as_list(data)
        if _pick(_dict(item).get("sender"), "user_id") == me and _pick(item, "message_id")
    ]
    mine = mine[-count:]
    if not mine:
        await _reply(ctx, "最近这些消息里没有我说的，没得撤。")
        return
    done = 0
    for message_id in mine:
        ok_del, _d, _e = await _api(
            bot, "delete_msg", message_id=int(message_id) if message_id.isdigit() else message_id
        )
        done += 1 if ok_del else 0
    await _reply(ctx, f"撤掉了自己最近 {done} 条消息。" if done else "一条都没撤掉（可能超过撤回时限）。")


async def _h_leave(ctx: Any, args: Any, raw: str) -> None:
    """喵/退群 确认：让她退出本群。

    为什么强制二次确认：这是唯一一条**把自己从群里删掉**的指令，
    手滑发出去就再也回不来了（群里连"撤回"都救不了）。
    """
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "私聊里没有「退群」这回事呢。")
        return
    if "确认" not in _raw_text(raw):
        await _reply(ctx, "真的要让我退群吗？这一步回不来。确认的话发「喵/退群 确认」。")
        return
    bot, _event = _bot_event(ctx)
    ok, _data, err = await _api(bot, "set_group_leave", group_id=gid, is_dismiss=False)
    if not ok:
        await _reply(ctx, f"没退成：{err}")
        return
    log_event(
        "admin_command.leave_group", level="warning", session_id=_group_id(ctx), actor=_self_id(ctx)
    )


# ---------------------------------------------------------------------------
# 风控：警告与黑名单
# ---------------------------------------------------------------------------


def _warn_store(cfg: Any = None) -> Any:
    from .admin_control import get_warn_store

    path = str(getattr(cfg, "admin_warn_path", "") or "").strip() if cfg is not None else ""
    return get_warn_store(path or None)


def _blacklist_store(cfg: Any = None) -> Any:
    from .admin_control import get_blacklist_store

    path = str(getattr(cfg, "admin_blacklist_path", "") or "").strip() if cfg is not None else ""
    return get_blacklist_store(path or None)


async def _h_warn(ctx: Any, args: Any, raw: str) -> None:
    """喵/警告 @某人 [原因]：记一次警告，满 WARN_LIMIT 次自动禁言。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "警告只能在群里记呢。")
        return
    targets, why = resolve_targets(ctx, args)
    if not targets:
        await _reply(ctx, why)
        return
    reason = _raw_text(raw).strip()
    cfg = _config_of(ctx)
    store = _warn_store(cfg)
    bot, _event = _bot_event(ctx)
    lines: list[str] = []
    for uid in targets[:5]:
        count = store.add(gid, uid, reason=reason, by=_self_id(ctx))
        if count >= WARN_LIMIT:
            ok, err = await _call(
                bot, "set_group_ban", group_id=gid, user_id=uid, duration=WARN_AUTO_MUTE_SECONDS
            )
            if ok:
                store.clear(gid, uid)
                lines.append(
                    f"{uid} 第 {count} 次警告，满 {WARN_LIMIT} 次了，"
                    f"先安静 {_fmt_duration(WARN_AUTO_MUTE_SECONDS)}。"
                )
            else:
                lines.append(f"{uid} 第 {count} 次警告（自动禁言没成功：{err}）。")
        else:
            lines.append(f"{uid} 第 {count} 次警告（满 {WARN_LIMIT} 次自动禁言）。")
        log_event(
            "admin_command.warn",
            session_id=_group_id(ctx),
            actor=_self_id(ctx),
            target=str(uid),
            count=count,
        )
    await _reply(ctx, "\n".join(lines))


async def _h_warn_list(ctx: Any, args: Any, raw: str) -> None:
    """喵/警告列表：本群谁被警告过、几次。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里用呢。")
        return
    store = _warn_store(_config_of(ctx))
    items = store.list_group(gid)
    if not items:
        await _reply(ctx, "本群没有警告记录。")
        return
    lines = [f"本群警告记录（{len(items)} 人）："]
    for uid, item in items[:30]:
        count = int(item.get("count") or 0)
        reasons = item.get("reasons") or []
        tail = f" —— {reasons[-1]}" if reasons else ""
        lines.append(f"  {uid} {count} 次{tail}")
    await _reply(ctx, "\n".join(lines))


async def _h_warn_clear(ctx: Any, args: Any, raw: str) -> None:
    """喵/警告清零 [@某人]：清掉某人的警告，不带目标则清空本群。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里用呢。")
        return
    store = _warn_store(_config_of(ctx))
    targets, _why = resolve_targets(ctx, args)
    if not targets:
        removed = store.clear(gid)
        await _reply(ctx, f"本群警告记录清空了（{removed} 条）。")
        return
    removed = sum(store.clear(gid, uid) for uid in targets)
    await _reply(ctx, f"清掉了 {removed} 条警告记录。" if removed else "这几个人本来就没被警告过。")


async def _h_blacklist(ctx: Any, args: Any, raw: str) -> None:
    """喵/拉黑 @某人 [原因]：她不再跟这个人说话（不影响签到等玩法）。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "黑名单是分群的，只能在群里拉黑呢。")
        return
    targets, why = resolve_targets(ctx, args)
    if not targets:
        await _reply(ctx, why)
        return
    reason = _raw_text(raw).strip()
    store = _blacklist_store(_config_of(ctx))
    added: list[str] = []
    for uid in targets[:10]:
        if store.add(gid, uid, reason=reason, by=_self_id(ctx)):
            added.append(str(uid))
        log_event(
            "admin_command.blacklist_add",
            session_id=_group_id(ctx),
            actor=_self_id(ctx),
            target=str(uid),
        )
    if not added:
        await _reply(ctx, "这几个人本来就在黑名单里。")
        return
    await _reply(ctx, f"拉黑了 {'、'.join(added)}，他们再说话我不会接了。")


async def _h_unblacklist(ctx: Any, args: Any, raw: str) -> None:
    """喵/解除拉黑 @某人。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里用呢。")
        return
    targets, why = resolve_targets(ctx, args)
    if not targets:
        await _reply(ctx, why)
        return
    store = _blacklist_store(_config_of(ctx))
    removed = [str(uid) for uid in targets[:10] if store.remove(gid, uid)]
    if not removed:
        await _reply(ctx, "他们本来就不在黑名单里。")
        return
    await _reply(ctx, f"解除了 {'、'.join(removed)} 的黑名单。")


async def _h_blacklist_list(ctx: Any, args: Any, raw: str) -> None:
    """喵/黑名单：本群被拉黑的人。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里用呢。")
        return
    store = _blacklist_store(_config_of(ctx))
    items = store.list_group(gid)
    if not items:
        await _reply(ctx, "本群黑名单是空的。")
        return
    lines = [f"本群黑名单（{len(items)} 人）："]
    for uid, item in items[:30]:
        reason = str(item.get("reason") or "").strip()
        when = str(item.get("at") or "").strip()
        tail = " " + " ".join(part for part in (when, reason) if part)
        lines.append(f"  {uid}{tail}")
    await _reply(ctx, "\n".join(lines))


# ---------------------------------------------------------------------------
# 运维：记忆、权限、健康、日志、热重载
# ---------------------------------------------------------------------------


def _session_of(ctx: Any) -> str:
    return str(getattr(ctx, "session_id", "") or "")


def _history_entries(ctx: Any) -> list[dict[str, str]]:
    from .handlers import _get_memory

    try:
        return list(_get_memory(_config_of(ctx)).load_all(_session_of(ctx)) or [])
    except Exception:  # noqa: BLE001 — 读记忆失败不该让指令崩掉
        return []


async def _h_memory(ctx: Any, args: Any, raw: str) -> None:
    """喵/记忆：本会话记得多少、最近几轮是什么。"""
    entries = _history_entries(ctx)
    if not entries:
        await _reply(ctx, "这个会话我还没有记忆呢。")
        return
    lines = [f"这个会话我记得 {len(entries)} 条消息，最近几条："]
    for item in entries[-8:]:
        role = {"user": "对方", "assistant": "我"}.get(str(item.get("role") or ""), "?")
        body = str(item.get("content") or "").replace("\n", " ")
        lines.append(f"  {role}：{body[:60]}" + ("…" if len(body) > 60 else ""))
    await _reply(ctx, "\n".join(lines))


async def _h_forget(ctx: Any, args: Any, raw: str) -> None:
    """喵/忘记 全部 | 喵/忘记 <关键词>：从本会话记忆里删掉内容。

    为什么不做「喵/忘记 @某人」：记忆里只存 role/content，没有「这条属于谁」，
    按人删得先做一轮归属标注，那是另一个工程量。按关键词删是诚实可行的版本。
    """
    from .handlers import _get_memory

    word = _raw_text(raw).strip()
    if not word:
        await _reply(ctx, "要忘掉什么呢？「喵/忘记 全部」或者「喵/忘记 关键词」。")
        return
    memory = _get_memory(_config_of(ctx))
    session = _session_of(ctx)
    entries = _history_entries(ctx)
    if not entries:
        await _reply(ctx, "本来就没有记忆，不用忘。")
        return
    if word in ("全部", "所有", "all"):
        memory.clear(session)
        await _reply(ctx, f"清掉了 {len(entries)} 条记忆。")
        return
    kept = [item for item in entries if word not in str(item.get("content") or "")]
    removed = len(entries) - len(kept)
    if not removed:
        await _reply(ctx, f"没找到含「{word}」的记忆。")
        return
    memory.save(session, kept)
    await _reply(ctx, f"忘掉了 {removed} 条带「{word}」的记忆。")


async def _h_perm(ctx: Any, args: Any, raw: str) -> None:
    """喵/权限：谁能用管理指令（号码部分打码）。"""
    cfg = _config_of(ctx)
    ids: set[str] = set()
    try:
        ids = {str(item) for item in cfg.admin_id_set()}
    except Exception:  # noqa: BLE001 — 老配置对象可能没有这个方法
        ids = set()
    allow_group_admin = bool(getattr(cfg, "admin_allow_group_admin", False))
    lines = [f"管理指令可用人数：{len(ids)}"]
    for uid in sorted(ids):
        # 打码：这张表会被发到群里，完整 QQ 号等于把「谁是管」广播给所有人
        masked = uid if len(uid) <= 4 else f"{uid[:2]}****{uid[-2:]}"
        lines.append(f"  {masked}")
    lines.append("群管理员也能用：" + ("是" if allow_group_admin else "否"))
    lines.append("（名单来自 .env 的 DL_SENPAI_ADMIN_QQ）")
    await _reply(ctx, "\n".join(lines))


async def _h_health(ctx: Any, args: Any, raw: str) -> None:
    """喵/健康：跑一遍自检，把「哪一项坏了」直接说出来。"""
    cfg = _config_of(ctx)
    root = Path(__file__).resolve().parents[3]
    lines = ["自检："]
    # 1) 模型配置
    model = ""
    try:
        model = str(cfg.active_model())
    except Exception:  # noqa: BLE001
        model = ""
    lines.append(f"  模型：{model or '读不到（配置异常）'}")
    # 2) 数据目录可写（真的写一次，别只看权限位）
    data_dir = root / "data" / "dl_senpai"
    probe = data_dir / ".health_probe"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        lines.append("  数据目录：可写")
    except OSError as exc:
        lines.append(f"  数据目录：不可写（{type(exc).__name__}）")
    # 3) 三个管理员状态文件
    from .admin_control import (
        default_blacklist_path,
        default_silence_path,
        default_warn_path,
        get_blacklist_store,
        get_silence_store,
        get_warn_store,
    )

    silence_path = str(getattr(cfg, "admin_silence_path", "") or "") or str(default_silence_path())
    warn_path = str(getattr(cfg, "admin_warn_path", "") or "") or str(default_warn_path())
    block_path = str(getattr(cfg, "admin_blacklist_path", "") or "") or str(default_blacklist_path())
    try:
        silence_count = len(get_silence_store(silence_path)._load()["groups"])
    except Exception:  # noqa: BLE001
        silence_count = -1
    try:
        warn_count = get_warn_store(warn_path).count()
    except Exception:  # noqa: BLE001
        warn_count = -1
    try:
        block_count = get_blacklist_store(block_path).count()
    except Exception:  # noqa: BLE001
        block_count = -1
    lines.append(f"  静默群数：{silence_count if silence_count >= 0 else '读取失败'}")
    lines.append(f"  警告记录：{warn_count if warn_count >= 0 else '读取失败'}")
    lines.append(f"  黑名单条数：{block_count if block_count >= 0 else '读取失败'}")
    # 4) 磁盘空间：留一点余量给日志和记忆文件
    try:
        usage = shutil.disk_usage(str(root))
        free_gb = usage.free / (1024 ** 3)
        lines.append(f"  磁盘剩余：{free_gb:.1f} GB" + ("（偏少）" if free_gb < 2 else ""))
    except OSError:
        lines.append("  磁盘剩余：读不到")
    # 5) 我在这个群的身份：禁言/踢人都依赖它
    gid = _int_gid(ctx)
    me = _self_id(ctx)
    if gid is not None and me.isdigit():
        bot, _event = _bot_event(ctx)
        mine, err = await _member_info(bot, gid, int(me))
        if mine:
            role = _pick(mine, "role")
            lines.append(
                "  本群身份：" + _role_zh(role) + ("（能管人）" if role in ("owner", "admin") else "（管不了人）")
            )
        else:
            lines.append(f"  本群身份：查不到（{err}）")
    await _reply(ctx, "\n".join(lines))


async def _h_reload(ctx: Any, args: Any, raw: str) -> None:
    """喵/重载配置：改完 .env 不用重启进程。"""
    try:
        from .config_watcher import get_watcher

        ok = get_watcher().force_reload()
    except Exception as exc:  # noqa: BLE001 — 重载失败要说清原因
        await _reply(ctx, f"重载失败（{type(exc).__name__}）：{exc}")
        return
    log_event("admin_command.reload_config", session_id=_group_id(ctx), actor=_self_id(ctx), ok=ok)
    await _reply(ctx, "配置重载好了。" if ok else "重载没成功，原因写在日志里了（看 data/logs）。")


async def _h_log(ctx: Any, args: Any, raw: str) -> None:
    """喵/日志 [N]：最近 N 条警告/错误（默认 10 条）。"""
    count = args.duration if args.duration and args.duration > 0 else 10
    count = max(1, min(50, int(count)))
    root = Path(__file__).resolve().parents[3]
    log_dir = root / "data" / "logs"
    try:
        files = sorted(log_dir.glob("bot-*.log"))
    except OSError:
        files = []
    if not files:
        await _reply(ctx, "没找到日志文件（data/logs/bot-YYYYMMDD.log）。")
        return
    path = files[-1]
    try:
        # 只读尾部：日志可能几十 MB，整份读进内存会把进程拖顿
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        await _reply(ctx, f"日志读不了：{type(exc).__name__}")
        return
    hits = [
        line
        for line in text.splitlines()
        if "WARNING" in line or "ERROR" in line or "Traceback" in line
    ]
    if not hits:
        await _reply(ctx, f"{path.name} 里没有警告或错误。")
        return
    tail = hits[-count:]
    body = "\n".join(line[:160] for line in tail)
    await _reply(ctx, f"{path.name} 最近 {len(tail)} 条异常：\n{body}")



# ---------------------------------------------------------------------------
# 小工具：骰子 / 抽签 / 计算
#
# 这三条看起来「她本来就会」，加进来的理由是**确定性**：
# 让模型算 17.6*23.4 它是可能算错的，而且每算一次都要花 token；
# 骰子更明显——模型摇骰子是真的会摇出「全是 6」。
# ---------------------------------------------------------------------------

# 数量允许 3 位：999d6 这种要落到「范围不对」的提示上，
# 而不是含糊地回一句「用法」——前者才让人知道该改成什么。
_DICE_RE = re.compile(r"^(\d{1,3})?[dD](\d{1,4})$")

#: 计算器允许的运算。白名单而不是 eval：eval 一旦漏一个口子，
#: 能碰到它的就是群里任何一个人。
_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_MAX_EXPR_LEN = 100
_MAX_POW = 1000


def _safe_eval(node: ast.AST) -> float:
    """把 AST 求值成数字；不在白名单里的一律拒绝。"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("只支持数字")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW:
            raise ValueError("指数太大了")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _safe_eval(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    raise ValueError("只支持加减乘除、括号、乘方")


async def _h_dice(ctx: Any, args: Any, raw: str) -> None:
    """喵/骰子 [NdM]：默认 1d6。"""
    token = _raw_text(raw).strip().replace(" ", "")
    count, faces = 1, 6
    if token:
        matched = _DICE_RE.match(token)
        if matched:
            count = int(matched.group(1) or 1)
            faces = int(matched.group(2))
        elif token.isdigit():
            count = int(token)
        else:
            await _reply(ctx, "用法：喵/骰子 2d6（2 个 6 面骰），或者 喵/骰子 3。")
            return
    if not 1 <= count <= 20 or not 2 <= faces <= 1000:
        await _reply(ctx, "骰子数量 1-20 个，面数 2-1000。")
        return
    rolls = [random.randint(1, faces) for _ in range(count)]
    total = sum(rolls)
    if count == 1:
        await _reply(ctx, f"🎲 {total}")
        return
    await _reply(ctx, "🎲 " + " + ".join(str(item) for item in rolls) + f" = {total}")


#: 抽签结果：口吻偏她，不写「大凶」这种会让人真不开心的词。
_LOTS = (
    ("大吉", "今天想做的事基本都会顺，适合动手不适合犹豫。"),
    ("中吉", "稳一点走就好，别抢第一个开口。"),
    ("小吉", "小事上有运气，大事上别赌。"),
    ("吉", "平常的一天，但有人会记得你说过的话。"),
    ("末吉", "先难后易。前半段别泄气。"),
    ("平", "不好不坏。适合把拖着的事理一理。"),
    ("小凶", "今天容易说错话，打字前多看一眼。"),
)


async def _h_lots(ctx: Any, args: Any, raw: str) -> None:
    """喵/抽签 [事项]：纯本地随机。"""
    name, hint = random.choice(_LOTS)
    topic = _raw_text(raw).strip()
    head = f"关于「{topic}」：" if topic else ""
    await _reply(ctx, f"🔮 {head}{name} —— {hint}")


async def _h_calc(ctx: Any, args: Any, raw: str) -> None:
    """喵/计算 <表达式>：确定结果，不经过模型。"""
    expr = _raw_text(raw).strip().replace("×", "*").replace("÷", "/").replace("^", "**")
    if not expr:
        await _reply(ctx, "算什么呢？例如：喵/计算 (17.6*23.4+8)/3")
        return
    if len(expr) > _MAX_EXPR_LEN:
        await _reply(ctx, "式子太长了，我算不过来。")
        return
    try:
        value = _safe_eval(ast.parse(expr, mode="eval"))
    except ZeroDivisionError:
        await _reply(ctx, "除数是 0 呢。")
        return
    except (SyntaxError, ValueError) as exc:
        await _reply(ctx, f"算不了：{exc}")
        return
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    await _reply(ctx, f"{expr} = {value}")


async def _h_wordbank(ctx: Any, args: Any, raw: str) -> None:
    """喵/词库：群关键词自动回复（实现在 admin_words）。"""
    from .admin_words import handle_word_command

    await handle_word_command(ctx, args, raw)


async def _h_timer(ctx: Any, args: Any, raw: str) -> None:
    """喵/定时：到点替群里发话（实现在 admin_schedule）。"""
    from .admin_schedule import handle_timer_command

    await handle_timer_command(ctx, args, raw)



async def _h_bad(ctx: Any, args: Any, raw: str) -> None:
    """喵bad：群违禁词表（实现在 admin_badwords）。"""
    from .admin_badwords import handle_badword_command

    await handle_badword_command(ctx, args, raw)

# ---------------------------------------------------------------------------
# 合并回主表
# ---------------------------------------------------------------------------

ALIASES_EXTRA: dict[str, tuple[str, ...]] = {
    "whois": ("查", "查询", "资料", "whois", "info"),
    "members": ("成员", "成员列表", "成员表", "members"),
    "admins": ("管理列表", "管理们", "群管理", "管理员列表", "admins"),
    "mutelist": ("禁言列表", "谁被禁言", "禁言名单", "mutelist"),
    "groupinfo": ("群信息", "群资料", "群状态", "groupinfo"),
    "notices": ("公告列表", "看公告", "公告查询", "notices"),
    "noticedel": ("撤回公告", "删公告", "删除公告", "noticedel"),
    "files": ("群文件", "文件列表", "文件", "files"),
    "essence": ("精华", "群精华", "精华消息", "essence"),
    "albums": ("相册", "群相册", "albums"),
    "recallrecent": ("撤回最近", "批量撤回", "撤回几条", "recallrecent", "purge"),
    "leave": ("退群", "离开本群", "leave"),
    "warn": ("警告", "记过", "warn"),
    "warnlist": ("警告列表", "警告名单", "warnlist"),
    "warnclear": ("警告清零", "清除警告", "warnclear"),
    "blacklist": ("拉黑", "进黑名单", "blacklist", "block"),
    "unblacklist": ("解除拉黑", "取消拉黑", "移出黑名单", "unblacklist", "unblock"),
    "blacklistlist": ("黑名单", "黑名单列表", "blocklist"),
    "memory": ("记忆", "查看记忆", "记忆列表", "memory"),
    "forget": ("忘记", "删记忆", "忘掉", "forget"),
    "perm": ("权限", "管理员名单", "权限列表", "perm", "permission"),
    "health": ("健康", "体检", "健康检查", "health", "doctor"),
    "reload": ("重载配置", "重载", "重新加载配置", "reload"),
    "log": ("日志", "看日志", "最近日志", "log", "logs"),
    "dice": ("骰子", "掷骰", "roll", "dice"),
    "lots": ("抽签", "求签", "运势", "lots"),
    "calc": ("计算", "算一下", "calc"),
    "wordbank": ("词库", "关键词", "wordbank", "words"),
    "timer": ("定时", "定时消息", "提醒", "timer", "schedule"),
    "bad": ("bad", "违禁词", "敏感词", "屏蔽词", "badword"),
}

DESCRIPTIONS_EXTRA: dict[str, str] = {
    "whois": "whois @某人 — 角色、入群时间、名片、是否在禁言里",
    "members": "members [页] — 成员列表，一页 20 人",
    "admins": "admins — 群主与管理员",
    "mutelist": "mutelist — 谁在禁言里、各剩多久",
    "groupinfo": "groupinfo — 群名、人数上限、建群时间",
    "notices": "notices — 群公告列表",
    "noticedel": "noticedel [序号] — 删群公告，默认最新那条",
    "files": "files — 群文件",
    "essence": "essence — 群精华",
    "albums": "albums — 群相册",
    "recallrecent": "recallrecent [N] — 撤掉她自己最近 N 条（默认 3，最多 20）",
    "leave": "leave 确认 — 退出本群，必须带「确认」",
    "warn": "warn @某人 [原因] — 记一次警告，满 3 次禁言 1 天 23 小时 59 分",
    "warnlist": "warnlist — 谁被警告过、各有几次",
    "warnclear": "warnclear [@某人] — 清警告，不带人则清空本群",
    "blacklist": "blacklist @某人 [原因] — 她不再接这个人的话",
    "unblacklist": "unblacklist @某人 — 恢复接话",
    "blacklistlist": "blacklistlist — 本群被拉黑的人",
    "memory": "memory — 本会话记得多少、最近说了什么",
    "forget": "forget 全部|关键词 — 从本群记忆里删内容",
    "perm": "perm — 谁能用管理指令、群管是否也能用",
    "health": "health — 自检：模型、数据目录、磁盘、本群身份",
    "reload": "reload — 改完 .env 不用重启进程",
    "log": "log [N] — 最近 N 条警告/错误（默认 10，最多 50）",
    "dice": "dice [2d6] — 掷骰，默认 1d6",
    "lots": "lots [事项] — 摇一签",
    "calc": "calc 1+2*3 — 确定性计算，不经过模型",
    "wordbank": "wordbank add 你好=你好呀 — 群关键词自动回复（add/del/on/off/list）",
    "timer": "timer add 30m 记得交作业 — 到点替群里发话（add/del/clear/list）",
    "bad": "bad add 傻逼 — 违禁词：踩中就记一次警告，满 3 次禁言",
}

HANDLERS_EXTRA = {
    "whois": _h_whois,
    "members": _h_members,
    "admins": _h_admins,
    "mutelist": _h_mutelist,
    "groupinfo": _h_groupinfo,
    "notices": _h_notices,
    "noticedel": _h_notice_del,
    "files": _h_files,
    "essence": _h_essence,
    "albums": _h_albums,
    "recallrecent": _h_recall_recent,
    "leave": _h_leave,
    "warn": _h_warn,
    "warnlist": _h_warn_list,
    "warnclear": _h_warn_clear,
    "blacklist": _h_blacklist,
    "unblacklist": _h_unblacklist,
    "blacklistlist": _h_blacklist_list,
    "memory": _h_memory,
    "forget": _h_forget,
    "perm": _h_perm,
    "health": _h_health,
    "reload": _h_reload,
    "log": _h_log,
    "dice": _h_dice,
    "lots": _h_lots,
    "calc": _h_calc,
    "wordbank": _h_wordbank,
    "timer": _h_timer,
    "bad": _h_bad,
}

__all__ = ["ALIASES_EXTRA", "DESCRIPTIONS_EXTRA", "HANDLERS_EXTRA"]
