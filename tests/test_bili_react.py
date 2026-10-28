"""B站点赞 / 收藏：主人要的是「刷到的视频会点赞收藏」。

这层是真实账号行为，所以测试盯的是节制而不是功能：
1. 同一条视频只能赞一次（重复赞是明显的脚本特征）；
2. 当日上限要真的挡住，不然刷得勤的时候一天能点几十个；
3. 收藏只在模型说「值得回看」时才收——收藏夹变垃圾桶比不收藏更糟；
4. 接口报错要记进台账，不能静默当成功。
"""

from __future__ import annotations

import asyncio
import json

from dl_senpai import bili as bl
from dl_senpai import bili_actions as ba
from dl_senpai import bili_browse as bb


class Cfg:
    bili_browse_enable = True
    bili_browse_comment_enable = False
    bili_browse_transcript_enable = False
    bili_browse_diary_probability = 0.0
    bili_write_enable = False
    bili_browse_log_file = ""
    bili_memory_file = ""
    bili_actions_file = ""
    diary_enable = False
    bili_like_enable = True
    bili_like_mode = "always"
    bili_like_daily_max = 12
    bili_fav_enable = True
    bili_fav_daily_max = 6
    own_life_quiet_start = 3
    own_life_quiet_end = 8


class _Video:
    def __init__(self, bvid: str = "BV1xxx", title: str = "某个视频") -> None:
        self.bvid = bvid
        self.title = title
        self.owner = "某UP"
        self.desc = "简介"
        self.comments = []


def _setup(tmp_path, monkeypatch, *, answers, likes: list[str], favs: list[str], fail: str = ""):
    cfg = Cfg()
    cfg.bili_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.bili_memory_file = str(tmp_path / "memo.json")
    cfg.bili_actions_file = str(tmp_path / "actions.json")

    async def _feed(limit=12, *, cfg=None):
        # browse_once 现在走 bili_feed（返回 (视频列表, 来源)），不是 bili_hot。
        # 只 mock 旧名字等于没 mock：测试会直接去打真实 B 站接口，
        # 每次跑拿到的 bvid 都不一样（BV181Hh6bE27 / BV1kRn36oEvr），
        # 失败的报错看着像断言写错了，实际是网络真打了。
        return [_Video()], "hot"

    def _no_network(*args, **kwargs):
        # 没被 mock 的抓取入口一律炸出来：宁可测试红，也别静默打真网络。
        raise AssertionError("测试里不许打真实 B 站接口")

    async def _video(ref, *, comment_limit=5, cfg=None):
        return _Video()

    async def _like(ref, *, cfg=None, undo=False):
        if fail:
            raise bl.BiliError(fail)
        likes.append(ref)
        return {"ok": True}

    async def _fav(ref, *, cfg=None, folder_id=0, undo=False):
        favs.append(ref)
        return {"ok": True, "folder_id": 42}

    queue = list(answers)

    async def _ask(c, instruction, data, *, llm=None):
        return json.dumps(queue.pop(0) if queue else {}, ensure_ascii=False)

    monkeypatch.setattr(bl, "bili_feed", _feed)
    monkeypatch.setattr(bl, "bili_hot", _no_network)
    monkeypatch.setattr(bl, "bili_video", _video)
    monkeypatch.setattr(bl, "bili_like", _like)
    monkeypatch.setattr(bl, "bili_favorite", _fav)
    monkeypatch.setattr("dl_senpai.ask_json.ask_json", _ask)
    monkeypatch.setattr("dl_senpai.heybox_browse._in_quiet_hours", lambda cfg, now=None: False)
    monkeypatch.setattr("dl_senpai.diary.get_diary_store", lambda d=None: None)
    return cfg


def test_likes_the_video_it_watched(tmp_path, monkeypatch) -> None:
    likes: list[str] = []
    favs: list[str] = []
    cfg = _setup(tmp_path, monkeypatch, answers=[{"pick": 1}, {}], likes=likes, favs=favs)
    record = asyncio.run(bb.browse_once(cfg, force=True))
    assert record.get("like") is True
    assert likes == ["BV1xxx"]
    assert ba.count_today(cfg, "like") == 1


