"""多语言风格档案：对接微调模型 / 中转站 model id，控制台可切换。

真微调在仓库外完成（MirrorFlow / WeClone + LLaMA-Factory），
产出部署到 OpenAI 兼容端点后，在此登记为一条 style，学姐对话走对应 model。

数据：data/dl_senpai/speak_styles/styles.json
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DIR = _ROOT / "data" / "dl_senpai" / "speak_styles"
_FILE = "styles.json"

_BUILTIN_ID = "senpai_default"


@dataclass
class SpeakStyle:
    id: str
    name: str
    kind: str = "builtin"  # builtin | finetune | prompt
    enabled: bool = True
    # 空 = 跟随全局 .env OPENAI_* / Ollama
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    # 追加到 system prompt 的风格说明（prompt 型或微调后的轻提示）
    system_overlay: str = ""
    # 元数据
    source_person: str = ""  # 模仿对象备注
    qq_data_note: str = ""  # QQ 导出/训练备注
    trained_model_ref: str = ""  # 如 LoRA 名、方舟 cm-xxx
    notes: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SpeakStyle":
        return cls(
            id=str(raw.get("id") or "").strip(),
            name=str(raw.get("name") or "").strip() or str(raw.get("id") or "未命名"),
            kind=str(raw.get("kind") or "prompt").strip() or "prompt",
            enabled=bool(raw.get("enabled", True)),
            model=str(raw.get("model") or "").strip(),
            base_url=str(raw.get("base_url") or "").strip().rstrip("/"),
            api_key=str(raw.get("api_key") or "").strip(),
            system_overlay=str(raw.get("system_overlay") or "").strip(),
            source_person=str(raw.get("source_person") or "").strip(),
            qq_data_note=str(raw.get("qq_data_note") or "").strip(),
            trained_model_ref=str(raw.get("trained_model_ref") or "").strip(),
            notes=str(raw.get("notes") or "").strip(),
            updated_at=float(raw.get("updated_at") or 0.0),
        )


def _default_payload() -> dict[str, Any]:
    now = time.time()
    return {
        "active_id": _BUILTIN_ID,
        "styles": [
            SpeakStyle(
                id=_BUILTIN_ID,
                name="默认学姐",
                kind="builtin",
                enabled=True,
                system_overlay="",
                source_person="学姐人设",
                notes="使用 .env 全局模型 + persona.py，不切换微调权重",
                updated_at=now,
            ).to_dict()
        ],
    }


class SpeakStyleStore:
    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.dir = Path(data_dir) if data_dir else _DEFAULT_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / _FILE
        self._mtime = 0.0
        self._cache: dict[str, Any] | None = None

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            payload = _default_payload()
            self._write(payload)
            return payload
        mtime = self.path.stat().st_mtime
        if self._cache is not None and mtime == self._mtime:
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = _default_payload()
        if not isinstance(data, dict):
            data = _default_payload()
        data.setdefault("active_id", _BUILTIN_ID)
        styles = data.get("styles")
        if not isinstance(styles, list) or not styles:
            data = _default_payload()
        # 保证内置项存在
        ids = {str(s.get("id")) for s in data["styles"] if isinstance(s, dict)}
        if _BUILTIN_ID not in ids:
            data["styles"].insert(0, _default_payload()["styles"][0])
        self._cache = data
        self._mtime = mtime
        return data

    def _write(self, payload: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tmp.replace(self.path)
        self._cache = payload
        self._mtime = self.path.stat().st_mtime

    def list_styles(self) -> list[SpeakStyle]:
        with _LOCK:
            data = self._read()
            out: list[SpeakStyle] = []
            for row in data.get("styles") or []:
                if isinstance(row, dict) and row.get("id"):
                    out.append(SpeakStyle.from_dict(row))
            return out

    def get(self, style_id: str) -> SpeakStyle | None:
        sid = (style_id or "").strip()
        for s in self.list_styles():
            if s.id == sid:
                return s
        return None

    def active_id(self) -> str:
        with _LOCK:
            return str(self._read().get("active_id") or _BUILTIN_ID)

    def active(self) -> SpeakStyle:
        aid = self.active_id()
        s = self.get(aid)
        if s and s.enabled:
            return s
        builtin = self.get(_BUILTIN_ID)
        return builtin or SpeakStyle(id=_BUILTIN_ID, name="默认学姐", kind="builtin")

    def set_active(self, style_id: str) -> SpeakStyle:
        with _LOCK:
            data = self._read()
            sid = (style_id or "").strip()
            found = None
            for row in data.get("styles") or []:
                if isinstance(row, dict) and str(row.get("id")) == sid:
                    found = SpeakStyle.from_dict(row)
                    break
            if found is None:
                raise ValueError(f"未知风格: {sid}")
            if not found.enabled:
                raise ValueError(f"风格已禁用: {sid}")
            data["active_id"] = sid
            self._write(data)
            return found

    def upsert(self, style: SpeakStyle) -> SpeakStyle:
        with _LOCK:
            if not style.id:
                raise ValueError("id 不能为空")
            if style.id == _BUILTIN_ID:
                # 允许改显示名/备注，不允许删、不允许改成非 builtin 无模型跟随
                style.kind = "builtin"
            data = self._read()
            style.updated_at = time.time()
            rows = [r for r in (data.get("styles") or []) if isinstance(r, dict)]
            replaced = False
            for i, r in enumerate(rows):
                if str(r.get("id")) == style.id:
                    rows[i] = style.to_dict()
                    replaced = True
                    break
            if not replaced:
                rows.append(style.to_dict())
            data["styles"] = rows
            self._write(data)
            return style

    def delete(self, style_id: str) -> None:
        with _LOCK:
            sid = (style_id or "").strip()
            if sid == _BUILTIN_ID:
                raise ValueError("不能删除默认学姐风格")
            data = self._read()
            rows = [
                r
                for r in (data.get("styles") or [])
                if isinstance(r, dict) and str(r.get("id")) != sid
            ]
            data["styles"] = rows
            if str(data.get("active_id")) == sid:
                data["active_id"] = _BUILTIN_ID
            self._write(data)

    def snapshot(self) -> dict[str, Any]:
        active = self.active()
        return {
            "active_id": active.id,
            "active": active.to_dict(),
            "styles": [s.to_dict() for s in self.list_styles()],
            "path": str(self.path),
        }


_store: SpeakStyleStore | None = None


def get_speak_style_store(data_dir: str | Path | None = None) -> SpeakStyleStore:
    global _store
    if data_dir is not None:
        return SpeakStyleStore(data_dir)
    if _store is None:
        _store = SpeakStyleStore()
    return _store


def active_style_overlay() -> str:
    s = get_speak_style_store().active()
    text = (s.system_overlay or "").strip()
    if not text:
        return ""
    return (
        f"\n【当前语言风格：{s.name}】\n"
        f"{text}\n"
        "请优先按上述风格说话；仍保持学姐身份保密与安全底线。\n"
    )
