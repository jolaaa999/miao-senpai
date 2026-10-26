"""每轮才需要的那一小撮动态上下文。

拼提示词的地方已经有一堆 brief（热梗、印象、内里状态……），这里再单开一个
模块，是因为下面这三样都有同一个特点：**它们只在特定时刻存在，而且只该
说一次**。塞进 persona 的常量里会变成常驻噪音，塞进调用方又要在好几处重复。

现在装三样：

1. 断线补课（backfill.pending）——取走即删，说一次就够；
2. 上一条没发出去（send_fail）——原版踩过的坑：通道卡了 23 分钟，
   她那边正常生成，一条都没发出去，而她自己不知道，还以为在聊天。
   修好之后第一件事就是告诉她「你刚才说的没发出去」；
3. 最近的开场白（openers）——固定问候听三天就能背下来，所以要提醒她换个说法。

全部是「有就加、没有就一个字都不加」：关掉时拼出来的 prompt 与老版本一致。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from nonebot import logger

__all__ = [
    "build_context_extras",
    "note_outgoing",
    "note_send_failure",
    "recent_openers",
    "take_send_failure",
]

# 发送失败的事实只保留半小时：超过这个时间的「你刚才没发出去」会让模型
# 莫名其妙地道歉，比不说更糟。
SEND_FAIL_TTL_SEC = 1800
# 开场白记忆的容量。只留最近 8 条：够看出「又在用同一句」，又不至于占地方。
_MAX_OPENERS = 8
# 判定「开场白重复」时取的前缀长度。太短（3 字）会误伤，太长就没用了。
_OPENER_CHARS = 8


def _state_dir(cfg: Any = None) -> Path:
    raw = getattr(cfg, "context_extra_dir", "") if cfg is not None else ""
    if raw:
        return Path(str(raw))
    base = Path(str(getattr(cfg, "memory_dir", "data/dl_senpai/memory"))) if cfg is not None else Path("data/dl_senpai/memory")
    return base.parent / "context"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 读不到就当没有，这类状态丢了不影响功能
        return default


def _write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.debug(f"dl_senpai context_extra write failed: {exc}")


# ------------------------------------------------------------------ 发送失败


def note_send_failure(reason: str, *, session_id: str = "") -> None:
    """记下「这一轮生成的话没发出去」。由 reply_send 在失败/被闸门拦下时调用。"""
    path = _state_dir() / "send_fail.json"
    _write_json(
        path,
        {"reason": str(reason or "")[:120], "at": time.time(), "session_id": str(session_id or "")},
    )


def take_send_failure(cfg: Any = None) -> str:
    """取走发送失败的事实（读后删）。过期的不取。"""
    path = _state_dir(cfg) / "send_fail.json"
    if not path.exists():
        return ""
    data = _read_json(path, {})
    try:
        path.unlink()
    except OSError:
        pass
    if not isinstance(data, dict):
        return ""
    age = time.time() - float(data.get("at") or 0)
    if age > SEND_FAIL_TTL_SEC:
        return ""
    reason = str(data.get("reason") or "发送通道出错")
    return (
        f"【重要事实】你上一条回复**没有发出去**（{reason}，约 {int(age // 60)} 分钟前）。"
        "对方没看到那句话，不是你被无视了。如果要提，就说一句「刚才那条没发出去」；"
        "不要假装对方看过，也不要反复道歉。"
    )


# ------------------------------------------------------------------ 开场白


def note_outgoing(text: str, *, session_id: str = "") -> None:
    """记下一条刚发出去的话的前缀，用来提醒她别重复同一个开场。"""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) < _OPENER_CHARS:
        return
    path = _state_dir() / "openers.json"
    rows = _read_json(path, [])
    if not isinstance(rows, list):
        rows = []
    rows.append(
        {
            "opener": cleaned[:_OPENER_CHARS],
            "at": time.time(),
            "session_id": str(session_id or ""),
        }
    )
    _write_json(path, rows[-_MAX_OPENERS:])


def recent_openers(cfg: Any = None, n: int = 5) -> list[str]:
    rows = _read_json(_state_dir(cfg) / "openers.json", [])
    if not isinstance(rows, list):
        return []
    texts = [str(row.get("opener") or "") for row in rows if isinstance(row, dict)]
    return [t for t in texts if t][-n:]


# ------------------------------------------------------------------ 汇总


def build_context_extras(cfg: Any = None) -> str:
    """把当前存在的那几条拼成一段。没有任何内容时返回空串。"""
    if cfg is None:
        try:
            from .config import get_config

            cfg = get_config()
        except Exception:  # noqa: BLE001
            return ""

    blocks: list[str] = []

    failure = take_send_failure(cfg)
    if failure:
        blocks.append(failure)

    if bool(getattr(cfg, "backfill_enable", False)):
        try:
            from .backfill import take_pending_brief

            patch = take_pending_brief(cfg)
            if patch:
                blocks.append(patch)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"dl_senpai backfill brief skipped: {exc}")

    if bool(getattr(cfg, "gallery_enable", False)):
        try:
            from .gallery import build_gallery_brief

            gallery = build_gallery_brief(cfg, int(getattr(cfg, "gallery_context_n", 3) or 3))
            if gallery:
                blocks.append(gallery)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"dl_senpai gallery brief skipped: {exc}")

    if bool(getattr(cfg, "greeting_dedupe_enable", True)):
        openers = recent_openers(cfg, 6)
        # 只在**真的重复**时才提：每轮都挂一句「别重复开场白」本身就是噪音，
        # 而且会让模型开始琢磨「我在被监听吗」。判定看前 4 个字是否撞车。
        heads = [t[:4] for t in openers if len(t) >= 4]
        repeated = [h for h in set(heads) if heads.count(h) >= 2]
        if repeated:
            blocks.append(
                "你最近几次是这么开口的："
                + "、".join(f"「{t}」" for t in openers[-4:])
                + "。其中「" + "」「".join(repeated) + "」用重复了，"
                "这次换个说法，或者直接说事。"
            )

    if bool(getattr(cfg, "impression_refresh_enable", True)):
        stale_days = _impression_stale_days(cfg)
        limit = int(getattr(cfg, "impression_refresh_days", 14) or 14)
        if stale_days >= limit:
            blocks.append(
                f"你对这个人的印象已经 {stale_days} 天没更新过了。人是会变的——"
                "如果这次聊天里发现他跟印象里写的不太一样（说话方式、在乎的东西、"
                "最近在忙什么），用 <<<IMPRESSION>>> 把那一条改掉；没变化就不用动。"
            )

    return "\n\n".join(blocks)


def _impression_stale_days(cfg: Any) -> int:
    """印象目录里最新一次写入距今多少天。

    为什么看 mtime 而不是给每个人记一个 refresh_at：印象文件本来就是
    「她一更新就重写」的，mtime 天然就是这个时间点。多存一份时间戳，
    就多一处会和真实情况不一致的地方。
    """
    raw = getattr(cfg, "person_memory_dir", None) or "data/dl_senpai/person_memory"
    try:
        base = Path(str(raw))
        if not base.exists():
            return 0
        newest = 0.0
        for item in base.rglob("*.json"):
            try:
                newest = max(newest, item.stat().st_mtime)
            except OSError:
                continue
        if not newest:
            return 0
        return int((time.time() - newest) // 86400)
    except Exception:  # noqa: BLE001
        return 0
