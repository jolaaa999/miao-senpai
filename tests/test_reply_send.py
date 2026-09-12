from __future__ import annotations

from nonebot.adapters.onebot.v11 import MessageSegment

from dl_senpai.reply_send import (
    build_forward_nodes,
    should_split_image_voice,
    split_human_bubbles,
    split_reply_chunks,
)


def test_split_short_text() -> None:
    assert split_reply_chunks("短回复", 900) == ["短回复"]


def test_split_prefers_paragraphs() -> None:
    text = ("第一段内容。" * 20) + "\n\n" + ("第二段内容。" * 20)
    chunks = split_reply_chunks(text, 80)
    assert len(chunks) >= 2
    assert all(len(c) <= 80 + 20 for c in chunks)  # 允许句子边界略超一点？实际上 cut 在 limit 内
    assert all(len(c) <= 80 for c in chunks)


def test_split_long_plain() -> None:
    text = "测" * 250
    chunks = split_reply_chunks(text, 100)
    assert len(chunks) == 3
    assert "".join(chunks) == text


def test_should_split_image_voice() -> None:
    image = MessageSegment.image(file="file:///tmp/a.png")
    voice = MessageSegment.record(file="file:///tmp/a.wav")
    assert should_split_image_voice(image, voice) is True
    assert should_split_image_voice(image, None) is False
    assert should_split_image_voice(None, voice) is False
    assert should_split_image_voice(None, None) is False


def test_build_forward_nodes_with_sticker() -> None:
    nodes = build_forward_nodes(
        ["第一段", "第二段"],
        user_id=12345,
        nickname="学姐",
        sticker=MessageSegment.face(id_=1),
    )
    assert len(nodes) == 3
    assert all(n.type == "node" for n in nodes)


def test_split_human_bubbles_short_unchanged() -> None:
    assert split_human_bubbles("嗯。", min_chars=40) == ["嗯。"]


def test_split_human_bubbles_by_sentences() -> None:
    text = "你这笑点有点怪啊。行吧确实好笑。别整天复读了哼。再这样不理你了。"
    bubbles = split_human_bubbles(text, min_chars=20, prefer_chars=18, max_bubbles=4)
    assert len(bubbles) >= 2
    assert "".join(bubbles) == text


def test_split_human_bubbles_respects_max() -> None:
    text = "一句。两句。三句。四句。五句。"
    bubbles = split_human_bubbles(text, min_chars=5, prefer_chars=8, max_bubbles=3)
    assert 1 <= len(bubbles) <= 3
