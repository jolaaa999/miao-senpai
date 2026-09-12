"""Cursor MCP：服装店商品爬取 / 识图反推提示词 / 写入 catalog。

启动：python scripts/run-mcp-shop-import.py
"""

from __future__ import annotations

import json
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
from dl_senpai.shop_import import (  # noqa: E402
    analyze_clothing_image,
    crawl_and_download,
    default_catalog_path,
    format_import_batch_summary,
    import_from_reference,
    import_from_search,
    list_catalog_items,
    search_fashion_images,
)
from dl_senpai.image_gen import generate_image_file  # noqa: E402
from dl_senpai.config import get_config  # noqa: E402

load_repo_env(root=_ROOT)

mcp = FastMCP("shop_import_mcp")


@mcp.tool(
    name="shop_search_fashion_images",
    annotations={
        "title": "搜索服装参考图",
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    },
)
async def shop_search_fashion_images(query: str, limit: int = 5) -> str:
    """用 DuckDuckGo 图片搜索找服装/穿搭参考图（需 pip install ddgs）。

    Args:
        query: 搜索词，如「蓝色洛丽塔」「JK制服」「汉服女装」
        limit: 返回条数 1-12
    """
    hits = await search_fashion_images(query, limit=limit)
    if not hits:
        return json.dumps(
            {
                "query": query,
                "count": 0,
                "hint": "无结果。请安装 ddgs：python -m pip install ddgs",
                "items": [],
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "query": query,
            "count": len(hits),
            "items": [h.to_dict() for h in hits],
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(
    name="shop_crawl_fashion_images",
    annotations={
        "title": "爬取并下载服装参考图",
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
    },
)
async def shop_crawl_fashion_images(query: str, limit: int = 5) -> str:
    """搜索服装图并下载到 data/dl_senpai/shop/refs/，返回本地路径。

    Args:
        query: 搜索词
        limit: 下载张数 1-12
    """
    rows = await crawl_and_download(query, limit=limit)
    return json.dumps(
        {"query": query, "downloaded": sum(1 for r in rows if r.get("downloaded")), "items": rows},
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(
    name="shop_analyze_clothing_image",
    annotations={
        "title": "识图反推生图提示词",
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
    },
)
async def shop_analyze_clothing_image(
    image_path: str,
    name_hint: str = "",
) -> str:
    """用大模型视觉能力分析服装参考图，反推 outfit_en / thumb_prompt 等 catalog 字段。

    需要 OPENAI_API_KEY（或中转）且模型支持识图。

    Args:
        image_path: 本地图片绝对路径
        name_hint: 可选名称提示（如网页标题）
    """
    analyzed = await analyze_clothing_image(image_path, name_hint=name_hint)
    if analyzed is None:
        return json.dumps(
            {"ok": False, "error": "分析失败", "image_path": image_path},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "ok": True,
            "name": analyzed.name,
            "outfit_en": analyzed.outfit_en,
            "thumb_prompt": analyzed.thumb_prompt,
            "price": analyzed.price,
            "category": analyzed.category,
            "catalog_item": analyzed.to_catalog_item("preview_id"),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(
    name="shop_import_from_image",
    annotations={
        "title": "从参考图导入商店商品",
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": False,
    },
)
async def shop_import_from_image(
    image_path: str,
    name_hint: str = "",
    generate_preview: bool = False,
    add_to_catalog: bool = True,
) -> str:
    """单张参考图：识图反推 → 可选生图预览 → 写入 catalog.json。

    Args:
        image_path: 本地图片路径
        name_hint: 名称提示
        generate_preview: 是否调用生图 API 生成预览全身图（较慢）
        add_to_catalog: 是否写入 catalog（默认 true）
    """
    result = await import_from_reference(
        image_path,
        name_hint=name_hint,
        generate_preview=generate_preview,
        add_to_catalog=add_to_catalog,
    )
    return json.dumps(
        {
            "ok": result.ok,
            "message": result.message,
            "item": result.item,
            "reference_path": result.reference_path,
            "preview_path": result.preview_path,
            "catalog_path": str(default_catalog_path()),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(
    name="shop_import_from_search",
    annotations={
        "title": "搜索并批量导入商店商品",
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
    },
)
async def shop_import_from_search(
    query: str,
    limit: int = 3,
    generate_preview: bool = False,
) -> str:
    """一条龙：搜图 → 下载 → 识图反推提示词 → 写入 catalog。

    Args:
        query: 如「洛丽塔连衣裙」「日系JK制服」
        limit: 尝试导入条数 1-6
        generate_preview: 是否为每件生成预览图（慢）
    """
    lim = max(1, min(6, int(limit)))
    results = await import_from_search(
        query,
        limit=lim,
        generate_preview=generate_preview,
    )
    summary = format_import_batch_summary(results)
    return json.dumps(
        {
            "summary_text": summary,
            "catalog_path": str(default_catalog_path()),
            "results": [
                {
                    "ok": r.ok,
                    "message": r.message,
                    "item": r.item,
                    "reference_path": r.reference_path,
                    "preview_path": r.preview_path,
                }
                for r in results
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(
    name="shop_list_catalog",
    annotations={
        "title": "列出商店商品目录",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def shop_list_catalog() -> str:
    """列出 catalog.json 中全部商品（id / name / price）。"""
    items = list_catalog_items()
    return json.dumps(
        {
            "catalog_path": str(default_catalog_path()),
            "count": len(items),
            "items": [
                {
                    "id": i.get("id"),
                    "name": i.get("name"),
                    "price": i.get("price"),
                    "category": i.get("category"),
                }
                for i in items
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(
    name="shop_generate_item_preview",
    annotations={
        "title": "为目录商品生成预览图",
        "readOnlyHint": False,
        "destructiveHint": False,
    },
)
async def shop_generate_item_preview(item_id: str) -> str:
    """按 catalog 中 outfit_en 调用生图 API 生成预览图。

    Args:
        item_id: catalog 中的 id
    """
    cfg = get_config()
    items = list_catalog_items()
    row = next((i for i in items if str(i.get("id")) == item_id), None)
    if row is None:
        return json.dumps({"ok": False, "error": f"未找到 id={item_id}"})
    outfit = str(row.get("outfit_en") or "")
    if not outfit:
        return json.dumps({"ok": False, "error": "outfit_en 为空"})
    path = await generate_image_file(outfit, cfg)
    if path is None:
        return json.dumps({"ok": False, "error": "生图失败"})
    return json.dumps(
        {"ok": True, "item_id": item_id, "preview_path": str(path.resolve())},
        ensure_ascii=False,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
