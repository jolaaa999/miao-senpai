from __future__ import annotations

from dl_senpai.style import append_meow, polish_reply


def test_polish_reply_keeps_text() -> None:
    assert polish_reply("先别慌。过拟合就是记太死了。") == "先别慌。过拟合就是记太死了。"


def test_polish_reply_keeps_tone() -> None:
    assert polish_reply("已经有了哦～") == "已经有了哦～"


def test_polish_reply_empty() -> None:
    assert polish_reply("") == "在呢～"


def test_polish_reply_strips_fake_emoji_and_markers() -> None:
    text = polish_reply("哼！\n<<<STICKER 傲娇>>>\n[学姐温柔微笑 emoji]")
    assert "<<<STICKER" not in text
    assert "emoji" not in text.lower()
    assert "哼" in text


def test_polish_reply_strips_search_leak() -> None:
    text = polish_reply(
        "这次按 Wiki 核对。\n"
        "<<<SEARCH 怪物猎人世界 Wiki 珊瑚水妖鸟 弱点 肉质 冰属性 >>"
    )
    assert "<<<SEARCH" not in text
    assert "Wiki" in text


def test_polish_reply_strips_counselor_and_cant_talk() -> None:
    text = polish_reply(
        "好的，小笨蛋，已经帮你禁言了～ "
        "对了，你现在暂时不能发消息，学姐会在旁边呢。"
        "是不是最近遇到了什么烦心事？学姐在呢，来跟学姐说说吧。"
        "哼，谁是笨蛋啊！"
    )
    assert "发不了消息" not in text and "不能发消息" not in text
    assert "心事" not in text
    assert "说说吧" not in text
    assert "哼" in text


def test_strip_unapplied_mute_claims() -> None:
    from dl_senpai.style import strip_unapplied_mute_claims

    text = strip_unapplied_mute_claims(
        "好的，小笨蛋，已经帮你禁言了～ 哼，你才笨蛋呢。"
    )
    assert "已经帮你禁言了" not in text
    assert "笨蛋" in text


def test_append_meow_alias() -> None:
    assert append_meow("测试") == "测试"


def test_polish_reply_strips_markdown_flavor() -> None:
    text = polish_reply(
        "先说结论：\n"
        "- **过拟合**就是记太死了\n"
        "- 先砍学习率\n"
        "1. 看日志\n"
        "2. 再改 batch\n"
        "### 小结\n"
        "就这样。"
    )
    assert "**" not in text
    assert not any(line.lstrip().startswith("- ") for line in text.splitlines())
    assert "过拟合" in text
    assert "学习率" in text
    assert "#" not in text
    assert "就这样" in text
