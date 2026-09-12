from __future__ import annotations

import shutil
import uuid
from pathlib import Path


def test_load_repo_env_sets_search_vars(monkeypatch) -> None:
    monkeypatch.delenv("DL_SENPAI_SEARCH_PROVIDER", raising=False)
    base = Path("data") / "_pytest_mcp_env" / uuid.uuid4().hex
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    try:
        (base / ".env").write_text(
            'DL_SENPAI_SEARCH_PROVIDER="tavily"\nDL_SENPAI_SEARCH_MAX_RESULTS=7\n',
            encoding="utf-8",
        )
        from mcp_servers._env import load_repo_env

        load_repo_env(root=base)
        import os

        assert os.environ.get("DL_SENPAI_SEARCH_PROVIDER") == "tavily"
        assert os.environ.get("DL_SENPAI_SEARCH_MAX_RESULTS") == "7"
    finally:
        shutil.rmtree(base, ignore_errors=True)
