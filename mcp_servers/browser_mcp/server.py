"""Cursor / Claude 可用的浏览器逛网站 MCP（stdio）。

复用 dl_senpai.browser_agent。
启动：python scripts/run-mcp-browser.py
配置见 docs/browser-mcp.md
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _ROOT / "src" / "plugins"
for p in (_PLUGIN_DIR, _ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from mcp_servers._env import load_repo_env  # noqa: E402
from dl_senpai.browser_agent import (  # noqa: E402
    BrowseTask,
    browser_configured,
    run_browse_tasks,
)
from dl_senpai.config import get_config  # noqa: E402

load_repo_env(root=_ROOT)

mcp = FastMCP("browser_mcp")


def _ensure_ready() -> str | None:
    if not browser_configured():
        return (
            "浏览器未就绪。请运行: pip install -e \".[browser,mcp,search]\" "
            "&& playwright install chromium"
        )
    return None


@mcp.tool(
    name="browser_screenshot",
    annotations={
        "title": "网页截图",
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    },
)
async def browser_screenshot(url: str) -> str:
    """打开网址并截图，返回本地图片路径。"""
    err = _ensure_ready()
    if err:
        return err
    result = await run_browse_tasks(
        [BrowseTask(kind="screenshot", url=(url or "").strip())],
        config=get_config(),
    )
    if not result.ok:
        return f"截图失败：{result.message}"
    paths = [str(p) for p in result.image_paths]
    return f"ok\npage={result.page_url}\nfiles:\n" + "\n".join(paths)


@mcp.tool(
    name="browser_search_taobao",
    annotations={
        "title": "淘宝搜索截图",
        "readOnlyHint": True,
        "openWorldHint": True,
    },
)
async def browser_search_taobao(query: str) -> str:
    """在淘宝搜索商品并截图搜索结果。"""
    err = _ensure_ready()
    if err:
        return err
    q = (query or "").strip()
    if not q:
        return "查询为空"
    result = await run_browse_tasks([BrowseTask(kind="taobao", query=q)], config=get_config())
    if not result.ok:
        return f"淘宝搜索失败：{result.message}"
    paths = [str(p) for p in result.image_paths]
    return f"ok\npage={result.page_url}\nfiles:\n" + "\n".join(paths)


@mcp.tool(
    name="browser_search_pixiv",
    annotations={
        "title": "Pixiv 搜索截图",
        "readOnlyHint": True,
        "openWorldHint": True,
    },
)
async def browser_search_pixiv(query: str) -> str:
    """在 Pixiv 按标签搜索并截图/抓取作品。"""
    err = _ensure_ready()
    if err:
        return err
    q = (query or "").strip()
    if not q:
        return "查询为空"
    result = await run_browse_tasks([BrowseTask(kind="pixiv", query=q)], config=get_config())
    if not result.ok:
        return f"Pixiv 搜索失败：{result.message}"
    paths = [str(p) for p in result.image_paths]
    return f"ok\npage={result.page_url}\nfiles:\n" + "\n".join(paths)


@mcp.tool(
    name="browser_image_search",
    annotations={
        "title": "图片搜索",
        "readOnlyHint": True,
        "openWorldHint": True,
    },
)
async def browser_image_search(query: str) -> str:
    """通用图片搜索（DuckDuckGo），下载图片到本地。"""
    err = _ensure_ready()
    if err:
        return err
    q = (query or "").strip()
    if not q:
        return "查询为空"
    result = await run_browse_tasks([BrowseTask(kind="image", query=q)], config=get_config())
    if not result.ok:
        return f"搜图失败：{result.message}"
    paths = [str(p) for p in result.image_paths]
    return f"ok\npage={result.page_url}\nfiles:\n" + "\n".join(paths)


if __name__ == "__main__":
    asyncio.run(mcp.run())
