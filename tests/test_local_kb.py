"""本地 Markdown 知识库与组合检索的测试。"""

from __future__ import annotations

import asyncio

import pytest

from dl_senpai.knowledge.local_kb import (
    LocalMarkdownRetriever,
    reset_local_kb_cache,
)
from dl_senpai.knowledge.models import (
    KnowledgeChunk,
    KnowledgeQuery,
    KnowledgeResult,
)
from dl_senpai.knowledge.retriever import CompositeRetriever


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_local_kb_cache()
    yield
    reset_local_kb_cache()


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _kb(tmp_path):
    """造一个小知识库：两个领域 + 一个该被忽略的文件。"""
    _write(tmp_path, "怪猎/百科站点.md", (
        "---\n领域: 怪猎\n类型: 站点索引\n---\n\n"
        "# 怪猎 · 权威站点\n\n"
        "## 灰机wiki\n\n- 网址：https://mhw.huijiwiki.com/\n- 说明：怪物装备资料\n\n"
        "## NGA\n\n- 网址：https://bbs.nga.cn/\n"
    ))
    _write(tmp_path, "武科大/百科站点.md", (
        "---\n领域: 武科大\n---\n\n"
        "# 武科大 · 权威站点\n\n"
        "## 学校主页\n\n- 网址：https://www.wust.edu.cn/\n- 说明：学校官方门户\n"
    ))
    _write(tmp_path, "_模板.md", "# 模板\n\n## 标题\n\n这里应该被忽略\n")
    _write(tmp_path, "README.md", "# 说明\n\n## 目录\n\n这里也该被忽略\n")
    return tmp_path


def _run(coro):
    return asyncio.run(coro)


# ---- 扫描 ----

def test_scans_domain_folders_and_skips_readme_and_underscore(tmp_path):
    r = LocalMarkdownRetriever(_kb(tmp_path))
    res = _run(r.retrieve(KnowledgeQuery(query="灰机wiki")))
    assert res.ok
    # _模板.md 与 README.md 的内容不该被检索到
    res2 = _run(r.retrieve(KnowledgeQuery(query="应该被忽略 该被忽略")))
    assert all("忽略" not in c.text for c in res2.chunks)


def test_list_sources_returns_one_per_domain(tmp_path):
    r = LocalMarkdownRetriever(_kb(tmp_path))
    ids = sorted(s.source_id for s in r.list_sources())
    # 按码点排序：怪(0x602A) 在 武(0x6B66) 前面
    assert ids == ["local:怪猎", "local:武科大"]


def test_missing_directory_is_empty_not_error(tmp_path):
    r = LocalMarkdownRetriever(tmp_path / "不存在")
    res = _run(r.retrieve(KnowledgeQuery(query="随便什么")))
    assert res.ok and not res.chunks


# ---- 命中 ----

def test_query_hits_matching_domain(tmp_path):
    r = LocalMarkdownRetriever(_kb(tmp_path))
    res = _run(r.retrieve(KnowledgeQuery(query="武科大 学校主页", top_k=3)))
    assert res.chunks
    assert "武科大" in res.chunks[0].source_name
    assert "wust.edu.cn" in res.chunks[0].text


def test_unrelated_query_returns_nothing(tmp_path):
    """零重合必须返回空 —— 塞不相关内容正是幻觉的来源。"""
    r = LocalMarkdownRetriever(_kb(tmp_path))
    res = _run(r.retrieve(KnowledgeQuery(query="量子纠缠 拓扑绝缘体 弦论")))
    assert res.ok and not res.chunks


def test_top_k_is_respected(tmp_path):
    _write(tmp_path, "领域/多节.md", (
        "# 标题\n\n" + "\n\n".join(
            "## 小节%d\n\n共同关键词 甲乙丙 %d" % (i, i) for i in range(6)
        )
    ))
    r = LocalMarkdownRetriever(tmp_path)
    res = _run(r.retrieve(KnowledgeQuery(query="共同关键词 甲乙丙", top_k=2)))
    assert len(res.chunks) <= 2


