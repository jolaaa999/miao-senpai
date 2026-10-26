"""她自己的相册：她自己挑的图，和她自己写的理由。

跟 draw_store 的区别（这条最容易被搞混）
----------------------------------------
draw_store 是**台账**：谁在什么时候让她画了什么，审计用。
相册是**她的**：她从看过的图里挑了几张留下，还写了一句为什么。

原版那句话点明了全部意义：**意义不在「图」，在于「她挑」**。
你给她看一百张，她自己挑了三张——那三张就是她的了。
所以这里的接口只有「收藏」和「看看我的」，**没有删除**：
删是管理员干的活，给她删除权等于给她一键销毁自己记忆的权利。

存储
----
    数据目录/gallery/index.jsonl   一行一条（人能直接看、能 grep）
    数据目录/gallery/files/        收藏的图片副本

为什么用 jsonl 而不是数据库：作者的原始建议，也是这里唯一合理的选择——
你能双击打开看、能手改、备份就是一个文件。
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from nonebot import logger

__all__ = [
    "GalleryItem",
    "build_gallery_brief",
    "gallery_root",
    "keep_picture",
    "list_items",
    "parse_keep_marker",
]

# 收藏理由的长度上限：印象要短，长了就变成小作文
_MAX_REASON_CHARS = 40


class GalleryItem(dict):
    """一条收藏记录（dict 子类，方便直接喂给 json）。"""

    @property
    def reason(self) -> str:
        return str(self.get("reason") or "")

    @property
    def path(self) -> str:
        return str(self.get("path") or "")


def gallery_root(cfg: Any) -> Path:
    return Path(str(getattr(cfg, "gallery_dir", "data/dl_senpai/gallery")))


def _index_path(cfg: Any) -> Path:
    return gallery_root(cfg) / "index.jsonl"


def _files_dir(cfg: Any) -> Path:
    return gallery_root(cfg) / "files"


def _allowed_roots(cfg: Any) -> list[Path]:
    """允许收藏的图片来源。

    只认「她自己的工作产物」所在目录：生成图目录、沙箱工作区。
    为什么不放开到任意路径：那等于给了一个「把系统里任何文件复制进相册
    再发出去」的通道，绕过 file_tools 的沙箱与权限判定。
    """
    roots: list[Path] = []
    for key, default in (
        # 键名必须与 config.py 里的一致，写错会静默退回默认路径
        ("draw_dir", "data/dl_senpai/draw"),
        ("agent_sandbox_dir", "data/dl_senpai/workspace"),
        ("image_dir", "data/dl_senpai/images"),
    ):
        raw = getattr(cfg, key, None) or default
        roots.append(Path(str(raw)))
    return roots


def _resolve_source(cfg: Any, raw_path: str) -> Path | None:
    candidate = Path(str(raw_path))
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    for root in _allowed_roots(cfg):
        try:
            root_resolved = root.resolve()
        except OSError:
            continue
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved if resolved.exists() else None
    return None


def keep_picture(cfg: Any, source: str, reason: str, *, actor: str = "") -> GalleryItem | None:
    """收藏一张图。来源必须在她自己的工作目录里，理由会被截断到 40 字。"""
    src = _resolve_source(cfg, source)
    if src is None:
        logger.info(f"dl_senpai gallery rejected source={source!r} actor={actor}")
        return None
    reason_text = " ".join(str(reason or "").split())[:_MAX_REASON_CHARS]

    dest_dir = _files_dir(cfg)
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix or ".png"
    dest = dest_dir / f"{int(time.time())}-{uuid.uuid4().hex[:6]}{suffix}"
    try:
        shutil.copyfile(src, dest)
    except OSError as exc:
        logger.warning(f"dl_senpai gallery copy failed: {exc}")
        return None

    item = GalleryItem(
        id=uuid.uuid4().hex[:12],
        path=str(dest),
        source=str(src),
        reason=reason_text,
        at=time.time(),
        actor=str(actor or ""),
    )
    index = _index_path(cfg)
    index.parent.mkdir(parents=True, exist_ok=True)
    try:
        with index.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning(f"dl_senpai gallery index append failed: {exc}")
        return None

    _trim(cfg)
    logger.info(f"dl_senpai gallery kept reason={reason_text!r} actor={actor}")
    return item


def _trim(cfg: Any) -> None:
    """超过上限就丢最老的记录行（文件本身留着，避免删到正在发的图）。"""
    limit = int(getattr(cfg, "gallery_max_items", 200) or 200)
    path = _index_path(cfg)
    try:
        rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return
    if len(rows) <= limit:
        return
    keep = rows[-limit:]
    try:
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except OSError:
        pass


def list_items(cfg: Any, limit: int = 5) -> list[GalleryItem]:
    path = _index_path(cfg)
    if not path.exists():
        return []
    items: list[GalleryItem] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                items.append(GalleryItem(data))
    except OSError:
        return []
    items.sort(key=lambda x: float(x.get("at") or 0))
    return items[-max(1, limit):]


def build_gallery_brief(cfg: Any, n: int = 3) -> str:
    """拼一段「你自己收藏过的图」，注入提示词。"""
    if not bool(getattr(cfg, "gallery_enable", False)):
        return ""
    items = list_items(cfg, n)
    if not items:
        return ""
    lines = []
    for item in items:
        stamp = time.strftime("%m-%d", time.localtime(float(item.get("at") or 0)))
        lines.append(f"- {stamp}「{item.reason or '（没写理由）'}」（{item.path}）")
    return (
        "这些是你自己留下来、还写过理由的图（你的相册，不是别人的收藏夹）：\n"
        + "\n".join(lines)
        + "\n提到它们的时候按你的记性说，不要背清单。"
    )


def parse_keep_marker(text: str) -> tuple[str, list[dict[str, str]]]:
    """解析 <<<KEEP 路径 | 理由>>>。返回 (清洗后的正文, 收藏请求列表)。"""
    import re as _re

    pattern = _re.compile(r"<<<KEEP\s+([^>|\n]+?)\s*(?:\|\s*([^>\n]*?))?\s*>>>", _re.IGNORECASE)
    requests: list[dict[str, str]] = []
    for match in pattern.finditer(text or ""):
        requests.append({"path": (match.group(1) or "").strip(), "reason": (match.group(2) or "").strip()})
    cleaned = pattern.sub("", text or "") if requests else (text or "")
    return cleaned, requests
