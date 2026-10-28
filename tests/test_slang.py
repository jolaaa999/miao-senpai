"""群黑话词表：清洗 / 存储 / 注入 / 权限。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from dl_senpai.slang import (
    SlangStore,
    build_slang_brief,
    execute_slang,
    normalize_term,
    parse_slang_request,
    reset_slang_store,
)


class _Cfg:
    slang_enable = True
    slang_brief_enable = True
    owner_ids = "42"

    def __init__(self, tmp_path: Path) -> None:
        self.slang_dir = str(tmp_path / "slang")

    def is_owner(self, user_id) -> bool:
        return str(user_id) == "42"


def test_normalize_term_strips_quotes_and_spaces():
    assert normalize_term(" 「绝绝子」 ") == "绝绝子"
    assert normalize_term("栓 Q") == "栓Q"
    assert normalize_term("x" * 40) == "x" * 12


def test_store_note_is_idempotent_and_counts(tmp_path):
    store = SlangStore(tmp_path / "slang.json")
    store.note("绝绝子", now=1.0)
    entry = store.note("绝绝子", now=2.0)
    assert entry.hits == 2
    assert store.get("绝绝子").status == "pending"


def test_store_confirm_then_persist(tmp_path):
    path = tmp_path / "slang.json"
    store = SlangStore(path)
    store.confirm("绝绝子", "好看得离谱")
    again = SlangStore(path)
    entry = again.get("绝绝子")
    assert entry is not None
    assert entry.status == "confirmed"
    assert entry.meaning == "好看得离谱"


def test_store_drop(tmp_path):
    store = SlangStore(tmp_path / "slang.json")
    store.confirm("栓Q", "谢谢（阴阳怪气版）")
    assert store.drop("栓Q")
    assert store.get("栓Q") is None
    assert not store.drop("不存在的词")


def test_brief_only_contains_confirmed(tmp_path):
    reset_slang_store()
    cfg = _Cfg(tmp_path)
    store = SlangStore(Path(cfg.slang_dir) / "slang.json")
    store.confirm("绝绝子", "好看得离谱")
    store.note("栓Q")
    brief = build_slang_brief(cfg)
    assert "绝绝子" in brief
    assert "栓Q" not in brief


def test_brief_empty_when_nothing_confirmed(tmp_path):
    reset_slang_store()
    assert build_slang_brief(_Cfg(tmp_path)) == ""


def test_brief_respects_disabled_flag(tmp_path):
    reset_slang_store()
    cfg = _Cfg(tmp_path)
    cfg.slang_brief_enable = False
    SlangStore(Path(cfg.slang_dir) / "slang.json").confirm("绝绝子", "好看得离谱")
    assert build_slang_brief(cfg) == ""


def test_parse_ops():
    _, calls = parse_slang_request("<<<SLANG LIST>>>")
    assert calls[0].arguments["op"] == "list"

    _, calls = parse_slang_request("<<<SLANG NEW 绝绝子>>>")
    assert calls[0].arguments == {"op": "new", "term": "绝绝子", "meaning": ""}

    text, calls = parse_slang_request("懂了 <<<SLANG ADD 绝绝子=好看得离谱>>> 吧")
    assert "SLANG" not in text
    assert calls[0].arguments["op"] == "add"
    assert calls[0].arguments["term"] == "绝绝子"
    assert calls[0].arguments["meaning"] == "好看得离谱"


def test_parse_bare_term_is_get():
    _, calls = parse_slang_request("<<<SLANG 绝绝子>>>")
    assert calls[0].arguments["op"] == "get"
    assert calls[0].arguments["term"] == "绝绝子"


def _ctx(tmp_path, *, user_id="42"):
    return SimpleNamespace(config=_Cfg(tmp_path), user_id=user_id, session_id="group:1")


@pytest.mark.asyncio
async def test_execute_add_requires_owner(tmp_path):
    reset_slang_store()
    from dl_senpai.agent.tools.models import ToolCall

    call = ToolCall(
        tool_name="slang",
        arguments={"op": "add", "term": "绝绝子", "meaning": "好看得离谱"},
    )
    denied = await execute_slang(_ctx(tmp_path, user_id="999"), call)
    assert not denied.success
    assert denied.error_type == "not_owner"

    allowed = await execute_slang(_ctx(tmp_path), call)
    assert allowed.success


@pytest.mark.asyncio
async def test_execute_get_unknown_tells_model_not_to_guess(tmp_path):
    reset_slang_store()
    from dl_senpai.agent.tools.models import ToolCall

    call = ToolCall(tool_name="slang", arguments={"op": "get", "term": "绝绝子"})
    result = await execute_slang(_ctx(tmp_path), call)
    assert result.success
    assert "没有" in result.output
    assert "别硬猜" in result.output


@pytest.mark.asyncio
async def test_execute_get_pending_does_not_leak_meaning(tmp_path):
    reset_slang_store()
    from dl_senpai.agent.tools.models import ToolCall

    store = SlangStore(Path(_Cfg(tmp_path).slang_dir) / "slang.json")
    store.note("绝绝子")
    call = ToolCall(tool_name="slang", arguments={"op": "get", "term": "绝绝子"})
    result = await execute_slang(_ctx(tmp_path), call)
    assert "还没确认" in result.output
