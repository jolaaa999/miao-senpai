"""从仓库根目录加载 .env（MCP 进程与 bot 共用变量名）。"""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_repo_env(*, root: Path | None = None) -> Path:
    """把 .env / .env.dev 写入 os.environ（不覆盖已有变量）。"""
    base = root or repo_root()
    for name in (".env", ".env.dev"):
        path = base / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, val = stripped.partition("=")
            key = key.strip()
            if not key:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
                val = val[1:-1]
            os.environ.setdefault(key, val)
    return base
