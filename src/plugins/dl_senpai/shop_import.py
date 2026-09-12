"""服装店商品导入：爬图 / 搜图 → 视觉模型反推生图提示词 → 写入 catalog。

供 Cursor MCP 与后续管理端复用。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI

try:
    from nonebot import logger
except Exception:  # noqa: BLE001

    class _Logger:
        def warning(self, msg: str) -> None:
            print(msg)

        def info(self, msg: str) -> None:
            print(msg)

    logger = _Logger()  # type: ignore[assignment]

from .config import PluginConfig, get_config
from .image_gen import generate_image_file

_ANALYZE_SYSTEM = (
    "You help build an anime fashion shop catalog for a pink-haired fox-ear girl character. "
    "Given a reference clothing photo, output ONLY one JSON object (no markdown) with:\n"
    "- name: short Chinese display name (e.g. 蓝色洛丽塔, JK水手服)\n"
    "- outfit_en: detailed English clothing prompt for anime FULL BODY illustration "
    "(describe garment type, colors, fabrics, accessories; clothing only)\n"
    "- thumb_prompt: English prompt for catalog thumbnail "
    "(legacy; shop uses outfit_en to render senpai wearing the outfit)\n"
    "- price: integer points 35-95 based on outfit complexity\n"
    "- category: one word tag like lolita, jk, hanfu, maid, casual, gothic, swimwear\n"
    "Rules: prompts target anime illustration not photorealistic copy. "
    "No brand logos or watermarks. Do not mention Angelina game default red jacket."
)

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")
_SLUG_RE = re.compile(r"[^a-z0-9_]+")


@dataclass
class FashionImageHit:
    title: str
    image_url: str
    page_url: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "image_url": self.image_url,
            "page_url": self.page_url,
            "source": self.source,
        }


@dataclass
class AnalyzedClothing:
    name: str
    outfit_en: str
    thumb_prompt: str
    price: int
    category: str
    raw_json: dict[str, Any]

    def to_catalog_item(self, item_id: str) -> dict[str, Any]:
        return {
            "id": item_id,
            "name": self.name,
            "price": self.price,
            "outfit_en": self.outfit_en,
            "thumb_prompt": self.thumb_prompt,
            "category": self.category,
        }


@dataclass
class ImportResult:
    ok: bool
    message: str
    item: dict[str, Any] | None = None
    reference_path: str = ""
    preview_path: str = ""


def _project_root(config: PluginConfig | None = None) -> Path:
    cfg = config or get_config()
    return cfg.project_root()


def default_catalog_path(config: PluginConfig | None = None) -> Path:
    cfg = config or get_config()
    path = Path(cfg.shop_catalog_path)
    if not path.is_file():
        path = _project_root(cfg) / "data/dl_senpai/shop/catalog.json"
    return path


def default_refs_dir(config: PluginConfig | None = None) -> Path:
    cfg = config or get_config()
    root = Path(cfg.shop_dir)
    if not root.is_absolute():
        root = _project_root(cfg) / root
    refs = root / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    return refs


def _vision_client(config: PluginConfig) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=config.active_api_key(),
        base_url=config.active_base_url(),
        timeout=120.0,
    )


def _slug_from_name(name: str, category: str = "") -> str:
    base = category.strip().lower() or "item"
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    cleaned = _SLUG_RE.sub("_", base.lower()).strip("_")
    return f"{cleaned}_{digest}"


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    cat_path = path or default_catalog_path()
    if not cat_path.is_file():
        return {"items": []}
    try:
        raw = json.loads(cat_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": []}
    if not isinstance(raw, dict):
        return {"items": []}
    items = raw.get("items")
    if not isinstance(items, list):
        raw["items"] = []
    return raw


def save_catalog(data: dict[str, Any], path: Path | None = None) -> Path:
    cat_path = path or default_catalog_path()
    cat_path.parent.mkdir(parents=True, exist_ok=True)
    cat_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return cat_path


def list_catalog_items(path: Path | None = None) -> list[dict[str, Any]]:
    data = load_catalog(path)
    items = data.get("items")
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


def catalog_has_name(name: str, path: Path | None = None) -> bool:
    target = name.strip()
    for item in list_catalog_items(path):
        if str(item.get("name") or "").strip() == target:
            return True
    return False


def add_catalog_item(
    item: dict[str, Any],
    *,
    path: Path | None = None,
    skip_duplicate_name: bool = True,
) -> tuple[bool, str, dict[str, Any] | None]:
    name = str(item.get("name") or "").strip()
    outfit_en = str(item.get("outfit_en") or "").strip()
    if not name or not outfit_en:
        return False, "name 与 outfit_en 不能为空", None
    if skip_duplicate_name and catalog_has_name(name, path):
        return False, f"目录里已有同名「{name}」", None

    item_id = str(item.get("id") or "").strip()
    if not item_id:
        item_id = _slug_from_name(name, str(item.get("category") or ""))
    thumb = str(item.get("thumb_prompt") or "").strip()
    if not thumb:
        thumb = (
            f"fashion catalog thumbnail, {outfit_en} on mannequin, "
            "soft pastel background, no face"
        )
    price = int(item.get("price") or 55)
    price = max(35, min(95, price))

    row = {
        "id": item_id,
        "name": name,
        "price": price,
        "outfit_en": outfit_en,
        "thumb_prompt": thumb,
    }
    if item.get("category"):
        row["category"] = str(item["category"])

    data = load_catalog(path)
    items = data.setdefault("items", [])
    existing_ids = {str(i.get("id")) for i in items if isinstance(i, dict)}
    if item_id in existing_ids:
        item_id = f"{item_id}_{uuid.uuid4().hex[:6]}"
        row["id"] = item_id

    items.append(row)
    saved = save_catalog(data, path)
    return True, f"已写入 {saved}", row


async def search_fashion_images(
    query: str,
    *,
    limit: int = 5,
    region: str = "cn-zh",
) -> list[FashionImageHit]:
    q = (query or "").strip()
    if not q:
        return []
    lim = max(1, min(12, int(limit)))

    def _run_ddgs() -> list[FashionImageHit]:
        hits: list[FashionImageHit] = []
        try:
            from ddgs import DDGS  # type: ignore[import-untyped]
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # type: ignore[import-untyped]
            except ImportError:
                return []
        with DDGS() as ddgs:
            rows = ddgs.images(
                keywords=q,
                region=region if region != "zh-cn" else "cn-zh",
                safesearch="moderate",
                max_results=lim,
            )
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                image_url = str(row.get("image") or row.get("thumbnail") or "").strip()
                if not image_url.startswith(("http://", "https://")):
                    continue
                title = str(row.get("title") or row.get("name") or q).strip()
                page_url = str(row.get("url") or row.get("source") or image_url).strip()
                hits.append(
                    FashionImageHit(
                        title=title[:120],
                        image_url=image_url,
                        page_url=page_url,
                        source="ddgs_images",
                    )
                )
                if len(hits) >= lim:
                    break
        return hits

    return await asyncio.to_thread(_run_ddgs)


async def download_image(url: str, dest: Path, *, timeout: float = 30.0) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content = resp.content
        if len(content) < 512:
            return False
        dest.write_bytes(content)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"shop_import download failed url={url[:72]} err={exc}")
        return False


def _image_to_data_url(path: Path) -> str | None:
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    mime = "image/jpeg"
    if suffix == ".png":
        mime = "image/png"
    elif suffix == ".webp":
        mime = "image/webp"
    try:
        raw = path.read_bytes()
        if not raw:
            return None
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except OSError:
        return None


def _parse_analyze_json(text: str) -> AnalyzedClothing | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = str(data.get("name") or "").strip()
    outfit_en = str(data.get("outfit_en") or "").strip()
    thumb_prompt = str(data.get("thumb_prompt") or "").strip()
    category = str(data.get("category") or "casual").strip()
    if not name or not outfit_en:
        return None
    if not thumb_prompt:
        thumb_prompt = (
            f"fashion catalog thumbnail, {outfit_en} on mannequin, "
            "soft pastel background, no face"
        )
    price = int(data.get("price") or 55)
    price = max(35, min(95, price))
    return AnalyzedClothing(
        name=name,
        outfit_en=outfit_en,
        thumb_prompt=thumb_prompt,
        price=price,
        category=category,
        raw_json=data,
    )


async def analyze_clothing_image(
    image_path: str | Path,
    *,
    name_hint: str = "",
    config: PluginConfig | None = None,
) -> AnalyzedClothing | None:
    cfg = config or get_config()
    if not cfg.api_configured():
        return None
    path = Path(image_path)
    data_url = _image_to_data_url(path)
    if not data_url:
        return None

    hint = (name_hint or "").strip()
    user_text = (
        "Analyze this clothing reference for our anime shop catalog. "
        "Output JSON only."
    )
    if hint:
        user_text += f" Name hint from page: {hint}"

    client = _vision_client(cfg)
    model = cfg.active_model()
    try:
        resp = await client.chat.completions.create(
            model=model,
            temperature=0.2,
            max_tokens=800,
            messages=[
                {"role": "system", "content": _ANALYZE_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
        )
        content = (resp.choices[0].message.content or "").strip()
        return _parse_analyze_json(content)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"shop_import vision analyze failed: {exc}")
        return None


async def crawl_and_download(
    query: str,
    *,
    limit: int = 5,
    refs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    hits = await search_fashion_images(query, limit=limit)
    dest_dir = refs_dir or default_refs_dir()
    out: list[dict[str, Any]] = []
    for i, hit in enumerate(hits):
        digest = hashlib.sha256(hit.image_url.encode()).hexdigest()[:12]
        ext = Path(urlparse(hit.image_url).path).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            ext = ".jpg"
        local = dest_dir / f"{digest}{ext}"
        ok = await download_image(hit.image_url, local)
        out.append(
            {
                **hit.to_dict(),
                "local_path": str(local.resolve()) if ok else "",
                "downloaded": ok,
            }
        )
    return out


async def import_from_reference(
    image_path: str | Path,
    *,
    name_hint: str = "",
    generate_preview: bool = False,
    add_to_catalog: bool = True,
    config: PluginConfig | None = None,
    catalog_path: Path | None = None,
) -> ImportResult:
    cfg = config or get_config()
    path = Path(image_path)
    if not path.is_file():
        return ImportResult(ok=False, message=f"图片不存在：{path}")

    analyzed = await analyze_clothing_image(path, name_hint=name_hint, config=cfg)
    if analyzed is None:
        return ImportResult(
            ok=False,
            message="视觉模型未能从图片反推提示词（检查 OPENAI_API_KEY / 模型是否支持识图）",
            reference_path=str(path.resolve()),
        )

    item_id = _slug_from_name(analyzed.name, analyzed.category)
    item = analyzed.to_catalog_item(item_id)
    preview_path = ""

    if generate_preview and cfg.draw_configured():
        preview = await generate_image_file(analyzed.outfit_en, cfg)
        if preview is not None:
            preview_path = str(preview.resolve())

    if add_to_catalog:
        ok, msg, row = add_catalog_item(item, path=catalog_path)
        if not ok:
            return ImportResult(
                ok=False,
                message=msg,
                item=item,
                reference_path=str(path.resolve()),
                preview_path=preview_path,
            )
        item = row or item
        return ImportResult(
            ok=True,
            message=msg,
            item=item,
            reference_path=str(path.resolve()),
            preview_path=preview_path,
        )

    return ImportResult(
        ok=True,
        message="分析完成（未写入目录）",
        item=item,
        reference_path=str(path.resolve()),
        preview_path=preview_path,
    )


async def import_from_search(
    query: str,
    *,
    limit: int = 3,
    generate_preview: bool = False,
    config: PluginConfig | None = None,
    catalog_path: Path | None = None,
) -> list[ImportResult]:
    cfg = config or get_config()
    crawled = await crawl_and_download(query, limit=limit)
    results: list[ImportResult] = []
    for row in crawled:
        local = str(row.get("local_path") or "")
        if not local:
            results.append(
                ImportResult(ok=False, message=f"下载失败：{row.get('title', '')}")
            )
            continue
        hint = str(row.get("title") or query)
        result = await import_from_reference(
            local,
            name_hint=hint,
            generate_preview=generate_preview,
            add_to_catalog=True,
            config=cfg,
            catalog_path=catalog_path,
        )
        results.append(result)
    return results


def format_import_batch_summary(results: list[ImportResult]) -> str:
    if not results:
        return "没有处理任何条目。"
    lines = ["👗 服装店导入结果", "────────"]
    ok_count = 0
    for i, r in enumerate(results, start=1):
        if r.ok:
            ok_count += 1
            name = (r.item or {}).get("name", "?")
            lines.append(f"{i}. ✅ {name}")
            if r.item:
                lines.append(f"   id={r.item.get('id')} · {r.item.get('price')} 分")
        else:
            lines.append(f"{i}. ❌ {r.message[:80]}")
    lines.append("────────")
    lines.append(f"成功 {ok_count}/{len(results)}")
    return "\n".join(lines)