def test_result_is_sorted_by_score(tmp_path):
    r = LocalMarkdownRetriever(_kb(tmp_path))
    res = _run(r.retrieve(KnowledgeQuery(query="怪猎 灰机wiki 装备", top_k=5)))
    scores = [c.score for c in res.chunks]
    assert scores == sorted(scores, reverse=True)


# ---- 缓存 ----

def test_cache_invalidated_when_file_changes(tmp_path):
    root = _kb(tmp_path)
    r = LocalMarkdownRetriever(root)
    assert not _run(r.retrieve(KnowledgeQuery(query="新加的暗号词"))).chunks
    _write(root, "新领域/条目.md", "# 新领域\n\n## 条目\n\n新加的暗号词 在这里\n")
    res = _run(r.retrieve(KnowledgeQuery(query="新加的暗号词")))
    assert res.chunks, "文件变了却没重新扫描"


# ---- 组合检索 ----

class _FakeRetriever:
    def __init__(self, name, texts, *, ok=True):
        self._name = name
        self._texts = texts
        self._ok = ok

    @property
    def name(self):
        return self._name

    async def retrieve(self, query):
        return KnowledgeResult(
            chunks=[KnowledgeChunk(text=t, score=0.9 - i * 0.1)
                    for i, t in enumerate(self._texts)],
            provider=self._name, ok=self._ok,
        )

    def list_sources(self):
        return []


class _BoomRetriever:
    name = "boom"

    async def retrieve(self, query):
        raise RuntimeError("模拟 provider 挂掉")

    def list_sources(self):
        raise RuntimeError("模拟 list_sources 挂掉")


def test_composite_interleaves_sources():
    """不同来源的分数不可比，必须轮转，否则一方会被挤干净。"""
    a = _FakeRetriever("a", ["a1", "a2", "a3"])
    b = _FakeRetriever("b", ["b1", "b2", "b3"])
    comp = CompositeRetriever([a, b], name="hybrid")
    res = _run(comp.retrieve(KnowledgeQuery(query="x", top_k=4)))
    got = [c.text for c in res.chunks]
    assert got == ["a1", "b1", "a2", "b2"]


def test_composite_survives_one_broken_source():
    a = _FakeRetriever("a", ["a1"])
    comp = CompositeRetriever([a, _BoomRetriever()], name="hybrid")
    res = _run(comp.retrieve(KnowledgeQuery(query="x", top_k=5)))
    assert res.ok
    assert [c.text for c in res.chunks] == ["a1"]


def test_composite_all_failed_is_not_ok():
    comp = CompositeRetriever([_BoomRetriever()], name="hybrid")
    res = _run(comp.retrieve(KnowledgeQuery(query="x", top_k=5)))
    assert res.ok is False
    assert res.error == "all_failed"


def test_composite_empty_returns_empty():
    comp = CompositeRetriever([], name="hybrid")
    res = _run(comp.retrieve(KnowledgeQuery(query="x", top_k=5)))
    assert res.ok and not res.chunks


def test_composite_dedupes_same_text():
    a = _FakeRetriever("a", ["同一段内容"])
    b = _FakeRetriever("b", ["同一段内容"])
    comp = CompositeRetriever([a, b], name="hybrid")
    res = _run(comp.retrieve(KnowledgeQuery(query="x", top_k=5)))
    assert [c.text for c in res.chunks] == ["同一段内容"]


# ---- 回归：计分模型踩过的坑 ----
#
# 下面几条都是实际调试中出过的问题，行为依赖打分细节，很容易被改回去。

def test_question_suffix_does_not_hide_the_right_section(tmp_path):
    """疑问词不该把正确答案挤出结果。

    曾经的做法是"命中词数占查询词数的比例"过了阈值才算命中。中文的
    "是什么"会被切成 是什/什么/核是 等一堆无意义 n-gram，把分母撑大，
    结果只命中核心词「考核」的正确段落覆盖率反而不达标被丢掉。
    """
    _write(tmp_path, "社团/介绍.md", (
        "# 介绍\n\n## 学习方式\n\n大家有问题可以问社团的学长。\n\n"
        "## 考核一：试卷\n\n考核内容以主观题为主。\n"
    ))
    r = LocalMarkdownRetriever(tmp_path)
    res = _run(r.retrieve(KnowledgeQuery(query="社团考核是什么", top_k=5)))
    sections = [c.section for c in res.chunks]
    assert any("考核" in s for s in sections), "正确答案被挤掉了：%s" % sections


