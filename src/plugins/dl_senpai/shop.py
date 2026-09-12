from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageSegment
from PIL import Image, ImageDraw, ImageFont

from .config import PluginConfig, get_config
from .image_gen import (
    build_angelina_outfit_prompt,
    generate_image_file,
)
from .memory import session_id_for_group

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001
    TZ = timezone(timedelta(hours=8))

ShopCmd = Literal["open", "buy", "warehouse", "wear", "help", "none"]
SHOP_SLOT_COUNT = 4
SHOP_GRID_COLS = 2
SHOP_GRID_ROWS = 2
_PLAIN_SENPAI_PROMPT = (
    "Anime full body fashion illustration. "
    "face reference only: Arknights Angelina inspired, long soft pink hair, "
    "fox ears, fluffy fox tail, warm brown eyes, youthful gentle anime face. "
    "wearing simple plain white t-shirt and light gray casual skirt, "
    "plain casual clothes, not game default costume, standing pose, gentle smile, "
    "clean soft pastel shop background, full body visible"
)


@dataclass(frozen=True)
class ClothingItem:
    id: str
    name: str
    price: int
    outfit_en: str
    thumb_prompt: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClothingItem | None:
        item_id = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()
        outfit_en = str(data.get("outfit_en") or "").strip()
        if not item_id or not name or not outfit_en:
            return None
        thumb = str(data.get("thumb_prompt") or "").strip()
        if not thumb:
            thumb = (
                f"fashion catalog thumbnail, {outfit_en} on mannequin or flat lay, "
                "soft pastel background, no person, no face, clothing only"
            )
        return cls(
            id=item_id,
            name=name,
            price=max(1, int(data.get("price") or 50)),
            outfit_en=outfit_en,
            thumb_prompt=thumb,
        )


@dataclass
class WarehouseEntry:
    item_id: str
    buyer_id: str
    buyer_name: str
    purchased_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WarehouseEntry | None:
        item_id = str(data.get("item_id") or "").strip()
        if not item_id:
            return None
        return cls(
            item_id=item_id,
            buyer_id=str(data.get("buyer_id") or ""),
            buyer_name=str(data.get("buyer_name") or ""),
            purchased_at=str(data.get("purchased_at") or ""),
        )


@dataclass
class GroupShopState:
    slots: list[str] = field(default_factory=list)
    current_outfit_id: str = ""
    user_warehouses: dict[str, list[WarehouseEntry]] = field(default_factory=dict)
    slots_refresh_day: str = ""  # 按凌晨刷新日 YYYY-MM-DD（Asia/Shanghai, 4 点前算前一天）

    def to_dict(self) -> dict[str, Any]:
        return {
            "slots": self.slots,
            "current_outfit_id": self.current_outfit_id,
            "slots_refresh_day": self.slots_refresh_day,
            "user_warehouses": {
                uid: [e.to_dict() for e in entries]
                for uid, entries in self.user_warehouses.items()
            },
        }

    def user_warehouse_entries(self, user_id: int | str) -> list[WarehouseEntry]:
        return list(self.user_warehouses.get(str(user_id), []))

    def user_owned_ids(self, user_id: int | str) -> set[str]:
        return {e.item_id for e in self.user_warehouse_entries(user_id) if e.item_id}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GroupShopState:
        slots_raw = data.get("slots")
        slots: list[str] = []
        if isinstance(slots_raw, list):
            slots = [str(s).strip() for s in slots_raw if str(s).strip()]

        user_warehouses: dict[str, list[WarehouseEntry]] = {}
        uwh_raw = data.get("user_warehouses")
        if isinstance(uwh_raw, dict):
            for uid, rows in uwh_raw.items():
                if not isinstance(rows, list):
                    continue
                entries: list[WarehouseEntry] = []
                for row in rows:
                    if isinstance(row, dict):
                        entry = WarehouseEntry.from_dict(row)
                        if entry is not None:
                            entries.append(entry)
                if entries:
                    user_warehouses[str(uid)] = entries

        return cls(
            slots=slots,
            current_outfit_id=str(data.get("current_outfit_id") or "").strip(),
            user_warehouses=user_warehouses,
            slots_refresh_day=str(data.get("slots_refresh_day") or "").strip(),
        )


