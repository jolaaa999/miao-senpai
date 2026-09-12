# 服装店商品导入 MCP（Cursor）

从网上搜服装参考图 → 视觉大模型反推 `outfit_en` / `thumb_prompt` → 写入 `data/dl_senpai/shop/catalog.json`，供 QQ 群积分商店使用。

## 前置

```bash
python -m pip install -e ".[mcp,search]"
```

- `mcp`：MCP 服务端
- `search`：含 `ddgs`，用于图片搜索（`shop_search_fashion_images` / `shop_crawl_fashion_images`）

环境变量（与 bot 共用 `.env`）：

| 变量 | 用途 |
|------|------|
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | 识图反推提示词（需支持 vision） |
| `DL_SENPAI_DRAW_API_KEY` 等 | 可选，生成预览全身图 |

## Cursor 配置

项目已含 `.cursor/mcp.json` 中的 `qqbot-shop-import`。

Settings → MCP 确认出现 **qqbot-shop-import**。

## 工具一览

| 工具 | 作用 |
|------|------|
| `shop_search_fashion_images` | 仅搜索，返回图片 URL |
| `shop_crawl_fashion_images` | 搜索并下载到 `data/dl_senpai/shop/refs/` |
| `shop_analyze_clothing_image` | 单张本地图 → 反推 JSON 字段 |
| `shop_import_from_image` | 单张图 → 分析 → 可选生图 → 写入 catalog |
| `shop_import_from_search` | **推荐** 一条龙批量导入 |
| `shop_list_catalog` | 查看当前目录 |
| `shop_generate_item_preview` | 按 id 生预览图 |

## 典型工作流

在 Cursor 对话中说：

> 用 shop_import 搜「日系JK制服」「蓝色洛丽塔」，各导入 2 件到商店目录

Agent 应调用：

1. `shop_import_from_search(query="日系JK制服", limit=2)`
2. `shop_import_from_search(query="蓝色洛丽塔", limit=2)`
3. `shop_list_catalog` 核对

导入成功后 **重启 bot**，新款式会进入商店「未收藏池」。

## 数据流

```
DuckDuckGo 图片搜索 (ddgs)
    → 下载参考图 (refs/)
    → 视觉 LLM 反推 name / outfit_en / thumb_prompt / price
    → catalog.json
    →（可选）生图 API 预览
    → QQ 群「商店」上架（未买过的新款）
```

## 注意

- Bot 在货架缺货且目录无未收藏款时，会**自动**调用与 MCP 相同的 `shop_import` 流程补货（无需 Cursor 介入）
- 环境变量 `DL_SENPAI_SHOP_AUTO_IMPORT`（默认 true）、`DL_SENPAI_SHOP_AUTO_IMPORT_QUERIES` 控制补货
- 参考图仅供提示词反推，商店内展示图由生图 API 按 `outfit_en` 生成动漫立绘
- 同名商品会自动跳过，避免重复
- 扩充目录：编辑 `catalog.json` 或继续用 MCP 导入
