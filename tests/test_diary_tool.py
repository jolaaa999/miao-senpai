"""日记工具：遗忘机制的执行端。

守四件事：
1. marker 能解析、半截的能剥掉（不能漏给群友）；
2. 五个动作各自真的改了盘上的数据（不是只回一句「好了」）；
3. 编号写错一位也能靠前缀找回来；
4. 日记没开时不暴露工具。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from dl_senpai.agent.tools.diary_tool import (
    _parse_diary,
    _visible_diary,
    build_diary_tool,
    execute_diary,
)
from dl_senpai.agent.tools.models import ToolCall
from dl_senpai.diary import DiaryEntry, DiaryStore, TIER_IMPORTANT, TIER_PARKED, TIER_UNFORGETTABLE

DAY = 86400.0


def _ctx(tmp_path, *, enable=True):
    cfg = SimpleNamespace(diary_enable=enable, diary_dir=str(tmp_path / "diary"))
    return SimpleNamespace(config=cfg, group_id="980229149", private_chat=False)


def _seed(tmp_path, *, days=1.0, text="他问我德育线怎么算。"):
    store = DiaryStore(tmp_path / "diary")
    store.add(DiaryEntry(text=text, created_at=time.time() - days * DAY))
    return store


def test_parse_tagged_marker():
    cleaned, calls = _parse_diary("嗯。<<<DIARY 留 44a601cf>>>")
    assert cleaned == "嗯。"
    assert calls[0].tool_name == "diary"
    assert calls[0].arguments == {"action": "留", "id": "44a601cf"}


def test_half_written_marker_is_stripped():
    cleaned, calls = _parse_diary("算了。\n<<<DIARY 留 44a6")
    assert calls == []
    assert "DIARY" not in cleaned


def test_extend_by_prefix(tmp_path):
    store = _seed(tmp_path)
    eid = store.entries()[0].entry_id
    # 只写前 4 位，靠前缀找回来
    result = asyncio.run(execute_diary(_ctx(tmp_path), ToolCall(
        tool_name="diary", arguments={"action": "留", "id": eid[:4]}
    )))
    assert result.success is True, result.error
    assert store.find(eid).total_life_days == 10


def test_upgrade_to_important(tmp_path):
    store = _seed(tmp_path)
    eid = store.entries()[0].entry_id
    asyncio.run(execute_diary(_ctx(tmp_path), ToolCall(
        tool_name="diary", arguments={"action": "重要", "id": eid}
    )))
    entry = store.find(eid)
    assert entry.tier == TIER_IMPORTANT
    assert entry.total_life_days == 21


def test_park_freezes(tmp_path):
    store = _seed(tmp_path, days=100)
    eid = store.entries()[0].entry_id
    asyncio.run(execute_diary(_ctx(tmp_path), ToolCall(
        tool_name="diary", arguments={"action": "搁着", "id": eid}
    )))
    entry = store.find(eid)
    assert entry.tier == TIER_PARKED
    assert entry.days_left() > 1000


def test_remove_goes_to_recycle_bin(tmp_path):
    store = _seed(tmp_path)
    eid = store.entries()[0].entry_id
    asyncio.run(execute_diary(_ctx(tmp_path), ToolCall(
        tool_name="diary", arguments={"action": "划掉", "id": eid}
    )))
    assert store.entries() == []
    assert any(e.is_deleted for e in store.entries(include_deleted=True))


def test_unknown_action_and_missing_entry(tmp_path):
    store = _seed(tmp_path)
    eid = store.entries()[0].entry_id
    bad = asyncio.run(execute_diary(_ctx(tmp_path), ToolCall(
        tool_name="diary", arguments={"action": "烧了", "id": eid}
    )))
    assert bad.success is False and bad.error_type == "bad_action"
    miss = asyncio.run(execute_diary(_ctx(tmp_path), ToolCall(
        tool_name="diary", arguments={"action": "留", "id": "deadbeef"}
    )))
    assert miss.success is False and miss.error_type == "not_found"


def test_disabled_and_visibility(tmp_path):
    result = asyncio.run(execute_diary(_ctx(tmp_path, enable=False), ToolCall(
        tool_name="diary", arguments={"action": "留", "id": "44a601cf"}
    )))
    assert result.success is False and result.error_type == "disabled"
    assert _visible_diary(_ctx(tmp_path)) is True
    assert _visible_diary(_ctx(tmp_path, enable=False)) is False


def test_tool_definition_contract():
    tool = build_diary_tool()
    assert tool.name == "diary" and tool.marker == "DIARY"
    assert tool.side_effect is True
    for word in ("留", "搁着", "划掉"):
        assert word in tool.prompt_text
