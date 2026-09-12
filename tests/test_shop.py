from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from dl_senpai.checkin import CheckinStore
from dl_senpai.config import PluginConfig
from dl_senpai import draw_store as draw_store_module
from dl_senpai.draw_store import DrawStore
from dl_senpai.shop import (
    ClothingItem,
    ShopCatalog,
    ShopStore,
    _normalize_buy_query,
    build_outfit_image_segment,
    format_shop_open_text,
    handle_shop_warehouse,
    parse_shop_command,
)
from dl_senpai.shop_discount import quote_shop_item


_WORK_TMP = Path(__file__).resolve().parents[1] / ".pytest_tmp"
_CATALOG_SAMPLE = {
    "items": [
        {
            "id": "blue_lolita",
            "name": "蓝色洛丽塔",
            "price": 85,
            "outfit_en": "blue lolita dress",
            "thumb_prompt": "blue dress thumbnail",
        },
        {
            "id": "jk_sailor",
            "name": "JK水手服",
            "price": 55,
            "outfit_en": "sailor uniform",
            "thumb_prompt": "sailor thumbnail",
        },
        {
            "id": "maid_classic",
            "name": "经典女仆装",
            "price": 65,
            "outfit_en": "maid dress",
            "thumb_prompt": "maid thumbnail",
        },
        {
            "id": "hanfu_pink",
            "name": "粉色汉服",
            "price": 75,
            "outfit_en": "pink hanfu",
            "thumb_prompt": "hanfu thumbnail",
        },
        {
            "id": "pajamas",
            "name": "星星睡衣",
            "price": 38,
            "outfit_en": "pajamas",
            "thumb_prompt": "pajamas thumbnail",
        },
    ]
}


@pytest.fixture
def shop_fixture() -> tuple[ShopStore, CheckinStore, Path]:
    _WORK_TMP.mkdir(parents=True, exist_ok=True)
    root = _WORK_TMP / f"shop-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=True)
    catalog_path = root / "catalog.json"
    catalog_path.write_text(json.dumps(_CATALOG_SAMPLE), encoding="utf-8")
    catalog = ShopCatalog(catalog_path)
    shop_dir = root / "shop_data"
    checkin_dir = root / "checkin"
    return ShopStore(shop_dir, catalog, slot_count=4), CheckinStore(checkin_dir), root


def test_parse_shop_commands() -> None:
    assert parse_shop_command("商店") == "open"
    assert parse_shop_command("积分商店") == "open"
    assert parse_shop_command("购买蓝色洛丽塔") == "buy"
    assert parse_shop_command("使用积分购买蓝色洛丽塔") == "buy"
    assert parse_shop_command("积分买JK水手服") == "buy"
    assert parse_shop_command("商店帮助") == "help"
    assert parse_shop_command("仓库") == "warehouse"
    assert parse_shop_command("换上蓝色洛丽塔") == "wear"
    assert parse_shop_command("签到") == "none"


def test_normalize_buy_query() -> None:
    assert _normalize_buy_query("使用积分购买蓝色洛丽塔") == "蓝色洛丽塔"
    assert _normalize_buy_query("购买JK水手服") == "JK水手服"


def test_ensure_slots_and_daily_refresh(shop_fixture: tuple[ShopStore, CheckinStore, Path]) -> None:
    store, _, _ = shop_fixture
    gid = "12345"
    state, items = store.ensure_slots(gid)
    assert len(state.slots) == 4
    assert len(items) == 4
    assert state.slots_refresh_day == store.shop_refresh_day()

    first_id = state.slots[0]
    item = store.catalog.get(first_id)
    assert item is not None
    matched = store.match_item_in_slots(f"购买{item.name}", items)
    assert matched is not None

    store.remove_from_slots(gid, first_id)
    after_buy = store.load_state(gid)
    assert first_id not in after_buy.slots
    assert len(after_buy.slots) == 3

    # 同一天内不会自动补满空位
    _, items2 = store.ensure_slots(gid)
    assert len(items2) == 3

    # 模拟次日刷新
    state2 = store.load_state(gid)
    state2.slots_refresh_day = "2000-01-01"
    store.save_state(gid, state2)
    state3, items3 = store.ensure_slots(gid)
    assert len(state3.slots) == 4
    assert state3.slots_refresh_day == store.shop_refresh_day()


