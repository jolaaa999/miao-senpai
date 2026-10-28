"""喵指令：管理员用「喵 + 指令」直接管学姐。

## 为什么要有这一层

以前她只有 reset / 人设卡这类零散入口，而且散在各个业务模块里、判定方式各不相同
（有的要求管理员、有的不判定）。群主要禁言一个人、撤回一条消息、改个名片，只能
自己去 QQ 里点——或者跟她说一遍让她「听懂」。前者麻烦，后者不可靠。

这一层把「人对 bot 的直接指令」收成一条通路：

    喵 + 指令名 [+ 参数]

例如 喵/禁言 @某人 10m / 喵/撤回 / 喵/状态。同时兼容市面上常见的写法，
让从别的 bot（HoshinoBot / AstrBot / 云崽 之类）迁过来的人不必改肌肉记忆：

    /silence  #sl  /reset  喵reset   （前缀可以是 喵 /  #  !  ，指令名另有英文别名）

## 三条设计取舍

1. **权限只认配置里的管理员 QQ 号**（DL_SENPAI_ADMIN_QQ，可多个）。
   身份取 user_id，绝不取群名片：名片是本人随手能改的，拿它做鉴权等于没有鉴权。

2. **权限不足时不吞消息**。命中指令但没权限 → 抛 AdminDenied，由 Router 静默跳过，
   消息继续交给 AI 正常处理。这样非管理员发「喵/禁言」不会像石沉大海，
   也不会被回一句「你没有权限」（等于向全群复述谁有权限）。

3. **每条操作都写审计日志**（admin_command.done / denied）。群管操作是不可逆的
   （踢人、撤回、改名片），出了事必须查得到是谁在什么时候做的。

## 与既有模块的关系

不重新实现已有的业务：禁言复用 group_mute 的时长语义，头衔复用
group_card.apply_special_title（含读取校验与降级），功能开关复用
group_features 的三态存储。这里只负责「解析指令 + 找目标 + 调 API + 回话」。

指令全表与「市面同类指令对照 + 后续待移植清单」见 docs/admin-commands.md。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Sequence

from .observability.logging import log_event
from .core.commands.models import CommandPermissionDenied

try:
    from nonebot import logger
except ImportError:  # 仅单测环境（无 nonebot driver）
    import logging

    logger = logging.getLogger("dl_senpai")  # type: ignore[assignment]


# 进程启动时刻：喵/状态里报「她在线多久了」。模块导入≈进程启动，
# 精确到秒足够，不需要单独的启动钩子。
_PROCESS_START = time.time()

# 允许的指令前缀。喵是本命格式，其余几个是为了兼容别的 bot 的习惯写法。
PREFIXES = "喵/／#＃!！"

# 禁言的默认时长与上限。QQ 侧上限是 30 天，超过会被协议端拒绝。
DEFAULT_MUTE_SECONDS = 60
MAX_MUTE_SECONDS = 30 * 86400

# 「喵/闭嘴」默认的静默时长（分钟）
DEFAULT_SILENCE_MINUTES = 30


# 权限异常定义在 core.commands.models：reset / persona / 管理指令三处共用同一个类型，
# Router 只需要认一个类。AdminDenied 保留为别名，外部与测试的旧引用继续有效。
AdminDenied = CommandPermissionDenied


@dataclass(frozen=True)
class AdminArgs:
    """一条管理指令解析出来的参数。

    targets：文本里直接写的 QQ 号（@ 与引用另外从消息段里取）。
    duration：解析出的时长（秒）；None = 没写。
    words：去掉目标与时长后剩下的文本（名片内容、公告正文、复读内容）。
    """

    targets: tuple[int, ...] = ()
    duration: int | None = None
    words: str = ""

    def arg(self, index: int = 0, default: str = "") -> str:
        parts = self.words.split()
        return parts[index] if 0 <= index < len(parts) else default


# 时长词：10 / 10s / 10秒 / 10m / 10分 / 1h / 2d / 1w
_DURATION_RE = re.compile(
    r"^(\d{1,6})\s*(s|sec|秒|m|min|分钟|分|h|小时|时|d|天|w|周|星期)?$",
    re.IGNORECASE,
)

_UNIT_SECONDS: dict[str, int] = {
    "": 1,
    "s": 1, "sec": 1, "秒": 1,
    "m": 60, "min": 60, "分钟": 60, "分": 60,
    "h": 3600, "小时": 3600, "时": 3600,
    "d": 86400, "天": 86400,
    "w": 604800, "周": 604800, "星期": 604800,
}

# @ 段渲染成 @名字 之后会混进参数里，这里把它剥掉再当文本用
_AT_TOKEN_RE = re.compile(r"@\S+")

# QQ 号形态：5~12 位数字。太短的（比如 "10"）当成时长，不当成用户。
_QQ_RE = re.compile(r"^\d{5,12}$")


def parse_duration(token: str, *, default_unit_seconds: int = 1) -> int | None:
    """把 one token 解析成秒数；不是时长就返回 None。

    default_unit_seconds 是「不带单位时按什么算」：禁言默认秒（跟 QQ 原生一致），
    喵/闭嘴默认分钟（用户说「闭嘴十分钟」不会说「六百秒」）。
    """
    m = _DURATION_RE.match((token or "").strip())
    if m is None:
        return None
    value = int(m.group(1))
    unit = (m.group(2) or "").lower()
    if not unit:
        return value * int(default_unit_seconds)
    return value * _UNIT_SECONDS.get(unit, 1)


def parse_admin_args(
    raw: str, *, default_unit_seconds: int = 1
) -> AdminArgs:
    """解析参数：拆出 QQ 号、时长，剩下的当正文。

    顺序无关（喵/禁言 @小明 10m 与 喵/禁言 10m @小明 等价）：
    管理指令经常是边想边打，要求固定顺序只会换来一堆「格式不对」。
    """
    text = _AT_TOKEN_RE.sub(" ", raw or "")
    targets: list[int] = []
    duration: int | None = None
    words: list[str] = []
    for token in text.split():
        if _QQ_RE.match(token):
            targets.append(int(token))
            continue
        # 时长只取第一个：一条指令里出现两个时长必然是打错了，
        # 与其猜，不如按先写的那个执行（后面的数会被当正文，肉眼可查）。
        if duration is None:
            parsed = parse_duration(token, default_unit_seconds=default_unit_seconds)
            if parsed is not None:
                duration = parsed
                continue
        words.append(token)
    return AdminArgs(
        targets=tuple(targets), duration=duration, words=" ".join(words).strip()
    )


def build_pattern(aliases: Sequence[str]) -> re.Pattern[str]:
    """按别名生成指令正则。

    指令名后面必须是空白、@ 或结尾：避免「喵/禁言语」这种句子被当成禁言指令。
    @ 也算合法边界，是因为中文输入法里「喵/禁言@某人」很常见。
    """
    names = "|".join(re.escape(a) for a in aliases)
    return re.compile(
        r"^\s*(?:喵|/|／|#|＃|!|！)\s*(?:" + names + r")(?=$|[\s@])"
        r"(?P<arg>.*)$",
        re.IGNORECASE | re.DOTALL,
    )


# ---- 上下文/权限 ----


def group_role_admin(ctx: Any) -> bool:
    """发言者是不是「QQ 群原生管理员/群主」，且配置允许他们用管理指令。

    单独一层开关（DL_SENPAI_ADMIN_ALLOW_GROUP_ADMIN，默认关）：
    群主 ≠ bot 的主人。群主换人是群里的事，换掉他不能顺手拿到
    「让她闭嘴一整天」这种能力。要开就显式打开，并且只影响指令，
    绝不影响高风险工具（那条链只认 QQ 名单，见 PermissionService.is_admin）。
    """
    cfg = _config_of(ctx)
    if not bool(getattr(cfg, "admin_allow_group_admin", False)):
        return False
    event = getattr(ctx, "raw_event", None)
    sender = getattr(event, "sender", None)
    role = str(getattr(sender, "role", "") or "").strip().lower()
    return role in {"admin", "owner"}


def ensure_admin(ctx: Any) -> None:
    """确认发言者是管理员，否则抛 AdminDenied（由 Router 静默跳过）。"""
    from .agent.permissions import PermissionService

    if PermissionService().is_admin(ctx) or group_role_admin(ctx):
        return
    log_event(
        "admin_command.denied",
        level="warning",
        session_id=getattr(ctx, "session_id", "") or None,
        actor=str(getattr(ctx, "user_id", "") or ""),
        command=str(getattr(ctx, "plain_text", "") or "")[:40],
    )
    raise AdminDenied("not_admin")


def _is_admin_ctx(ctx: Any) -> bool:
    """判断当前发言者是不是主人。help 靠它决定给谁看哪一半。"""
    from .agent.permissions import PermissionService

    try:
        return bool(PermissionService().is_admin(ctx) or group_role_admin(ctx))
    except Exception:  # noqa: BLE001 — 判定失败一律按普通群友处理，宁可少给
        return False



def _bot_event(ctx: Any) -> tuple[Any, Any]:
    """取回 (bot, raw_event)。与 handlers._ctx_bot_event 同一约定。"""
    return getattr(ctx, "bot", None), getattr(ctx, "raw_event", None)


def _group_id(ctx: Any) -> str:
    gid = str(getattr(ctx, "group_id", "") or "").strip()
    if gid:
        return gid
    event = getattr(ctx, "raw_event", None)
    return str(getattr(event, "group_id", "") or "").strip()


def _self_id(ctx: Any) -> str:
    sid = str(getattr(ctx, "self_id", "") or "").strip()
    if sid:
        return sid
    bot, _event = _bot_event(ctx)
    return str(getattr(bot, "self_id", "") or "").strip()


def _at_ids(ctx: Any) -> list[int]:
    """从原始消息段里取被 @ 的人（排除 @全体 与 @她自己）。"""
    _bot, event = _bot_event(ctx)
    self_id = _self_id(ctx)
    out: list[int] = []
    try:
        for seg in getattr(event, "message", None) or []:
            if str(getattr(seg, "type", "") or "") != "at":
                continue
            qq = str((getattr(seg, "data", {}) or {}).get("qq") or "").strip()
            if not qq or qq == "all" or (self_id and qq == self_id):
                continue
            if qq.isdigit():
                out.append(int(qq))
    except Exception:  # noqa: BLE001 — 消息段结构异常就当没 @ 人
        return []
    return out


def _reply_author_id(ctx: Any) -> int | None:
    """被引用消息的作者 QQ 号。

    「喵/禁言」不写目标时，管理员的习惯是**引用那条消息**再发指令：
    这比 @ 更准（群里有同名/改名），也不用先切输入法打 @。
    """
    _bot, event = _bot_event(ctx)
    reply = getattr(event, "reply", None)
    if reply is None:
        return None
    sender = getattr(reply, "sender", None)
    for holder in (sender, reply):
        uid = getattr(holder, "user_id", None)
        if uid is None:
            continue
        text = str(uid).strip()
        if text.isdigit():
            return int(text)
    return None


def resolve_targets(ctx: Any, args: AdminArgs) -> tuple[list[int], str]:
    """决定指令作用在谁身上；返回 (目标列表, 失败原因)。

    优先级：@ 的人 > 引用消息作者 > 文本里写的 QQ 号。
    三者可以叠加（@ 两个人再补一个号码也照样支持），但**不猜名字**：
    按昵称匹配成员要拉全量群成员表，既慢又容易被同名骗。
    """
    self_id = _self_id(ctx)
    ids: list[int] = []
    for uid in _at_ids(ctx):
        if uid not in ids:
            ids.append(uid)
    if not ids:
        author = _reply_author_id(ctx)
        if author is not None and str(author) != self_id:
            ids.append(author)
    for uid in args.targets:
        if str(uid) == self_id:
            continue
        if uid not in ids:
            ids.append(uid)
    if not ids:
        return [], "要 @ 那个人、引用他的消息，或者直接写 QQ 号。"
    return ids, ""


def _fmt_duration(seconds: int) -> str:
    """把秒数说成人话（群里看「10 分钟」比看「600 秒」直观）。"""
    seconds = int(seconds)
    if seconds <= 0:
        return "0"
    if seconds % 86400 == 0:
        return f"{seconds // 86400} 天"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


async def _reply(ctx: Any, text: str) -> None:
    """走既有的回复通道（带清洗、限流、分条）。

    不直接 bot.send：群管结果也要遵守「她说话的方式」，
    否则管理指令的回复会变成另一套生硬文案，一眼就不像她。
    但**不排拟人打字延迟**：那是闲聊的节奏，管理回执要立刻到。
    """
    bot, event = _bot_event(ctx)
    if bot is None or event is None:
        return
    from .handlers import _reply as _send_reply

    # humanize=False：管理回执是工具输出，不是她在闲聊。
    await _send_reply(bot, event, text, humanize=False)


async def _call(bot: Any, api: str, **kwargs: Any) -> tuple[bool, str]:
    """调一个 OneBot API，把「协议端不支持/被拒」变成可回话的结果。

    管理指令失败最常见的原因不是代码错，而是**她不是群管理员**或协议端没实现
    这个接口。这两种情况必须回一句具体的，否则管理员只会看到「没反应」。
    """
    fn = getattr(bot, api, None)
    if fn is None:
        return False, f"这个协议端没提供 {api}，我做不了这件事。"
    try:
        result = fn(**kwargs)
        if hasattr(result, "__await__"):
            await result
    except Exception as exc:  # noqa: BLE001 — 失败原因要原样告诉管理员
        logger.warning(f"dl_senpai admin api failed api={api} err={type(exc).__name__}: {exc}")
        return False, f"操作被拒了（{type(exc).__name__}）。我可能不是群管理员，或者没这个权限。"
    return True, ""


def _feature_key(word: str) -> str | None:
    """功能名 → 内部 key（中文名与 key 都认）。"""
    from .group_features import FEATURES

    probe = (word or "").strip().lower()
    if not probe:
        return None
    for key, label in FEATURES:
        if probe == key.lower() or probe == label:
            return key
    return None


def _switch_value(word: str) -> bool | None | str:
    """开关词 → True / False / None(跟随全局) / "?"(不认识)。"""
    probe = (word or "").strip().lower()
    if probe in {"开", "开启", "打开", "on", "true", "1", "启用"}:
        return True
    if probe in {"关", "关闭", "关掉", "off", "false", "0", "停用"}:
        return False
    if probe in {"跟随", "默认", "auto", "inherit", "清除", "无"}:
        return None
    return "?"


# ---- 各指令实现 ----


def _raw_text(raw: str) -> str:
    """指令正文（剥掉 @ 名字，保留数字）。

    为什么这些指令不用 parse_admin_args 的 words：公告正文、名片、复读内容里
    出现一串数字是正常的，被当成 QQ 号吃掉会静默改错内容。
    """
    return _AT_TOKEN_RE.sub(" ", raw or "").strip()


def _int_gid(ctx: Any) -> int | None:
    gid = _group_id(ctx)
    return int(gid) if gid.isdigit() else None


async def _h_mute(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/禁言 / 喵/解禁。"""
    await _apply_mute(ctx, args, unmute=False)


