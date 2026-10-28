"""群词库：管理员挂关键词自动回复（喵/词库）。

为什么现在才做，以及它为什么必须**完全匹配**：

群里如果对「包含触发词」也回复，一句「今天天气不错」里只要有个「天气」，
她就会插一句预设答案，把正常聊天打断得莫名其妙。所以默认只有整句等于
触发词才回；管理员想放宽，可以在触发词末尾加 *（表示前缀包含）。

它和她的 AI 回复是什么关系：
词库命中就**直接回预设文本并结束**，不进 LLM。这是刻意的优先级设计——
管理员写死一句话，意思就是「这句话不要再让模型发挥了」。
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

MAX_TRIGGER = 40
MAX_REPLY = 300
#: 一个群最多挂多少条：没有上限的话，某人可以灌一万条把文件读爆。
MAX_ITEMS_PER_GROUP = 200

_TIMER_HINT = re.compile(r"^\s*(加|添加|新增|add)\s+(.+?)\s*=\s*(.+?)\s*$", re.DOTALL)
_DEL_HINT = re.compile(r"^\s*(删|删除|移除|del)\s+(.+?)\s*$", re.DOTALL)


def default_words_path(project_root: str | Path | None = None) -> Path:
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[3]
    return root / "data" / "dl_senpai" / "admin" / "words.json"


class WordStore:
    """群词库：{群号: {"enabled": bool, "items": {触发词: {reply, by, at}}}}。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._cache: dict[str, Any] | None = None
        self._mtime: float | None = None

    def _load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if self._cache is not None and self._mtime == mtime:
            return self._cache
        data: dict[str, Any] = {"version": 1, "groups": {}}
        if mtime is not None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("groups"), dict):
                    data = {"version": 1, "groups": loaded["groups"]}
            except (OSError, json.JSONDecodeError):
                logger.warning(f"dl_senpai words: failed to read {self.path}")
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
            bucket = {"enabled": True, "items": {}}
            groups[gid] = bucket
        if not isinstance(bucket.get("items"), dict):
            bucket["items"] = {}
        return bucket

    def enabled(self, group_id: int | str) -> bool:
        bucket = self._bucket(self._load(), group_id, create=False)
        return bool(bucket.get("enabled", True)) if bucket else True

    def set_enabled(self, group_id: int | str, value: bool) -> None:
        data = self._load()
        bucket = self._bucket(data, group_id, create=True)
        bucket["enabled"] = bool(value)
        self._save(data)

    def items(self, group_id: int | str) -> dict[str, dict[str, Any]]:
        bucket = self._bucket(self._load(), group_id, create=False)
        raw = bucket.get("items") if bucket else {}
        return {
            str(key): dict(value)
            for key, value in (raw or {}).items()
            if isinstance(value, dict)
        }

    def add(self, group_id: int | str, trigger: str, reply: str, *, by: str = "") -> tuple[bool, str]:
        """加一条；返回 (是否成功, 失败原因)。"""
        key = (trigger or "").strip()
        body = (reply or "").strip()
        if not key:
            return False, "触发词不能是空的。"
        if not body:
            return False, "回复内容不能是空的。"
        if len(key) > MAX_TRIGGER:
            return False, f"触发词太长了（上限 {MAX_TRIGGER} 字）。"
        if len(body) > MAX_REPLY:
            return False, f"回复太长了（上限 {MAX_REPLY} 字）。"
        data = self._load()
        bucket = self._bucket(data, group_id, create=True)
        items = bucket["items"]
        if key not in items and len(items) >= MAX_ITEMS_PER_GROUP:
            return False, f"这个群词库满了（上限 {MAX_ITEMS_PER_GROUP} 条）。"
        items[key] = {"reply": body, "by": str(by or ""), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._save(data)
        return True, ""

    def remove(self, group_id: int | str, trigger: str) -> bool:
        data = self._load()
        bucket = self._bucket(data, group_id, create=False)
        items = bucket.get("items") if bucket else {}
        removed = items.pop((trigger or "").strip(), None) is not None
        if removed:
            self._save(data)
        return removed

    def clear(self, group_id: int | str) -> int:
        data = self._load()
        bucket = self._bucket(data, group_id, create=False)
        items = bucket.get("items") if bucket else {}
        count = len(items)
        if count:
            bucket["items"] = {}
            self._save(data)
        return count

    def lookup(self, group_id: int | str, text: str) -> str:
        """按整句命中查回复；触发词以 * 结尾时做前缀匹配。"""
        sentence = (text or "").strip()
        if not sentence:
            return ""
        items = self.items(group_id)
        exact = items.get(sentence)
        if isinstance(exact, dict):
            return str(exact.get("reply") or "")
        for key, value in items.items():
            if not key.endswith("*"):
                continue
            prefix = key[:-1].strip()
            if prefix and sentence.startswith(prefix):
                return str(value.get("reply") or "")
        return ""


_store: WordStore | None = None
_store_path: str | None = None


def get_word_store(path: str | Path | None = None) -> WordStore:
    global _store, _store_path
    if path is None:
        if _store is None:
            _store = WordStore(default_words_path())
            _store_path = str(_store.path)
        return _store
    resolved = str(Path(path))
    if _store is None or _store_path != resolved:
        _store = WordStore(resolved)
        _store_path = resolved
    return _store


def _cfg_path(cfg: Any) -> str:
    return str(getattr(cfg, "admin_words_path", "") or "").strip() if cfg is not None else ""


def _cfg_of_ctx(ctx: Any) -> Any:
    cfg = getattr(ctx, "config", None)
    if cfg is not None:
        return cfg
    try:
        from .config_runtime.runtime import get_config

        return get_config()
    except Exception:  # noqa: BLE001
        return None


def word_reply_for(ctx: Any) -> str:
    """这条消息命中词库就返回要回的话，否则返回空串。"""
    group_id = str(getattr(ctx, "group_id", "") or "")
    if not group_id:
        return ""
    cfg = _cfg_of_ctx(ctx)
    store = get_word_store(_cfg_path(cfg) or None)
    if not store.enabled(group_id):
        return ""
    return store.lookup(group_id, str(getattr(ctx, "plain_text", "") or ""))


def word_guard(ctx: Any) -> bool:
    """命令 guard：只有命中词库才让这条命令接管。

    为什么用「一个匹配任意文本的命令 + guard」而不是动态生成正则：
    词库随时会被改，动态正则意味着每次改词库都要重注册命令，
    而重注册过程中一旦出异常，整个命令表就废了。

    黑名单检查放在这里（而不是只放在 agent 阶段）：词库命令在命令阶段就
    把消息 consume 掉了，agent 阶段的拦截根本轮不到——实测「拉黑之后
    他发你好，词库照样回」。黑名单的语义是「她不理这个人」，词库回话
    同样是她的回应，必须一起挡住。
    """
    if _blacklisted(ctx):
        return False
    return bool(word_reply_for(ctx))


def _blacklisted(ctx: Any) -> bool:
    """这个人是不是在本群被拉黑了（存储坏了就当作没拉黑）。"""
    group_id = str(getattr(ctx, "group_id", "") or "")
    user_id = str(getattr(ctx, "user_id", "") or "")
    if not group_id or not user_id:
        return False
    try:
        from .admin_control import is_blacklisted

        return is_blacklisted(group_id, user_id, cfg=_cfg_of_ctx(ctx))
    except Exception:  # noqa: BLE001
        return False


async def word_reply_handler(ctx: Any, match: Any) -> None:
    reply = word_reply_for(ctx)
    if not reply:
        return
    bot = getattr(ctx, "bot", None)
    event = getattr(ctx, "raw_event", None)
    if bot is None or event is None:
        return
    try:
        from .handlers import _reply as send_reply

        await send_reply(bot, event, reply)
    except Exception as exc:  # noqa: BLE001 — 词库回复失败不该影响别的
        logger.warning(f"dl_senpai words: reply failed {type(exc).__name__}: {exc}")
        return
    try:
        from .observability.logging import log_event

        log_event(
            "words.replied",
            session_id=str(getattr(ctx, "session_id", "") or "") or None,
            trigger=str(getattr(ctx, "plain_text", "") or "")[:40],
        )
    except Exception:  # noqa: BLE001
        pass


def parse_word_command(raw: str) -> tuple[str, str, str]:
    """解析「喵/词库」的参数：返回 (子命令, 触发词, 回复)。

    认得三种：
        加 你好=你好呀     → (add, 你好, 你好呀)
        删 你好            → (del, 你好, )
        开 / 关 / 列表      → (on/off/list, , )
    """
    text = (raw or "").strip()
    if not text:
        return "list", "", ""
    if text in ("开", "打开", "启用", "on"):
        return "on", "", ""
    if text in ("关", "关闭", "停用", "off"):
        return "off", "", ""
    if text in ("列表", "清空", "list", "清空全部"):
        return ("clear" if text == "清空" else "list"), "", ""
    added = _TIMER_HINT.match(text)
    if added:
        return "add", added.group(2).strip(), added.group(3).strip()
    deleted = _DEL_HINT.match(text)
    if deleted:
        return "del", deleted.group(2).strip(), ""
    # 不给子命令但写成 a=b 也当新增：管理员最常直接发「喵/词库 你好=你好呀」
    if "=" in text:
        trigger, _, reply = text.partition("=")
        return "add", trigger.strip(), reply.strip()
    return "list", "", ""


async def handle_word_command(ctx: Any, args: Any, raw: str) -> None:
    """喵/词库：增删查，以及本群开关。"""
    from .admin_commands import _int_gid, _reply

    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "词库是按群存的，这个只能在群里用呢。")
        return
    cfg = _cfg_of_ctx(ctx)
    store = get_word_store(_cfg_path(cfg) or None)
    action, trigger, reply = parse_word_command(raw)
    operator = str(getattr(ctx, "user_id", "") or "")

    if action == "add":
        ok, why = store.add(gid, trigger, reply, by=operator)
        await _reply(
            ctx,
            f"记下了：说「{trigger}」我就回那句。" if ok else f"没加成：{why}",
        )
        return
    if action == "del":
        removed = store.remove(gid, trigger)
        await _reply(ctx, f"删掉了「{trigger}」。" if removed else f"词库里没有「{trigger}」。")
        return
    if action == "clear":
        count = store.clear(gid)
        await _reply(ctx, f"清空了本群词库（{count} 条）。")
        return
    if action == "on":
        store.set_enabled(gid, True)
        await _reply(ctx, "词库打开了。")
        return
    if action == "off":
        store.set_enabled(gid, False)
        await _reply(ctx, "词库关掉了（我不会再接那些关键词）。")
        return

    items = store.items(gid)
    state = "开" if store.enabled(gid) else "关"
    if not items:
        await _reply(ctx, f"本群词库是空的（当前状态：{state}）。")
        return
    lines = [f"本群词库 {len(items)} 条（当前状态：{state}）："]
    for key, value in list(items.items())[:30]:
        lines.append(f"  {key} → {str(value.get('reply') or '')[:30]}")
    if len(items) > 30:
        lines.append(f"（还有 {len(items) - 30} 条没列出来）")
    await _reply(ctx, "\n".join(lines))


__all__ = [
    "WordStore",
    "default_words_path",
    "get_word_store",
    "parse_word_command",
    "handle_word_command",
    "word_guard",
    "word_reply_handler",
    "word_reply_for",
]
