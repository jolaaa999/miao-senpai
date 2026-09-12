from __future__ import annotations

import pytest

from dl_senpai.persona import build_system_prompt
from dl_senpai.web_search import (
    SearchHit,
    WebSearcher,
    build_auto_search_queries,
    expand_search_queries,
    format_search_for_prompt,
    parse_search_request,
    refine_search_query,
    score_hit_relevance,
    should_auto_search,
)


def test_parse_search_request() -> None:
    raw = "学姐去瞅一眼～\n<<<SEARCH 今日天气 北京>>>\n<<<SEARCH 今日天气 北京>>>"
    req = parse_search_request(raw)
    assert req.queries == ["今日天气 北京"]
    assert "<<<SEARCH" not in req.text
    assert "学姐去瞅一眼" in req.text


def test_parse_search_request_accepts_double_angle_close() -> None:
    raw = (
        "好的\n"
        "<<<SEARCH 怪物猎人世界 Wiki 珊瑚水妖鸟 弱点 肉质 冰属性 >>\n"
        "<<<SEARCH 怪物猎人世界 Wiki 冰属性弓 Icicle Blizzard>>"
    )
    req = parse_search_request(raw)
    assert len(req.queries) == 2
    assert "珊瑚水妖鸟" in req.queries[0]
    assert "冰属性弓" in req.queries[1]
    assert "<<<SEARCH" not in req.text


def test_format_search_for_prompt() -> None:
    hits = [
        SearchHit("标题A", "https://a.example", "摘要A", "bing"),
        SearchHit("标题B", "https://b.example", "摘要B", "bing"),
    ]
    text = format_search_for_prompt(hits, query="测试", max_chars=200)
    assert "联网检索结果" in text
    assert "标题A" in text
    assert "https://a.example" in text


def test_format_search_empty() -> None:
    text = format_search_for_prompt([], query="无结果")
    assert "没有搜到" in text


def test_persona_includes_search() -> None:
    prompt = build_system_prompt(search_enable=True)
    assert "联网检索" in prompt
    assert "自动联网" in prompt
    assert "防写串" in prompt or "防瞎编" in prompt
    assert "怪物猎人" in prompt
    assert "MHW伙伴" in prompt
    assert "<<<SEARCH" in prompt
    assert "联网检索" not in build_system_prompt(search_enable=False)


def test_persona_includes_monster_hunter_expertise() -> None:
    prompt = build_system_prompt()
    assert "十四把武器" in prompt
    assert "怪猎wiki" in prompt or "怪猎 wiki" in prompt
    assert "听龙车" in prompt or "压返回" in prompt


def test_should_not_auto_search_on_reply_meta_and_brackets() -> None:
    chat = (
        "【引用消息】喵喵喵？！：这次 先查清楚怪物资料和大师位可制作装\n"
        "【我现在说的】你最好去怪物猎人世界wiki里面搜索再回答"
    )
    assert not should_auto_search(chat)
    assert not should_auto_search("【我现在说的】你最好去怪物猎人世界wiki里面搜索再回答")


def test_user_intent_text_prefers_plain() -> None:
    from dl_senpai.reply_quote import user_intent_text

    assert user_intent_text("今天油价", "【引用消息】旧\n【我现在说的】别的") == "今天油价"


def test_should_auto_search() -> None:
    assert should_auto_search("今天北京天气怎么样")
    assert should_auto_search("帮我搜一下 GTA6 发售日")
    assert should_auto_search("最近油价多少")
    assert should_auto_search("珊瑚水妖鸟是什么怪物")
    assert should_auto_search("有没有叫冰原Legiana的弓")
    assert should_auto_search("DeepSeek R2什么时候出", aggressive=True)
    assert should_auto_search("csdiy是什么", aggressive=True)
    assert not should_auto_search("过拟合怎么办")
    assert not should_auto_search("别搜了，就闲聊")
    assert not should_auto_search("在吗学姐")
    assert not should_auto_search("hi")
    assert not should_auto_search("学姐是大笨蛋这都不认识")
    assert not should_auto_search("这不是冰牙龙吗学姐你是不是认错了")


def test_find_trend_search_queries() -> None:
    from dl_senpai.web_search import find_trend_search_queries

    titles = ["某明星官宣结婚", "OpenAI 发布新模型", "明日方舟新活动"]
    hits = find_trend_search_queries("学姐 OpenAI 新模型咋样", titles)
    assert hits
    assert any("OpenAI" in q for q in hits)


