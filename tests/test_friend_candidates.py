from __future__ import annotations

import asyncio
import json
from pathlib import Path

import dl_senpai.friend_candidates as fc


class _FakeBot:
    self_id = "10000"

    async def call_api(self, api: str, **kwargs):
        if api == "get_friend_list":
            return [{"user_id": 111}]
        if api == "get_group_member_list":
            gid = kwargs["group_id"]
            if gid == 100:
                return [
                    {"user_id": 10000, "nickname": "bot"},
                    {"user_id": 111, "nickname": "already"},
                    {"user_id": 222, "nickname": "new", "card": "小王"},
                    {"user_id": 333, "nickname": "also"},
                ]
            if gid == 200:
                return [
                    {"user_id": 222, "nickname": "new2"},
                    {"user_id": 444, "nickname": "only200"},
                ]
            return []
        raise RuntimeError(api)


def test_collect_and_export(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fc, "_OUT_DIR", tmp_path)

    class _Cfg:
        friend_add_exclude = "333"

    monkeypatch.setattr(fc, "get_config", lambda: _Cfg())

    async def _scan(bot, config):
        return ["100", "200"]

    monkeypatch.setattr(fc, "_list_scan_group_ids", _scan)

    bot = _FakeBot()
    payload = asyncio.run(fc.collect_non_friend_candidates(bot))  # type: ignore[arg-type]
    uids = {c["user_id"] for c in payload["candidates"]}
    assert uids == {"222", "444"}
    assert "111" not in uids and "10000" not in uids and "333" not in uids
    c222 = next(c for c in payload["candidates"] if c["user_id"] == "222")
    assert set(c222["groups"]) == {"100", "200"}

    info = asyncio.run(fc.export_non_friend_candidates(bot))  # type: ignore[arg-type]
    assert info["candidate_count"] == 2
    latest = Path(info["latest_txt"])
    assert latest.is_file()
    lines = {x for x in latest.read_text(encoding="utf-8").splitlines() if x}
    assert lines == {"222", "444"}
    data = json.loads(Path(info["latest_json"]).read_text(encoding="utf-8"))
    assert data["candidate_count"] == 2