@dataclass
class PurchaseResult:
    ok: bool
    message: str = ""
    item: ClothingItem | None = None
    remaining_points: int = 0
    outfit_image: MessageSegment | None = None


@dataclass
class WearResult:
    ok: bool
    message: str = ""
    outfit_image: MessageSegment | None = None


class ShopCatalog:
    def __init__(self, catalog_path: Path) -> None:
        self.catalog_path = catalog_path
        self._items: dict[str, ClothingItem] = {}
        self._load()

    def _load(self) -> None:
        self._items = {}
        if not self.catalog_path.is_file():
            logger.warning(f"dl_senpai shop catalog missing: {self.catalog_path}")
            return
        try:
            raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"dl_senpai shop catalog parse failed: {exc}")
            return
        rows = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = ClothingItem.from_dict(row)
            if item is not None:
                self._items[item.id] = item

    def reload(self) -> None:
        self._load()

    def all_items(self) -> list[ClothingItem]:
        return list(self._items.values())

    def get(self, item_id: str) -> ClothingItem | None:
        return self._items.get(str(item_id).strip())

    def pick_random(self, *, exclude: set[str], count: int, rng: random.Random) -> list[str]:
        pool = [i.id for i in self._items.values() if i.id not in exclude]
        if not pool:
            return []
        if len(pool) <= count:
            rng.shuffle(pool)
            return pool[:count]
        return rng.sample(pool, count)

    def count_unowned(self, owned_ids: set[str]) -> int:
        return sum(1 for i in self._items.values() if i.id not in owned_ids)


