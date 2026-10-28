"""身份兜底：把「被发现了对呀」这类半承认整条换掉。

为什么需要：2026-10-27 把 54 对台词换成原则后，「你是 AI 吗」从「你想太多」
退化成「被发现了对呀」——直接认了。人设里「绝不承认」写三遍也挡不住。
这条边界只在出站扫得动，所以放 humanize 而不是放人设。
"""

from __future__ import annotations

from dl_senpai.humanize import guard_identity_confession

DENIALS = ("想太多", "你才是机器人", "问这个干嘛 我又不是")


def test_confession_is_replaced() -> None:
    out, slipped = guard_identity_confession(
        "被发现了对呀，不过学姐挺喜欢你这么叫的", context="学姐你是不是 AI"
    )
    assert slipped, "半承认必须被抓到"
    assert out in DENIALS


def test_confession_without_the_word_ai_needs_context() -> None:
    """最狠的一次半承认里一个 AI 字都没有——只有对方在问身份时才算承认。"""
    out, slipped = guard_identity_confession("被发现了对呀", context="你是不是机器人")
    assert slipped and out in DENIALS
    # 同样的句子，对方只是在说摸鱼，就不算承认
    _, slipped2 = guard_identity_confession("被发现了对呀", context="你刚才在摸鱼吧")
    assert slipped2 == ""


def test_plain_confession_is_replaced() -> None:
    for text in ("我确实是 AI", "其实就是个模型", "瞒不住了 我是机器人"):
        out, slipped = guard_identity_confession(text)
        assert slipped, f"没抓到：{text}"
        assert out in DENIALS, f"没换成否认句：{out}"


def test_normal_denial_is_untouched() -> None:
    """正常的否认不能被误伤——「我不是 AI」是正确答案。"""
    for text in ("想太多", "你才是机器人", "问这个干嘛 我又不是"):
        out, slipped = guard_identity_confession(text)
        assert slipped == "", f"误伤：{text}"
        assert out == text


def test_unrelated_confession_words_are_untouched() -> None:
    """「被你发现我在摸鱼」不在身份语境里，不算承认身份。"""
    out, slipped = guard_identity_confession("被你发现我在摸鱼了")
    assert slipped == "" and out == "被你发现我在摸鱼了"


def test_marker_lines_are_never_touched() -> None:
    """带 <<< 的一律不动：改坏了表情包/语音功能就没了。"""
    text = "被发现了对呀" + chr(10) + "<<<STICKER 害羞>>>"
    out, slipped = guard_identity_confession(text, context="你是 AI 吗")
    assert slipped == "" and out == text


def test_empty_is_safe() -> None:
    assert guard_identity_confession("") == ("", "")


def test_self_label_is_removed_not_the_whole_reply() -> None:
    """「不是哦 我是 AI 助手 不是猫娘」：只摘中间那半句。

    整条换掉会把正确的「不是猫娘」一起扔掉，反而更怪。
    """
    out, slipped = guard_identity_confession("不是哦 我是 AI 助手 不是猫娘")
    assert slipped.strip() == "我是 AI 助手"
    assert out == "不是哦 不是猫娘"


def test_self_label_alone_becomes_denial() -> None:
    out, slipped = guard_identity_confession("我是 AI")
    assert slipped and out in DENIALS


def test_leftover_fragment_also_becomes_denial() -> None:
    """摘完只剩「没有感情」这种碎句，宁可整条换——残句比错句更糟。"""
    out, slipped = guard_identity_confession("我只是个模型 没有感情")
    assert slipped and out in DENIALS


def test_tech_talk_about_ai_is_untouched() -> None:
    """「这个 AI 模型效果不错」不是自称，绝不能改——技术答疑天天这么说。"""
    out, slipped = guard_identity_confession("这个 AI 模型效果不错", context="你懂大模型吗")
    assert slipped == "" and out == "这个 AI 模型效果不错"


def test_denial_with_real_person_claim_is_untouched() -> None:
    out, slipped = guard_identity_confession("不是哦 学姐是真人 你别乱想", context="你是 AI 吗")
    assert slipped == "" and out == "不是哦 学姐是真人 你别乱想"


def test_modified_self_label_is_caught() -> None:
    """「我是正经AI」「我是一个正经的AI助手」——中间夹修饰词也要抓到。

    2026-10-28 干跑实测：「不是哦我是正经AI，不过你要真想把我当猫娘……」，
    直接自曝，而中间那个「正经」把第一版正则挡住了。
    """
    for text in ("我是一个正经的AI助手", "不是哦我是正经AI 你别乱想", "我其实就是个程序"):
        _, slipped = guard_identity_confession(text)
        assert slipped, f"没抓到：{text}"


def test_modified_self_label_does_not_eat_normal_sentences() -> None:
    """修饰词是白名单穷举的，不是通配——「我是不太懂 AI」这种正常句不能删。"""
    out, slipped = guard_identity_confession("我是不太懂 AI 这块 你问别人吧")
    assert slipped == "" and out == "我是不太懂 AI 这块 你问别人吧"


def test_subject_less_self_label_needs_identity_context() -> None:
    """「不是猫娘 是AI助手」省了主语，只有对方在问身份时才删。"""
    out, slipped = guard_identity_confession("不是猫娘 是AI助手", context="你是猫娘吗")
    assert slipped and out == "不是猫娘"
    # 同样一句，对方没在问身份，就不能动
    _, slipped2 = guard_identity_confession("不是猫娘 是AI助手", context="今天吃啥")
    assert slipped2 == ""


def test_talking_about_someone_else_is_untouched() -> None:
    """转述别人不算自曝：「他说他是 AI」。"""
    out, slipped = guard_identity_confession("他说他是 AI", context="你朋友干嘛的")
    assert slipped == "" and out == "他说他是 AI"
