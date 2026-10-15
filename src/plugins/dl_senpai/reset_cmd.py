"""reset喵：把当前群被污染的上下文清掉。

为什么需要它：人设文件改对了（2026-10-12 从傲娇改为软萌），但
data/dl_senpai/memory/*.json 里存着**改人设之前**的 assistant 发言，
它们会作为上下文原样发给模型，而上下文里的语气实例通常比 system prompt
的风格指令更有力 —— 结果就是「改了说明书，没改记忆」。

设计取舍：
- **只清当前会话**：清理是破坏性的，绝不能让一个群的操作波及其它群。
  群聊里只动这个群的三个文件；私聊里只动这个人自己的会话与个人卡片。
- **先备份再删**：改名成 *.bak-<时间戳> 而不是 unlink，误操作可回捞。
  磁盘上已有同样先例（styles.json.bak-20261011-193106）。
- **分级**：对话历史是主要污染源；卡片、群风格代价更大，默认不动。
- **不碰表情包**：图本身没有风格，是历史在带偏，删关键词只会让她少一批图。

2026-10-14：支持私聊。三个层级的文件命名在两种会话下并不一致：
    群聊   memory/group_<gid>.json      cards/group_<gid>.json  style/group_<gid>.json
    私聊   memory/private_<uid>.json    cards/user_<uid>.json   （无群风格）
私聊的历史是 private_ 前缀（与 ChatMemory 的 session_id_for_private 一致），
而卡片是 user_ 前缀——**个人卡片跨群共享**，所以在私聊清卡片会影响到
她在别的群对这个人的印象，回复里必须讲清楚，不能默默做掉。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .observability.logging import log_event

# 中文别名 → 内部层级。
# 用中文是因为指令本身就是中文（reset喵 卡片），
# 但内部统一成英文标识，日志和测试读起来不受输入语言影响。
_ALIASES = {
    "": "history",
    "历史": "history",
    "对话": "history",
    "记忆": "history",
    "卡片": "cards",
    "记忆卡": "cards",
    "风格": "style",
    "群风格": "style",
    "全部": "all",
    "所有": "all",
}

# 备份后缀里放时间戳，重复执行不会互相覆盖。
_BACKUP_STAMP_FORMAT = "%Y%m%d-%H%M%S"


def parse_reset_scope(arg: str) -> str | None:
    """把参数解析成层级；无法识别时返回 None（调用方会回一句用法提示）。"""
    return _ALIASES.get((arg or "").strip().lower())


def scope_key(group_id: str) -> str:
    """群作用域键，与 ChatMemory / 卡片存储的命名保持一致。"""
    return f"group_{group_id}"


@dataclass(frozen=True)
class ResetTarget:
    """一次 reset 的作用域（某个群，或某个人的私聊）。

    三种存储的作用域键在群/私聊下**并不相同**，所以不在这里算好，
    而是让每个调用点各自拼名字——拼错一个就是"清理跑了个空"，
    比直接报错更难发现。
    """

    history_key: str
    cards_key: str
    style_key: str
    #: 回复文案里的称谓（"本群" / "咱俩"）
    label: str
    is_group: bool


def resolve_target(*, group_id: str, user_id: str) -> ResetTarget | None:
    """决定清理哪个作用域；两个 id 都没有时返回 None（无法定位，调用方拒绝）。"""
    group_id = str(group_id or "").strip()
    user_id = str(user_id or "").strip()

    if group_id:
        key = scope_key(group_id)
        return ResetTarget(
            history_key=key, cards_key=key, style_key=key,
            label="本群", is_group=True,
        )

    if user_id:
        return ResetTarget(
            # 与 ChatMemory.session_id_for_private 一致
            history_key=f"private_{user_id}",
            # 与 memory_facade.get_cards 里 store.active(f"user_{uid}") 一致
            cards_key=f"user_{user_id}",
            # 私聊不采样群风格，没有对应文件；空串表示"这一层不适用"
            style_key="",
            label="咱俩", is_group=False,
        )

    return None


def _backup(path: Path) -> Path | None:
    """把文件改名成带时间戳的备份；不存在或改名失败都返回 None。

    为什么不复制再删：改名是原子的，中途进程被杀也不会留下
    「备份了但原文件还在」或「原文件没了备份也没成」的中间态。
    """
    if not path.is_file():
        return None
    stamp = time.strftime(_BACKUP_STAMP_FORMAT)
    target = path.with_name(f"{path.name}.bak-{stamp}")
    # 同一秒内重复执行会撞名，加序号兜底
    seq = 1
    while target.exists():
        target = path.with_name(f"{path.name}.bak-{stamp}-{seq}")
        seq += 1
    try:
        path.rename(target)
    except OSError:
        return None
    return target


# ---- 各层级的清理实现 ----

def _clean_history(config: Any, key: str) -> list[str]:
    """清当前会话的对话历史（主要污染源）。"""
    directory = Path(str(getattr(config, "memory_dir", "data/dl_senpai/memory")))
    return _clean_dir_files(directory, (f"{key}.json",)) if key else []


def _clean_cards(config: Any, key: str) -> list[str]:
    """清当前作用域的记忆卡片。

    卡片文件名走 cards.py 的 _safe() 净化，作用域键里只有字母数字和
    下划线，净化后不变，所以这里按同样规则拼名即可。
    """
    directory = Path(str(getattr(config, "memory_cards_dir", "data/dl_senpai/memory_cards")))
    return _clean_dir_files(directory, (f"{key}.json",)) if key else []


def _clean_style(config: Any, key: str) -> list[str]:
    """清群风格学习（从群聊里学来的词频）。私聊没有这一层，key 为空时直接跳过。"""
    directory = Path(str(getattr(config, "group_style_dir", "data/dl_senpai/group_style")))
    return _clean_dir_files(directory, (f"{key}.json",)) if key else []


def _clean_recent_chat(config: Any, key: str) -> list[str]:
    """清当前会话的「当天聊天流水」。

    为什么跟着「历史」一起清：流水是给她的背景资料，清了历史却留着它，
    她会继续引用刚被清掉的话题，用户会觉得「根本没清干净」。

    注意存储键与文件名的形态不同（流水按 group:123 记，文件是 group_123），
    所以要先把文件键翻回会话 id，否则内存缓存清不掉、下一轮又写回去。
    """
    if not key:
        return []
    from .recent_chat import get_recent_chat_store
    from .session_fs import fs_key_to_session_id

    directory = Path(str(getattr(config, "recent_chat_dir", "data/dl_senpai/recent_chat")))
    removed = _clean_dir_files(directory, (f"{key}.json",))
    try:
        get_recent_chat_store(str(directory)).forget(fs_key_to_session_id(key))
    except Exception:  # noqa: BLE001 — 内存态清不掉不该拦住文件清理
        pass
    return removed


def _clean_dir_files(directory: Path, names: tuple[str, ...]) -> list[str]:
    """把指定目录下匹配的文件逐个备份；返回成功备份的文件名。"""
    done: list[str] = []
    if not directory.is_dir():
        return done
    for name in names:
        if _backup(directory / name) is not None:
            done.append(name)
    return done


async def handle_reset_command(ctx: Any, match: Any) -> None:
    """reset喵 的处理器。

    权限：只有配置里的管理员能清（agent_admin_qq）。
    范围：永远是当前群，不存在「顺手清掉别的群」的写法。

    回复走既有的 bot/event 通道：Router 的 RouteContext 本身没有发送能力，
    其余命令（dispatch_checkin_command 等）都是先取回 bot/event 再交给
    旧的 handle_* 实现。跟着这个模式走，才能复用同一套回复与限流逻辑。
    """
    from .agent.permissions import PermissionService
    from .handlers import _ctx_bot_event

    scope = parse_reset_scope(match.arg(0) if hasattr(match, "arg") else "")
    bot, event = _ctx_bot_event(ctx)
    if bot is None or event is None:
        return

    if scope is None:
        await _reply(bot, event, "用法：reset喵 [历史|卡片|风格|全部]，不写参数只清本群对话历史。")
        return

    # 群聊用 group_id，私聊退回 user_id：两种会话各有自己的清理范围。
    target = resolve_target(
        group_id=str(getattr(event, "group_id", "") or ""),
        user_id=str(getattr(ctx, "user_id", "") or getattr(event, "user_id", "") or ""),
    )
    if target is None:
        # 既没有群号也没有 QQ 号，定位不到要清什么；宁可不清
        await _reply(bot, event, "没能确认要清哪个会话，这次先不动。")
        return

    config = _config_of(ctx)
    if not PermissionService(config).is_admin(ctx):
        # 拒绝时不透露配置结构，只说没有权限
        await _reply(bot, event, "这个只有管理员能用呢。")
        log_event(
            "reset.denied",
            level="warning",
            session_id=getattr(ctx, "session_id", "") or None,
            actor=str(getattr(ctx, "user_id", "") or ""),
            scope=scope,
        )
        return

    removed: dict[str, list[str]] = {}
    if scope in ("history", "all"):
        removed["history"] = _clean_history(config, target.history_key)
        # 当天流水属于「她眼前的对话」，跟历史一起清
        removed["recent_chat"] = _clean_recent_chat(config, target.history_key)
    if scope in ("cards", "all"):
        removed["cards"] = _clean_cards(config, target.cards_key)
    if scope in ("style", "all"):
        removed["style"] = _clean_style(config, target.style_key)

    total = sum(len(v) for v in removed.values())
    log_event(
        "reset.done",
        session_id=getattr(ctx, "session_id", "") or None,
        actor=str(getattr(ctx, "user_id", "") or ""),
        scope=scope,
        files=total,
        detail=removed,
        scope_kind="group" if target.is_group else "private",
    )

    label = {"history": "对话历史", "cards": "记忆卡片", "style": "群风格", "all": "全部"}[scope]

    # 私聊里没有群风格这一层。必须明说"跳过了"，否则对方会以为已经清干净。
    notes: list[str] = []
    if not target.is_group and scope in ("style", "all"):
        notes.append("私聊没有群风格这一层，那部分没事可清。")
    # 个人卡片是跨群共享的，在私聊清掉会影响她在别的群对这个人的印象——
    # 这种"看不见的连带影响"必须在回复里讲明白。
    if not target.is_group and scope in ("cards", "all") and removed.get("cards"):
        notes.append("提醒一下：个人记忆卡是跨群共用的，别的群也会一起忘掉。")

    if total == 0:
        text = f"{target.label}的{label}本来就是空的，没什么好清的～"
        if notes:
            text = text + " " + " ".join(notes)
        await _reply(bot, event, text)
        return

    text = f"好啦，{target.label}的{label}已经清掉（{total} 个文件，都留了备份）。"
    if notes:
        text = text + " " + " ".join(notes)
    await _reply(bot, event, text)


def _config_of(ctx: Any) -> Any:
    """优先用 ctx 上的配置（测试会注入），否则退回全局配置。"""
    cfg = getattr(ctx, "config", None)
    if cfg is not None:
        return cfg
    from .config import get_config

    return get_config()


async def _reply(bot: Any, event: Any, text: str) -> None:
    """复用既有的 _reply 实现（带表情清洗与限流）。

    延迟 import：handlers 是重量级模块，且它在导入期会反向引用本包，
    放在模块顶层会造成循环导入。
    """
    from .handlers import _reply as _send_reply

    await _send_reply(bot, event, text)


__all__ = [
    "ResetTarget",
    "handle_reset_command",
    "parse_reset_scope",
    "resolve_target",
    "scope_key",
]