def test_file_name_context_disambiguates(tmp_path):
    """领域/文件名是主题标签，正文里没出现时靠它兜底。

    曾经只拿"标题+正文"做匹配，于是查询里那个最关键的词如果只写在文件名上
    （例如"永劫无间/站点.md"），正文中就没有任何一段能同时命中它和另一个
    关键词，结果只返回一堆泛泛的"官方"段。
    """
    _write(tmp_path, "永劫无间/站点.md", (
        "# 永劫无间 · 站点\n\n## 写文件注意\n\n数值每次大版本都可能变。\n\n"
        "## 官方网站\n\n- 说明：官方站点，版本公告。\n"
    ))
    _write(tmp_path, "其他/站点.md", (
        "# 其他 · 站点\n\n## 官方文档\n\n- 说明：官方文档地址。\n"
    ))
    r = LocalMarkdownRetriever(tmp_path)
    res = _run(r.retrieve(KnowledgeQuery(query="永劫无间 官方", top_k=2)))
    assert res.chunks
    assert "官方网站" in res.chunks[0].section, [c.section for c in res.chunks]


def test_section_title_outweighs_body_mention(tmp_path):
    """同一个词出现在标题里比出现在正文里更有说明力。

    标题就是这一段的主题。不给标题加权时，一段只是顺口提到该词的正文
    会压过标题就是这个词的段落。
    """
    _write(tmp_path, "领域/文档.md", (
        "# 文档\n\n## 杂谈\n\n顺便说一下甲词的来历，顺便。\n\n"
        "## 甲词\n\n- 这是甲词的正式说明。\n"
    ))
    r = LocalMarkdownRetriever(tmp_path)
    res = _run(r.retrieve(KnowledgeQuery(query="甲词", top_k=2)))
    assert res.chunks
    assert "甲词" in res.chunks[0].section


def test_question_words_alone_match_nothing(tmp_path):
    """整句都是口水词时，不能变成"随便返回一条"。

    这条是防着"剥掉疑问词后实词为空"的退化路径 —— 那时若退回全量词匹配，
    就会拿"什么""怎么"去撞任何一段，把无关内容当证据喂给模型。
    """
    r = LocalMarkdownRetriever(_kb(tmp_path))
    for q in ["红烧肉怎么做", "今天天气不错", "股票行情怎么样"]:
        res = _run(r.retrieve(KnowledgeQuery(query=q, top_k=3)))
        assert not res.chunks, "%s 不该命中：%s" % (q, [c.section for c in res.chunks])


def test_injected_text_carries_identity_guard(tmp_path):
    """检索原文注入 prompt 时必须带上"别改身份"的约束。

    知识库里可能躺着别的社团的招新公告（"欢迎各位新生来到 XX 俱乐部"）。
    不划线的话模型会照着念，把自己说成那个社团的人 —— 而它应该是
    百度飞桨的学姐，别的社团只是资料。
    """
    from dl_senpai.knowledge.models import (
        REFERENCE_ONLY_HINT,
        KnowledgeChunk,
        KnowledgeResult,
    )

    result = KnowledgeResult(
        chunks=[KnowledgeChunk(text="欢迎各位新生来到别的俱乐部招新群")],
        provider="local_md", ok=True,
    )
    text = result.to_prompt_text()
    assert "欢迎各位新生" in text          # 原文要给到
    assert REFERENCE_ONLY_HINT in text      # 约束也必须给到
    assert "不要照抄" in text and "改变你自己的身份" in text

