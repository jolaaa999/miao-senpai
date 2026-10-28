"""出站整形：把口语化那几道工序接到 agent 出口。

回归的 bug（2026-10-28 看线上日志发现的）：口语化（空格断句、去破折号、
清空行）全在 llm.chat 那条路上，而线上 100% 走 agent（19/19 条 agent.entry）——
所以干跑看着像人，群里发出去的是「拆楼和住楼是两件事——拆完砖头归你们，
我拎着梯子就走。」这种杂文。38 条闲聊里 33 条带中文逗号、12 条带破折号、
空格断句只有 4 条。

这里分两层钉住：tone.shape_for_send 本身的行为，以及它确实被 build_plan 调用。
"""

from __future__ import annotations

from typing import Any

from dl_senpai.agent.context import AgentContext, AgentResult
from dl_senpai.agent.response import ResponsePipeline
from dl_senpai.tone import shape_for_send


def ctx(**kw: Any) -> AgentContext:
    base: dict[str, Any] = dict(session_id="group:1", trace_id="t1")
    base.update(kw)
    return AgentContext(**base)


# ================================================================
# shape_for_send 本身
# ================================================================


def test_dash_becomes_space_for_casual() -> None:
    """破折号是文青腔最刺眼的标记，闲聊里换成空格。"""
    out, counts = shape_for_send("拆楼和住楼是两件事——拆完砖头归你们", casual=True)
    assert "——" not in out
    assert counts["dash"] == 1


def test_commas_become_spaces_for_casual() -> None:
    out, counts = shape_for_send("我也不知道，反正就这样。", casual=True)
    assert out == "我也不知道 反正就这样"
    assert counts["comma"] == 1


def test_serious_answer_is_left_alone() -> None:
    """技术答疑不整形：那里的逗号是语义分隔，换空格会读成结巴。"""
    text = "先看 lr，再改 batch，最后跑 50 step。"
    out, counts = shape_for_send(text, casual=False)
    assert out == text
    assert counts["comma"] == 0 and counts["dash"] == 0


def test_action_only_line_is_dropped() -> None:
    """单独一条只有「（笑了一下）」，是真事，也是真出戏。"""
    out, counts = shape_for_send("（笑了一下）" + chr(10) + "说得漂亮", casual=True)
    assert out == "说得漂亮"
    assert counts["action"] == 1


def test_all_action_lines_fall_back_to_original() -> None:
    """整条都是动作就退回原文——宁可不完美，不能发空消息。"""
    text = "（笑了一下）"
    out, _ = shape_for_send(text, casual=True)
    assert out == text


def test_blank_runs_are_collapsed() -> None:
    """连续空行会拆出空气泡。"""
    out, counts = shape_for_send("这段" + chr(10) * 3 + "下一段", casual=True)
    assert out == "这段" + chr(10) * 2 + "下一段"
    assert counts["blank"] == 1


def test_code_fence_and_table_are_protected() -> None:
    """代码和表格里的逗号是语法，一个都不能动。"""
    fence = chr(96) * 3
    code = fence + "python" + chr(10) + "a, b = 1, 2" + chr(10) + fence
    out, counts = shape_for_send(code, casual=True)
    assert "a, b = 1, 2" in out and counts["comma"] == 0
    table = "| a | b |" + chr(10) + "| 1, 2 | 3 |"
    out2, counts2 = shape_for_send(table, casual=True)
    assert "1, 2" in out2 and counts2["comma"] == 0


def test_marker_line_is_kept_verbatim() -> None:
    """表情包标记行一个字节都不能动，而且必须还在最后。"""
    text = "好的呀，我懂了。" + chr(10) + "<<<STICKER 委屈>>>"
    out, _ = shape_for_send(text, casual=True)
    assert "<<<STICKER 委屈>>>" in out
    assert out.split(chr(10))[-1] == "<<<STICKER 委屈>>>"
    assert "，" not in out.split(chr(10))[0]


def test_empty_is_safe() -> None:
    assert shape_for_send("") == ("", {})
    assert shape_for_send("   ") == ("   ", {})


# ================================================================
# build_plan：agent 出口确实调了它
# ================================================================


def test_build_plan_shapes_casual_reply() -> None:
    """这条是回归钉子：agent 出口必须整形，不整形就等于白改。"""
    result = AgentResult(text="我也不知道，反正就这样。")
    plan = ResponsePipeline().build_plan(result, ctx(user_text="在干嘛"))
    assert "，" not in plan.text
    assert plan.text == "我也不知道 反正就这样"


def test_build_plan_leaves_serious_reply_alone() -> None:
    """技术问题不整形，原样发出。"""
    text = "先看 lr，再改 batch，最后跑 50 step。"
    result = AgentResult(text=text)
    plan = ResponsePipeline().build_plan(
        result, ctx(user_text="学姐我那个 loss 一直不降 帮我看下 pytorch")
    )
    assert plan.text == text


def test_build_plan_drops_action_only_line() -> None:
    result = AgentResult(text="（笑了一下）" + chr(10) + "说得漂亮")
    plan = ResponsePipeline().build_plan(result, ctx(user_text="在吗"))
    assert plan.text.strip() == "说得漂亮"

