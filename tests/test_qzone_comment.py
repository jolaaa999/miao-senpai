from __future__ import annotations

import asyncio

from dl_senpai.qzone_comment import (
    _clean_comment,
    _fallback_comment,
    _too_generic,
    generate_qzone_comment,
)


def test_fallback_food_and_clean() -> None:
    t = _fallback_comment("今天火锅太香了", "小王")
    assert t
    assert "火锅" in t or "吃" in t or "饿" in t or "美食" in t or "吃饭" in t
    assert _clean_comment('「哈哈哈测试」') == "哈哈哈测试"
    assert _too_generic("好看！")
    assert not _too_generic("这顿火锅看起来绝了")


def test_generate_uses_fallback_when_llm_off(monkeypatch) -> None:
    class _Cfg:
        qzone_comment_llm = False

        def api_configured(self):
            return False

    text = asyncio.run(
        generate_qzone_comment(_Cfg(), summary="周末去爬山打卡", nickname="阿华")  # type: ignore[arg-type]
    )
    assert text
    assert len(text) >= 4
