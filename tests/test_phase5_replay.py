"""Phase 5：Replay corpus 测试（offline replay 证据）。

严格区分证据类型（第五十八节）：
    本文件 = **offline replay**（构造样本，无真实流量）
"""

from __future__ import annotations

import pytest

from tests.replay import build_corpus, corpus_stats, run_case, run_corpus


@pytest.fixture(scope="module")
def corpus():
    return build_corpus()


def test_corpus_meets_minimum_size(corpus) -> None:
    """第四节要求：最低 50 个样本。"""
    assert len(corpus) >= 50, f"corpus 只有 {len(corpus)} 个样本"


def test_corpus_covers_required_categories(corpus) -> None:
    """必须覆盖第四节列出的全部场景。"""
    stats = corpus_stats(corpus)
    required = {
        "plain", "private", "mention", "interrupt",
        "command", "marker_single", "marker_multi", "tolerance",
        "malformed", "no_marker", "local_3b", "permission",
        "tool_failure", "recovery", "stop_reason",
    }
    missing = required - set(stats)
    assert not missing, f"corpus 缺少分类: {sorted(missing)}"


def test_corpus_has_no_real_identifiers(corpus) -> None:
    """脱敏检查：不得出现真实群号/QQ号。

    真实群号取自 config 的 checkin_groups（980229149 等），
    这里直接检查字面量。
    """
    real_ids = {"980229149", "1050332675", "600478436", "1072442840"}
    for case in corpus:
        assert case.group_id not in real_ids, f"{case.id} 使用了真实群号"
        assert case.user_id not in real_ids, f"{case.id} 使用了真实 QQ"
        for rid in real_ids:
            assert rid not in case.input_text
            assert rid not in case.model_reply


def test_corpus_has_no_secrets(corpus) -> None:
    """不得含 API key 等敏感串。"""
    import re

    pattern = re.compile(r"sk-[A-Za-z0-9]{20,}")
    for case in corpus:
        blob = f"{case.input_text} {case.model_reply}"
        assert pattern.search(blob) is None, f"{case.id} 含疑似密钥"


def test_corpus_ids_unique(corpus) -> None:
    ids = [c.id for c in corpus]
    assert len(ids) == len(set(ids)), "corpus id 重复"


def test_corpus_stats_reported(corpus) -> None:
    """报告实际样本数与分类分布。"""
    stats = corpus_stats(corpus)
    print(f"\n=== Replay corpus: {len(corpus)} 样本 ===")
    for name, count in stats.items():
        print(f"  {name:16} {count}")
    assert sum(stats.values()) == len(corpus)


# ================================================================
# 逐条执行：行为决策验证
# ================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("case", build_corpus(), ids=lambda c: c.id)
async def test_replay_case(case) -> None:
    """每条样本的行为决策必须符合预期。"""
    result = await run_case(case)
    assert result.passed, (
        f"{case.id} [{case.category}] {case.note}\n"
        f"  mismatch: {result.mismatch}\n"
        f"  error: {result.error}"
    )


@pytest.mark.asyncio
async def test_replay_report(corpus) -> None:
    """整体报告（用于 Phase 5 报告的数据来源）。"""
    report = await run_corpus(corpus)
    print("\n" + report.summary())
    assert report.failed == 0, report.summary()
    assert report.passed == report.total