# ================================================================
# 2026-10-13：检索噪音修复的回归
#
# 背景：线上出现「问校园卡补办，返回一堆社团和课程资料」。
# 查下来是两个原因，下面分别钉住：
#   1. 知识文件里的「## 待补充」清单被当成正文检索 ——
#      里面写着"校园卡、宿舍、选课等学生事务入口"，于是库里
#      根本没有校园卡内容，这段 TODO 却命中了。
#   2. 查询里属于领域名的碎片（武科/科大）在领域内每段都有，
#      光靠它们就能通过闸门，于是问宿舍、问食堂也能拿到噪音。
# ================================================================


def _kb_with_todo(tmp_path):
    """一个只有站点索引的知识库，待补充里提到别的主题。"""
    _write(tmp_path, "武科大/百科站点.md", (
        "---\n领域: 武科大\n---\n\n"
        "# 武科大 · 权威站点\n\n"
        "> 武汉科技大学（WUST），域名统一是 wust.edu.cn。\n\n"
        "## 学校主页\n\n- 网址：https://www.wust.edu.cn/\n"
        "- 说明：学校官方门户\n\n"
        "## 待补充\n\n"
        "- [ ] 校园卡、宿舍、选课等学生事务入口\n"
        "- [ ] 各专业的培养方案\n"
    ))
    _write(tmp_path, "武科大/ACM俱乐部.md", (
        "---\n领域: 武科大\n---\n\n"
        "# 武科大 · ACM 俱乐部\n\n"
        "## 欢迎辞\n\n欢迎各位新生来到武科大 ACM 俱乐部招新群。\n\n"
        "## 传送门\n\n- WUST OJ：https://oj.wust-acm.top/\n"
    ))


def test_todo_section_is_not_searchable(tmp_path):
    """待补充清单不是知识，不能命中。

    这是最贵的一类错误：把"我还没查"当成"我知道"。
    """
    retriever = LocalMarkdownRetriever(tmp_path, name="t")
    result = asyncio.run(
        retriever.retrieve(KnowledgeQuery(query="校园卡怎么补办", top_k=5))
    )
    assert result.chunks == [], "待补充清单被当成资料返回了"


def test_todo_section_does_not_bleed_into_other_queries(tmp_path):
    """待补充里的"宿舍""选课"同样不该命中。"""
    retriever = LocalMarkdownRetriever(tmp_path, name="t")
    for q in ("宿舍怎么样", "怎么选课", "培养方案在哪"):
        result = asyncio.run(retriever.retrieve(KnowledgeQuery(query=q, top_k=5)))
        assert result.chunks == [], "%s 命中了待补充清单" % q


def test_topic_word_alone_is_not_enough(tmp_path):
    """只命中领域名（武科大）不能算相关。

    武科大目录下每段都含"武科大"，如果这就算命中，
    那么问宿舍、问食堂、问校车都会拿到 ACM 俱乐部和站点索引这两段噪音。
    """
    _kb_with_todo(tmp_path)
    retriever = LocalMarkdownRetriever(tmp_path, name="t")
    for q in ("武科大的宿舍怎么样", "武科大食堂好吃吗", "武科大的校车几点发"):
        result = asyncio.run(retriever.retrieve(KnowledgeQuery(query=q, top_k=5)))
        assert result.chunks == [], "%s 只靠领域名就命中了" % q


def test_real_subject_still_matches(tmp_path):
    """收紧闸门不能把真命中一起收紧掉。"""
    _kb_with_todo(tmp_path)
    retriever = LocalMarkdownRetriever(tmp_path, name="t")
    result = asyncio.run(
        retriever.retrieve(KnowledgeQuery(query="ACM 俱乐部怎么加入", top_k=5))
    )
    assert result.chunks, "ACM 俱乐部的真命中被误杀了"
    assert any("ACM" in c.source_name for c in result.chunks)


def test_domain_topic_query_still_works(tmp_path):
    """整句就是领域名时（没有别的实词），应当照常返回该领域内容。"""
    _kb_with_todo(tmp_path)
    retriever = LocalMarkdownRetriever(tmp_path, name="t")
    result = asyncio.run(
        retriever.retrieve(KnowledgeQuery(query="武科大", top_k=5))
    )
    assert result.chunks, "纯领域名查询被误杀了"
