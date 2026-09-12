from __future__ import annotations

from dl_senpai.friend_add_web import (
    _add_ok,
    _parse_cgi_payload,
    gtk_from_skey,
    parse_cookie_string,
)
from dl_senpai.qzone_client import normalize_feeds


def test_gtk_and_cookie_parse() -> None:
    assert parse_cookie_string("uin=o1; skey=abc; p_skey=xyz")["p_skey"] == "xyz"
    assert gtk_from_skey("abc").isdigit()


def test_add_ok_codes() -> None:
    assert _add_ok({"code": 0}) is True
    assert _add_ok({"ret": 0, "message": "ok"}) is True
    assert _add_ok({"code": 1, "message": "已经是好友"}) is True
    assert _add_ok({"code": 1, "message": "频率限制"}) is False
    assert _add_ok({"_error": "html_response"}) is False


def test_parse_html_callback() -> None:
    html = '<html><script>cb({"code":0,"message":"成功"});</script></html>'
    assert _parse_cgi_payload(html).get("code") == 0


def test_normalize_feeds() -> None:
    rows = normalize_feeds(
        {
            "list": [
                {"uin": 1, "tid": "a", "content": "x", "isLiked": True},
                {"user_id": 2, "tid": "b", "summary": "y"},
                {"bad": True},
            ]
        }
    )
    assert len(rows) == 2
    assert rows[0]["user_id"] == "1" and rows[0]["liked"] is True
    assert rows[1]["tid"] == "b"
