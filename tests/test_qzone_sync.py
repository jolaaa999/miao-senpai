from __future__ import annotations

from dl_senpai.qzone_sync import (
    cookie_map_to_string,
    cookies_usable,
    merge_cookie_maps,
    normalize_qzone_cookies,
)


def test_normalize_and_usable() -> None:
    raw = {"skey": "s", "p_skey": "p"}
    fixed = normalize_qzone_cookies(raw, self_id="2480465844")
    assert fixed["uin"].endswith("2480465844")
    assert cookies_usable(fixed)
    assert "p_skey=p" in cookie_map_to_string(fixed)


def test_merge_maps() -> None:
    m = merge_cookie_maps({"a": "1"}, {"b": "2", "a": "9"})
    assert m == {"a": "9", "b": "2"}
