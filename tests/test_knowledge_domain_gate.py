"""本地知识库领域闸门：查询没点名已知领域时不许硬塞切片。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dl_senpai.knowledge.local_kb import LocalMarkdownRetriever, reset_local_kb_cache
from dl_senpai.knowledge.models import KnowledgeQuery


def _make_kb(root: Path) -> Path:
    """造两个领域：一个游戏领域（含目标词），一个学校领域。"""
    game = root / "明日方舟"
    game.mkdir(parents=True)
    (game / "a.md").write_text(
        "# 干员培养\n明日方舟新手先练哪个干员：先练能站场的。", encoding="utf-8"
    )
    school = root / "武科大"
    school.mkdir(parents=True)
    (school / "b.md").write_text(
        "# 助学金\n研究生国家助学金按月发放，标准见下表。", encoding="utf-8"
    )
    return root


def _retrieve(root: Path, query: str, require_domain: bool):
    reset_local_kb_cache()
    retriever = LocalMarkdownRetriever(root, require_named_domain=require_domain)
    return asyncio.run(retriever.retrieve(KnowledgeQuery(query=query, top_k=5)))


def test_gate_returns_empty_when_query_names_no_domain(tmp_path: Path) -> None:
    """问编程、库里只有游戏和学校：没有领域被点名，必须返回空。"""
    result = _retrieve(_make_kb(tmp_path), "我大二想一年内打区域赛，给我路线和自测标准", True)
    assert result.chunks == []
    assert result.metadata.get("gate") == "no_named_domain"


def test_gate_keeps_hits_when_domain_named(tmp_path: Path) -> None:
    """问到库里真有的领域时不能被误杀。"""
    result = _retrieve(_make_kb(tmp_path), "明日方舟新手先练哪个干员", True)
    assert result.chunks, "点名了领域却被闸门误杀"


def test_gate_off_keeps_old_behavior(tmp_path: Path) -> None:
    """require_named_domain=False 时退回旧行为（只按分数）。"""
    result = _retrieve(_make_kb(tmp_path), "我大二想一年内打区域赛，给我路线和自测标准", False)
    assert result.chunks
