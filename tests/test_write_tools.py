"""写入层（说说 / 黑盒评论）的围栏测试。

这一层说错一句就是"以她的名义在公开平台发了一条"，撤不回来。
所以钉的是四道门，不是发布效果：
1. 内容清洗（超长 / 链接 / 手机号 / 加群引流都要拒）
2. 草稿能存、能列、能标记已发
3. 不是主人触发 -> 拒；群里 publish -> 拒
4. 工具默认不注册（开关是 false）
真发那条链路要登录态，单测不碰网络。
"""

from __future__ import annotations

import asyncio

import pytest

from dl_senpai import qzone as qz
from dl_senpai.heybox import HeyboxError, clean_comment
from dl_senpai.qzone import QzoneError, compose_text, gtk_from_skey
from dl_senpai.agent.tools.write_tools import (
    _parse_heypost,
    _parse_qzone,
    build_write_tools,
)


class _Cfg:
    def __init__(self, tmp: str) -> None:
        self.qzone_max_chars = 120
        self.qzone_draft_file = tmp
        self.heybox_comment_max_chars = 300
        self.qzone_enable = True
        self.heybox_write_enable = True
        self.qzone_auto_post = False
        self.qzone_daily_limit = 1
        self.web_login_guard_owner = True
        self.owner_ids = "123456"
        self.heybox_comment_daily_limit = 3

    def owner_id_set(self) -> set[str]:
        return {"123456"}

    def is_owner(self, user_id: object) -> bool:
        return str(user_id) == "123456"


# ---------------------------------------------------------------- 内容清洗


def test_compose_accepts_normal_text() -> None:
    assert compose_text("  今天晚饭煮了番茄面，真香  ") == "今天晚饭煮了番茄面，真香"


def test_compose_collapses_blank_lines() -> None:
    assert compose_text("一\n\n\n二") == "一\n二"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "看看这个 https://example.com 好文",
        "打我电话 13812345678",
        "想进的加群 123456789",
    ],
)
def test_compose_rejects_bad_text(text: str) -> None:
    with pytest.raises(QzoneError):
        compose_text(text)


def test_compose_rejects_too_long() -> None:
    with pytest.raises(QzoneError):
        compose_text("啊" * 200)


def test_clean_comment_rejects_links() -> None:
    with pytest.raises(HeyboxError):
        clean_comment("看 https://a.com")


def test_clean_comment_ok() -> None:
    assert clean_comment("这套配装我试了，斩杀确实稳") == "这套配装我试了，斩杀确实稳"


# ---------------------------------------------------------------- 草稿


def test_draft_roundtrip(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "drafts.json"))
    draft = qz.save_draft("今天风好大", cfg)
    assert draft.pending
    pending = qz.list_drafts(cfg, only_pending=True)
    assert [d.text for d in pending] == ["今天风好大"]
    assert qz.mark_published(draft.draft_id, tid="t1", cfg=cfg) is True
    assert qz.list_drafts(cfg, only_pending=True) == []


def test_draft_rejects_bad_content_without_writing(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "drafts.json"))
    with pytest.raises(QzoneError):
        qz.save_draft("https://example.com", cfg)
    assert qz.list_drafts(cfg) == []


def test_drop_single_draft(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "drafts.json"))
    first = qz.save_draft("第一条", cfg)
    qz.save_draft("第二条", cfg)
    assert qz.drop_draft(first.draft_id, cfg) is True
    assert [d.text for d in qz.list_drafts(cfg)] == ["第二条"]
    # 删第二次应当返回 False：已经没了
    assert qz.drop_draft(first.draft_id, cfg) is False


def test_drop_all_keeps_published(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "drafts.json"))
    sent = qz.save_draft("发过的", cfg)
    qz.mark_published(sent.draft_id, tid="t9", cfg=cfg)
    qz.save_draft("没发的", cfg)
    assert qz.clear_drafts(cfg) == 1
    assert [d.text for d in qz.list_drafts(cfg)] == ["发过的"]


def test_parse_qzone_drop() -> None:
    _cleaned, calls = _parse_qzone("<<<QZONE drop all>>>")
    assert calls[0].arguments == {"action": "drop", "text": "all"}


def test_latest_draft_returns_newest_pending(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "drafts.json"))
    qz.save_draft("第一条", cfg)
    qz.save_draft("第二条", cfg)
    latest = qz.latest_draft(cfg)
    assert latest is not None and latest.text == "第二条"


# ---------------------------------------------------------------- g_tk


