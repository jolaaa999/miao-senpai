from __future__ import annotations

import json
from pathlib import Path

from dl_senpai.speak_styles import SpeakStyle, SpeakStyleStore, _BUILTIN_ID


def test_speak_style_switch_and_builtin(tmp_path: Path) -> None:
    store = SpeakStyleStore(tmp_path)
    snap = store.snapshot()
    assert snap["active_id"] == _BUILTIN_ID
    assert any(s["id"] == _BUILTIN_ID for s in snap["styles"])

    st = SpeakStyle(
        id="alice_ft",
        name="Alice",
        kind="finetune",
        model="alice-lora",
        base_url="http://127.0.0.1:8000/v1",
        system_overlay="说话更短更冲",
        source_person="Alice",
    )
    store.upsert(st)
    active = store.set_active("alice_ft")
    assert active.id == "alice_ft"
    assert store.active().model == "alice-lora"

    store.delete("alice_ft")
    assert store.active_id() == _BUILTIN_ID


def test_cannot_delete_builtin(tmp_path: Path) -> None:
    store = SpeakStyleStore(tmp_path)
    _ = store.snapshot()  # ensure styles.json exists
    try:
        store.delete(_BUILTIN_ID)
        assert False, "should raise"
    except ValueError:
        pass
    assert (tmp_path / "styles.json").is_file()
    raw = json.loads((tmp_path / "styles.json").read_text(encoding="utf-8"))
    assert raw["active_id"] == _BUILTIN_ID
    assert any(s["id"] == _BUILTIN_ID for s in raw["styles"])
