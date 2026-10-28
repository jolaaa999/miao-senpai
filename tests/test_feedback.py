"""反馈记录：落盘 / 轮转 / 解析。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dl_senpai.feedback import (
    append_feedback,
    execute_feedback,
    feedback_path,
    parse_feedback_request,
    read_feedback,
)


class _Cfg:
    feedback_enable = True

    def __init__(self, tmp_path):
        self.feedback_dir = str(tmp_path / "feedback")


def test_append_and_read_roundtrip(tmp_path):
    cfg = _Cfg(tmp_path)
    append_feedback("不像人", "回得像客服", config=cfg, session_id="group:1")
    records = read_feedback(cfg)
    assert len(records) == 1
    assert records[0]["kind"] == "不像人"
    assert records[0]["text"] == "回得像客服"


def test_unknown_kind_falls_back(tmp_path):
    cfg = _Cfg(tmp_path)
    record = append_feedback("莫名其妙", "x", config=cfg)
    assert record["kind"] == "不像人"


def test_text_is_capped(tmp_path):
    cfg = _Cfg(tmp_path)
    record = append_feedback("不像人", "字" * 200, config=cfg)
    assert len(record["text"]) == 80


def test_rotation_keeps_latest_file_bounded(tmp_path, monkeypatch):
    import dl_senpai.feedback as mod

    monkeypatch.setattr(mod, "_MAX_LINES", 3)
    cfg = _Cfg(tmp_path)
    for i in range(6):
        append_feedback("不像人", f"第{i}条", config=cfg)
    path = feedback_path(cfg)
    backup = path.with_suffix(".jsonl.1")
    assert backup.exists()
    assert len(read_feedback(cfg)) <= 3


def test_parse_feedback_request():
    text, calls = parse_feedback_request("算了 <<<FEEDBACK 不像人|刚才那句太像客服>>>")
    assert "FEEDBACK" not in text
    assert calls[0].arguments["kind"] == "不像人"
    assert calls[0].arguments["text"] == "刚才那句太像客服"


@pytest.mark.asyncio
async def test_execute_feedback_writes_record(tmp_path):
    cfg = _Cfg(tmp_path)
    from dl_senpai.agent.tools.models import ToolCall

    ctx = SimpleNamespace(config=cfg, session_id="group:1", user_id="9")
    call = ToolCall(tool_name="feedback", arguments={"kind": "被冷落", "text": "问了没人接"})
    result = await execute_feedback(ctx, call)
    assert result.success
    assert read_feedback(cfg)[0]["kind"] == "被冷落"


@pytest.mark.asyncio
async def test_execute_feedback_empty_text_fails(tmp_path):
    cfg = _Cfg(tmp_path)
    from dl_senpai.agent.tools.models import ToolCall

    ctx = SimpleNamespace(config=cfg, session_id="group:1", user_id="9")
    result = await execute_feedback(ctx, ToolCall(tool_name="feedback", arguments={"kind": "不像人"}))
    assert not result.success
    assert result.error_type == "empty_text"