def test_looks_like_casual_banter() -> None:
    from dl_senpai.web_search import looks_like_casual_banter

    assert looks_like_casual_banter("学姐是大笨蛋这都不认识")
    assert looks_like_casual_banter("在吗学姐")
    assert looks_like_casual_banter("你被KO了吗")
    assert not should_auto_search("你被KO了吗", aggressive=True)
    assert not looks_like_casual_banter("今天北京天气怎么样")


def test_refine_search_query() -> None:
    assert refine_search_query("珊瑚水妖鸟是什么") == "珊瑚水妖鸟"
    assert "Legiana" in refine_search_query("有没有冰原 Legiana 这把弓")


def test_build_auto_search_queries() -> None:
    q = build_auto_search_queries("@学姐 今天油价多少")
    assert q[0] == "今天油价多少"
    quoted = build_auto_search_queries(
        "【引用消息】小明：你看这个\n【我现在说的】最新版 Python 什么时候出"
    )
    assert quoted[0] == "最新版 Python 什么时候出"


def test_expand_search_queries_arknights() -> None:
    queries = expand_search_queries("今年明日方舟春节限定有哪些", max_total=6)
    assert any("明日方舟" in q for q in queries)
    assert any("2026" in q or str(__import__("datetime").datetime.now().year) in q for q in queries)
    assert any("prts" in q.casefold() or "wiki" in q.casefold() for q in queries)


def test_score_hit_relevance_filters_noise() -> None:
    query = "今年明日方舟春节限定有哪些"
    good = SearchHit("明日方舟春节限定干员一览", "https://prts.wiki", "2026春节卡池", "bing")
    bad = SearchHit("2026年日历放假安排", "https://gov.cn", "国务院发布", "bing")
    assert score_hit_relevance(query, good) > score_hit_relevance(query, bad)


@pytest.mark.asyncio
async def test_search_many_retries_on_weak(monkeypatch: pytest.MonkeyPatch) -> None:
    searcher = WebSearcher(provider="bing", max_results=3, timeout=5.0)
    calls: list[str] = []

    async def fake_search(query: str) -> list[SearchHit]:
        calls.append(query)
        if "site:prts.wiki" in query or "wiki" in query.casefold():
            return [
                SearchHit(
                    "明日方舟春节限定干员",
                    "https://prts.wiki",
                    "限定干员名单",
                    "bing",
                )
            ]
        return [SearchHit("2026世界杯赛程", "https://fifa.com", "世界杯", "bing")]

    monkeypatch.setattr(searcher, "search", fake_search)
    text = await searcher.search_many(
        ["今年明日方舟春节限定"],
        intent_query="今年明日方舟春节限定有哪些",
        min_relevance=0.35,
    )
    assert "prts" in text.casefold() or "明日方舟" in text
    assert len(calls) >= 2


@pytest.mark.asyncio
async def test_search_provider_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    searcher = WebSearcher(provider="auto", max_results=3, timeout=5.0)

    async def boom(_q: str) -> list[SearchHit]:
        raise RuntimeError("down")

    async def ok(_q: str) -> list[SearchHit]:
        return [SearchHit("ok", "https://ok.example", "snippet", "bing")]

    monkeypatch.setattr(searcher, "_search_tavily", boom)
    monkeypatch.setattr(searcher, "_search_searxng", boom)
    monkeypatch.setattr(searcher, "_search_ddgs", boom)
    monkeypatch.setattr(searcher, "_search_duckduckgo_instant", boom)
    monkeypatch.setattr(searcher, "_search_bing", ok)

    hits = await searcher.search("hello")
    assert len(hits) == 1
    assert hits[0].title == "ok"


def test_should_skip_auto_search_for_pixiv_task() -> None:
    from dl_senpai.web_search import should_skip_auto_search

    msg = (
        "你能不能去pixiv里面搜索对应的图片然后提取生图提示词，"
        "再在原本的提示词的基础上进行优化，保存怪物猎人原版风格"
    )
    assert should_skip_auto_search(msg)
    assert not should_auto_search(msg)


def test_build_auto_search_queries_skips_pixiv_workflow() -> None:
    msg = (
        "你能不能去pixiv里面搜索对应的图片然后提取生图提示词，"
        "再在原本的提示词的基础上进行优化，保存怪物猎人原版风格，冰呪龙"
    )
    assert build_auto_search_queries(msg) == []


def test_build_auto_search_queries_caps_monster_hunter_question() -> None:
    queries = build_auto_search_queries("怪物猎人世界冰呪龙弱点肉质")
    assert queries
    assert all(len(q) <= 48 for q in queries)
    assert not any("提取" in q for q in queries)
