"""评论区读取 / 留言回复 / 昵称解码的测试。

为什么钉这些：
1. 解析层坏掉时不会报错，只会让 she 以为"没人回我"（假阴性最误导人）；
2. `%uXXXX` 昵称解码错一次，她就在评论区认不出自己；
3. 回复 marker 的三段式如果没排在两段式前面，会被普通评论规则吃掉。
"""

from __future__ import annotations

import json

from dl_senpai import heybox as hs
from dl_senpai.write_guard import check_can_comment, record_comment


def _tree_payload(floors: list[dict]) -> str:
    return json.dumps({"status": "ok", "msg": "", "result": {"comments": floors}}, ensure_ascii=False)


def _comment(cid: str, user: str, text: str, **extra) -> dict:
    data = {
        "commentid": cid,
        "text": text,
        "user": {"username": user},
        "create_at": 1792808701,
        "up": 3,
        "floor_num": 1,
    }
    data.update(extra)
    return data


def test_parse_tree_floor_with_preview_children() -> None:
    body = _tree_payload(
        [
            {
                "comment": [
                    _comment("100", "路人甲", "根评论", child_num=5, has_more=1),
                    _comment("101", "九儿的小肥羊", "我说一句"),
                    _comment("102", "路人乙", "回你一句"),
                ]
            }
        ]
    )
    rows, captcha = hs._parse_tree_body(body)
    assert captcha is False
    assert [c.comment_id for c in rows] == ["100", "101", "102"]
    assert rows[0].child_num == 5
    assert rows[0].has_more is True
    assert rows[1].user == "九儿的小肥羊"


def test_parse_tree_captcha_is_reported() -> None:
    rows, captcha = hs._parse_tree_body('{"msg":"","result":{},"status":"show_captcha"}')
    assert rows == []
    assert captcha is True


def test_parse_tree_garbage_returns_empty() -> None:
    assert hs._parse_tree_body("<html>不是 JSON</html>") == ([], False)


def test_reply_marker_parsed_before_comment_marker() -> None:
    from dl_senpai.agent.tools.write_tools import _parse_heypost

    cleaned, calls = _parse_heypost("<<<HEYPOST reply 249d72b2b8ac|哥们这句话|我同意>>>")
    assert len(calls) == 1
    assert calls[0].arguments["action"] == "reply"
    assert calls[0].arguments["ref"] == "249d72b2b8ac"
    assert calls[0].arguments["target"] == "哥们这句话"
    assert calls[0].arguments["text"] == "我同意"
    assert cleaned.strip() == ""


def test_plain_comment_marker_still_works() -> None:
    from dl_senpai.agent.tools.write_tools import _parse_heypost

    _, calls = _parse_heypost("<<<HEYPOST 249d72b2b8ac|随便说一句>>>")
    assert calls[0].arguments["action"] == "comment"
    assert calls[0].arguments["text"] == "随便说一句"


def test_reply_allows_second_comment_on_same_post(tmp_path) -> None:
    class Cfg:
        heybox_write_state_file = str(tmp_path / "s.json")
        heybox_comment_min_gap_sec = 0

    cfg = Cfg()
    record_comment("abc", "第一次", cfg)
    assert check_can_comment("abc", "完全不同的一句", cfg) != ""
    assert check_can_comment("abc", "完全不同的一句", cfg, allow_same_link=True) == ""


def test_nickname_js_escape_decoded(tmp_path, monkeypatch) -> None:
    from dl_senpai import web_login

    state = tmp_path / "heybox.json"
    state.write_text(
        json.dumps(
            {"cookies": [{"name": "nickname", "value": "%u4E5D%u513F%u7684%u5C0F%u80A5%u7F8A"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_login, "state_path", lambda site, cfg=None: state)
    assert web_login.read_nickname("heybox", object()) == "九儿的小肥羊"
