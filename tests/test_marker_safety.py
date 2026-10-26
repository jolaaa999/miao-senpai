# -*- coding: utf-8 -*-
"""内部协议不许漏给用户的回归测试。

背景（第六问真实翻车）：模型写 <<<CODE（少 >>>）时旧正则一条都不认，
整段 Python 原样发进群里；同时发送路径没有任何兜底剥离。
"""

from __future__ import annotations

import pytest

from dl_senpai.agent.tools.exec_tools import _parse_code
from dl_senpai.reply_send import strip_residual_markers
from dl_senpai.web_search import augment_queries_with_intent


@pytest.mark.parametrize(
    "raw",
    [
        "<<<CODE>>>\nprint(1)\n<<<END>>>",
        "<<<CODE\nprint(2)\n<<<END",
        "<<<CODE python>>>\nimport math\nprint(math.pi)\n<<<END>>>",
        "<<<CODE import math\nprint(3)\n<<<END>>>",
    ],
)
def test_code_marker_tolerated(raw: str) -> None:
    """缺闭括号/缺换行/带语言 都要能解析，不能漏给用户。"""
    cleaned, calls = _parse_code(raw)
    assert len(calls) == 1
    assert "<<<" not in cleaned
    assert "print" not in cleaned


def test_code_block_without_end_eats_to_end() -> None:
    """连 <<<END 都没有：宁可吞掉，也不许把代码发出去。"""
    cleaned, calls = _parse_code("<<<CODE\nprint(4)")
    assert len(calls) == 1
    assert cleaned.strip() == ""


def test_code_fence_stripped() -> None:
    """模型套的 ``` 围栏要去掉，只留纯代码。"""
    _, calls = _parse_code("<<<CODE>>>\n```python\nprint(5)\n```\n<<<END>>>")
    assert calls[0].arguments["code"].strip() == "print(5)"


def test_strip_residual_markers_keeps_normal_text() -> None:
    assert strip_residual_markers("学姐在的") == "学姐在的"


def test_strip_residual_markers_removes_inline() -> None:
    assert strip_residual_markers("好的<<<SEARCH 签到>>>") == "好的"


def test_strip_residual_markers_removes_block() -> None:
    out = strip_residual_markers("嘿嘿\n<<<CODE\nprint(9)\n<<<END\n就这样")
    assert out == "嘿嘿\n\n就这样"


def test_strip_residual_markers_removes_orphan_line() -> None:
    assert strip_residual_markers("正文\n<<<CODE") == "正文"


def test_augment_keeps_original_sentence() -> None:
    """模型把整句压成关键词时，原话必须补进去。"""
    qs = augment_queries_with_intent(["图书馆馆长"], "我们学校图书馆馆长是谁")
    assert "我们学校图书馆馆长是谁" in qs


def test_augment_no_dup_when_model_copied_intent() -> None:
    """模型已经照抄原话就别重复，省一轮预算。"""
    qs = augment_queries_with_intent(["冰弓前期配装怎么搭"], "冰弓前期配装怎么搭")
    assert qs == ["冰弓前期配装怎么搭"]