async def _h_unmute(ctx: Any, args: AdminArgs, raw: str) -> None:
    await _apply_mute(ctx, args, unmute=True)


async def _apply_mute(ctx: Any, args: AdminArgs, *, unmute: bool) -> None:
    """禁言与解禁共用一条路径（解禁就是时长 0），少一份重复逻辑。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "禁言只能在群里用呢。")
        return
    targets, why = resolve_targets(ctx, args)
    if not targets:
        await _reply(ctx, why)
        return
    duration = 0 if unmute else (args.duration if args.duration is not None else DEFAULT_MUTE_SECONDS)
    # 负值/超长一律夹紧：QQ 侧超过 30 天会直接被协议端拒，不如自己先夹住
    duration = max(0, min(MAX_MUTE_SECONDS, int(duration)))
    bot, _event = _bot_event(ctx)
    done: list[int] = []
    errors: list[str] = []
    for uid in targets:
        ok, err = await _call(
            bot, "set_group_ban", group_id=gid, user_id=uid, duration=duration
        )
        if ok:
            done.append(uid)
        else:
            errors.append(err)
    if done:
        if unmute or duration == 0:
            text = f"好啦，{'、'.join(str(u) for u in done)} 可以说话了。"
        else:
            text = (
                f"收到，{'、'.join(str(u) for u in done)} 安静 {_fmt_duration(duration)}～"
            )
        if errors:
            text += " 另外几个没成功：" + errors[0]
        await _reply(ctx, text)
    else:
        await _reply(ctx, errors[0] if errors else "没能禁言任何人。")


async def _h_muteall(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/全员禁言 [开/关]。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "全员禁言只能在群里用呢。")
        return
    value = _switch_value(args.words) if args.words else True
    if value == "?":
        await _reply(ctx, "用法：喵/全员禁言 开 / 喵/全员禁言 关")
        return
    enable = bool(value) if value is not None else True
    bot, _event = _bot_event(ctx)
    ok, err = await _call(bot, "set_group_whole_ban", group_id=gid, enable=enable)
    if not ok:
        await _reply(ctx, err)
        return
    await _reply(ctx, "全员禁言已打开，谁也别说话。" if enable else "全员禁言关掉了，大家可以说话啦。")


async def _h_kick(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/踢 @某人 [拉黑]。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "踢人只能在群里用呢。")
        return
    targets, why = resolve_targets(ctx, args)
    if not targets:
        await _reply(ctx, why)
        return
    reject = any(word in raw for word in ("拉黑", "拒绝", "reject"))
    bot, _event = _bot_event(ctx)
    done: list[int] = []
    errors: list[str] = []
    for uid in targets:
        ok, err = await _call(
            bot, "set_group_kick", group_id=gid, user_id=uid, reject_add_request=reject
        )
        if ok:
            done.append(uid)
        else:
            errors.append(err)
    if done:
        text = f"已经请 {'、'.join(str(u) for u in done)} 出去了" + ("，并拉黑了。" if reject else "。")
        await _reply(ctx, text + (f" {errors[0]}" if errors else ""))
    else:
        await _reply(ctx, errors[0] if errors else "没能踢出任何人。")


async def _h_recall(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/撤回 [消息id]：优先撤回被引用的那条。"""
    _bot, event = _bot_event(ctx)
    reply = getattr(event, "reply", None)
    message_id = ""
    if reply is not None:
        message_id = str(getattr(reply, "message_id", "") or "").strip()
    if not message_id and args.targets:
        # 没有引用时，把唯一那个数字当成消息 id（撤回 QQ 号和消息 id 都是数字，
        # 这条指令的语义只有「撤回某条消息」，所以按消息 id 解释）
        message_id = str(args.targets[0])
    if not message_id:
        await _reply(ctx, "引用那条要撤回的消息再发「喵/撤回」，或者直接给我消息 id。")
        return
    bot, _event2 = _bot_event(ctx)
    ok, err = await _call(bot, "delete_msg", message_id=int(message_id) if message_id.isdigit() else message_id)
    await _reply(ctx, "撤掉啦。" if ok else err)


