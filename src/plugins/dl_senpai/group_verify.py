"""新人进群认证：限时内 @学姐 说「认证」，超时踢出。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

_VERIFY_WORD_RE = re.compile(r"(认证|驗證|验证入群|入群认证)")


@dataclass
class PendingVerify:
    group_id: int
    user_id: int
    nickname: str = ""
    joined_at: float = 0.0
    deadline: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingVerify:
        return cls(
            group_id=int(data["group_id"]),
            user_id=int(data["user_id"]),
            nickname=str(data.get("nickname") or ""),
            joined_at=float(data.get("joined_at") or 0),
            deadline=float(data.get("deadline") or 0),
        )


def _key(group_id: int, user_id: int) -> str:
    return f"{group_id}:{user_id}"


class GroupVerifyStore:
    """待认证名单：内存 + 落盘，重启后可续期踢人。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "pending.json"
        self._lock = Lock()
        self._pending: dict[str, PendingVerify] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        now = time.time()
        for key, row in raw.items():
            if not isinstance(row, dict):
                continue
            try:
                item = PendingVerify.from_dict(row)
            except (KeyError, TypeError, ValueError):
                continue
            # 已过期的仍保留，启动后立刻踢；未过期续调度
            if item.deadline <= 0:
                continue
            self._pending[str(key)] = item
        # 清掉极端脏数据
        expired_gone = [k for k, v in self._pending.items() if v.deadline < now - 86400]
        for k in expired_gone:
            self._pending.pop(k, None)
        self._save()

    def _save(self) -> None:
        payload = {k: v.to_dict() for k, v in self._pending.items()}
        with self._lock:
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def is_pending(self, group_id: int, user_id: int) -> bool:
        return _key(group_id, user_id) in self._pending

    def get(self, group_id: int, user_id: int) -> PendingVerify | None:
        return self._pending.get(_key(group_id, user_id))

    def register(
        self,
        *,
        group_id: int,
        user_id: int,
        nickname: str,
        timeout_sec: int,
    ) -> PendingVerify:
        now = time.time()
        item = PendingVerify(
            group_id=group_id,
            user_id=user_id,
            nickname=(nickname or "").strip(),
            joined_at=now,
            deadline=now + max(30, int(timeout_sec)),
        )
        key = _key(group_id, user_id)
        self._cancel_task(key)
        self._pending[key] = item
        self._save()
        logger.info(
            f"dl_senpai verify pending group={group_id} user={user_id} "
            f"timeout={timeout_sec}s"
        )
        return item

    def mark_passed(self, group_id: int, user_id: int) -> PendingVerify | None:
        key = _key(group_id, user_id)
        item = self._pending.pop(key, None)
        self._cancel_task(key)
        if item is not None:
            self._save()
            logger.info(f"dl_senpai verify passed group={group_id} user={user_id}")
        return item

    def drop(self, group_id: int, user_id: int) -> None:
        key = _key(group_id, user_id)
        if key in self._pending:
            self._pending.pop(key, None)
            self._cancel_task(key)
            self._save()

    def all_pending(self) -> list[PendingVerify]:
        return list(self._pending.values())

    def _cancel_task(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    def schedule_kick(
        self,
        bot: Bot,
        item: PendingVerify,
        *,
        reject_add_friend: bool = False,
    ) -> None:
        key = _key(item.group_id, item.user_id)
        self._cancel_task(key)

        async def _runner() -> None:
            delay = max(0.0, item.deadline - time.time())
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            # 已被认证则不踢
            if not self.is_pending(item.group_id, item.user_id):
                return
            await kick_unverified(bot, item)
            self.drop(item.group_id, item.user_id)

        self._tasks[key] = asyncio.create_task(
            _runner(),
            name=f"dl_senpai_verify_{key}",
        )


def looks_like_verify_message(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(_VERIFY_WORD_RE.search(raw))


def build_verify_prompt(*, timeout_min: int) -> str:
    mins = max(1, int(timeout_min))
    return (
        f"进群先认证一下哦～请在 {mins} 分钟内 @学姐 并发送「认证」，"
        f"超时会被请出群聊，别忘啦。"
    )


def build_verify_pass_text(nickname: str) -> str:
    name = (nickname or "新同学").strip() or "新同学"
    return f"{name} 认证通过～欢迎留下玩，有问题再喊学姐。"


def build_verify_kick_notice(nickname: str) -> str:
    name = (nickname or "新同学").strip() or "新同学"
    return f"{name} 超时未认证，先请出群啦。想进的话重新加、记得认证～"


async def bot_can_kick(bot: Bot, group_id: int) -> bool:
    try:
        info = await bot.get_group_member_info(
            group_id=group_id,
            user_id=int(bot.self_id),
        )
    except Exception:  # noqa: BLE001
        return False
    return str(info.get("role") or "") in {"admin", "owner"}


async def kick_unverified(bot: Bot, item: PendingVerify) -> bool:
    if not await bot_can_kick(bot, item.group_id):
        logger.warning(
            f"dl_senpai verify kick skipped (not admin) "
            f"group={item.group_id} user={item.user_id}"
        )
        return False
    notice = build_verify_kick_notice(item.nickname or str(item.user_id))
    try:
        await bot.send_group_msg(
            group_id=item.group_id,
            message=notice,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            f"dl_senpai verify kick notice failed group={item.group_id}"
        )
    try:
        await bot.set_group_kick(
            group_id=item.group_id,
            user_id=item.user_id,
            reject_add_request=False,
        )
        logger.info(
            f"dl_senpai verify kicked group={item.group_id} user={item.user_id}"
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"dl_senpai verify kick failed group={item.group_id} "
            f"user={item.user_id}: {exc}"
        )
        return False


_store: GroupVerifyStore | None = None


def get_verify_store(data_dir: str | Path | None = None) -> GroupVerifyStore:
    """单例：必须复用同一 store，否则踢人任务与认证通过会脱节。"""
    global _store
    if _store is None:
        if data_dir is None:
            from .config import get_config

            data_dir = get_config().verify_dir
        _store = GroupVerifyStore(data_dir)
    return _store


def reset_verify_store() -> None:
    global _store
    _store = None


async def reschedule_pending_kicks(bot: Bot) -> int:
    """Bot 重连后，为磁盘上的待认证重新挂超时踢人任务。"""
    from .config import get_config

    cfg = get_config()
    if not cfg.verify_enable:
        return 0
    store = get_verify_store(cfg.verify_dir)
    count = 0
    for item in store.all_pending():
        if not cfg.is_verify_group(item.group_id):
            continue
        store.schedule_kick(bot, item)
        count += 1
    if count:
        logger.info(f"dl_senpai verify rescheduled pending={count}")
    return count
