from __future__ import annotations

from dl_senpai.friend_add_web import (
    _add_ok,
    _parse_cgi_payload,
    gtk_from_skey,
    parse_cookie_string,
)


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


