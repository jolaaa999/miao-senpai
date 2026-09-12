from __future__ import annotations

from dl_senpai.browser_agent import (
    BrowseCooldown,
    infer_browse_tasks,
    parse_browse_request,
    resolve_browse_tasks,
    strip_browse_markers,
    wants_browser_help,
    _host_allowed,
    _parse_task_line,
    _validate_url,
)
from dl_senpai.config import PluginConfig
from dl_senpai.persona import build_system_prompt
import pytest


def test_parse_browse_request_taobao() -> None:
    parsed = parse_browse_request("好～学姐去瞅一眼\n<<<BROWSE taobao 黑色卫衣 女>>>")
    assert parsed.force is True
    assert len(parsed.tasks) == 1
    assert parsed.tasks[0].kind == "taobao"
    assert "黑色卫衣" in parsed.tasks[0].query
    assert "BROWSE" not in parsed.text


def test_parse_browse_request_pixiv() -> None:
    parsed = parse_browse_request("<<<BROWSE pixiv 明日方舟 壁纸>>>")
    assert parsed.tasks[0].kind == "pixiv"
    assert "明日方舟" in parsed.tasks[0].query


def test_parse_browse_request_screenshot_url() -> None:
    parsed = parse_browse_request("<<<BROWSE https://example.com>>>")
    assert parsed.tasks[0].kind == "screenshot"
    assert parsed.tasks[0].url == "https://example.com"


def test_wants_browser_help() -> None:
    assert wants_browser_help("@学姐 帮我去淘宝找黑色卫衣")
    assert wants_browser_help("去 pixiv 找明日方舟壁纸")
    assert wants_browser_help("截图这个网页 https://example.com")
    assert not wants_browser_help("别打开淘宝了")
    assert not wants_browser_help("你好")


def test_infer_browse_tasks() -> None:
    tasks = infer_browse_tasks("学姐帮我去淘宝找一件黑色卫衣")
    assert tasks and tasks[0].kind == "taobao"
    assert "黑色卫衣" in tasks[0].query

    tasks2 = infer_browse_tasks("去 pixiv 搜 原神 壁纸")
    assert tasks2 and tasks2[0].kind == "pixiv"


def test_infer_pixiv_monster_query() -> None:
    tasks = infer_browse_tasks(
        "去pixiv搜冰呪龙壁纸，提取生图提示词再优化"
    )
    assert tasks
    assert tasks[0].kind == "pixiv"
    assert "冰呪龙" in tasks[0].query


def test_resolve_browse_tasks_prefers_marker() -> None:
    marker = _parse_task_line("taobao 连衣裙")
    assert marker is not None
    tasks = resolve_browse_tasks(marker_tasks=[marker], user_request="随便搜搜")
    assert tasks[0].query == "连衣裙"


def test_strip_browse_markers() -> None:
    text = strip_browse_markers("找到了～\n<<<BROWSE image cat>>>")
    assert "BROWSE" not in text
    assert "找到了" in text


def test_host_allowed_public_when_no_custom_list() -> None:
    assert _host_allowed("example.com", None)
    assert _host_allowed("www.pixiv.net", None)
    assert not _host_allowed("127.0.0.1", None)


def test_validate_url_rejects_private_host() -> None:
    cfg = PluginConfig()
    with pytest.raises(Exception):
        _validate_url("http://127.0.0.1/page", cfg)


def test_validate_url_allows_default_domain() -> None:
    cfg = PluginConfig()
    url = _validate_url("https://www.pixiv.net/tags/test", cfg)
    assert url.startswith("https://")


def test_browse_cooldown() -> None:
    cd = BrowseCooldown()
    assert cd.ready("s1", 60, now=100.0)
    cd.mark("s1", now=100.0)
    assert not cd.ready("s1", 60, now=120.0)
    assert cd.ready("s1", 60, now=161.0)


def test_system_prompt_includes_browse_when_enabled() -> None:
    prompt = build_system_prompt(browser_enable=True)
    assert "BROWSE" in prompt
    assert "淘宝" in prompt
