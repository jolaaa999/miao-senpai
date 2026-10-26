# -*- coding: utf-8 -*-
"""2026-10-24 六问复盘的余下四项：检索词补原问、抓网页正文、本地库字面命中、persona 主线。

背景（实测）：
    1. 模型把问题压成一两个词（「签到」「弓」）丢给搜索，捞回同名歌曲、
       课堂考勤、DNF 签到入口，然后被当成答案抄进回复。
    2. 摘要只有标题级信息（「XX官网首页」），模型只能照着标题编，
       或者套【防写串】把话说成空话。
    3. 本地库的领域闸门只认「查询点名了某个领域」，
       「我们学校图书馆馆长」这种问某一节内容的话，整库被判无关。
"""

from __future__ import annotations

import pytest

from dl_senpai.knowledge.local_kb import (
    LocalMarkdownRetriever,
    reset_local_kb_cache,
)
from dl_senpai.knowledge.models import KnowledgeQuery
from dl_senpai.agent.tools.models import ToolCall
from dl_senpai.persona import SYSTEM_PROMPT, SEARCH_SYSTEM_EXTRA
from dl_senpai.web_search import (
    SearchHit,
    WebSearcher,
    _excerpt_from_html,
    fetch_page_excerpt,
    format_search_for_prompt,
    prepare_tool_queries,
)


# ---------------------------------------------------------------- 检索词补原问


@pytest.mark.parametrize(
    "query,user_text,expect",
    [
        # 一个字/两个字的压缩词：并回原话
        ("弓", "冰弓前期配装怎么搭", "冰弓前期配装怎么搭"),
        ("签到", "你也可以找 签到", "你也可以找 签到"),
        # 原话里没这个词：原词 + 原话，两个都留着
        ("攻略", "学姐帮我看看这个", "攻略 看看这个"),
        # 写全的检索词原样放行，不擅自改写
        ("冰弓 前期配装 强弓珠", "冰弓前期配装怎么搭", "冰弓 前期配装 强弓珠"),
        ("武科大图书馆电话", "学姐武科大图书馆电话多少", "武科大图书馆电话"),
    ],
)
def test_prepare_tool_queries_fills_context(
    query: str, user_text: str, expect: str
) -> None:
    assert prepare_tool_queries([query], user_text) == [expect]


def test_prepare_tool_queries_keeps_short_query_without_context() -> None:
    assert prepare_tool_queries(["签到"], "") == ["签到"]


def test_prepare_tool_queries_dedupes_and_drops_empty() -> None:
    out = prepare_tool_queries(["冰弓 前期配装", "冰弓 前期配装", "  "], "冰弓前期配装怎么搭")
    assert out == ["冰弓 前期配装"]


