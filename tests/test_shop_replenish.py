from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from dl_senpai.shop import ShopCatalog, ShopStore, SHOP_SLOT_COUNT
from dl_senpai.shop_import import ImportResult
from dl_senpai.shop_replenish import needs_auto_replenish, try_auto_replenish

_WORK_TMP = Path(__file__).resolve().parents[1] / ".pytest_tmp"
_CATALOG = {
    "items": [
        {
            "id": "a",
            "name": "A款",
            "price": 50,
            "outfit_en": "dress a",
            "thumb_prompt": "a",
        },
    ]
}


@pytest.fixture
def store() -> ShopStore:
    _WORK_TMP.mkdir(parents=True, exist_ok=True)
    root = _WORK_TMP / f"replenish-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    cat_path = root / "catalog.json"
    cat_path.write_text(json.dumps(_CATALOG), encoding="utf-8")
    catalog = ShopCatalog(cat_path)
    return ShopStore(root / "data", catalog, slot_count=SHOP_SLOT_COUNT)


def test_needs_auto_replenish_when_catalog_small(store: ShopStore) -> None:
    gid = "g1"
    state = store.load_state(gid)
    state.slots = []
    state.slots_refresh_day = store.shop_refresh_day()
    store.save_state(gid, state)
    assert needs_auto_replenish(store, gid)


@pytest.mark.asyncio
async def test_try_auto_replenish_adds_items(store: ShopStore) -> None:
    gid = "g2"
    state = store.load_state(gid)
    state.slots = []
    state.slots_refresh_day = store.shop_refresh_day()
    store.save_state(gid, state)

    fake_item = {
        "id": "new_dress",
        "name": "新款裙",
        "price": 60,
        "outfit_en": "new dress",
        "thumb_prompt": "new",
    }
    fake_result = ImportResult(
        ok=True,
        message="ok",
        item=fake_item,
    )

    with patch(
        "dl_senpai.shop_import.import_from_search",
        new_callable=AsyncMock,
        return_value=[fake_result],
    ) as mock_import:
        added, note = await try_auto_replenish(gid, store)
        assert added == 1
        assert "上新" in note
        assert mock_import.called
        call_kw = mock_import.await_args.kwargs
        assert call_kw.get("catalog_path") == store.catalog.catalog_path
