"""群违禁词：管理员维护词表，群友踩中就记一次警告（喵bad）。

和词库（admin_words）的区别，也是它必须单独存在的原因：
- 词库是「整句等于触发词才回」，违禁词是「消息里**包含**就算」；
- 词库命中是回一句话，违禁词命中是记一笔账，满三次就禁言。
两者的出错代价完全不同：词库配错顶多答非所问，违禁词配错会把人禁掉。
所以分开存、分开开关——配错时能原地停掉这一张表，不牵连词库。

它落在哪一层：一条 priority 55、consume=True 的 Command（见
core/runtime.py 的 _register_badwords_command）。命中就拦下这条消息不进
模型——违禁词消息不该再让她闲聊接话，否则「骂人 + 卖萌回复」会很怪。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from threading import Lock
from typing import Any

try:
    from nonebot import logger
except ImportError:  # 仅单测环境
    import logging

    logger = logging.getLogger("dl_senpai")  # type: ignore[assignment]

#: 单个词最长多少字。太长的「词」多半是整段复制，拦下来只会误伤。
MAX_WORD_LEN = 40
#: 一个群最多多少条：没上限的话，某人可以灌一万条把文件读爆。
MAX_WORDS_PER_GROUP = 200

_ADD_HINT = re.compile(r"^\s*(加|添加|新增|add)\s+(.+?)\s*$", re.DOTALL)
_DEL_HINT = re.compile(r"^\s*(删|删除|移除|del)\s+(.+?)\s*$", re.DOTALL)


def default_badwords_path(project_root: str | Path | None = None) -> Path:
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[3]
    return root / "data" / "dl_senpai" / "admin" / "badwords.json"


class BadwordStore:
    """违禁词表：{群号: {enabled, words: {词: {by, at}}}}。

    为什么按群存而不是全局：每个群的尺度不一样（技术群能说「傻」，闲聊群不行），
    而且一个管理员配错了只影响他自己的群。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._cache: dict[str, Any] | None = None
        self._mtime: float | None = None

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "groups": {}}

    def _load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if self._cache is not None and self._mtime == mtime:
            return self._cache
        data = self._empty()
        if mtime is not None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("groups"), dict):
                    data = {"version": 1, "groups": loaded["groups"]}
            except (OSError, json.JSONDecodeError):
                logger.warning(f"dl_senpai badwords: failed to read {self.path}")
        self._cache = data
        self._mtime = mtime
        return data

    def _save(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(".json.tmp")
        with self._lock:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
            try:
                self._mtime = self.path.stat().st_mtime
            except OSError:
                self._mtime = None
            self._cache = data

    def _bucket(self, data: dict[str, Any], group_id: int | str, *, create: bool) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        if not gid:
            return {}
        groups = data["groups"]
        bucket = groups.get(gid)
        if not isinstance(bucket, dict):
            if not create:
                return {}
            bucket = {"enabled": True, "words": {}}
            groups[gid] = bucket
        bucket.setdefault("enabled", True)
        if not isinstance(bucket.get("words"), dict):
            bucket["words"] = {}
        return bucket

    # -- 读 --

    def enabled(self, group_id: int | str) -> bool:
        bucket = self._bucket(self._load(), group_id, create=False)
        return bool(bucket.get("enabled", True)) if bucket else True

    def list_group(self, group_id: int | str) -> list[str]:
        bucket = self._bucket(self._load(), group_id, create=False)
        words = bucket.get("words") if bucket else {}
        return sorted(words) if isinstance(words, dict) else []

    def hit(self, group_id: int | str, text: str) -> str:
        """这条消息踩中哪个词；没踩中返回空串。

        取**最长**的那个命中词：词表里同时有「傻」和「傻逼」时，
        记警告要记更精确的那个，不然管理员看日志看不出到底骂了什么。
        英文按小写比，中文原样比（大小写在中文里没意义，但英文词很常见）。
        """
        body = (text or "").lower()
        if not body:
            return ""
        best = ""
        for word in self.list_group(group_id):
            probe = word.lower()
            if probe and probe in body and len(word) > len(best):
                best = word
        return best

    # -- 写 --

    def set_enabled(self, group_id: int | str, on: bool) -> None:
        data = self._load()
        bucket = self._bucket(data, group_id, create=True)
        bucket["enabled"] = bool(on)
        self._save(data)

    def add(self, group_id: int | str, word: str, *, by: str = "") -> tuple[bool, str]:
        """加一个词，返回 (成功, 说明)。失败时说明要能直接回给管理员看。"""
        text = (word or "").strip()
        if not text:
            return False, "要加什么词呢？写法：喵bad add 傻逼"
        if len(text) > MAX_WORD_LEN:
            return False, f"这个词太长了（超过 {MAX_WORD_LEN} 字），换个短点的。"
        data = self._load()
        bucket = self._bucket(data, group_id, create=True)
        words = bucket["words"]
        if text in words:
            return False, f"「{text}」本来就在表里了。"
        if len(words) >= MAX_WORDS_PER_GROUP:
            return False, f"本群已经 {MAX_WORDS_PER_GROUP} 条了，先删几个吧。"
        words[text] = {"by": str(by or ""), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._save(data)
        return True, f"加上了「{text}」，现在共 {len(words)} 条。"

    def remove(self, group_id: int | str, word: str) -> bool:
        data = self._load()
        bucket = self._bucket(data, group_id, create=False)
        words = bucket.get("words") if bucket else {}
        if not isinstance(words, dict) or (word or "").strip() not in words:
            return False
        words.pop((word or "").strip(), None)
        self._save(data)
        return True

    def clear(self, group_id: int | str) -> int:
        data = self._load()
        bucket = self._bucket(data, group_id, create=False)
        words = bucket.get("words") if bucket else {}
        count = len(words) if isinstance(words, dict) else 0
        if count:
            bucket["words"] = {}
            self._save(data)
        return count


_store: BadwordStore | None = None
_store_path: str | None = None


def get_badword_store(path: str | Path | None = None) -> BadwordStore:
    global _store, _store_path
    if path is None:
        if _store is None:
            _store = BadwordStore(default_badwords_path())
            _store_path = str(_store.path)
        return _store
    resolved = str(Path(path))
    if _store is None or _store_path != resolved:
        _store = BadwordStore(resolved)
        _store_path = resolved
    return _store


def _cfg_path(cfg: Any) -> str:
    return str(getattr(cfg, "admin_badwords_path", "") or "").strip() if cfg is not None else ""


def _cfg_of_ctx(ctx: Any) -> Any:
    cfg = getattr(ctx, "config", None)
    if cfg is not None:
        return cfg
    try:
        from .config_runtime.runtime import get_config

        return get_config()
    except Exception:  # noqa: BLE001
        return None


def parse_badword_command(raw: str) -> tuple[str, list[str]]:
    """解析「喵bad」的参数：返回 (子命令, 词列表)。

    一次允许加多个词（空格隔开）：管理员想把一批脏话导进来时，
    一条一条发指令是最容易半途而废的用法。
    """
    text = (raw or "").strip()
    if not text:
        return "list", []
    if text in ("开", "打开", "启用", "on"):
        return "on", []
    if text in ("关", "关闭", "停用", "off"):
        return "off", []
    if text in ("列表", "查看", "list"):
        return "list", []
    if text in ("清空", "clear", "清空全部"):
        return "clear", []
    added = _ADD_HINT.match(text)
    if added:
        return "add", [w for w in re.split(r"[\s,，、]+", added.group(2)) if w.strip()]
    deleted = _DEL_HINT.match(text)
    if deleted:
        return "del", [w for w in re.split(r"[\s,，、]+", deleted.group(2)) if w.strip()]
    return "list", []


async def handle_badword_command(ctx: Any, args: Any, raw: str) -> None:
    """喵bad：增删查、开关、清空。"""
    from .admin_commands import _int_gid, _reply

    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "违禁词是按群存的，这个只能在群里用呢。")
        return
    store = get_badword_store(_cfg_path(_cfg_of_ctx(ctx)) or None)
    action, words = parse_badword_command(raw)
    operator = str(getattr(ctx, "user_id", "") or "")

    if action == "on":
        store.set_enabled(gid, True)
        await _reply(ctx, "违禁词检测打开了。")
        return
    if action == "off":
        store.set_enabled(gid, False)
        await _reply(ctx, "违禁词检测关了——词表还在，只是不再记警告。")
        return
    if action == "clear":
        removed = store.clear(gid)
        await _reply(ctx, f"清空了 {removed} 条违禁词。" if removed else "本群还没有违禁词。")
        return
    if action == "add":
        if not words:
            await _reply(ctx, "要加什么词呢？写法：喵bad add 傻逼")
            return
        lines = [store.add(gid, word, by=operator)[1] for word in words[:20]]
        await _reply(ctx, "\n".join(lines))
        return
    if action == "del":
        if not words:
            await _reply(ctx, "要删哪个词？写法：喵bad del 傻逼")
            return
        hits = [f"删掉了「{word}」" if store.remove(gid, word) else f"「{word}」本来就不在表里" for word in words[:20]]
        await _reply(ctx, "\n".join(hits))
        return

    # 默认：列表
    items = store.list_group(gid)
    state = "开" if store.enabled(gid) else "关"
    if not items:
        await _reply(
            f"本群违禁词表是空的（检测{state}）。\n"
            "加词：喵bad add 傻逼\n一次加多个用空格隔开。"
        )
        return
    await _reply(ctx, f"本群违禁词 {len(items)} 条（检测{state}）：\n" + "、".join(items[:60]))


def _is_exempt(ctx: Any) -> bool:
    """主人和（开了开关的）群管不受违禁词管。

    不豁免的话，主人自己在群里吐槽一句就被自己禁言了——最难排查的那种事故。
    """
    try:
        from .admin_commands import group_role_admin
        from .agent.permissions import PermissionService

        return bool(PermissionService().is_admin(ctx) or group_role_admin(ctx))
    except Exception:  # noqa: BLE001 — 判定失败按「不豁免」，宁可管严
        return False


def badword_guard(ctx: Any) -> bool:
    """命令 guard：这条消息踩中违禁词才让本命令接管。

    用「匹配任意文本 + guard 判定」而不是动态正则：词表随时在改，
    动态正则意味着每次改词都要重注册命令，重注册中途出异常整张命令表就废了。
    """
    group_id = str(getattr(ctx, "group_id", "") or "")
    user_id = str(getattr(ctx, "user_id", "") or "")
    if not group_id or not user_id:
        return False  # 私聊不管
    if _is_exempt(ctx):
        return False
    # 带「喵」的是管理指令，不该被违禁词拦下来
    text = str(getattr(ctx, "plain_text", "") or "")
    if text.lstrip().startswith("喵"):
        return False
    store = get_badword_store(_cfg_path(_cfg_of_ctx(ctx)) or None)
    if not store.enabled(group_id):
        return False
    return bool(store.hit(group_id, text))


async def badword_handler(ctx: Any, match: Any) -> None:
    """踩中违禁词：撤回 + 记一次警告，满三次禁言。"""
    from .admin_commands import _bot_event, _call, _reply, _self_id
    from .admin_commands_extra import WARN_LIMIT, WARN_AUTO_MUTE_SECONDS, _warn_store
    from .observability.logging import log_event

    gid = str(getattr(ctx, "group_id", "") or "")
    uid = str(getattr(ctx, "user_id", "") or "")
    text = str(getattr(ctx, "plain_text", "") or "")
    cfg = _cfg_of_ctx(ctx)
    store = get_badword_store(_cfg_path(cfg) or None)
    word = store.hit(gid, text)
    if not word:
        return

    bot, event = _bot_event(ctx)
    # 先撤回：晚一步撤回，群里已经有人看见了
    message_id = getattr(event, "message_id", None)
    if bot is not None and message_id is not None:
        ok, _err = await _call(
            bot, "delete_msg",
            message_id=int(message_id) if str(message_id).isdigit() else message_id,
        )
        # 撤回失败是常事（她不是管理员 / 超过两分钟），不影响记警告
        log_event("badword.recall", target=uid, ok=ok)

    warns = _warn_store(cfg)
    count = warns.add(gid, uid, reason=f"违禁词：{word}", by=_self_id(ctx))
    log_event(
        "badword.hit",
        level="warning",
        session_id=str(getattr(ctx, "session_id", "") or "") or None,
        actor=uid,
        word=word,
        count=count,
    )

    if count >= WARN_LIMIT:
        ok, err = await _call(
            bot, "set_group_ban", group_id=int(gid), user_id=int(uid),
            duration=WARN_AUTO_MUTE_SECONDS,
        )
        if ok:
            warns.clear(gid, uid)
            await _reply(
                ctx,
                f"第 {count} 次了，{uid} 先安静 {_fmt_span(WARN_AUTO_MUTE_SECONDS)}。",
            )
            return
        await _reply(ctx, f"第 {count} 次警告（自动禁言没成功：{err}）。")
        return

    await _reply(ctx, f"这条里有「{word}」，{uid} 记一次警告（{count}/{WARN_LIMIT}）。")


def _fmt_span(seconds: int) -> str:
    """把秒数说成「1 天 23 小时 59 分」这种人话。

    不能用 admin_commands._fmt_duration：那个只处理整数倍（1 天 / 2 小时），
    172740 秒会原样吐「172740 秒」，群里没人看得懂。
    """
    seconds = int(seconds)
    days, rest = divmod(max(0, seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, _ = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分")
    return " ".join(parts) or f"{seconds} 秒"


__all__ = [
    "BadwordStore",
    "MAX_WORD_LEN",
    "MAX_WORDS_PER_GROUP",
    "badword_guard",
    "badword_handler",
    "default_badwords_path",
    "get_badword_store",
    "handle_badword_command",
    "parse_badword_command",
]