@pytest.mark.asyncio
async def test_execute_search_uses_original_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型只写「签到」时，真正发出去的检索词要带上用户原话。"""
    from dl_senpai.agent.tools import executors

    seen: dict[str, object] = {}

    class _FakeSearcher:
        async def search_many(self, queries, **kwargs):
            seen["queries"] = list(queries)
            return "【联网检索结果】\n1. 某条结果"

    monkeypatch.setattr(
        "dl_senpai.web_search.searcher_from_config", lambda _cfg: _FakeSearcher()
    )

    class _Ctx:
        config = object()
        plain_text = "你也可以找 签到"

    result = await executors.execute_search(
        _Ctx(), ToolCall(tool_name="search", arguments={"queries": ["签到"]})
    )
    assert result.success
    assert seen["queries"] == ["你也可以找 签到"]


# ---------------------------------------------------------------- 抓网页正文


def test_excerpt_strips_scripts_and_styles() -> None:
    html = (
        "<html><head><style>.a{color:red}</style><script>var x=1</script></head>"
        "<body><nav>登录 注册</nav><p>周一至周五 8:00-22:00，馆长办公室在二楼。</p></body></html>"
    )
    text = _excerpt_from_html(html, query="馆长 时间", max_chars=500)
    assert "var x" not in text
    assert "color:red" not in text
    assert "8:00-22:00" in text


def test_excerpt_windows_on_query_term() -> None:
    """长页面要切到命中查询词的那一段，而不是永远截开头。"""
    filler = "导读" * 600
    html = f"<p>{filler}</p><p>馆长办公室在二楼 208。</p>" + "<p>尾巴</p>" * 50
    text = _excerpt_from_html(html, query="馆长办公室", max_chars=120)
    assert "208" in text
    assert len(text) <= 130


@pytest.mark.asyncio
async def test_fetch_page_excerpt_skips_non_http_and_binary() -> None:
    # 不需要联网的拒绝路径：非法协议 / 明显是文件的链接
    assert await fetch_page_excerpt("") == ""
    assert await fetch_page_excerpt("ftp://example.com/a") == ""
    assert await fetch_page_excerpt("https://example.com/a.pdf") == ""


def test_format_prompt_prefers_body_over_snippet() -> None:
    hits = [
        SearchHit(
            "图书馆开放时间",
            "https://lib.example/x",
            "摘要只写了标题",
            "bing",
            body="周一至周五 8:00-22:00，周六 9:00-17:00。",
        )
    ]
    text = format_search_for_prompt(hits, query="图书馆时间", max_chars=800)
    assert "网页正文" in text
    assert "8:00-22:00" in text
    # 正文顶替摘要，同一段话不占两遍预算
    assert "摘要只写了标题" not in text


@pytest.mark.asyncio
async def test_search_many_enriches_body_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    searcher = WebSearcher(provider="bing", max_results=2, fetch_body=True)

    async def fake_search(_q: str) -> list[SearchHit]:
        return [SearchHit("图书馆开放时间", "https://lib.example/x", "短摘要", "bing")]

    async def fake_fetch(url: str, *, query: str = "", **_kw) -> str:
        return "周一至周五 8:00-22:00，馆长办公室在二楼。"

    monkeypatch.setattr(searcher, "search", fake_search)
    monkeypatch.setattr("dl_senpai.web_search.fetch_page_excerpt", fake_fetch)

    text = await searcher.search_many(["图书馆 开放时间"], intent_query="图书馆几点开门")
    assert "网页正文" in text
    assert "馆长办公室" in text


@pytest.mark.asyncio
async def test_search_many_skips_body_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关闭时要真的一个请求都不发（测试与 MCP 场景靠这条）。"""
    searcher = WebSearcher(provider="bing", max_results=2)
    called: list[str] = []

    async def fake_search(_q: str) -> list[SearchHit]:
        return [SearchHit("图书馆开放时间", "https://lib.example/x", "短摘要", "bing")]

    async def fake_fetch(url: str, **_kw) -> str:
        called.append(url)
        return "不该出现"

    monkeypatch.setattr(searcher, "search", fake_search)
    monkeypatch.setattr("dl_senpai.web_search.fetch_page_excerpt", fake_fetch)

    text = await searcher.search_many(["图书馆 开放时间"], intent_query="图书馆几点开门")
    assert called == []
    assert "不该出现" not in text


# ---------------------------------------------------------------- 本地库字面命中


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_local_kb_cache()
    yield
    reset_local_kb_cache()


def _write(root, rel: str, text: str):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _kb(tmp_path):
    _write(
        tmp_path,
        "武科大/校内事务.md",
        "# 图书馆馆长\n\n馆长办公室在图书馆二楼 208，工作日上午在。\n\n"
        "# 校园卡补办\n\n先去卡务中心挂失，再去窗口领新卡。\n",
    )


@pytest.mark.asyncio
async def test_literal_title_hit_passes_domain_gate(tmp_path) -> None:
    """「我们学校图书馆馆长」不带领域词，但字面命中了小节标题，必须答得出来。"""
    _kb(tmp_path)
    r = LocalMarkdownRetriever(tmp_path, require_named_domain=True)
    res = await r.retrieve(KnowledgeQuery(query="我们学校图书馆馆长是谁", top_k=3))
    assert res.chunks, "字面命中标题应放行"
    assert "208" in res.chunks[0].text


@pytest.mark.asyncio
async def test_unrelated_query_still_gated(tmp_path) -> None:
    _kb(tmp_path)
    r = LocalMarkdownRetriever(tmp_path, require_named_domain=True)
    res = await r.retrieve(KnowledgeQuery(query="今天天气怎么样", top_k=3))
    assert res.chunks == []
    assert res.metadata.get("gate") == "no_named_domain"


# ---------------------------------------------------------------- persona 主线


@pytest.mark.parametrize(
    "keyword",
    ["能答就直接答", "拒绝也要给东西", "不许把问题推回给对方", "算完要用话把结论讲出来"],
)
def test_persona_has_answer_baseline(keyword: str) -> None:
    assert keyword in SYSTEM_PROMPT


def test_persona_search_forbids_keyword_compression() -> None:
    assert "检索词别压缩" in SEARCH_SYSTEM_EXTRA
