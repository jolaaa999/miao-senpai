"""本轮语气判定单测。

守两件事：

1. **判定取向**：宁可把吐槽误判成「普通」，也不能把真倾诉判成「吐槽」。
   少损一句最多显得平淡；真难受的人被回「谁让你熬夜的」是硬伤。
2. **提示词注入的措辞**：不能出现「他」，也不能写成「直接回对方本人」这种
   祈使句——都实测过，会让模型以为自己在帮用户起草回复。
"""

from __future__ import annotations

from dl_senpai.tone import (
    build_tone_hint,
    classify_tone,
    has_comfort_tone,
    is_casual_prompt,
)


def test_vent_detection():
    for text in ("今天好累啊，作业写到两点", "加班到现在，麻了", "好烦啊，组会又要汇报"):
        assert classify_tone(text) == "vent", text


def test_distress_detection():
    for text in ("最近一直睡不着，感觉挺难受的", "真的撑不住了", "能陪我说说话吗"):
        assert classify_tone(text) == "distress", text


def test_distress_beats_vent():
    """同一句里既有吐槽词又有求救词，按求救处理。"""
    assert classify_tone("好累啊，真的撑不住了") == "distress"


def test_plain_chat_is_normal():
    for text in ("在吗", "哈哈哈", "晚安"):
        assert classify_tone(text) == "normal", text


def test_empty_is_normal():
    assert classify_tone("") == "normal"
    assert classify_tone("   ") == "normal"


def test_repeated_venting_becomes_distress():
    """同一件烦心事反复说，按真倾诉处理——真人反复提就是想被接一下。"""
    history = ["作业写不完了好烦", "作业真的写不完了"]
    assert classify_tone("作业还是写不完", recent=history) == "distress"


def test_comfort_markers_cover_the_usual_suspects():
    for text in ("抱抱你", "辛苦了", "你已经很努力了", "先睡吧，别熬了", "身体扛不住"):
        assert has_comfort_tone(text), text
    for text in ("两点睡，活该困", "课设重做就重做，能咋办", "那你还不是天天熬，笑死"):
        assert not has_comfort_tone(text), text


def test_casual_prompt_detection():
    assert is_casual_prompt("在吗")
    assert is_casual_prompt("今天好累啊，作业写到两点")
    # 求教类不算随口一问，压短就成了懒
    assert not is_casual_prompt("Python 怎么入门")
    assert not is_casual_prompt("这段代码报错了帮我看看")
    # 长消息本身说明对方在讲事情，不算随口一问
    assert not is_casual_prompt("我" * 30)


def test_tone_hint_wording_has_no_third_person():
    """提示词里不能有「他」，也不能有「直接回对方本人」这种祈使句。

    这两种写法都实测过会让模型开始替用户起草回复，
    输出过「回他：辛苦啦」和「你是想让我帮你回这位群友吧？」。
    """
    for verdict in ("vent", "distress"):
        hint = build_tone_hint(verdict)
        assert hint
        assert "他" not in hint.replace("对方", ""), hint
        assert "直接回" not in hint, hint


def test_normal_verdict_injects_nothing():
    """普通情况不给指令：每轮都提醒「注意语气」本身就会让回复变僵硬。"""
    assert build_tone_hint("normal") == ""
    assert build_tone_hint("") == ""
