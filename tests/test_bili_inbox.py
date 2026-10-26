"""B站：留言解析 / 评论 marker / 台账分平台的测试。

为什么钉这些：
1. msgfeed 的字段有一堆历史包袱（oid 在 reply、root 可能在 item），
   解析错了不会报错，只会让她"回错地方"；
2. 两段式和四段式 marker 顺序错了，回复会被当成顶层评论发出去；
3. 小黑盒和 B 站的台账必须分开，否则"这篇评过了"会跨平台误伤。
"""

from __future__ import annotations

from dl_senpai import bili as bl
from dl_senpai.write_guard import check_can_comment, load_state, record_comment


def test_parse_msgfeed_reply_item() -> None:
    payload = {
        "code": 0,
        "data": {
            "items": [
                {
                    "user": {"mid": 1, "nickname": "路人丙"},
                    "item": {"type": "reply", "subject_id": 114514, "root_id": 777},
                    "reply": {
                        "id": 888,
                        "oid": 114514,
                        "root": 777,
                        "ctime": 1792800000,
                        "content": {"message": "你这套配装不行"},
                        "target_reply": {"content": {"message": "我这套能打"}},
                    },
                }
            ]
        },
    }
    rows = bl._parse_msgfeed(payload)
    assert len(rows) == 1
    row = rows[0]
    assert row.user == "路人丙"
    assert row.text == "你这套配装不行"
    assert row.my_text == "我这套能打"
    assert row.oid == 114514
    assert row.root_id == 777
    assert row.parent_id == 888
    assert row.message_id == "114514:888"
    assert row.replyable is True


def test_parse_msgfeed_skips_at_and_like() -> None:
    payload = {
        "data": {
            "items": [
                {"item": {"type": "at"}, "reply": {"id": 1, "oid": 2, "content": {"message": "@你"}}},
                {"item": {"type": "like"}, "reply": {"id": 3, "oid": 4}},
            ]
        }
    }
    assert bl._parse_msgfeed(payload) == []


def test_parse_msgfeed_root_falls_back_to_parent() -> None:
    payload = {
        "data": {
            "items": [
                {
                    "user": {"nickname": "路人丁"},
                    "item": {"type": "reply", "subject_id": 9},
                    "reply": {"id": 55, "oid": 9, "content": {"message": "顶"}},
                }
            ]
        }
    }
    row = bl._parse_msgfeed(payload)[0]
    assert row.root_id == 55 == row.parent_id


def test_bilipost_reply_marker_before_comment() -> None:
    from dl_senpai.agent.tools.write_tools import _parse_bilipost

    _, calls = _parse_bilipost("<<<BILIPOST reply BV1xx411c7mD|777|888|顺着你说一句>>>")
    assert len(calls) == 1
    assert calls[0].arguments["action"] == "reply"
    assert calls[0].arguments["root_id"] == 777
    assert calls[0].arguments["parent_id"] == 888
    assert calls[0].arguments["text"] == "顺着你说一句"


def test_bilipost_plain_comment_marker() -> None:
    from dl_senpai.agent.tools.write_tools import _parse_bilipost

    _, calls = _parse_bilipost("<<<BILIPOST BV1xx411c7mD|这期剪得挺顺>>>")
    assert calls[0].arguments["action"] == "comment"
    assert calls[0].arguments["text"] == "这期剪得挺顺"


def test_ledger_separated_by_platform(tmp_path) -> None:
    class Cfg:
        heybox_write_state_file = str(tmp_path / "heybox.json")
        bili_write_state_file = str(tmp_path / "bili.json")
        heybox_comment_min_gap_sec = 0
        bili_comment_min_gap_sec = 0

    cfg = Cfg()
    record_comment("BV1xx411c7mD", "B站说一句", cfg, platform="bili")
    # B站的台账不能被小黑盒的检查读到
    assert check_can_comment("BV1xx411c7mD", "另一句", cfg, platform="heybox") == ""
    assert "评过" in check_can_comment("BV1xx411c7mD", "另一句", cfg, platform="bili")
    assert load_state(cfg, "bili")["comments"][-1]["link_id"] == "BV1xx411c7mD"
    assert load_state(cfg, "heybox")["comments"] == []
