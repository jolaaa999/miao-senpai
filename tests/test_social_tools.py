"""B站 / 小黑盒只读层的测试。

钉三件事，都是「说错就丢人」的：
1. marker 必须解析干净——漏一半进群里，她就像在对方面前念代码；
2. 提示词里必须写清「你看到的是文字」——否则她会描述自己没看过的画面；
3. 每日配额必须真的拦得住、失败要能退回——否则额度很快就被人白嫖光。
抓取效果靠真实链接验证，单测不碰网络。
"""

from __future__ import annotations

import asyncio

import pytest

from dl_senpai.bili import BiliVideo, extract_bvid, render_hot, render_search, render_video
from dl_senpai.heybox import HeyboxPost, HeyboxSearch, render_heybox_search
from dl_senpai.quota import check_quota, read_usage, refund_quota
from dl_senpai.agent.tools.social_tools import (
    _parse_bili,
    _parse_heybox,
    build_web_extra_tools,
)


class _Cfg:
    """最小配置替身。"""

    def __init__(self, path: str = "quota.json") -> None:
        self.quota_file = path
        self.bili_enable = True
        self.bili_daily_limit = 60
        self.bili_timeout = 15.0
        self.heybox_enable = True
        self.heybox_daily_limit = 40
        self.heybox_timeout = 30.0


# ---------------------------------------------------------------- marker 解析


def test_parse_bili_hot_default() -> None:
    cleaned, calls = _parse_bili("看看热榜 <<<BILI hot>>>")
    assert cleaned.strip() == "看看热榜"
    assert len(calls) == 1
    assert calls[0].arguments == {"action": "hot", "limit": 10}


def test_parse_bili_hot_limit() -> None:
    _cleaned, calls = _parse_bili("<<<BILI hot 20>>>")
    assert calls[0].arguments["limit"] == 20


def test_parse_bili_hot_limit_capped() -> None:
    """超过 20 条的请求要压回去：一次抓一百条只会把时间预算烧光。"""
    _cleaned, calls = _parse_bili("<<<BILI hot 200>>>")
    assert calls[0].arguments["limit"] == 20


def test_parse_bili_search_keeps_spaces() -> None:
    cleaned, calls = _parse_bili("帮我找 <<<BILI search 怪物猎人 太刀配装>>> 这个")
    assert calls[0].arguments["action"] == "search"
    assert calls[0].arguments["keyword"] == "怪物猎人 太刀配装"
    assert "<<<" not in cleaned and "BILI" not in cleaned


def test_parse_bili_video() -> None:
    _cleaned, calls = _parse_bili("<<<BILI video BV1xx411c7mD>>>")
    assert calls[0].arguments["action"] == "video"
    assert calls[0].arguments["ref"] == "BV1xx411c7mD"


def test_parse_bili_empty_search_dropped() -> None:
    """只写了 search 却没给词：不产生调用，而且 marker 要清掉。"""
    cleaned, calls = _parse_bili("<<<BILI search>>>")
    assert calls == []
    assert "BILI" not in cleaned


def test_parse_bili_tail_marker_removed() -> None:
    """流式输出被截断时，半截 marker 绝不能漏进群里。"""
    cleaned, calls = _parse_bili("行，我去看看\n<<<BILI hot")
    assert calls == []
    assert "BILI" not in cleaned


def test_parse_heybox_search() -> None:
    cleaned, calls = _parse_heybox("<<<HEYBOX 大肥鱼 部署>>>")
    assert calls[0].arguments["action"] == "search"
    assert calls[0].arguments["keyword"] == "大肥鱼 部署"
    assert "HEYBOX" not in cleaned


def test_parse_heybox_read() -> None:
    _cleaned, calls = _parse_heybox("<<<HEYBOX read 8e7f729adcb3>>>")
    assert calls[0].arguments == {"action": "read", "ref": "8e7f729adcb3"}


def test_parse_heybox_read_without_ref_dropped() -> None:
    cleaned, calls = _parse_heybox("<<<HEYBOX read>>>")
    assert calls == []
    assert "HEYBOX" not in cleaned


# ---------------------------------------------------------------- BV 号识别


