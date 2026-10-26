"""梗密度：这轮该不该玩梗、玩到什么程度。

来源：sakurawwwxh/qq-agent-plus 的 memeDensityPrompt。
区别在于我们做成了**自动定档**：人设里那句「可以轻度接梗」等于没约束，
实测要么一轮堆几个梗，要么正经答疑里也硬塞一个。

这里测的是定档规则本身——它错了，提示词写得再好也没用。
"""

from __future__ import annotations

from dl_senpai.meme import build_meme_hint, has_meme_marker, normalize_level, resolve_level


def test_auto_off_for_serious_question():
    """正经问报错怎么办，不该玩梗。"""
    level = resolve_level("auto", verdict="normal", casual=False, user_text="这个报错怎么解决")
    assert level == "off"


def test_auto_off_for_distress():
    """真倾诉的时候玩梗就是没接住情绪。"""
    assert resolve_level("auto", verdict="distress", user_text="最近一直睡不着") == "off"


def test_auto_light_for_vent():
    assert resolve_level("auto", verdict="vent", user_text="加班到现在，麻了") == "light"


def test_auto_normal_when_user_is_joking():
    """对方自己在用梗，接住是本分。"""
    level = resolve_level("auto", verdict="normal", casual=True, user_text="笑死，这课设又崩了")
    assert level == "normal"


def test_auto_light_for_plain_casual():
    assert resolve_level("auto", casual=True, user_text="晚上吃啥") == "light"


def test_explicit_config_wins():
    """显式配了档位就照配置，不再自动判。"""
    assert resolve_level("heavy", verdict="distress", user_text="难受") == "heavy"
    assert resolve_level("off", verdict="vent", user_text="笑死") == "off"


def test_broken_config_falls_back_to_auto():
    """配置手改坏了不能把提示词整段弄丢。"""
    assert normalize_level("乱七八糟") == "auto"
    assert normalize_level(None) == "auto"


def test_every_level_has_wording():
    for level in ("off", "light", "normal", "heavy"):
        hint = build_meme_hint(level)
        assert hint.startswith("【梗密度】"), level
        assert len(hint) > 8


def test_hint_has_no_third_person_pronoun():
    """提示词里出现第三人称或祈使短语，模型会切换成「替你起草回复」。

    这个坑踩过：写了「回他一句」，模型直接输出「回他：…」「给你几个版本」。
    """
    for level in ("off", "light", "normal", "heavy"):
        hint = build_meme_hint(level)
        assert "他" not in hint
        assert "回一句" not in hint


def test_meme_marker_detection():
    assert has_meme_marker("笑死，这课设又崩了") is True
    assert has_meme_marker("今天作业写完了") is False
