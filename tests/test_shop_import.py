from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from dl_senpai.shop_import import (
    _parse_analyze_json,
    add_catalog_item,
    catalog_has_name,
    list_catalog_items,
    load_catalog,
)


_WORK_TMP = Path(__file__).resolve().parents[1] / ".pytest_tmp"


@pytest.fixture
def catalog_path() -> Path:
    _WORK_TMP.mkdir(parents=True, exist_ok=True)
    p = _WORK_TMP / f"catalog-{uuid.uuid4().hex}.json"
    p.write_text(json.dumps({"items": []}), encoding="utf-8")
    return p


def test_parse_analyze_json() -> None:
    raw = (
        '{"name":"蓝色洛丽塔","outfit_en":"blue lolita dress with lace",'
        '"thumb_prompt":"blue dress on mannequin","price":80,"category":"lolita"}'
    )
    parsed = _parse_analyze_json(raw)
    assert parsed is not None
    assert parsed.name == "蓝色洛丽塔"
    assert "lolita" in parsed.outfit_en
    assert parsed.price == 80


def test_add_catalog_item(catalog_path: Path) -> None:
    ok, msg, row = add_catalog_item(
        {
            "name": "测试JK",
            "outfit_en": "navy sailor uniform",
            "thumb_prompt": "sailor on mannequin",
            "price": 55,
            "category": "jk",
        },
        path=catalog_path,
    )
    assert ok
    assert row is not None
    assert row["name"] == "测试JK"
    assert catalog_has_name("测试JK", catalog_path)
    items = list_catalog_items(catalog_path)
    assert len(items) == 1

    ok2, msg2, _ = add_catalog_item(
        {"name": "测试JK", "outfit_en": "duplicate"},
        path=catalog_path,
    )
    assert not ok2
    assert "同名" in msg2


def test_load_catalog_missing_file() -> None:
    _WORK_TMP.mkdir(parents=True, exist_ok=True)
    missing = _WORK_TMP / f"missing-{uuid.uuid4().hex}.json"
    data = load_catalog(missing)
    assert data["items"] == []
