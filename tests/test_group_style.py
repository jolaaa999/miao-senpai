from __future__ import annotations

import shutil
from pathlib import Path

from dl_senpai.group_style import (
    GroupStyleStore,
    build_group_style_brief,
    extract_style_tokens,
)

_TEST_ROOT = Path(__file__).resolve().parent / "_tmp_group_style"


def _fresh_dir(name: str) -> Path:
    path = _TEST_ROOT / name
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_extract_tokens_filters_stopwords() -> None:
    toks = extract_style_tokens("这个冰牙龙真的难打 太刀见切")
    assert "冰牙龙" in toks or "太刀" in toks or "见切" in toks
    assert "这个" not in toks
    assert "真的" not in toks


def test_observe_and_brief() -> None:
    store = GroupStyleStore(_fresh_dir("observe"))
    for _ in range(5):
        store.observe(99, "冰牙龙 太刀 见切 配装")
    brief = build_group_style_brief(store, 99, top_n=5, min_count=3)
    assert "【本群常说】" in brief
    assert "冰牙龙" in brief or "太刀" in brief


def test_brief_empty_when_sparse() -> None:
    store = GroupStyleStore(_fresh_dir("sparse"))
    store.observe(1, "随便一句")
    assert build_group_style_brief(store, 1, min_count=3) == ""
