"""评论的节奏控制（不设条数上限，靠"像人"来防封）。

为什么不再按天卡死条数
----------------------
主人要的是"她想评论就评论"。硬性一天 N 条会让她憋着，
反而在能评论的时候一口气补上，更像机器人。

所以换成四条软约束，全部作用于"这一条该不该发"：
1. **最小间隔**：两条评论之间至少隔一会（默认 10 分钟）
2. **同帖只评一次**：同一篇帖子不重复出现
3. **文案不重复**：跟最近若干条相似度太高就换一句
4. **被挡就退避**：平台要验证时进冷却（在 heybox 层）

这三条都不是道德审查，是"别让自己看起来像脚本"。
留一份台账（数据文件），主人随时能翻她到底在别人帖子里说了什么。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

__all__ = [
    "check_can_comment",
    "handled_replies",
    "load_state",
    "mark_reply_handled",
    "record_comment",
    "recent_comments",
]

_DEFAULT_STATE = "data/dl_senpai/heybox_write_state.json"
# 每个平台一份台账：小黑盒的"评过哪些帖"不能拿来卡 B 站
_STATE_ATTR = {
    "heybox": "heybox_write_state_file",
    "bili": "bili_write_state_file",
}
_GAP_ATTR = {
    "heybox": "heybox_comment_min_gap_sec",
    "bili": "bili_comment_min_gap_sec",
}
# 回过哪些留言：同一条别人说的话只回一次，不然会变成"复读机"
_KEEP_HANDLED = 300
_KEEP_COMMENTS = 200
_KEEP_TEXTS = 20
_SIMILARITY_BLOCK = 0.72


def _path(cfg: Any, platform: str = "heybox") -> Path:
    attr = _STATE_ATTR.get(platform, "heybox_write_state_file")
    raw = str(getattr(cfg, attr, "") or "")
    if raw:
        return Path(raw)
    return Path(_DEFAULT_STATE.replace("heybox", platform))


def _bigrams(text: str) -> set[str]:
    cleaned = "".join(ch for ch in (text or "") if not ch.isspace())
    return {cleaned[i : i + 2] for i in range(max(0, len(cleaned) - 1))}


def _similar(a: str, b: str) -> float:
    """字符二元组 Jaccard。够用且不需要向量库。"""
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def load_state(cfg: Any, platform: str = "heybox") -> dict[str, Any]:
    path = _path(cfg, platform)
    if not path.exists():
        return {"last_comment_ts": 0.0, "comments": [], "texts": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"last_comment_ts": 0.0, "comments": [], "texts": []}
    if not isinstance(data, dict):
        return {"last_comment_ts": 0.0, "comments": [], "texts": []}
    data.setdefault("last_comment_ts", 0.0)
    data.setdefault("comments", [])
    data.setdefault("texts", [])
    return data


def _save_state(state: dict[str, Any], cfg: Any, platform: str = "heybox") -> None:
    path = _path(cfg, platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["comments"] = state.get("comments", [])[-_KEEP_COMMENTS:]
    state["texts"] = state.get("texts", [])[-_KEEP_TEXTS:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def recent_comments(cfg: Any, limit: int = 10, platform: str = "heybox") -> list[dict[str, Any]]:
    return list(load_state(cfg, platform).get("comments", []))[-limit:]


def handled_replies(cfg: Any, platform: str = "heybox") -> set[str]:
    """已经处理过的留言 id 集合（回过 / 决定不回都算处理过）。"""
    return {str(x) for x in load_state(cfg, platform).get("handled_replies", [])}


def mark_reply_handled(cfg: Any, comment_id: str, platform: str = "heybox") -> None:
    key = str(comment_id or "").strip()
    if not key:
        return
    state = load_state(cfg, platform)
    rows = [str(x) for x in state.get("handled_replies", []) if str(x) != key]
    rows.append(key)
    state["handled_replies"] = rows[-_KEEP_HANDLED:]
    _save_state(state, cfg, platform)


def check_can_comment(
    link_id: str,
    text: str,
    cfg: Any,
    *,
    allow_same_link: bool = False,
    platform: str = "heybox",
) -> str:
    """能不能发这条评论。返回空串=可以，否则是给她看的原因。

    allow_same_link=True 用在"回复别人留言"：同一篇帖子里对话是正常行为，
    但间隔和文案去重仍然要管住。
    """
    state = load_state(cfg, platform)
    gap = float(getattr(cfg, _GAP_ATTR.get(platform, "heybox_comment_min_gap_sec"), 600) or 0)
    last = float(state.get("last_comment_ts") or 0)
    if gap > 0 and last > 0:
        wait = gap - (time.time() - last)
        if wait > 0:
            minutes = max(1, int(wait // 60) + 1)
            return f"刚评过一条，隔 {minutes} 分钟再说（评太快像脚本）"

    key = str(link_id or "").strip()
    if not allow_same_link:
        for row in state.get("comments", []):
            if str(row.get("link_id") or "") == key:
                return "这篇已经评过了，同一篇不重复评"

    for previous in state.get("texts", []):
        if _similar(text, str(previous)) >= _SIMILARITY_BLOCK:
            return "这句跟你最近说过的话太像了，换一句"
    return ""


def record_comment(
    link_id: str, text: str, cfg: Any, *, title: str = "", platform: str = "heybox"
) -> None:
    """记一笔台账。主人随时能翻她到底在别人帖子里说了什么。"""
    state = load_state(cfg, platform)
    now = time.time()
    state["last_comment_ts"] = now
    state["comments"] = list(state.get("comments", [])) + [
        {"link_id": str(link_id), "text": text, "title": title, "ts": now}
    ]
    state["texts"] = list(state.get("texts", [])) + [text]
    _save_state(state, cfg, platform)
