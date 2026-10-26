"""记心事工具：她自己的心绪手札写入入口。

守三件事：
1. marker 解析可靠，且**不能泄漏**给群友（半截 marker 也要剥掉）；
2. 写进的作用域必须跟内心状态读的是同一把钥匙，否则写了读不到；
3. 关掉内心状态时不暴露工具，免得她调一个必然失败的写入口。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from dl_senpai.agent.tools.models import ToolCall
from dl_senpai.agent.tools.note_tool import (
    _parse_note,
    _visible_note,
    build_note_tool,
    execute_note,
)


def test_parse_tagged_note():
    cleaned, calls = _parse_note("行吧，就这样。<<<NOTE 被冷落|他一句话都没回我>>>")
    assert cleaned == "行吧，就这样。"
    assert len(calls) == 1
    assert calls[0].tool_name == "note"
    assert calls[0].arguments["tag"] == "被冷落"
    assert calls[0].arguments["text"] == "他一句话都没回我"


def test_parse_untagged_note():
    cleaned, calls = _parse_note("<<<NOTE 有点闷，说不上来>>>")
    assert cleaned == ""
    assert calls[0].arguments == {"tag": "", "text": "有点闷，说不上来"}


def test_half_written_marker_is_stripped():
    """写了一半的 marker 绝不能发到群里。"""
    cleaned, calls = _parse_note("嗯。\n<<<NOTE 被冷落|他没回")
    assert calls == []
    assert "NOTE" not in cleaned


def test_long_note_is_truncated():
    _cleaned, calls = _parse_note("<<<NOTE 不安|" + "啊" * 100 + ">>>")
    assert len(calls[0].arguments["text"]) == 40


def _ctx(tmp_path, *, enable=True, group="980229149", private=False):
    cfg = SimpleNamespace(
        inner_state_enable=enable,
        inner_state_dir=str(tmp_path / "inner"),
        inner_state_per_group=True,
    )
    return SimpleNamespace(config=cfg, group_id=group, private_chat=private, user_id="u1")


def test_execute_writes_into_same_scope_as_inner_state(tmp_path):
    from dl_senpai.inner_state import get_inner_state_store, scope_key

    ctx = _ctx(tmp_path)
    call = ToolCall(tool_name="note", arguments={"tag": "被夸", "text": "他说我代码写得好"})
    result = asyncio.run(execute_note(ctx, call))
    assert result.success is True

    key = scope_key(per_group=True, group_id="980229149", private=False)
    state = get_inner_state_store(str(tmp_path / "inner")).load(key)
    assert state.notes and state.notes[-1]["text"] == "他说我代码写得好"
    assert state.notes[-1]["tag"] == "被夸"


def test_execute_respects_three_day_life(tmp_path):
    """旧手札被顺手清掉，不会无限堆积。"""
    from dl_senpai.inner_state import get_inner_state_store, scope_key

    ctx = _ctx(tmp_path)
    key = scope_key(per_group=True, group_id="980229149", private=False)
    store = get_inner_state_store(str(tmp_path / "inner"))
    store.add_note(key, "五天前的旧账", "被冷落", now=time.time() - 5 * 86400)

    asyncio.run(execute_note(ctx, ToolCall(tool_name="note", arguments={"tag": "被夸", "text": "今天被夸了"})))
    state = store.load(key)
    assert [n["text"] for n in state.notes] == ["今天被夸了"]


def test_execute_fails_when_disabled(tmp_path):
    ctx = _ctx(tmp_path, enable=False)
    result = asyncio.run(execute_note(ctx, ToolCall(tool_name="note", arguments={"text": "随便"})))
    assert result.success is False
    assert result.error_type == "disabled"


def test_visibility_follows_inner_state(tmp_path):
    assert _visible_note(_ctx(tmp_path)) is True
    assert _visible_note(_ctx(tmp_path, enable=False)) is False
    assert _visible_note(SimpleNamespace(config=None)) is False


def test_tool_definition_contract():
    tool = build_note_tool()
    assert tool.name == "note"
    assert tool.marker == "NOTE"
    assert tool.side_effect is True, "写盘工具必须标副作用，fallback 才不会重放"
    assert "记" in tool.prompt_text
