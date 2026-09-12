from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from nonebot import logger

from .config import PluginConfig, get_config
from .session_fs import session_id_to_fs_key


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class DrawRecord:
    id: str
    prompt: str
    scene: str = ""
    user_request: str = ""
    source_user: str = ""
    source_user_id: str = ""
    local_path: str = ""
    model: str = ""
    size: str = ""
    created_at: str = ""


class DrawStore:
    """按 session 归档学姐生成的图片，供管理端浏览。"""

    def __init__(
        self,
        draw_dir: str | Path,
        *,
        max_per_session: int = 200,
    ) -> None:
        self.draw_dir = Path(draw_dir)
        self.draw_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir = self.draw_dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.max_per_session = max(10, max_per_session)
        self._lock = Lock()

    def _index_path(self, session_id: str) -> Path:
        safe = session_id_to_fs_key(session_id)
        return self.draw_dir / f"{safe}.json"

    def _load_raw(self, session_id: str) -> list[dict[str, Any]]:
        path = self._index_path(session_id)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []

    def _save_raw(self, session_id: str, items: list[dict[str, Any]]) -> None:
        path = self._index_path(session_id)
        trimmed = items[-self.max_per_session :]
        path.write_text(
            json.dumps(trimmed, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def archive_from_cache(
        self,
        session_id: str,
        *,
        cache_path: Path,
        prompt: str,
        scene: str = "",
        user_request: str = "",
        source_user: str = "",
        source_user_id: str = "",
        model: str = "",
        size: str = "",
    ) -> Path | None:
        if not session_id or not cache_path.is_file():
            return None
        record_id = uuid.uuid4().hex[:16]
        target = self.files_dir / f"{record_id}.png"
        try:
            shutil.copy2(cache_path, target)
        except OSError as exc:
            logger.warning(f"dl_senpai draw archive copy failed: {exc}")
            return None

        record = DrawRecord(
            id=record_id,
            prompt=(prompt or "").strip(),
            scene=(scene or "").strip(),
            user_request=(user_request or "").strip()[:500],
            source_user=(source_user or "").strip(),
            source_user_id=(source_user_id or "").strip(),
            local_path=str(target.resolve()),
            model=(model or "").strip(),
            size=(size or "").strip(),
            created_at=_utc_now(),
        )
        with self._lock:
            items = self._load_raw(session_id)
            items.append(asdict(record))
            self._save_raw(session_id, items)
        logger.info(
            f"dl_senpai draw archived session={session_id} id={record_id} "
            f"bytes={target.stat().st_size}"
        )
        return target


_draw_store: DrawStore | None = None


def get_draw_store(config: PluginConfig | None = None) -> DrawStore:
    global _draw_store
    cfg = config or get_config()
    if _draw_store is None:
        _draw_store = DrawStore(
            cfg.draw_dir,
            max_per_session=max(10, cfg.draw_max_archive),
        )
    return _draw_store
