from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from dl_senpai.config import PluginConfig
from dl_senpai.stickers import (
    StickerRecord,
    StickerStore,
    extract_keywords,
    extract_sticker_segments,
    maybe_pick_sticker_reply,
    parse_sticker_request,
    score_sticker,
)


def _sticker_test_dir(name: str) -> Path:
    base = Path("data") / "_pytest_stickers" / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)
    return base


def test_extract_sticker_segments() -> None:
    msg = Message(
        MessageSegment.text("今天好开心")
        + MessageSegment.face(id_=123)
        + MessageSegment("image", {"file": "abc.jpg", "sub_type": 1})
        + MessageSegment("image", {"file": "photo.jpg", "sub_type": 0})
    )
    found = extract_sticker_segments(msg)
    types = [item[0] for item in found]
    assert types == ["image"]


def test_extract_sticker_segments_ignores_qq_face() -> None:
    msg = Message(MessageSegment.face(id_=178))
    assert extract_sticker_segments(msg) == []


@pytest.mark.asyncio
async def test_collect_and_deduplicate() -> None:
    store = StickerStore(_sticker_test_dir("collect"), max_per_session=10)
    session_id = "group:123"
    msg = Message(
        MessageSegment("mface", {"summary": "[动画表情]", "emoji_id": "42"})
        + MessageSegment.text("摸鱼")
    )
    added = await store.collect_from_event(
        _FakeEvent(msg),
        session_id=session_id,
        context_text="摸鱼",
        source_user="小明",
    )
    assert added == 1
    again = await store.collect_from_event(
        _FakeEvent(msg),
        session_id=session_id,
        context_text="继续摸鱼",
        source_user="小明",
    )
    assert again == 0
    records = store.load(session_id)
    assert len(records) == 1
    assert "继续摸鱼" in records[0].context_text


def test_pick_prefers_matching_context(monkeypatch: pytest.MonkeyPatch) -> None:
    store = StickerStore(_sticker_test_dir("pick"))
    session_id = "group:456"
    happy = StickerRecord(
        id="happy",
        segment_type="face",
        segment_data={"id": "1"},
        context_text="今天好开心",
        keywords=["开心"],
    )
    sad = StickerRecord(
        id="sad",
        segment_type="face",
        segment_data={"id": "2"},
        context_text="好难啊",
        keywords=["难"],
    )
    store.save(session_id, [happy, sad])
    monkeypatch.setattr("dl_senpai.stickers.random.random", lambda: 0.0)
    picked = store.pick(
        session_id,
        user_text="我也好开心",
        reply_text="那就庆祝一下",
        reply_prob=1.0,
    )
    assert picked is not None
    assert picked.id == "happy"


def test_score_and_keywords() -> None:
    record = StickerRecord(
        id="x",
        segment_type="face",
        segment_data={"id": "1"},
        context_text="过拟合好难",
        keywords=["过拟合", "难"],
    )
    assert score_sticker(record, extract_keywords("过拟合怎么办")) >= 0.8


def test_maybe_pick_sticker_reply_respects_probability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sticker_dir = _sticker_test_dir("prob")
    cfg = PluginConfig(
        DL_SENPAI_STICKER_ENABLE=True,
        DL_SENPAI_STICKER_DIR=str(sticker_dir),
        DL_SENPAI_STICKER_REPLY_PROB=0.0,
    )
    store = StickerStore(sticker_dir)
    store.save(
        "group:1",
        [
            StickerRecord(
                id="a",
                segment_type="face",
                segment_data={"id": "1"},
                context_text="hi",
            )
        ],
    )
    monkeypatch.setattr("dl_senpai.stickers._store", store)
    assert (
        maybe_pick_sticker_reply(
            "group:1",
            user_text="hi",
            reply_text="hello",
            interrupt=False,
            config=cfg,
        )
        is None
    )


def test_parse_sticker_request_strips_marker_and_fake_emoji() -> None:
    raw = "哼，你才是猫娘！\n<<<STICKER 炸毛傲娇>>>\n[学姐温柔微笑 emoji]"
    req = parse_sticker_request(raw)
    assert req.force is True
    assert "炸毛" in req.query or "傲娇" in req.query
    assert "<<<STICKER" not in req.text
    assert "emoji" not in req.text.lower()


def test_pick_force_ignores_probability(monkeypatch: pytest.MonkeyPatch) -> None:
    store = StickerStore(_sticker_test_dir("force"))
    session_id = "group:789"
    store.save(
        session_id,
        [
            StickerRecord(
                id="img1",
                segment_type="image",
                segment_data={"file": "a.jpg", "sub_type": 1},
                context_text="傲娇炸毛",
                keywords=["傲娇", "炸毛"],
            )
        ],
    )
    monkeypatch.setattr("dl_senpai.stickers.random.random", lambda: 0.99)
    picked = store.pick(
        session_id,
        user_text="你是猫娘",
        reply_text="哼",
        reply_prob=0.0,
        force=True,
        extra_query="傲娇",
    )
    assert picked is not None
    assert picked.id == "img1"


class _FakeEvent:
    def __init__(self, message: Message) -> None:
        self.message = message