@pytest.mark.parametrize(
    "text,expected",
    [
        ("BV1xx411c7mD", "BV1xx411c7mD"),
        ("https://www.bilibili.com/video/BV1GJ411x7h7?p=2", "BV1GJ411x7h7"),
        ("av80433022", "av80433022"),
        ("https://www.bilibili.com/video/av80433022", "av80433022"),
    ],
)
def test_extract_bvid(text: str, expected: str) -> None:
    assert extract_bvid(text) == expected


def test_extract_bvid_rejects_garbage() -> None:
    assert extract_bvid("https://www.bilibili.com/") == ""
    assert extract_bvid("没啥") == ""


# ---------------------------------------------------------------- 每日配额


def test_quota_allows_then_blocks(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "quota.json"))

    async def run() -> None:
        assert await check_quota("bili", 2, cfg) == (True, 1)
        assert await check_quota("bili", 2, cfg) == (True, 2)
        assert await check_quota("bili", 2, cfg) == (False, 2)
        # 不同计数器互不影响
        assert await check_quota("heybox", 2, cfg) == (True, 1)

    asyncio.run(run())


def test_quota_refund(tmp_path) -> None:
    """失败要能退回——否则一次超时白扣一格，她很快就"没额度"了。"""
    cfg = _Cfg(str(tmp_path / "quota.json"))

    async def run() -> None:
        await check_quota("bili", 5, cfg)
        await check_quota("bili", 5, cfg)
        assert await read_usage("bili", cfg) == 2
        await refund_quota("bili", cfg)
        assert await read_usage("bili", cfg) == 1
        # 退到 0 之后再退也不该变成负数
        await refund_quota("bili", cfg)
        await refund_quota("bili", cfg)
        assert await read_usage("bili", cfg) == 0

    asyncio.run(run())


def test_quota_zero_limit_means_unlimited(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "quota.json"))

    async def run() -> None:
        for _ in range(5):
            allowed, _used = await check_quota("bili", 0, cfg)
            assert allowed is True

    asyncio.run(run())


# ---------------------------------------------------------------- 渲染话术


def _video() -> BiliVideo:
    return BiliVideo(
        bvid="BV1xx411c7mD",
        aid=1,
        title="【MHWI】冰原太刀配装",
        author="某UP",
        desc="简介内容",
        duration="12:34",
        play=123456,
        pubdate=1700000000,
        url="https://www.bilibili.com/video/BV1xx411c7mD",
        comments=[("路人", "这套不错", 12)],
    )


def test_render_hot_marks_unwatched() -> None:
    text = render_hot([_video()])
    assert "BV1xx411c7mD" in text
    # 关键：不许把标题当正文聊
    assert "没点开" in text or "不要编" in text


def test_render_search_hints_video_action() -> None:
    text = render_search([_video()], keyword="太刀")
    assert "太刀" in text
    assert "<<<BILI video" in text


def test_render_video_keeps_comments_and_disclaimer() -> None:
    text = render_video(_video())
    assert "热评" in text and "这套不错" in text
    assert "不要描述视频里" in text


def test_render_video_without_comments_says_so() -> None:
    video = _video()
    video.comments = []
    text = render_video(video)
    assert "没拿到" in text


def test_render_heybox_search_points_to_read() -> None:
    result = HeyboxSearch(
        keyword="大肥鱼",
        posts=[HeyboxPost(link_id="19", share_id="8e7f729adcb3", title="如何部署", desc="步骤")],
    )
    text = render_heybox_search(result)
    assert "如何部署" in text
    assert "<<<HEYBOX read 8e7f729adcb3>>>" in text


# ---------------------------------------------------------------- 注册开关


def test_build_tools_respects_enable() -> None:
    cfg = _Cfg()
    assert {t.name for t in build_web_extra_tools(cfg)} == {"bili", "heybox"}

    cfg.bili_enable = False
    assert {t.name for t in build_web_extra_tools(cfg)} == {"heybox"}

    cfg.heybox_enable = False
    assert build_web_extra_tools(cfg) == []


def test_tools_are_read_only() -> None:
    """只读层不许有副作用标记——这决定了她"重跑一次"是否安全。"""
    for tool in build_web_extra_tools(_Cfg()):
        assert tool.side_effect is False
        assert tool.owner == "social_layer"
