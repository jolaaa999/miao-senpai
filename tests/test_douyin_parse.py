"""抖音解析：接口给什么就用什么，结构不对不许瞎猜。

抖音的响应字段经常整块消失（广告卡、直播卡、风控返回空列表），
所以这一层测试的重点是「缺东西时不要编」：
1. 直播卡片没有 video.duration，不能当成刷到一个视频；
2. 没有 aweme_id 的直接丢；
3. 评论里空文本丢掉；
4. 描述里的换行要压平（抖音描述常带一堆换行和话题标签）。
"""

from __future__ import annotations

from dl_senpai.douyin import (
    DouyinVideo,
    parse_comments,
    parse_feed,
    render_feed,
    render_video,
)


def _item(aweme_id="7300000000000000000", **over):
    item = {
        "aweme_id": aweme_id,
        "desc": "今天这条鱼\n#钓鱼 #日常",
        "author": {"nickname": "钓鱼佬"},
        "statistics": {"digg_count": 12000, "comment_count": 300, "share_count": 8},
        "video": {"duration": 15300},
        "create_time": 1793000000,
        "music": {"title": "原声"},
    }
    item.update(over)
    return item


def test_parse_feed_basic() -> None:
    rows = parse_feed({"aweme_list": [_item()]})
    assert len(rows) == 1
    video = rows[0]
    assert video.aweme_id == "7300000000000000000"
    assert video.author == "钓鱼佬"
    assert video.digg == 12000
    assert video.duration_sec == 15
    # 换行要压平，否则提示词里一行变三行
    assert "\n" not in video.desc
    assert video.url.endswith("7300000000000000000")


def test_live_card_without_video_is_skipped() -> None:
    # 直播卡没有 video 字段；当成视频会让她"刷"到一个不存在的东西
    rows = parse_feed({"aweme_list": [_item(), {"aweme_id": "1", "desc": "直播中"}]})
    assert [r.aweme_id for r in rows] == ["7300000000000000000"]


def test_missing_id_is_skipped() -> None:
    assert parse_feed({"aweme_list": [_item(aweme_id="")]}) == []


def test_empty_and_broken_payload() -> None:
    assert parse_feed({}) == []
    assert parse_feed({"aweme_list": None}) == []
    assert parse_feed({"aweme_list": ["不是字典"]}) == []


def test_parse_comments() -> None:
    rows = parse_comments(
        {
            "comments": [
                {"text": "这鱼真大", "user": {"nickname": "路人甲"}, "digg_count": 30},
                {"text": "   ", "user": {"nickname": "空"}},
            ]
        }
    )
    assert rows == [("路人甲", "这鱼真大", 30)]


def test_render_helpers() -> None:
    assert render_feed([]) == "抖音这次没刷出东西。"
    video = DouyinVideo(aweme_id="1", desc="标题", author="某人", digg=12000)
    assert "1.2万" in render_feed([video])
    assert "某人" in render_video(video)