def test_same_video_is_not_liked_twice(tmp_path, monkeypatch) -> None:
    """同一条视频不能赞两次。

    2026-10-27 改：browse_once 现在带「最近刷过去重」（bili_browse_dedupe_hours），
    第二轮根本挑不到同一条，会直接 skipped=all_seen——所以 "already" 这条路
    在这一层已经走不到了。这里改成守去重，动作账本那层由下面那个用例单独守。
    """
    likes: list[str] = []
    favs: list[str] = []
    cfg = _setup(
        tmp_path,
        monkeypatch,
        answers=[{"pick": 1}, {}, {"pick": 1}, {}],
        likes=likes,
        favs=favs,
    )
    first = asyncio.run(bb.browse_once(cfg, force=True))
    second = asyncio.run(bb.browse_once(cfg, force=True))
    assert first.get("like") is True
    assert second.get("skipped") == "all_seen", "第二次不该再把同一条挑出来刷"
    assert likes == ["BV1xxx"], "同一条被赞了两次"


def test_react_refuses_a_second_like_on_the_same_bvid(tmp_path, monkeypatch) -> None:
    """绕过去重直接调 _react：动作账本本身也必须挡住重复赞。

    为什么单独守：去重靠的是浏览台账（12 小时窗口）。窗口一过，同一条视频
    会重新出现在推荐里——那时候唯一还挡着重复赞的就是 bili_actions 账本。
    """
    likes: list[str] = []
    favs: list[str] = []
    cfg = _setup(tmp_path, monkeypatch, answers=[], likes=likes, favs=favs)
    video = _Video()
    first: dict = {}
    second: dict = {}
    asyncio.run(bb._react(cfg, video, {}, first))
    asyncio.run(bb._react(cfg, video, {}, second))
    assert first.get("like") is True
    assert second.get("like") == "already"
    assert likes == ["BV1xxx"], "同一条被赞了两次"


def test_daily_cap_blocks_further_likes(tmp_path, monkeypatch) -> None:
    likes: list[str] = []
    favs: list[str] = []
    cfg = _setup(tmp_path, monkeypatch, answers=[{"pick": 1}, {}], likes=likes, favs=favs)
    cfg.bili_like_daily_max = 1
    ba.note(cfg, "like", "BV0old", title="昨天已经赞过的另一条")
    record = asyncio.run(bb.browse_once(cfg, force=True))
    assert record.get("like") == "daily_cap"
    assert likes == []


def test_favorites_only_when_model_says_worth_it(tmp_path, monkeypatch) -> None:
    likes: list[str] = []
    favs: list[str] = []
    cfg = _setup(
        tmp_path,
        monkeypatch,
        answers=[{"pick": 1}, {"fav": True, "fav_why": "以后照着做"}],
        likes=likes,
        favs=favs,
    )
    record = asyncio.run(bb.browse_once(cfg, force=True))
    assert record.get("fav") is True
    assert record.get("fav_why") == "以后照着做"
    assert favs == ["BV1xxx"]


def test_not_favorited_when_model_says_no(tmp_path, monkeypatch) -> None:
    likes: list[str] = []
    favs: list[str] = []
    cfg = _setup(tmp_path, monkeypatch, answers=[{"pick": 1}, {"fav": False}], likes=likes, favs=favs)
    record = asyncio.run(bb.browse_once(cfg, force=True))
    assert "fav" not in record
    assert favs == []


def test_like_failure_is_recorded(tmp_path, monkeypatch) -> None:
    likes: list[str] = []
    favs: list[str] = []
    cfg = _setup(
        tmp_path,
        monkeypatch,
        answers=[{"pick": 1}, {}],
        likes=likes,
        favs=favs,
        fail="B站没接受点赞：code=-412 被 B 站风控挡了，先停一会儿",
    )
    record = asyncio.run(bb.browse_once(cfg, force=True))
    assert "-412" in record.get("like_error", "")
    # 失败不能算进账本，不然今天就再也点不了了
    assert ba.count_today(cfg, "like") == 0