class ShopStore:
    def __init__(
        self,
        data_dir: str | Path,
        catalog: ShopCatalog,
        *,
        slot_count: int = SHOP_SLOT_COUNT,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.thumb_dir = self.data_dir / "thumbs"
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.outfit_dir = self.data_dir / "outfits"
        self.outfit_dir.mkdir(parents=True, exist_ok=True)
        self.composite_dir = self.data_dir / "composites"
        self.composite_dir.mkdir(parents=True, exist_ok=True)
        self.catalog = catalog
        self.slot_count = max(1, slot_count)
        self._lock = Lock()

    def _state_path(self, group_id: int | str) -> Path:
        return self.data_dir / f"group_{group_id}.json"

    def load_state(self, group_id: int | str) -> GroupShopState:
        path = self._state_path(group_id)
        if not path.is_file():
            return GroupShopState()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return GroupShopState()
        if isinstance(raw, dict):
            return GroupShopState.from_dict(raw)
        return GroupShopState()

    def save_state(self, group_id: int | str, state: GroupShopState) -> None:
        payload = {
            **state.to_dict(),
            "updated_at": datetime.now(TZ).isoformat(),
        }
        with self._lock:
            self._state_path(group_id).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _shop_exclude_ids(self, state: GroupShopState) -> set[str]:
        """每日上架从目录抽取；不因某人已购买而排除（仓库按人独立）。"""
        exclude: set[str] = set()
        if state.current_outfit_id:
            exclude.add(state.current_outfit_id)
        return exclude

    def shop_refresh_day(
        self,
        now: datetime | None = None,
        *,
        refresh_hour: int | None = None,
    ) -> str:
        from .config import get_config

        cfg = get_config()
        hour_cut = refresh_hour if refresh_hour is not None else cfg.shop_daily_refresh_hour
        hour_cut = max(0, min(23, int(hour_cut)))
        now = now or datetime.now(TZ)
        local = now.astimezone(TZ)
        if local.hour < hour_cut:
            local = local - timedelta(days=1)
        return local.date().isoformat()

    def apply_daily_refresh_if_needed(
        self,
        group_id: int | str,
        *,
        rng: random.Random | None = None,
    ) -> GroupShopState:
        """每天凌晨 refresh_hour 起从目录重新抽 4 件上架（已入仓库的不重复卖）。"""
        rng = rng or random.Random()
        state = self.load_state(group_id)
        day = self.shop_refresh_day()
        if state.slots_refresh_day == day:
            return state

        valid_ids = {i.id for i in self.catalog.all_items()}
        state.slots = [s for s in state.slots if s in valid_ids]
        if state.current_outfit_id and state.current_outfit_id not in valid_ids:
            state.current_outfit_id = ""

        exclude = self._shop_exclude_ids(state)
        state.slots = self.catalog.pick_random(
            exclude=exclude,
            count=self.slot_count,
            rng=rng,
        )
        state.slots_refresh_day = day
        self.save_state(group_id, state)
        logger.info(
            f"dl_senpai shop daily refresh group={group_id} day={day} slots={state.slots}"
        )
        return state

    def add_to_warehouse(
        self,
        group_id: int | str,
        item_id: str,
        *,
        buyer_id: int | str,
        buyer_name: str,
    ) -> None:
        uid = str(buyer_id)
        state = self.load_state(group_id)
        entries = state.user_warehouses.setdefault(uid, [])
        if any(e.item_id == item_id for e in entries):
            self.save_state(group_id, state)
            return
        entries.append(
            WarehouseEntry(
                item_id=item_id,
                buyer_id=uid,
                buyer_name=buyer_name or uid,
                purchased_at=datetime.now(TZ).isoformat(),
            )
        )
        self.save_state(group_id, state)

    def list_warehouse_items(
        self,
        group_id: int | str,
        user_id: int | str,
    ) -> list[ClothingItem]:
        state = self.load_state(group_id)
        seen: set[str] = set()
        items: list[ClothingItem] = []
        for entry in state.user_warehouse_entries(user_id):
            if entry.item_id in seen:
                continue
            item = self.catalog.get(entry.item_id)
            if item is not None:
                seen.add(entry.item_id)
                items.append(item)
        return items

    def user_owns_item(
        self,
        group_id: int | str,
        user_id: int | str,
        item_id: str,
    ) -> bool:
        state = self.load_state(group_id)
        return item_id in state.user_owned_ids(user_id)

    def match_item_in_owned(
        self,
        text: str,
        items: list[ClothingItem],
    ) -> ClothingItem | None:
        query = _normalize_wear_query(text)
        if not query or not items:
            return None
        for item in items:
            if query == item.name or query == item.id:
                return item
        for item in items:
            if item.name in query or query in item.name:
                return item
        for item in items:
            if _fuzzy_name_match(query, item.name):
                return item
        return None

    def ensure_slots(
        self,
        group_id: int | str,
        *,
        rng: random.Random | None = None,
    ) -> tuple[GroupShopState, list[ClothingItem]]:
        rng = rng or random.Random()
        state = self.apply_daily_refresh_if_needed(group_id, rng=rng)
        valid_ids = {i.id for i in self.catalog.all_items()}
        state.slots = [s for s in state.slots if s in valid_ids]
        if state.current_outfit_id and state.current_outfit_id not in valid_ids:
            state.current_outfit_id = ""
        self.save_state(group_id, state)
        items = [self.catalog.get(sid) for sid in state.slots]
        return state, [i for i in items if i is not None]

    def remove_from_slots(self, group_id: int | str, item_id: str) -> None:
        state = self.load_state(group_id)
        state.slots = [s for s in state.slots if s != item_id]
        self.save_state(group_id, state)

    def get_current_outfit(self, group_id: int | str) -> ClothingItem | None:
        state = self.load_state(group_id)
        if not state.current_outfit_id:
            return None
        return self.catalog.get(state.current_outfit_id)

    def set_current_outfit(self, group_id: int | str, item_id: str) -> None:
        state = self.load_state(group_id)
        state.current_outfit_id = item_id
        self.save_state(group_id, state)

    def match_item_in_slots(self, text: str, items: list[ClothingItem]) -> ClothingItem | None:
        query = _normalize_buy_query(text)
        if not query or not items:
            return None
        for item in items:
            if query == item.name or query == item.id:
                return item
        for item in items:
            if item.name in query or query in item.name:
                return item
        for item in items:
            if _fuzzy_name_match(query, item.name):
                return item
        return None


def _normalize_buy_query(text: str) -> str:
    raw = (text or "").strip()
    raw = re.sub(r"[@＠]\S+", "", raw)
    raw = re.sub(r"学姐|senpai", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+", "", raw)
    for prefix in (
        "使用积分购买",
        "用积分购买",
        "积分购买",
        "使用积分买",
        "用积分买",
        "积分买",
        "购买",
        "买下",
        "买",
    ):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.strip(" ，,。.!！?？~～")


def _fuzzy_name_match(query: str, name: str) -> bool:
    q = query.replace(" ", "")
    n = name.replace(" ", "")
    if len(q) < 2:
        return False
    return q in n or n in q


def _normalize_wear_query(text: str) -> str:
    raw = (text or "").strip()
    raw = re.sub(r"[@＠]\S+", "", raw)
    raw = re.sub(r"学姐|senpai", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+", "", raw)
    for prefix in (
        "给学姐穿上",
        "给学姐换上",
        "学姐穿上",
        "学姐换上",
        "穿上",
        "换上",
        "穿",
    ):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.strip(" ，,。.!！?？~～")


_OPEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^(商店|积分商店|服装店|学姐商店|衣服商店)$"),
    re.compile(r"^(打开商店|逛逛商店|看看商店)$"),
]
_WAREHOUSE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^(仓库|衣服仓库|衣橱|衣柜|学姐仓库|服装仓库)$"),
    re.compile(r"^(打开仓库|看看仓库|我的仓库)$"),
]
_WEAR_PATTERN = re.compile(
    r"^(?:给学姐|学姐)?(?:穿上|换上|穿)(.+)$"
)
_BUY_PATTERN = re.compile(
    r"^(?:使用积分|用积分|积分)?(?:购买|买下|买)(.+)$"
)
_HELP_PATTERN = re.compile(r"^(商店帮助|服装店帮助|积分商店帮助)$")


def parse_shop_command(text: str) -> ShopCmd:
    t = re.sub(r"\s+", "", (text or "").strip())
    if not t:
        return "none"
    for pat in _OPEN_PATTERNS:
        if pat.match(t):
            return "open"
    for pat in _WAREHOUSE_PATTERNS:
        if pat.match(t):
            return "warehouse"
    if _HELP_PATTERN.match(t):
        return "help"
    if _BUY_PATTERN.match(t):
        return "buy"
    if _WEAR_PATTERN.match(t):
        return "wear"
    return "none"


def format_shop_help() -> str:
    return (
        "👗 学姐服装店\n"
        "────────\n"
        "· 商店 —— 左侧 2×2 仅展示衣服，右侧学姐素衣\n"
        "· 购买 + 衣服名 —— 学姐穿上对应款式\n"
        "· 仓库 —— 查看你买过的衣服\n"
        "· 换上 + 衣服名 —— 从你的仓库给学姐换装\n"
        "\n"
        "每天凌晨 4 点从目录重新抽 4 件上架；\n"
        "当天买走的格子空着，次日 4 点再刷新。\n"
        "好感越高，货架上出现「好感特惠」的概率和折扣都更大哦～"
    )


def format_shop_empty_hint(store: ShopStore, group_id: int | str) -> str:
    state = store.load_state(group_id)
    total = len(store.catalog.all_items())
    on_shelf = len([s for s in state.slots if store.catalog.get(s)])
    if on_shelf == 0 and total > 0:
        return (
            "今天货架上的都卖光啦～\n"
            "每天凌晨 4 点会自动换一批上架。"
        )
    return (
        "🎉 商品目录暂时没货可上了。\n"
        "────────\n"
        "学姐正在联网搜新款补货，稍后再开「商店」～\n"
        "你已买的在「仓库」，发「换上 + 名字」随时换装。"
    )


def format_warehouse_text(
    store: ShopStore,
    group_id: int | str,
    *,
    user_id: int | str,
    user_name: str,
    current_name: str | None = None,
) -> str:
    items = store.list_warehouse_items(group_id, user_id)
    if not items:
        return (
            "📦 你的衣服仓库还是空的\n"
            "────────\n"
            f"{user_name}，去「商店」用积分给学姐买第一件衣服吧～"
        )
    lines = [
        "📦 我的衣服仓库",
        f"{user_name}",
        "────────",
        f"已收藏 {len(items)} 款",
    ]
    if current_name:
        lines.append(f"学姐当前穿着：{current_name}")
    lines.append("────────")
    for i, item in enumerate(items, start=1):
        lines.append(f"{i}. {item.name} · {item.price} 分")
    lines.append("────────")
    lines.append("发「换上 + 衣服名」给学姐免费换装～")
    return "\n".join(lines)


def format_shop_open_text(
    quotes: list,
    *,
    points: int,
    store: ShopStore | None = None,
    group_id: int | str | None = None,
    discount_hint: str = "",
) -> str:
    if not quotes:
        empty = ""
        if store is not None and group_id is not None:
            empty = format_shop_empty_hint(store, group_id)
        return (
            "👗 学姐服装店\n"
            "────────\n"
            f"你的积分：{points}\n"
            f"{empty}"
        )
    lines = [
        "👗 学姐服装店",
        "────────",
        f"你的积分：{points}",
        "左侧仅展示衣服，右侧是学姐素衣～",
        "每天凌晨 4 点换一批新款～",
    ]
    if discount_hint:
        lines.append(discount_hint)
    from .shop_discount import format_quote_price_line

    for i, quote in enumerate(quotes, start=1):
        lines.append(f"{i}. {format_quote_price_line(quote)}")
    lines.append("────────")
    lines.append("发「购买 + 衣服名」就能给学姐换装～")
    return "\n".join(lines)


def format_purchase_ok(
    *,
    name: str,
    item_name: str,
    price: int,
    remaining: int,
    list_price: int | None = None,
    discount_percent: int = 0,
) -> str:
    price_line = f"{name} 用 {price} 分给学姐买了「{item_name}」～"
    if list_price is not None and discount_percent > 0 and list_price > price:
        price_line = (
            f"{name} 用 {price} 分（原价 {list_price}，好感特惠 -{discount_percent}%）"
            f"给学姐买了「{item_name}」～"
        )
    return (
        f"✨ 换装成功\n"
        f"{price_line}\n"
        f"────────\n"
        f"学姐已经换上啦，快看看右边这张图！\n"
        f"剩余积分 {remaining}"
    )


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _fit_image(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_w, target_h = size
    src = img.convert("RGBA")
    ratio = min(target_w / src.width, target_h / src.height)
    new_w = max(1, int(src.width * ratio))
    new_h = max(1, int(src.height * ratio))
    resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", size, (255, 255, 255, 0))
    offset = ((target_w - new_w) // 2, (target_h - new_h) // 2)
    canvas.paste(resized, offset, resized)
    return canvas


async def _outfit_worn_path(
    item: ClothingItem,
    store: ShopStore,
    config: PluginConfig,
) -> Path | None:
    """学姐穿着该款式的立绘（购买/换装用，与左侧纯衣服缩略图分开生成）。"""
    worn = store.outfit_dir / f"{item.id}.png"
    if worn.is_file() and worn.stat().st_size > 0:
        return worn
    prompt = build_angelina_outfit_prompt(item.outfit_en)
    generated = await generate_image_file(prompt, config)
    if generated is None:
        return None
    try:
        with Image.open(generated) as img:
            img.convert("RGBA").save(worn, format="PNG")
        logger.info(f"dl_senpai shop outfit worn cached id={item.id}")
        return worn
    except OSError as exc:
        logger.warning(f"dl_senpai shop outfit worn save failed {item.id}: {exc}")
        return generated


async def _thumb_path(
    item: ClothingItem,
    store: ShopStore,
    config: PluginConfig,
) -> Path | None:
    """商店格子：仅衣服（人台/平铺），不含学姐。"""
    cached = store.thumb_dir / f"{item.id}.png"
    if cached.is_file() and cached.stat().st_size > 0:
        return cached
    prompt = item.thumb_prompt.strip()
    if not prompt:
        prompt = (
            f"fashion catalog thumbnail, {item.outfit_en} on mannequin or flat lay, "
            "soft pastel background, no person, no face, clothing only"
        )
    generated = await generate_image_file(prompt, config)
    if generated is None:
        return None
    try:
        with Image.open(generated) as img:
            thumb = img.convert("RGBA")
            thumb = thumb.resize((256, 256), Image.Resampling.LANCZOS)
            thumb.save(cached, format="PNG")
        return cached
    except OSError as exc:
        logger.warning(f"dl_senpai shop thumb save failed {item.id}: {exc}")
        return generated


async def _plain_senpai_path(store: ShopStore, config: PluginConfig) -> Path | None:
    cached = store.composite_dir / "plain_senpai.png"
    if cached.is_file() and cached.stat().st_size > 0:
        return cached
    generated = await generate_image_file(_PLAIN_SENPAI_PROMPT, config)
    if generated is None:
        return None
    try:
        with Image.open(generated) as img:
            img.convert("RGBA").save(cached, format="PNG")
        return cached
    except OSError:
        return generated


def _truncate_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        if draw.textlength(raw, font=font) <= max_width:
            return raw
        for n in range(len(raw), 0, -1):
            candidate = raw[:n] + "…"
            if draw.textlength(candidate, font=font) <= max_width:
                return candidate
    except Exception:  # noqa: BLE001
        if len(raw) > 10:
            return raw[:9] + "…"
    return raw


async def build_shop_composite(
    quotes: list,
    store: ShopStore,
    config: PluginConfig,
) -> Path | None:
    from .shop_discount import ShopItemQuote

    if not quotes:
        return None
    typed_quotes: list[ShopItemQuote] = [q for q in quotes if isinstance(q, ShopItemQuote)]
    if not typed_quotes:
        return None
    width, height = 1536, 1024
    canvas = Image.new("RGB", (width, height), (248, 245, 252))
    draw = ImageDraw.Draw(canvas)

    left_w = int(width * 0.46)
    draw.rectangle((0, 0, left_w, height), fill=(252, 248, 255))
    draw.line((left_w, 24, left_w, height - 24), fill=(220, 210, 235), width=2)

    title_font = _load_font(28)
    label_font = _load_font(18)
    small_font = _load_font(15)
    draw.text((28, 20), "学姐服装店", fill=(90, 70, 110), font=title_font)

    left_pad = 28
    top_pad = 72
    col_gap = 14
    row_gap = 16
    label_h = 50
    grid_inner_w = left_w - left_pad * 2 - col_gap
    cell_w = max(120, grid_inner_w // SHOP_GRID_COLS)
    thumb_box = min(cell_w - 8, 176)
    cell_total_h = thumb_box + label_h
    grid_total_h = SHOP_GRID_ROWS * cell_total_h + (SHOP_GRID_ROWS - 1) * row_gap
    start_y = top_pad + max(0, (height - top_pad - 24 - grid_total_h) // 2)

    display_quotes = typed_quotes[:SHOP_SLOT_COUNT]
    for idx, quote in enumerate(display_quotes):
        item = quote.item
        col = idx % SHOP_GRID_COLS
        row = idx // SHOP_GRID_COLS
        x = left_pad + col * (cell_w + col_gap)
        y = start_y + row * (cell_total_h + row_gap)
        draw.rounded_rectangle(
            (x, y, x + thumb_box, y + thumb_box),
            radius=14,
            fill=(255, 255, 255),
            outline=(210, 195, 225),
            width=2,
        )
        thumb_path = await _thumb_path(item, store, config)
        if thumb_path is not None:
            try:
                with Image.open(thumb_path) as thumb_img:
                    inner = thumb_box - 16
                    fitted = _fit_image(thumb_img, (inner, inner))
                    canvas.paste(fitted, (x + 8, y + 8), fitted)
            except OSError as exc:
                logger.warning(f"dl_senpai shop paste thumb failed: {exc}")

        text_y = y + thumb_box + 4
        name_text = _truncate_text(draw, item.name, label_font, thumb_box + 20)
        draw.text((x, text_y), name_text, fill=(60, 45, 80), font=label_font)
        if quote.has_discount:
            price_text = f"{quote.sale_price}分 -{quote.discount_percent}%"
            price_color = (200, 80, 110)
        else:
            price_text = f"{quote.sale_price} 积分"
            price_color = (130, 100, 150)
        draw.text(
            (x, text_y + 24),
            price_text,
            fill=price_color,
            font=small_font,
        )

    senpai_path = await _plain_senpai_path(store, config)
    right_x = left_w + 24
    right_w = width - right_x - 24
    right_h = height - 48
    if senpai_path is not None:
        try:
            with Image.open(senpai_path) as senpai_img:
                fitted = _fit_image(senpai_img, (right_w, right_h))
                canvas.paste(
                    fitted,
                    (right_x, 24),
                    fitted,
                )
        except OSError as exc:
            logger.warning(f"dl_senpai shop paste senpai failed: {exc}")
    else:
        draw.text(
            (right_x + 40, height // 2 - 20),
            "学姐正在整理衣架…",
            fill=(120, 100, 140),
            font=label_font,
        )

    out = store.composite_dir / (
        f"shop_{hash(tuple((q.item.id, q.sale_price) for q in typed_quotes)) & 0xFFFFFFFF:08x}.png"
    )
    canvas.save(out, format="PNG")
    return out


async def build_outfit_image_segment(
    item: ClothingItem,
    config: PluginConfig | None = None,
    store: ShopStore | None = None,
    *,
    archive_session_id: str = "",
    archive_source_user: str = "",
    archive_source_user_id: str = "",
    user_request: str = "",
) -> MessageSegment | None:
    cfg = config or get_config()
    shop_store = store or get_shop_store(cfg.shop_dir)
    path = await _outfit_worn_path(item, shop_store, cfg)
    if path is None:
        return None
    send_path = path
    if archive_session_id:
        from .draw_store import get_draw_store

        prompt = build_angelina_outfit_prompt(item.outfit_en)
        archived = get_draw_store(cfg).archive_from_cache(
            archive_session_id,
            cache_path=path,
            prompt=prompt,
            scene="outfit",
            user_request=user_request,
            source_user=archive_source_user,
            source_user_id=archive_source_user_id,
            model=cfg.draw_model_resolved(),
            size=cfg.draw_size.strip() or "1024x1024",
        )
        if archived is not None:
            send_path = archived
    return MessageSegment.image(file=send_path.resolve().as_uri())


async def handle_shop_open(
    group_id: int | str,
    *,
    user_id: int | str,
    points: int,
    affection_value: int | None,
    store: ShopStore,
    config: PluginConfig,
    rng: random.Random | None = None,
) -> tuple[str, MessageSegment | None]:
    from .shop_discount import format_discount_hint, quote_shop_items
    from .shop_replenish import ensure_shop_ready

    items, replenish_note = await ensure_shop_ready(
        group_id, store, config, rng=rng
    )
    refresh_day = store.shop_refresh_day()
    quotes = quote_shop_items(
        items,
        group_id=group_id,
        user_id=user_id,
        affection_value=affection_value,
        refresh_day=refresh_day,
        config=config,
    )
    discount_hint = format_discount_hint(affection_value, config)
    text = format_shop_open_text(
        quotes,
        points=points,
        store=store,
        group_id=group_id,
        discount_hint=discount_hint,
    )
    if replenish_note:
        text = f"{text}\n{replenish_note}"
    if not quotes:
        return text, None
    if not config.draw_enable or not config.draw_configured():
        return (
            f"{text}\n\n（生图服务未配置，暂时只能看文字列表哦～）",
            None,
        )
    composite = await build_shop_composite(quotes, store, config)
    if composite is None:
        return f"{text}\n\n（商店拼图没拼出来，晚点再试～）", None
    return text, MessageSegment.image(file=composite.resolve().as_uri())


def handle_shop_warehouse(
    group_id: int | str,
    user_id: int | str,
    store: ShopStore,
    *,
    user_name: str,
) -> str:
    current = store.get_current_outfit(group_id)
    return format_warehouse_text(
        store,
        group_id,
        user_id=user_id,
        user_name=user_name,
        current_name=current.name if current else None,
    )


async def handle_shop_wear(
    group_id: int | str,
    user_id: int | str,
    wear_text: str,
    *,
    display_name: str,
    store: ShopStore,
    config: PluginConfig,
) -> WearResult:
    owned = store.list_warehouse_items(group_id, user_id)
    item = store.match_item_in_owned(wear_text, owned)
    if item is None:
        names = "、".join(i.name for i in owned[:8]) if owned else "（仓库空空）"
        return WearResult(
            ok=False,
            message=(
                f"仓库里没有「{_normalize_wear_query(wear_text)}」哦～\n"
                f"你已收藏：{names}\n"
                "先发「仓库」看看，或去「商店」买新的～"
            ),
        )
    store.set_current_outfit(group_id, item.id)
    image = None
    if config.draw_enable and config.draw_configured():
        image = await build_outfit_image_segment(
            item,
            config,
            store=store,
            archive_session_id=session_id_for_group(group_id),
            archive_source_user=display_name,
            archive_source_user_id=str(user_id),
            user_request=f"换上{item.name}",
        )
    msg = (
        f"👗 换装完成\n"
        f"{display_name} 给学姐换上了「{item.name}」～\n"
        "────────\n"
        "仓库里的衣服随时可以再换，不用重复买哦。"
    )
    if image is None and config.draw_enable:
        msg += "\n（图没画出来……但衣服已经换好了～）"
    return WearResult(ok=True, message=msg, outfit_image=image)


async def handle_shop_purchase(
    group_id: int | str,
    user_id: int | str,
    buy_text: str,
    *,
    display_name: str,
    store: ShopStore,
    checkin_store: Any,
    config: PluginConfig,
    affection_value: int | None = 0,
    rng: random.Random | None = None,
) -> PurchaseResult:
    rng = rng or random.Random()
    from .shop_discount import quote_shop_item
    from .shop_replenish import ensure_shop_ready

    items, _ = await ensure_shop_ready(group_id, store, config, rng=rng)
    item = store.match_item_in_slots(buy_text, items)
    if item is None:
        names = "、".join(i.name for i in items) if items else "（店里空空）"
        return PurchaseResult(
            ok=False,
            message=(
                f"店里好像没有「{_normalize_buy_query(buy_text)}」哦～\n"
                f"当前在售：{names}\n"
                "先发「商店」看看货架～"
            ),
        )

    quote = quote_shop_item(
        item,
        group_id=group_id,
        user_id=user_id,
        affection_value=affection_value,
        refresh_day=store.shop_refresh_day(),
        config=config,
    )
    charge_price = quote.sale_price

    ok_spend, spend_msg, user = checkin_store.spend_points(
        group_id,
        user_id,
        charge_price,
    )
    if not ok_spend:
        if quote.has_discount:
            spend_msg = (
                f"{spend_msg}\n"
                f"（这件今天对你特惠 {quote.sale_price} 分，原价 {quote.list_price}）"
            )
        return PurchaseResult(ok=False, message=spend_msg, remaining_points=user.points)

    store.set_current_outfit(group_id, item.id)
    store.add_to_warehouse(
        group_id,
        item.id,
        buyer_id=user_id,
        buyer_name=display_name,
    )
    store.remove_from_slots(group_id, item.id)

    image = None
    if config.draw_enable and config.draw_configured():
        image = await build_outfit_image_segment(
            item,
            config,
            store=store,
            archive_session_id=session_id_for_group(group_id),
            archive_source_user=display_name,
            archive_source_user_id=str(user_id),
            user_request=f"购买{item.name}",
        )

    msg = format_purchase_ok(
        name=display_name,
        item_name=item.name,
        price=charge_price,
        remaining=user.points,
        list_price=quote.list_price if quote.has_discount else None,
        discount_percent=quote.discount_percent,
    )
    msg += "\n已收入你的「仓库」，随时可「换上」再穿～"
    msg += "\n（今天货架空出来的格子明天凌晨 4 点会换新款哦）"
    if image is None and config.draw_enable:
        msg += "\n（图没画出来……但衣服已经记下了，下次看学姐就会穿～）"

    return PurchaseResult(
        ok=True,
        message=msg,
        item=item,
        remaining_points=user.points,
        outfit_image=image,
    )


_catalog: ShopCatalog | None = None
_store: ShopStore | None = None


def get_shop_catalog(catalog_path: str | Path | None = None) -> ShopCatalog:
    global _catalog
    if catalog_path is not None:
        return ShopCatalog(Path(catalog_path))
    if _catalog is None:
        cfg = get_config()
        path = Path(cfg.shop_catalog_path)
        if not path.is_file():
            path = cfg.project_root() / "data/dl_senpai/shop/catalog.json"
        _catalog = ShopCatalog(path)
    return _catalog


def get_shop_store(
    data_dir: str | Path | None = None,
    catalog: ShopCatalog | None = None,
) -> ShopStore:
    global _store
    if data_dir is not None:
        cat = catalog or get_shop_catalog()
        return ShopStore(data_dir, cat)
    if _store is None:
        cfg = get_config()
        _store = ShopStore(cfg.shop_dir, get_shop_catalog())
    return _store


def reset_shop_store() -> None:
    global _store, _catalog
    _store = None
    _catalog = None


def group_outfit_en(group_id: int | str) -> str | None:
    store = get_shop_store()
    item = store.get_current_outfit(group_id)
    if item is None:
        return None
    return item.outfit_en


def group_outfit_name(group_id: int | str) -> str | None:
    store = get_shop_store()
    item = store.get_current_outfit(group_id)
    if item is None:
        return None
    return item.name