async def _h_card(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/名片 @某人 新名片（不写内容 = 清空名片）。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "改名片只能在群里用呢。")
        return
    targets, why = resolve_targets(ctx, args)
    if not targets:
        await _reply(ctx, why)
        return
    card = _raw_text(raw)
    bot, _event = _bot_event(ctx)
    done: list[int] = []
    errors: list[str] = []
    for uid in targets:
        ok, err = await _call(bot, "set_group_card", group_id=gid, user_id=uid, card=card)
        if ok:
            done.append(uid)
        else:
            errors.append(err)
    if done:
        who = "、".join(str(u) for u in done)
        await _reply(ctx, f"名片改成「{card}」了。" if card else f"{who} 的名片清空了。")
    else:
        await _reply(ctx, errors[0] if errors else "没能改名片。")


async def _h_title(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/头衔 @某人 头衔：走 group_card 的专用头衔实现（含校验与降级控制）。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "设头衔只能在群里用呢。")
        return
    targets, why = resolve_targets(ctx, args)
    if not targets:
        await _reply(ctx, why)
        return
    title = _raw_text(raw)
    if not title:
        await _reply(ctx, "要给我一个头衔内容呢，用法：喵/头衔 @某人 头衔。")
        return
    bot, _event = _bot_event(ctx)
    from .group_card import sanitize_special_title, special_title_blocker

    clean = sanitize_special_title(title)
    if not clean:
        await _reply(ctx, "这个头衔内容我不太能用（太长或者带了不允许的字符）。")
        return
    from .group_card import apply_special_title
    # 先问「能不能设」：群头衔只有群主能设，管理员身份发出去必被拒，
    # 而且被拒一次会锁 6 小时——所以在发请求之前就把话说清楚。
    blocked = await special_title_blocker(bot, group_id=gid)
    if blocked:
        await _reply(ctx, blocked)
        return

    # allow_card_fallback=False：管理员要的是「专属头衔」。如果协议端不支持，
    # 偷偷把群名片改成头衔内容是另一种操作，不该在管理员不知情时发生。
    ok = await apply_special_title(
        bot, group_id=gid, user_id=targets[0], title=clean, allow_card_fallback=False
    )
    if ok:
        await _reply(ctx, f"头衔设成「{clean}」了。")
    else:
        await _reply(ctx, "头衔没设上：请求发出去了，但腾讯那边没接受（可能需要群主身份，也可能接口在抽风）。")


