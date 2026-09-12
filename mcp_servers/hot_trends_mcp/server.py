"""Cursor / Claude 可用的热梗 MCP（stdio）。

复用 dl_senpai.trends 采集与缓存逻辑。
启动示例见 docs/hot-trends-mcp.md
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 保证可 import dl_senpai（仓库根或 editable install）
_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _ROOT / "src" / "plugins"
for p in (_PLUGIN_DIR, _ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from dl_senpai.trends import (  # noqa: E402
    SOURCE_LABELS,
    TrendsStore,
    format_trends_for_prompt,
)

mcp = FastMCP("hot_trends_mcp")


def _store() -> TrendsStore:
    sources_raw = os.getenv(
        "DL_SENPAI_TRENDS_SOURCES", "weibo,douyin,bilibili,baidu,zhihu"
    )
    sources = [s.strip().lower() for s in sources_raw.split(",") if s.strip()]
    return TrendsStore(
        cache_dir=os.getenv("DL_SENPAI_TRENDS_CACHE_DIR", "data/dl_senpai/trends"),
        base_url=os.getenv(
            "DL_SENPAI_TRENDS_BASE_URL",
            "https://uapis.cn/api/v1/misc/hotboard",
        ),
        sources=sources,
        ttl_sec=int(os.getenv("DL_SENPAI_TRENDS_TTL_SEC", "1800")),
        per_source_limit=int(os.getenv("DL_SENPAI_TRENDS_PER_SOURCE", "8")),
        fallback_bases=[
            b.strip()
            for b in os.getenv(
                "DL_SENPAI_TRENDS_FALLBACK_BASES", "https://api-hot.imsyy.top"
            ).split(",")
            if b.strip()
        ],
    )


@mcp.tool(
    name="hot_trending",
    annotations={
        "title": "按平台查热榜",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def hot_trending(source: str = "weibo", limit: int = 15) -> str:
    """获取单个平台热榜标题。

    Args:
        source: 平台 id，如 weibo / douyin / bilibili / baidu / zhihu
        limit: 返回条数上限（1-50）
    """
    src = (source or "weibo").strip().lower()
    lim = max(1, min(50, int(limit)))
    store = _store()
    items = await store.get_items(source=src, limit=lim)
    if not items:
        # 可能缓存里没有该源：强制刷新一次
        await store.get_snapshot(force=True)
        items = await store.get_items(source=src, limit=lim)
    label = SOURCE_LABELS.get(src, src)
    if not items:
        return json.dumps(
            {"source": src, "label": label, "items": [], "message": "暂无数据"},
            ensure_ascii=False,
        )
    payload = {
        "source": src,
        "label": label,
        "items": [
            {"rank": i.rank, "title": i.title, "hot": i.hot, "url": i.url} for i in items
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool(
    name="hot_summary",
    annotations={
        "title": "跨平台热梗摘要",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def hot_summary(limit: int = 20) -> str:
    """汇总多平台热榜标题，格式与学姐提示词备忘一致。

    Args:
        limit: 最多条数（1-40）
    """
    lim = max(1, min(40, int(limit)))
    store = _store()
    snap = await store.get_snapshot()
    if snap is None or not snap.items:
        return "暂无热梗数据（接口不可达或尚未缓存）。"
    return format_trends_for_prompt(
        snap.items,
        max_items=lim,
        max_chars=2000,
        stale=snap.stale,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
