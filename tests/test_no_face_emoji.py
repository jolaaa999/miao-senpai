"""学姐说话不该带黄脸 emoji。

两道防线各自独立：提示词明令禁止（persona），polish_reply 兜底剥离（style）。
这里把两道都钉住——只改提示词的话，模型在情绪重的轮次照样会漏几个出来。
"""

from __future__ import annotations

from dl_senpai.style import polish_reply, strip_face_emoji


def test_strips_face_emoji() -> None:
    assert strip_face_emoji("好呀😤") == "好呀"
    assert strip_face_emoji("😏 你猜") == "你猜"
    assert strip_face_emoji("今天真累😭😭") == "今天真累"
    assert strip_face_emoji("🥺抱抱") == "抱抱"


def test_strips_gesture_emoji() -> None:
    # 👀 看戏、👍 点赞 也是「拿符号代替说话」，同属这一类
    assert strip_face_emoji("我盯着你👀") == "我盯着你"
    assert strip_face_emoji("干得漂亮👍") == "干得漂亮"


def test_keeps_object_symbols() -> None:
    # 签到/好感榜单的固定装饰，不能跟着剥（会把这些界面改花）
    assert strip_face_emoji("💕 好感档案") == "💕 好感档案"
    assert strip_face_emoji("🥇 第一名") == "🥇 第一名"
    assert strip_face_emoji("✨ 寄语：顺利") == "✨ 寄语：顺利"


def test_english_spacing_survives() -> None:
    """剥 emoji 不能把中英混排的空格粘掉（hello world ≠ helloworld）。"""
    assert strip_face_emoji("hello 😏 world") == "hello world"


def test_keeps_plain_text_untouched() -> None:
    text = "好的，我在。刚才那个问题我觉得要先看文档。"
    assert strip_face_emoji(text) == text
    assert strip_face_emoji("") == ""


def test_polish_reply_drops_emoji_end_to_end() -> None:
    assert polish_reply("行吧😑") == "行吧"
    # 中间夹一个也要清干净，不留双空格
    assert polish_reply("就这 😏 还行") == "就这还行"


def test_persona_no_longer_teaches_emoji() -> None:
    """人设里原先写着「要情绪就用真 emoji」，那是这习惯的源头。"""
    from dl_senpai.persona import SYSTEM_PROMPT

    assert "要情绪就用真 emoji" not in SYSTEM_PROMPT
    assert "黄脸 emoji" in SYSTEM_PROMPT