def test_gtk_from_skey_matches_known_value() -> None:
    # hash33 的算法是固定的，用公开实现算出来的已知结果钉一下
    assert gtk_from_skey("") == "5381"
    assert gtk_from_skey("a") == "177670"
    assert gtk_from_skey("@abc") != gtk_from_skey("abc")


# ---------------------------------------------------------------- marker


def test_parse_qzone_draft() -> None:
    cleaned, calls = _parse_qzone("行 <<<QZONE draft 今天风好大>>>")
    assert calls[0].arguments == {"action": "draft", "text": "今天风好大"}
    assert "QZONE" not in cleaned


def test_parse_qzone_publish_without_text() -> None:
    _cleaned, calls = _parse_qzone("<<<QZONE publish>>>")
    assert calls[0].arguments["action"] == "publish"
    assert calls[0].arguments["text"] == ""


def test_parse_qzone_empty_draft_dropped() -> None:
    cleaned, calls = _parse_qzone("<<<QZONE draft>>>")
    assert calls == []
    assert "QZONE" not in cleaned


def test_parse_heypost() -> None:
    cleaned, calls = _parse_heypost("<<<HEYPOST 8e7f729adcb3 | 这套配装我试了>>>")
    assert calls[0].arguments == {
        "action": "comment",
        "ref": "8e7f729adcb3",
        "text": "这套配装我试了",
    }
    assert "HEYPOST" not in cleaned


def test_parse_heypost_tail_marker_removed() -> None:
    cleaned, calls = _parse_heypost("好\n<<<HEYPOST 8e7f729adcb3 |")
    assert calls == []
    assert "HEYPOST" not in cleaned


# ---------------------------------------------------------------- 四道门


class _Ctx:
    def __init__(self, cfg, user_id: int = 123456, group_id: str = "") -> None:
        self.config = cfg
        self.user_id = user_id
        self.group_id = group_id


def _call(tool: str, args: dict):
    from dl_senpai.agent.tools.models import ToolCall

    return ToolCall(tool_name=tool, arguments=args)


def test_group_publish_blocked(tmp_path) -> None:
    """群里喊一句「发」不算授权——这条最容易被绕过去。"""
    from dl_senpai.agent.tools.write_tools import execute_qzone

    cfg = _Cfg(str(tmp_path / "drafts.json"))
    qz.save_draft("草稿内容", cfg)
    ctx = _Ctx(cfg, user_id=123456, group_id="958691688")
    result = asyncio.run(execute_qzone(ctx, _call("qzone", {"action": "publish", "text": ""})))
    assert result.success is False
    assert "私聊" in (result.output or "")


def test_non_owner_cannot_draft(tmp_path) -> None:
    from dl_senpai.agent.tools.write_tools import execute_qzone

    cfg = _Cfg(str(tmp_path / "drafts.json"))
    ctx = _Ctx(cfg, user_id=999999, group_id="")
    result = asyncio.run(
        execute_qzone(ctx, _call("qzone", {"action": "draft", "text": "随便"}))
    )
    assert result.success is False
    assert qz.list_drafts(cfg) == []


def test_publish_without_draft_says_so(tmp_path) -> None:
    from dl_senpai.agent.tools.write_tools import execute_qzone

    cfg = _Cfg(str(tmp_path / "drafts.json"))
    ctx = _Ctx(cfg, user_id=123456, group_id="")
    result = asyncio.run(execute_qzone(ctx, _call("qzone", {"action": "publish", "text": ""})))
    assert result.success is False
    assert "草稿" in (result.output or "")


def test_non_owner_cannot_comment(tmp_path) -> None:
    from dl_senpai.agent.tools.write_tools import execute_heybox_write

    cfg = _Cfg(str(tmp_path / "drafts.json"))
    ctx = _Ctx(cfg, user_id=999999, group_id="")
    result = asyncio.run(
        execute_heybox_write(
            ctx, _call("heybox_write", {"action": "comment", "ref": "abc12345", "text": "顶"})
        )
    )
    assert result.success is False


def test_write_tools_default_off() -> None:
    class _Off:
        qzone_enable = False
        heybox_write_enable = False

    assert build_write_tools(_Off()) == []


def test_write_tools_are_side_effecting(tmp_path) -> None:
    """写入工具必须标 side_effect=True：这决定失败后会不会被重跑（重跑就是发第二条）。"""
    cfg = _Cfg(str(tmp_path / "drafts.json"))
    tools = build_write_tools(cfg)
    assert {t.name for t in tools} == {"qzone", "heybox_write"}
    assert all(t.side_effect is True for t in tools)
    assert all(t.owner == "write_layer" for t in tools)
