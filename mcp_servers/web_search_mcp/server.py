"""Cursor / Claude 可用的联网检索 MCP（stdio）。

复用 dl_senpai.web_search。
启动：python scripts/run-mcp-web-search.py
配置见 docs/web-search-mcp.md
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 保证可 import dl_senpai（仓库根或 editable install）
_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _ROOT / "src" / "plugins"
for p in (_PLUGIN_DIR, _ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from mcp_servers._env import load_repo_env  # noqa: E402
from dl_senpai.web_search import (  # noqa: E402
    WebSearcher,
    dumps_hits,
    format_search_for_prompt,
)

load_repo_env(root=_ROOT)

mcp = FastMCP("web_search_mcp")


def _searcher() -> WebSearcher:
    return WebSearcher(
        provider=os.getenv("DL_SENPAI_SEARCH_PROVIDER", "auto"),
        max_results=int(os.getenv("DL_SENPAI_SEARCH_MAX_RESULTS", "5")),
        timeout=float(os.getenv("DL_SENPAI_SEARCH_TIMEOUT", "12")),
        region=os.getenv("DL_SENPAI_SEARCH_REGION", "zh-cn"),
        searxng_url=os.getenv("DL_SENPAI_SEARCH_SEARXNG_URL", ""),
        tavily_api_key=os.getenv("DL_SENPAI_SEARCH_TAVILY_API_KEY", ""),
    )


def _now_label() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")


@mcp.tool(
    name="web_search_realtime",
    annotations={
        "title": "实时联网搜索",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def web_search_realtime(query: str, limit: int = 5) -> str:
    """实时联网搜索（带检索时间戳，适合新闻/价格/版本等时效问题）。

    Args:
        query: 搜索词（中文或英文均可）
        limit: 返回条数上限（1-10）
    """
    q = (query or "").strip()
    if not q:
        return f"【检索时间】{_now_label()}\n（查询为空）"
    lim = max(1, min(10, int(limit)))
    searcher = _searcher()
    searcher.max_results = lim
    hits = await searcher.search(q)
    body = format_search_for_prompt(hits, query=q, max_chars=2400)
    return f"【检索时间】{_now_label()}\n{body}"


@mcp.tool(
    name="web_search",
    annotations={
        "title": "联网搜索",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def web_search(query: str, limit: int = 5) -> str:
    """按关键词联网搜索，返回标题 / 摘要 / 链接（JSON）。

    Args:
        query: 搜索词（中文或英文均可）
        limit: 返回条数上限（1-10）
    """
    q = (query or "").strip()
    if not q:
        return "[]"
    lim = max(1, min(10, int(limit)))
    searcher = _searcher()
    searcher.max_results = lim
    hits = await searcher.search(q)
    return dumps_hits(hits)


@mcp.tool(
    name="web_search_brief",
    annotations={
        "title": "联网搜索摘要",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def web_search_brief(query: str, limit: int = 5) -> str:
    """联网搜索并格式化为适合喂给模型的文本摘要。

    Args:
        query: 搜索词
        limit: 返回条数上限（1-10）
    """
    q = (query or "").strip()
    if not q:
        return "【联网检索结果】\n（查询为空）"
    lim = max(1, min(10, int(limit)))
    searcher = _searcher()
    searcher.max_results = lim
    hits = await searcher.search(q)
    return format_search_for_prompt(hits, query=q, max_chars=2400)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
