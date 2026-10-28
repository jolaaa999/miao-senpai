"""自画像：挑图 / 无素材降级 / marker 解析。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from dl_senpai.portrait import (
    execute_self_image,
    list_portraits,
    parse_portrait_request,
    pick_portrait,
    portrait_dir,
)


class _Cfg:
    self_image_enable = True
    self_character = ""

    def __init__(self, tmp_path):
        self.self_image_dir = str(tmp_path / "self")


def _write(path, size=(32, 32)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (10, 200, 100)).save(path)
    return path


def test_portrait_dir_follows_config(tmp_path):
    cfg = _Cfg(tmp_path)
    assert portrait_dir(cfg).name == "self"


def test_list_portraits_only_images(tmp_path):
    cfg = _Cfg(tmp_path)
    _write(portrait_dir(cfg) / "a.png")
    _write(portrait_dir(cfg) / "b.jpg")
    (portrait_dir(cfg) / "notes.txt").write_text("x", encoding="utf-8")
    names = [p.name for p in list_portraits(cfg)]
    assert names == ["a.png", "b.jpg"]


def test_list_portraits_skips_normalized_copies(tmp_path):
    """归一化产物 *.norm.jpg 不能混进素材池，否则同一张图会挑两次。"""
    cfg = _Cfg(tmp_path)
    _write(portrait_dir(cfg) / "a.png")
    _write(portrait_dir(cfg) / "a.norm.jpg")
    assert [p.name for p in list_portraits(cfg)] == ["a.png"]


def test_list_portraits_missing_dir_is_empty(tmp_path):
    assert list_portraits(_Cfg(tmp_path)) == []


def test_pick_prefers_hint_match(tmp_path):
    cfg = _Cfg(tmp_path)
    _write(portrait_dir(cfg) / "全身.png")
    _write(portrait_dir(cfg) / "头像.png")
    picked = pick_portrait(cfg, "全身", rng=lambda items: items[0])
    assert picked.name == "全身.png"


def test_pick_falls_back_to_random_when_hint_misses(tmp_path):
    cfg = _Cfg(tmp_path)
    _write(portrait_dir(cfg) / "a.png")
    # 关键词「侧脸」在素材里不存在：不该失败，退化成随机挑一张
    picked = pick_portrait(cfg, "侧脸", rng=lambda items: items[0])
    assert picked.name == "a.png"


def test_pick_without_assets_is_none(tmp_path):
    assert pick_portrait(_Cfg(tmp_path)) is None


def test_parse_marker():
    text, calls = parse_portrait_request("等我找找 <<<SELFIMAGE 全身>>>")
    assert "SELFIMAGE" not in text
    assert calls[0].tool_name == "self_image"
    assert calls[0].arguments["hint"] == "全身"


@pytest.mark.asyncio
async def test_execute_without_assets_tells_model_not_to_fake_it(tmp_path):
    from dl_senpai.agent.tools.models import ToolCall

    ctx = SimpleNamespace(config=_Cfg(tmp_path), session_id="group:1")
    result = await execute_self_image(ctx, ToolCall(tool_name="self_image"))
    assert not result.success
    assert result.error_type == "no_assets"
    assert "别编" in result.error


@pytest.mark.asyncio
async def test_execute_sends_existing_asset(tmp_path):
    from dl_senpai.agent.tools.models import ToolCall

    cfg = _Cfg(tmp_path)
    _write(portrait_dir(cfg) / "全身.png", size=(2000, 2000))
    ctx = SimpleNamespace(config=cfg, session_id="group:1")
    result = await execute_self_image(ctx, ToolCall(tool_name="self_image"))
    assert result.success
    assert result.data["value"] is not None
    # 大图在出站前被压过，副本落在 cache 子目录（不能混回素材池）
    normalized = list((portrait_dir(cfg) / "cache").glob("*.jpg"))
    assert normalized, "2000px 的素材应该被归一化落盘一份"
    assert Image.open(normalized[0]).width <= 1280


@pytest.mark.asyncio
async def test_execute_respects_disable_switch(tmp_path):
    from dl_senpai.agent.tools.models import ToolCall

    cfg = _Cfg(tmp_path)
    cfg.self_image_enable = False
    ctx = SimpleNamespace(config=cfg, session_id="group:1")
    result = await execute_self_image(ctx, ToolCall(tool_name="self_image"))
    assert not result.success
    assert result.error_type == "disabled"
