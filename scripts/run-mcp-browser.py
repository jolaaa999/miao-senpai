#!/usr/bin/env python3
"""从任意 cwd 启动浏览器逛网站 MCP（供 Cursor .cursor/mcp.json 调用）。"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "src" / "plugins"
for p in (PLUGIN_DIR, ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from mcp_servers._env import load_repo_env  # noqa: E402

load_repo_env(root=ROOT)
runpy.run_path(str(ROOT / "mcp_servers" / "browser_mcp" / "server.py"), run_name="__main__")