def test_spend_points_on_purchase(shop_fixture: tuple[ShopStore, CheckinStore, Path]) -> None:
    store, checkin, _ = shop_fixture
    gid, uid = "980229149", "10001"
    users = checkin.load_group(gid)
    from dl_senpai.checkin import UserCheckin

    users[uid] = UserCheckin(user_id=uid, points=200, display_name="测试")
    checkin.save_group(gid, users)

    _, items = store.ensure_slots(gid)
    target = items[0]
    ok, msg, user = checkin.spend_points(gid, uid, target.price)
    assert ok
    assert user.points == 200 - target.price
    assert msg == ""

    ok2, msg2, user2 = checkin.spend_points(gid, uid, 9999)
    assert not ok2
    assert "积分不够" in msg2
    assert user2.points == user.points


def test_format_shop_open_text() -> None:
    item = ClothingItem(
        id="a",
        name="蓝色洛丽塔",
        price=85,
        outfit_en="blue",
        thumb_prompt="t",
    )
    quote = quote_shop_item(
        item,
        group_id="g1",
        user_id="u1",
        affection_value=0,
        refresh_day="2026-08-24",
        config=PluginConfig(shop_affection_discount_enable=False),
    )
    text = format_shop_open_text([quote], points=120)
    assert "蓝色洛丽塔" in text
    assert "85 分" in text
    assert "120" in text


def test_warehouse_and_exhausted_catalog(
    shop_fixture: tuple[ShopStore, CheckinStore, Path],
) -> None:
    store, _, _ = shop_fixture
    gid = "group_exhaust"
    uid = "user_a"
    for item in store.catalog.all_items():
        store.add_to_warehouse(gid, item.id, buyer_id=uid, buyer_name="测试A")
    state, items = store.ensure_slots(gid)
    assert len(items) == 4

    wh = handle_shop_warehouse(gid, uid, store, user_name="测试A")
    assert "我的衣服仓库" in wh
    assert "蓝色洛丽塔" in wh or "JK水手服" in wh


def test_wear_from_warehouse(shop_fixture: tuple[ShopStore, CheckinStore, Path]) -> None:
    store, _, _ = shop_fixture
    gid = "wear_test"
    uid = "u1"
    store.add_to_warehouse(gid, "blue_lolita", buyer_id=uid, buyer_name="买家")
    owned = store.list_warehouse_items(gid, uid)
    matched = store.match_item_in_owned("换上蓝色洛丽塔", owned)
    assert matched is not None
    assert matched.id == "blue_lolita"


@pytest.mark.asyncio
async def test_outfit_image_archived_to_draw_store(
    shop_fixture: tuple[ShopStore, CheckinStore, Path],
) -> None:
    store, _, root = shop_fixture
    draw_root = root / "draw"
    item = ClothingItem(
        id="blue_lolita",
        name="蓝色洛丽塔",
        price=85,
        outfit_en="blue lolita dress",
        thumb_prompt="blue dress thumbnail",
    )
    generated = root / "generated.png"
    Image.new("RGBA", (8, 8), (100, 150, 200, 255)).save(generated, format="PNG")

    class _Cfg:
        shop_dir = store.data_dir
        draw_dir = draw_root
        draw_max_archive = 50
        draw_size = "1024x1024"

        def draw_model_resolved(self) -> str:
            return "gpt-image-test"

    cfg = _Cfg()
    draw_store_module._draw_store = None

    with patch(
        "dl_senpai.shop.generate_image_file",
        new=AsyncMock(return_value=generated),
    ):
        seg = await build_outfit_image_segment(
            item,
            cfg,
            store=store,
            archive_session_id="group:12345",
            archive_source_user="测试用户",
            archive_source_user_id="10001",
            user_request="购买蓝色洛丽塔",
        )

    assert seg is not None
    draw_store = DrawStore(draw_root)
    items = draw_store._load_raw("group:12345")
    assert len(items) == 1
    assert items[0]["scene"] == "outfit"
    assert items[0]["user_request"] == "购买蓝色洛丽塔"
    assert items[0]["source_user"] == "测试用户"
    assert items[0]["source_user_id"] == "10001"
    assert Path(items[0]["local_path"]).is_file()