async def _h_setadmin(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/设管 @某人：把群成员设成群管理员。"""
    await _apply_group_admin(ctx, args, enable=True)


async def _h_unsetadmin(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/撤管 @某人：取消群管理员。"""
    await _apply_group_admin(ctx, args, enable=False)


async def _apply_group_admin(ctx: Any, args: AdminArgs, *, enable: bool) -> None:
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "这个只能在群里用呢。")
        return
    targets, why = resolve_targets(ctx, args)
    if not targets:
        await _reply(ctx, why)
        return
    bot, _event = _bot_event(ctx)
    done: list[int] = []
    errors: list[str] = []
    for uid in targets:
        ok, err = await _call(bot, "set_group_admin", group_id=gid, user_id=uid, enable=enable)
        if ok:
            done.append(uid)
        else:
            errors.append(err)
    if done:
        verb = "设成管理员" if enable else "的管理员撤掉了"
        await _reply(ctx, f"{'、'.join(str(u) for u in done)} {verb}。")
    else:
        await _reply(ctx, errors[0] if errors else "没能完成这个操作。")


async def _h_notice(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/公告 正文：发群公告。"""
    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "群公告只能在群里发呢。")
        return
    content = _raw_text(raw)
    if not content:
        await _reply(ctx, "要发什么公告呀？用法：喵/公告 正文。")
        return
    bot, _event = _bot_event(ctx)
    # 不同协议端的方法名不一样：优先公开名，其次 NapCat/go-cqhttp 的私有名。
    for api in ("send_group_notice", "_send_group_notice"):
        if getattr(bot, api, None) is not None:
            ok, err = await _call(bot, api, group_id=gid, content=content)
            await _reply(ctx, "公告发出去了。" if ok else err)
            return
    await _reply(ctx, "这个协议端不支持发群公告。")


async def _h_silence(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/闭嘴 [分钟]：本群静默，期间她对谁都不回话。"""
    gid = _group_id(ctx)
    if not gid:
        await _reply(ctx, "静默是按群设置的，私聊里不用这个。")
        return
    if args.words in {"关", "取消", "解除", "off", "停"}:
        _silence_store_set(gid, 0, by=str(getattr(ctx, "user_id", "")), cfg=_config_of(ctx))
        await _reply(ctx, "好啦，我恢复说话了。")
        return
    minutes = int(args.duration or 0) // 60 or DEFAULT_SILENCE_MINUTES
    minutes = max(1, min(7 * 24 * 60, minutes))
    _silence_store_set(gid, minutes * 60, by=str(getattr(ctx, "user_id", "")), cfg=_config_of(ctx))
    await _reply(ctx, f"好，我安静 {_fmt_duration(minutes * 60)}，到点自己回来。")


async def _h_wake(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/醒：解除本群静默。"""
    gid = _group_id(ctx)
    if not gid:
        await _reply(ctx, "私聊里我一直都在。")
        return
    existed = _silence_store_set(gid, 0, by=str(getattr(ctx, "user_id", "")), cfg=_config_of(ctx))
    await _reply(ctx, "我回来啦。" if existed else "我本来就没在安静呀。")


def _silence_store_set(group_id: str, seconds: int, *, by: str, cfg: Any = None) -> bool:
    """写静默状态；返回是否本来处于静默（给回话用）。

    路径优先取**调用方 ctx 上的配置**（测试会注入临时目录），拿不到才回退全局配置。
    以前这里直接读全局配置，于是测试里注入的临时路径根本不生效——
    静默会写进仓库真实的 data 目录，跨用例串味。
    """
    from .admin_control import get_silence_store

    path = str(getattr(cfg, "admin_silence_path", "") or "").strip() if cfg is not None else ""
    if not path:
        path = str(_silence_path_from_config() or "")
    store = get_silence_store(path or None)
    was_silenced = store.is_silenced(group_id)
    store.set(group_id, seconds, by=by)
    log_event(
        "admin_command.silence",
        session_id=f"group:{group_id}" if group_id else None,
        seconds=int(seconds),
        by=by or None,
    )
    return was_silenced


def _silence_path_from_config() -> str | None:
    try:
        from .config_runtime.runtime import get_config

        path = str(getattr(get_config(), "admin_silence_path", "") or "").strip()
        return path or None
    except Exception:  # noqa: BLE001 — 配置不可用时退回默认路径
        return None


async def _h_echo(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/复读 文本：让学姐原样说一句（管理员代发，不经过 AI）。"""
    text = _raw_text(raw)
    if not text:
        await _reply(ctx, "要我说什么呀？用法：喵/复读 文本。")
        return
    bot, event = _bot_event(ctx)
    if bot is None or event is None:
        return
    # 直接 send 而不是走 _reply：管理员要的是「原样这句话」，
    # 走她的回复管线会被字数裁剪/口语化改写，那就不是复读了。
    try:
        await bot.send(event, text)
    except Exception as exc:  # noqa: BLE001
        await _reply(ctx, f"说不出去（{type(exc).__name__}）。")


async def _h_feature(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/功能 [功能名 开/关/跟随]：按群开关她的能力。"""
    from .group_features import FEATURES, get_group_feature_store, is_feature_enabled

    gid = _group_id(ctx)
    name = args.arg(0)
    value_word = args.arg(1)
    if not name:
        if not gid:
            await _reply(ctx, "私聊里没有按群开关这一说呢。")
            return
        cfg = _config_of(ctx)
        store = get_group_feature_store()
        overrides = (store.get_group(gid) or {}).get("features", {})
        on: list[str] = []
        off: list[str] = []
        for key, label in FEATURES:
            if is_feature_enabled(cfg, gid, key):
                on.append(label)
            else:
                off.append(label)
        text = "本群开着的功能：" + ("、".join(on) if on else "（无）")
        text += "\n关掉的：" + ("、".join(off) if off else "（无）")
        if overrides:
            text += "\n单独设置过：" + "、".join(f"{k}={v}" for k, v in overrides.items())
        text += "\n改法：喵/功能 签到 关 / 喵/功能 签到 跟随"
        await _reply(ctx, text)
        return
    if not gid:
        await _reply(ctx, "功能开关是按群设的，私聊里改不了。")
        return
    key = _feature_key(name)
    if key is None:
        await _reply(ctx, f"没有叫「{name}」的功能呢。发「喵/功能」看有哪些。")
        return
    if not value_word:
        cfg = _config_of(ctx)
        state = "开着" if is_feature_enabled(cfg, gid, key) else "关着"
        await _reply(ctx, f"{name} 现在{state}。改法：喵/功能 {name} 开 / 关 / 跟随")
        return
    value = _switch_value(value_word)
    if value == "?":
        await _reply(ctx, "只认 开 / 关 / 跟随 这三个词。")
        return
    store = get_group_feature_store()
    store.set_feature(gid, key, value)  # type: ignore[arg-type]
    label = "跟随全局" if value is None else ("打开" if value else "关掉")
    await _reply(ctx, f"本群「{name}」已{label}。")


async def _h_status(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵/状态：她自己的运行状况快照。"""
    from .admin_control import get_silence_store

    cfg = _config_of(ctx)
    reg = _command_registry()
    uptime = int(time.time() - _PROCESS_START)
    hours, rest = divmod(uptime, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        uptime_text = f"{hours} 小时 {minutes} 分钟"
    elif minutes:
        uptime_text = f"{minutes} 分 {seconds} 秒"
    else:
        uptime_text = f"{seconds} 秒"

    lines = [f"喵学姐在线报告（本次已运行 {uptime_text}）"]
    lines.append(f"· 指令：{len(reg)} 条，命名空间 {len(reg.namespaces())} 个")
    admins = set()
    try:
        admins = set(cfg.admin_id_set())
    except Exception:  # noqa: BLE001 — 老配置对象没有这个方法时不必崩
        admins = set()
    lines.append(f"· 管理员：{len(admins)} 人" if admins else "· 管理员：还没配置（管理指令谁都调不动）")

    gid = _group_id(ctx)
    if gid:
        silence_path = str(getattr(cfg, "admin_silence_path", "") or "").strip()
        left = get_silence_store(silence_path or None).remaining(gid)
        if left:
            lines.append("· 本群静默：还剩 " + _fmt_duration(left))
        else:
            lines.append("· 本群静默：没有")
        try:
            from .group_features import get_group_feature_store

            overrides = (get_group_feature_store().get_group(gid) or {}).get("features", {})
        except Exception:  # noqa: BLE001
            overrides = {}
        if overrides:
            lines.append("· 本群功能覆盖：" + "、".join(f"{k}={v}" for k, v in overrides.items()))
    await _reply(ctx, "\n".join(lines))


#: 筛选结果（「喵help 群管」）仍然按普通消息发，每段控制在这个字数以下。
#: 阈值必须低于 DL_SENPAI_FORWARD_MIN_CHARS（默认 400），否则一条短筛选结果
#: 会被发送链路转成一张只有两行的转发卡片，反而更难读。
_HELP_SEGMENT_CHARS = 320

#: 段与段之间的间隔。连发几条长消息太快，既容易被风控当成刷屏，
#: 也容易被她自己的气泡合并逻辑拼成一坨。
_HELP_SEND_INTERVAL = 0.35


def _split_description(description: str) -> tuple[str, str, str]:
    """把「用法 — 说明」拆成 (指令, 参数, 说明)。

    用法段按第一个空格切开：前面是指令名，后面是参数。参数里带空格
    （@某人 [10m]）很正常，所以只切一刀。
    """
    usage, _, detail = (description or "").partition(" — ")
    usage = usage.strip()
    name, _, args = usage.partition(" ")
    return name.strip(), args.strip(), detail.strip()


def _help_rows(rows: list[tuple[str, str, str]]) -> list[str]:
    """把一组 (指令, 参数, 说明) 排成「一条两行」的正文。

    为什么不用空格对齐的三列表格（2026-10-28 改）：QQ 用的是比例字体，
    手机、电脑、不同字号下每个字符宽度都不一样，靠空格对出来的列在任何
    一端看都是歪的——「格式乱」的根源就是这个。换行分层不依赖字体，
    一行只放一条指令的用法，说明缩进两格另起一行，边界一眼能分出来。
    """
    out: list[str] = []
    for name, args, detail in rows:
        usage = f"• {name} {args}".rstrip()
        out.append(usage)
        if detail:
            out.append(f"   {detail}")
    return out



def help_groups() -> list[tuple[str, list[tuple[str, str, str]]]]:
    """按用途分组返回 [(组名, [(指令, 参数, 说明), ...]), ...]。

    没进分组表的指令一律补到「其他」，不许静默丢弃 —— 新加的指令忘了登记时，
    帮助里会露出来，而不是悄悄消失。
    """
    from .core.commands.admin import GROUPS, merged_tables

    _, descriptions = merged_tables()
    grouped: list[tuple[str, list[tuple[str, str, str]]]] = []
    used: set[str] = set()
    for name, actions in GROUPS.items():
        rows = [_split_description(descriptions[action]) for action in actions if action in descriptions]
        used.update(action for action in actions if action in descriptions)
        if rows:
            grouped.append((name, rows))
    rest = [
        _split_description(text) for action, text in descriptions.items() if action not in used
    ]
    if rest:
        grouped.append(("其他", rest))
    return grouped


def help_nodes(is_admin: bool = True) -> list[tuple[str, str]]:
    """生成「喵help」的合并转发节点：[(节点标题, 节点正文)]。

    两种身份看到的东西不一样（2026-10-28 用户要求）：
    - 主人：46 条管理指令按组排出，每组标题标「主人专属」，末尾再附一张
      「所有人可用」，一眼能分清哪些是自己的活、哪些是群友自己能玩的；
    - 群友：只发「所有人可用」那张，并说明「加了喵的是主人的指令，发了没人应」。
      不然他们对着 46 条不属于自己的指令，只会以为这功能坏了。
    """
    from .core.commands.admin import ALL_LEVEL

    # 玩法指令没有「参数」这一列，直接拼两行，避免 _help_rows 里空列留白
    play = [f"• {usage}\n   {detail}" for usage, detail in ALL_LEVEL]

    if not is_admin:
        overview = [
            "下面这些是你能用的，直接说就行——不用加「喵」。",
            "",
            "例如：签到、商店、好感度、今日任务",
            "",
            "带「喵」的是主人的专属指令（禁言、踢人、改名片那些），",
            "你发了不会有回应——不是坏了，是本来就没给你开。",
        ]
        return [
            ("你能用的", "\n".join(overview)),
            (f"所有人可用 · {len(play)} 条", "\n".join(play)),
        ]

    groups = help_groups()
    total = sum(len(rows) for _, rows in groups)
    counts = " · ".join(f"{name} {len(rows)}" for name, rows in groups)
    overview = [
        f"主人专属 {total} 条 + 所有人可用 {len(play)} 条。",
        "主人指令写法：喵 + 指令，例如「喵mute @某人 10m」，",
        "也认 /mute、#mute、喵/mute 这几种写法。",
        "",
        f"主人专属分组：{counts}",
        f"所有人可用那 {len(play)} 条不用加喵，见最后一张卡片。",
        "",
        "只看一组：喵help 群管    搜关键词：喵help 禁言",
    ]
    nodes: list[tuple[str, str]] = [("怎么用", "\n".join(overview))]
    for name, rows in groups:
        nodes.append((f"{name} · {len(rows)} 条（主人专属）", "\n".join(_help_rows(rows))))
    nodes.append((f"所有人可用 · {len(play)} 条", "\n".join(play)))
    return nodes


def _fits(buffer: list[str], extra: str) -> bool:
    return sum(len(line) + 1 for line in buffer) + len(extra) + 2 <= _HELP_SEGMENT_CHARS


def help_text(query: str = "", is_admin: bool = True) -> list[str]:
    """按分组名或关键词筛选，返回若干段纯文本（每段一条普通消息）。

    筛选同样按身份分叉。群友搜「踢」时不能只回一句「没有这条」——
    「这是主人的指令」和「没这个东西」对群友是完全不同的两种信息。
    """
    from .core.commands.admin import ALL_LEVEL

    groups = help_groups()
    total = sum(len(rows) for _, rows in groups)
    play_rows = [(usage, "", detail) for usage, detail in ALL_LEVEL]
    key = (query or "").strip()

    if not is_admin:
        if not key:
            return ["\n".join(_help_rows(play_rows))]
        picked = [(usage, "", detail) for usage, detail in ALL_LEVEL if key in usage or key in detail]
        if picked:
            # head 和条目拼成一段：拆成两条消息发，中间还夹着别人的话会很乱
            return ["\n".join(["能用的有这些，直接说就行（不用加喵）：", *_help_rows(picked)])]
        admin_hit = any(key in name or name in key for name, _ in groups) or any(
            key in " ".join(row) for _, rows in groups for row in rows
        )
        tail = (
            f"「{key}」是主人的指令，你发了我也做不了。"
            if admin_hit
            else f"没有和「{key}」对上的东西。"
        )
        return [f"{tail}\n发「喵help」看你能用的 {len(play_rows)} 条。"]

    if key:
        # 先当分组名（「群管」能命中「群管理」），筛不到再当关键词
        selected = [(name, rows) for name, rows in groups if key in name or name in key]
        if not selected:
            selected = [
                (name, [row for row in rows if key in " ".join(row)])
                for name, rows in groups
            ]
            selected = [(name, rows) for name, rows in selected if rows]
        play_picked = [(usage, "", detail) for usage, detail in ALL_LEVEL if key in usage or key in detail]
        if not selected and not play_picked:
            return [
                f"没有和「{key}」对上的指令。\n发「喵help」看全部 {total} 条，"
                "或者「喵help 群管」「喵help 风控」这样按组看。"
            ]
        matched = sum(len(rows) for _, rows in selected) + len(play_picked)
        head = [f"「{key}」相关的指令 {matched} 条："]
        groups = selected + ([("所有人可用", play_picked)] if play_picked else [])
    else:
        head = [
            f"主人专属 {total} 条 + 所有人可用 {len(play_rows)} 条。"
            "写法：喵 + 指令，例如「喵mute @某人 10m」。"
        ]

    segments: list[str] = []
    buffer: list[str] = list(head)

    def flush() -> None:
        nonlocal buffer
        if buffer:
            segments.append("\n".join(buffer))
            buffer = []

    for name, rows in groups:
        header = f"【{name}】{len(rows)} 条"
        if not _fits(buffer, header):
            flush()
        buffer.append(header)
        for line in _help_rows(rows):
            if not _fits(buffer, line):
                flush()
                # 一组被拆到两段时，新段开头补个「续」标题，
                # 否则读者看到一串没有归属的条目，会以为它们是另一组。
                buffer.append(f"【{name}（续）】")
            buffer.append(line)
    flush()
    return segments


async def _reply_forward(ctx: Any, nodes: list[tuple[str, str]]) -> bool:
    """把 [(标题, 正文)] 发成一条合并转发；发不出去返回 False（调用方退回分段发）。

    节点标题用 node 的 nickname 承载：QQ 的聊天记录卡片每条都显示
    「昵称：内容」，正好拿它当分组小标题，正文就不用再重复一遍组名。
    """
    bot, event = _bot_event(ctx)
    if bot is None or event is None or not nodes:
        return False
    try:
        from nonebot.adapters.onebot.v11 import Message as OBMessage
        from nonebot.adapters.onebot.v11 import MessageSegment

        from .reply_send import _send_forward

        user_id = int(getattr(ctx, "self_id", 0) or 0) or 10000
        segments = [
            MessageSegment.node_custom(
                user_id=user_id, nickname=title, content=OBMessage(body)
            )
            for title, body in nodes
        ]
        await _send_forward(bot, event, segments)
        return True
    except Exception as exc:  # noqa: BLE001 — 卡片发不出去不能把帮助弄丢
        logger.warning(f"dl_senpai help: forward failed {type(exc).__name__}: {exc}")
        return False


async def _h_help_dispatch(ctx: Any, args: AdminArgs, raw: str) -> None:
    """喵help [分组|关键词]：主人看全部，群友只看自己能用。"""
    is_admin = _is_admin_ctx(ctx)
    query = (_raw_text(raw) or "").strip()
    if not query:
        if await _reply_forward(ctx, help_nodes(is_admin)):
            return
        # 协议端不支持合并转发时退回分段发，功能不能因为发送方式缺失而消失
        segments = help_text("", is_admin)
    else:
        segments = help_text(query, is_admin)
    for index, segment in enumerate(segments):
        if index:
            await asyncio.sleep(_HELP_SEND_INTERVAL)
        await _reply(ctx, segment)



async def _h_ping(ctx: Any, args: AdminArgs, raw: str) -> None:
    await _reply(ctx, "在的在的，我一直醒着呢。")


# 指令 action → 处理器。
# 用一张表而不是 if/elif 长链：新增指令只加一行，也顺便让「有哪些 action」
# 一眼可见——漏接一个 action 会表现为「指令没反应」，最难查的那种 bug。
_HANDLERS = {
    "mute": _h_mute,
    "unmute": _h_unmute,
    "muteall": _h_muteall,
    "kick": _h_kick,
    "recall": _h_recall,
    "card": _h_card,
    "title": _h_title,
    "setadmin": _h_setadmin,
    "unsetadmin": _h_unsetadmin,
    "notice": _h_notice,
    "silence": _h_silence,
    "wake": _h_wake,
    "echo": _h_echo,
    "feature": _h_feature,
    "status": _h_status,
    "help": _h_help_dispatch,
    "ping": _h_ping,
}


def all_handlers() -> dict[str, Any]:
    """主表 + 扩展表（admin_commands_extra）。

    为什么延迟导入：extra 顶层要从本模块导入 _reply / resolve_targets 等工具，
    在本模块顶层导入 extra 会形成循环。函数内导入在 Python 里是廉价操作
    （模块已经进 sys.modules），每条指令多花的时间可以忽略。
    """
    from .admin_commands_extra import HANDLERS_EXTRA

    return {**_HANDLERS, **HANDLERS_EXTRA}


async def handle_admin_command(ctx: Any, match: Any) -> None:
    """喵指令的统一入口：鉴权 → 解析 → 分派 → 审计。

    鉴权放在最前面：解析里有读 message 段等动作，不该让非管理员触达。
    """
    action = str(getattr(match, "action", "") or "")
    # help 是唯一对所有人开放的指令：群友也要能查自己能用什么。
    # 其余一律先鉴权——解析里要读 message 段，不该让非管理员触达。
    if action != "help":
        ensure_admin(ctx)
    handler = all_handlers().get(action)
    raw = match.arg(0) if hasattr(match, "arg") else ""
    if handler is None:
        log_event(
            "admin_command.unknown",
            level="warning",
            action=action or None,
            actor=str(getattr(ctx, "user_id", "") or "") or None,
        )
        await _reply(ctx, "这个指令我还不认识呢，发「喵/帮助」看看有哪些。")
        return
    # 喵/闭嘴的裸数字按分钟理解，其余指令的裸数字按秒（与 QQ 原生禁言一致）
    args = parse_admin_args(
        raw, default_unit_seconds=60 if action == "silence" else 1
    )
    log_event(
        "admin_command.invoked",
        session_id=getattr(ctx, "session_id", "") or None,
        actor=str(getattr(ctx, "user_id", "") or "") or None,
        action=action,
        targets=list(args.targets),
        duration=args.duration,
        group_id=_group_id(ctx) or None,
    )
    await handler(ctx, args, raw)


def _command_registry() -> Any:
    """取「进程真正在用的」命令表。

    为什么不直接用 core.commands.registry.get_registry()：Runtime 自己 new 了一个
    CommandRegistry 实例（Runtime.commands），全部命令都注册在那上面，全局单例
    是空的。用错那一份的结果就是「喵/状态」报「指令：0 条」——一个看起来
    正常、其实全是假数字的快照（这个 bug 就是这么被端到端冒烟抓出来的）。

    没有 Runtime（单测/独立调用）时回退到全局单例。
    """
    try:
        from .core.runtime import get_runtime

        reg = getattr(get_runtime(), "commands", None)
        if reg is not None and len(reg) > 0:
            return reg
    except Exception:  # noqa: BLE001 — 拿不到 Runtime 就退回全局
        pass
    from .core.commands.registry import get_registry

    return get_registry()


def _config_of(ctx: Any) -> Any:
    """配置取值：优先 ctx 上的（测试注入），其次全局缓存配置。"""
    cfg = getattr(ctx, "config", None)
    if cfg is not None:
        return cfg
    from .config_runtime.runtime import get_config

    return get_config()


__all__ = [
    "AdminArgs",
    "AdminDenied",
    "PREFIXES",
    "build_pattern",
    "ensure_admin",
    "handle_admin_command",
    "help_text",
    "parse_admin_args",
    "parse_duration",
    "resolve_targets",
]
