"""表情本子：解析 / 备注 / 手动收藏 / 看图。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dl_senpai.sticker_book import _split_note, execute_emoji, parse_emoji_request
from dl_senpai.stickers import (
    StickerRecord,
    StickerStore,
    get_sticker_store,
    reset_sticker_store,
)


class _Cfg:
    sticker_book_enable = True

    def __init__(self, tmp_path):
        self.sticker_dir = str(tmp_path / "stickers")
        self.sticker_max_per_session = 200


def _record(sticker_id: str, *, collected_at: str, note: str = "") -> StickerRecord:
    return StickerRecord(
        id=sticker_id,
        segment_type="mface",
        segment_data={"emoji_id": sticker_id},
        context_text="",
        keywords=[],
        collected_at=collected_at,
        note=note,
    )


def test_parse_list_show_note_collect():
    _, calls = parse_emoji_request("<<<EMOJI>>>")
    assert calls[0].arguments["op"] == "list"

    _, calls = parse_emoji_request("<<<EMOJI 2>>>")
    assert calls[0].arguments["op"] == "show"
    assert calls[0].arguments["ref"] == "2"

    _, calls = parse_emoji_request("<<<EMOJI NOTE 3 阴阳人|#阴阳 #无语>>>")
    args = calls[0].arguments
    assert args["op"] == "note"
    assert args["ref"] == "3"
    assert args["note"] == "阴阳人"
    assert args["tags"] == ["阴阳", "无语"]

    text, calls = parse_emoji_request("看这个 <<<EMOJI COLLECT 123456>>>")
    assert "EMOJI" not in text
    assert calls[0].arguments["op"] == "collect"
    assert calls[0].arguments["ref"] == "123456"


def test_split_note_strips_tags_from_body():
    note, tags = _split_note("这图很阴阳|#阴阳 #无语")
    assert note == "这图很阴阳"
    assert tags == ["阴阳", "无语"]


def test_split_note_without_tags():
    note, tags = _split_note("就是这张")
    assert note == "就是这张"
    assert tags == []


def test_recent_orders_newest_first(tmp_path):
    store = StickerStore(tmp_path)
    store.save(
        "group:1",
        [
            _record("a", collected_at="2026-01-01T00:00:00"),
            _record("c", collected_at="2026-03-01T00:00:00"),
            _record("b", collected_at="2026-02-01T00:00:00"),
        ],
    )
    assert [r.id for r in store.recent("group:1")] == ["c", "b", "a"]


def test_find_by_index_and_prefix(tmp_path):
    store = StickerStore(tmp_path)
    store.save("group:1", [_record("image:abc", collected_at="2026-01-01T00:00:00")])
    assert store.find("group:1", "1").id == "image:abc"
    assert store.find("group:1", "image:ab").id == "image:abc"
    assert store.find("group:1", "9") is None


def test_set_note_merges_tags(tmp_path):
    store = StickerStore(tmp_path)
    store.save("group:1", [_record("a", collected_at="2026-01-01T00:00:00")])
    store.set_note("group:1", "1", note="阴阳人", tags=["阴阳"])
    updated = store.set_note("group:1", "1", tags=["无语", "阴阳"])
    assert updated.note == "阴阳人"
    # 重复标签不重复追加，顺序保留
    assert updated.tags == ["阴阳", "无语"]
    # 落盘后再读还在（save 是真写文件）
    assert store.find("group:1", "1").note == "阴阳人"


def _ctx(tmp_path, *, bot=None):
    cfg = _Cfg(tmp_path)
    return SimpleNamespace(
        config=cfg, session_id="group:1", user_id="300", plain_text="哈哈", bot=bot
    )


@pytest.mark.asyncio
async def test_list_on_empty_book(tmp_path):
    reset_sticker_store()
    from dl_senpai.agent.tools.models import ToolCall

    result = await execute_emoji(_ctx(tmp_path), ToolCall(tool_name="emoji", arguments={"op": "list"}))
    assert result.success
    assert "空" in result.output


@pytest.mark.asyncio
async def test_show_returns_segment(tmp_path):
    reset_sticker_store()
    from dl_senpai.agent.tools.models import ToolCall

    ctx = _ctx(tmp_path)
    store = get_sticker_store(ctx.config)
    store.save("group:1", [_record("mface:1", collected_at="2026-01-01T00:00:00")])
    result = await execute_emoji(
        ctx, ToolCall(tool_name="emoji", arguments={"op": "show", "ref": "1"})
    )
    assert result.success
    assert result.data["value"] is not None


@pytest.mark.asyncio
async def test_note_on_missing_index_fails(tmp_path):
    reset_sticker_store()
    from dl_senpai.agent.tools.models import ToolCall

    result = await execute_emoji(
        _ctx(tmp_path),
        ToolCall(tool_name="emoji", arguments={"op": "note", "ref": "5", "note": "x"}),
    )
    assert not result.success
    assert result.error_type == "not_found"


class _Bot:
    def __init__(self, message):
        self._message = message

    async def get_msg(self, message_id: int):
        return {"message_id": message_id, "message": self._message}


@pytest.mark.asyncio
async def test_collect_from_message_id(tmp_path):
    reset_sticker_store()
    from dl_senpai.agent.tools.models import ToolCall

    message = [{"type": "mface", "data": {"emoji_id": "abc", "summary": "[动画表情]"}}]
    ctx = _ctx(tmp_path, bot=_Bot(message))
    result = await execute_emoji(
        ctx, ToolCall(tool_name="emoji", arguments={"op": "collect", "ref": "123456"})
    )
    assert result.success
    assert result.data["added"] == 1
    assert get_sticker_store(ctx.config).count("group:1") == 1


@pytest.mark.asyncio
async def test_collect_without_sticker_in_message(tmp_path):
    reset_sticker_store()
    from dl_senpai.agent.tools.models import ToolCall

    ctx = _ctx(tmp_path, bot=_Bot([{"type": "text", "data": {"text": "纯文字"}}]))
    result = await execute_emoji(
        ctx, ToolCall(tool_name="emoji", arguments={"op": "collect", "ref": "1"})
    )
    assert result.success
    assert result.data["added"] == 0


@pytest.mark.asyncio
async def test_disabled_flag(tmp_path):
    from dl_senpai.agent.tools.models import ToolCall

    ctx = _ctx(tmp_path)
    ctx.config.sticker_book_enable = False
    result = await execute_emoji(ctx, ToolCall(tool_name="emoji", arguments={"op": "list"}))
    assert not result.success
    assert result.error_type == "disabled"
