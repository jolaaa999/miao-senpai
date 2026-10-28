"""reset喵 命令的测试。

重点覆盖三件"错了就很难发现"的事：
1. 权限 —— 非管理员绝不能清（破坏性操作）
2. 范围 —— 只动当前会话：群里不碰别的群，私聊不碰别人的会话
3. 备份 —— 删之前必须留下可回捞的备份

2026-10-14：reset喵 开始支持私聊。私聊的三个层级文件名和群聊并不一致
（历史 private_<uid>、卡片 user_<uid>、没有群风格），所以下面单独覆盖，
免得"清理跑了个空"却没人发现。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dl_senpai.reset_cmd import parse_reset_scope, scope_key


# ---- 替换掉 handlers 的回复，避免测试真的去发消息 ----

@pytest.fixture(autouse=True)
def _patch_reply(monkeypatch):
    sent: list[str] = []

    async def fake_reply(bot, event, text, **kwargs):
        sent.append(text)

    import dl_senpai.handlers as handlers

    monkeypatch.setattr(handlers, "_reply", fake_reply)
    return sent


def _make_ctx(*, group_id="900001", user_id="10001", admin="10001", config=None):
    """造一个最小可用的 ctx（命令走 Router，拿的是 bot/raw_event 通道）。

    group_id 传空串即模拟私聊：私聊事件本来就没有 group_id 字段，
    这里让 raw_event 也照同样的形状来，否则测不到真实的取字段路径。
    """
    from dl_senpai.gateway import MessageContext

    cfg = config or SimpleNamespace(
        memory_dir="", memory_cards_dir="", group_style_dir="",
        agent_admin_qq=admin,
    )
    private = not group_id
    if private:
        msg = MessageContext(
            message_type="private", group_id="", user_id=user_id,
            session_id=f"private:{user_id}", plain_text="reset喵",
            raw_event=SimpleNamespace(user_id=user_id),
            bot=object(),
        )
    else:
        msg = MessageContext(
            message_type="group", group_id=group_id, user_id=user_id,
            session_id=f"group:{group_id}", plain_text="reset喵",
            raw_event=SimpleNamespace(group_id=group_id, user_id=user_id),
            bot=object(),
        )
    return SimpleNamespace(
        group_id=group_id, user_id=user_id, session_id=msg.session_id,
        message_context=msg, raw_event=msg.raw_event, bot=msg.bot, config=cfg,
    )


class _Match:
    def __init__(self, arg=""):
        self._arg = arg

    def arg(self, index=0, default=""):
        return self._arg if index == 0 else default


def _run(ctx, arg=""):
    """执行 reset。

    权限拒绝现在**不回话**，而是抛 CommandPermissionDenied 让 Router 静默跳过
    （回一句「只有管理员能用」等于向全群复述谁有权限）。测试里吞掉它，
    各用例照旧用「文件有没有被清」判断结果。
    """
    from dl_senpai.core.commands.models import CommandPermissionDenied
    from dl_senpai.reset_cmd import handle_reset_command

    try:
        return asyncio.run(handle_reset_command(ctx, _Match(arg)))
    except CommandPermissionDenied:
        return None


def _seed(tmp_path: Path) -> dict[str, Any]:
    """建出三个存储目录，本群和另一个群各放一份。"""
    mem = tmp_path / "memory"
    cards = tmp_path / "cards"
    style = tmp_path / "style"
    stickers = tmp_path / "stickers"
    for d in (mem, cards, style, stickers):
        d.mkdir()
    (mem / "group_900001.json").write_text(json.dumps([{"role": "user", "content": "傲娇"}]), encoding="utf-8")
    (mem / "group_OTHER.json").write_text(json.dumps([{"role": "user", "content": "别的群"}]), encoding="utf-8")
    (cards / "group_900001.json").write_text(json.dumps({"cards": []}), encoding="utf-8")
    (cards / "group_OTHER.json").write_text(json.dumps({"cards": []}), encoding="utf-8")
    (style / "group_900001.json").write_text(json.dumps({"phrases": {}}), encoding="utf-8")
    (style / "group_OTHER.json").write_text(json.dumps({"phrases": {}}), encoding="utf-8")
    # 私聊：历史在 memory/private_<uid>.json，个人卡片在 cards/user_<uid>.json
    # （个人卡片跨群共享，名字是 user_ 前缀而不是 private_）
    (mem / "private_10001.json").write_text(json.dumps([{"role": "user", "content": "私聊历史"}]), encoding="utf-8")
    (mem / "private_OTHER.json").write_text(json.dumps([{"role": "user", "content": "别人"}]), encoding="utf-8")
    (cards / "user_10001.json").write_text(json.dumps({"cards": []}), encoding="utf-8")
    (cards / "user_OTHER.json").write_text(json.dumps({"cards": []}), encoding="utf-8")
    # 表情包：必须原封不动
    (stickers / "group_900001.json").write_text(json.dumps({"傲娇": []}), encoding="utf-8")
    return {"memory": mem, "cards": cards, "style": style, "stickers": stickers}


def _config(stores):
    return SimpleNamespace(
        memory_dir=str(stores["memory"]),
        memory_cards_dir=str(stores["cards"]),
        group_style_dir=str(stores["style"]),
        agent_admin_qq="10001,10002",
    )


# ---- 层级解析 ----

@pytest.mark.parametrize("arg,expected", [
    ("", "history"), ("历史", "history"), ("对话", "history"), ("记忆", "history"),
    ("卡片", "cards"), ("记忆卡", "cards"),
    ("风格", "style"), ("群风格", "style"),
    ("全部", "all"), ("所有", "all"),
])
def test_scope_aliases(arg, expected):
    assert parse_reset_scope(arg) == expected


@pytest.mark.parametrize("arg", ["乱七八糟", "emoji", "all", "history"])
def test_unknown_scope_is_rejected(arg):
    """不认识的参数必须拒绝，不能默默退化成默认层级。"""
    assert parse_reset_scope(arg) is None


# ---- 权限 ----

def test_non_admin_cannot_clean(tmp_path):
    stores = _seed(tmp_path)
    ctx = _make_ctx(user_id="99999", config=_config(stores))
    _run(ctx)
    assert (stores["memory"] / "group_900001.json").is_file(), "非管理员竟然清掉了历史"


def test_admin_can_clean(tmp_path, _patch_reply):
    stores = _seed(tmp_path)
    ctx = _make_ctx(user_id="10001", config=_config(stores))
    _run(ctx)
    assert not (stores["memory"] / "group_900001.json").is_file()
    assert any("清掉" in s for s in _patch_reply)


def test_second_admin_also_allowed(tmp_path):
    stores = _seed(tmp_path)
    ctx = _make_ctx(user_id="10002", config=_config(stores))
    _run(ctx)
    assert not (stores["memory"] / "group_900001.json").is_file()


def test_no_admin_configured_means_nobody(tmp_path):
    """配置为空 ⇒ 谁都不是管理员。反过来写会变成全开的后门。"""
    stores = _seed(tmp_path)
    cfg = _config(stores)
    cfg.agent_admin_qq = ""
    ctx = _make_ctx(user_id="10001", config=cfg)
    _run(ctx)
    assert (stores["memory"] / "group_900001.json").is_file()


# ---- 范围：只动本群 ----

def test_other_groups_are_never_touched(tmp_path):
    stores = _seed(tmp_path)
    ctx = _make_ctx(group_id="900001", config=_config(stores))
    _run(ctx, "全部")
    for key in ("memory", "cards", "style"):
        assert (stores[key] / "group_OTHER.json").is_file(), "%s 的别的群被动了" % key


# ---- 私聊（2026-10-14 起支持）----

def test_private_chat_cleans_private_history(tmp_path):
    """私聊里默认清的是 private_<uid>，不是任何群的历史。"""
    stores = _seed(tmp_path)
    ctx = _make_ctx(group_id="", config=_config(stores))
    _run(ctx)
    assert not (stores["memory"] / "private_10001.json").is_file()
    # 群历史一个都不能碰
    assert (stores["memory"] / "group_900001.json").is_file()
    assert (stores["memory"] / "group_OTHER.json").is_file()


def test_private_chat_never_touches_other_people(tmp_path):
    """别人的私聊历史必须原样保留 —— 这是私聊版最容易写错的地方。"""
    stores = _seed(tmp_path)
    ctx = _make_ctx(group_id="", user_id="10001", config=_config(stores))
    _run(ctx, "全部")
    assert (stores["memory"] / "private_OTHER.json").is_file()
    assert (stores["cards"] / "user_OTHER.json").is_file()


def test_private_cards_use_user_scope(tmp_path):
    """个人卡片的作用域是 user_<uid>（跨群共享），不是 private_<uid>。

    写错成 private_<uid> 不会报错，只会永远清不到东西——
    所以这条断言必须盯住真实文件名。
    """
    stores = _seed(tmp_path)
    ctx = _make_ctx(group_id="", config=_config(stores))
    _run(ctx, "卡片")
    assert not (stores["cards"] / "user_10001.json").is_file()
    # 历史不受影响（「卡片」只清卡片）
    assert (stores["memory"] / "private_10001.json").is_file()


def test_private_all_skips_group_style(tmp_path, _patch_reply):
    """私聊没有群风格这一层，必须明说跳过了，不能假装清过。"""
    stores = _seed(tmp_path)
    ctx = _make_ctx(group_id="", config=_config(stores))
    _run(ctx, "全部")
    assert (stores["style"] / "group_900001.json").is_file()
    assert any("群风格" in s and "没有" in s for s in _patch_reply), _patch_reply


def test_private_cards_reply_warns_about_cross_group(tmp_path, _patch_reply):
    """在私聊清掉的个人卡片是跨群共享的，回复必须提醒。"""
    stores = _seed(tmp_path)
    ctx = _make_ctx(group_id="", config=_config(stores))
    _run(ctx, "卡片")
    assert any("跨群" in s for s in _patch_reply), _patch_reply


def test_private_requires_admin(tmp_path):
    """私聊同样只认管理员，别把破坏性操作开放给任何人。"""
    stores = _seed(tmp_path)
    ctx = _make_ctx(group_id="", user_id="99999", admin="10001", config=_config(stores))
    _run(ctx)
    assert (stores["memory"] / "private_10001.json").is_file()


def test_private_reply_uses_its_own_wording(tmp_path, _patch_reply):
    """私聊回复不能说"本群"。"""
    stores = _seed(tmp_path)
    ctx = _make_ctx(group_id="", config=_config(stores))
    _run(ctx)
    assert _patch_reply
    assert not any("本群" in s for s in _patch_reply), _patch_reply


def test_private_backup_is_left(tmp_path):
    stores = _seed(tmp_path)
    original = (stores["memory"] / "private_10001.json").read_text(encoding="utf-8")
    ctx = _make_ctx(group_id="", config=_config(stores))
    _run(ctx)
    backups = list(stores["memory"].glob("private_10001.json.bak-*"))
    assert backups, "私聊清理也必须留备份"
    assert backups[0].read_text(encoding="utf-8") == original


def test_private_missing_history_is_not_an_error(tmp_path, _patch_reply):
    """没聊过的人发 reset喵，应答"本来就是空的"，不抛异常。

    注意 admin 要一起改：只换 user_id 会先被权限挡住，
    那测的就是"没权限"而不是"文件不存在"，看起来通过其实没覆盖到。
    """
    stores = _seed(tmp_path)
    cfg = _config(stores)
    cfg.agent_admin_qq = "10001,77777"
    ctx = _make_ctx(group_id="", user_id="77777", config=cfg)
    _run(ctx)
    assert any("空" in s for s in _patch_reply), _patch_reply


# ---- 分级行为 ----

def test_default_only_cleans_history(tmp_path):
    """不写参数只清历史 —— 卡片和风格代价更大，不该被默认带走。"""
    stores = _seed(tmp_path)
    ctx = _make_ctx(config=_config(stores))
    _run(ctx)
    assert not (stores["memory"] / "group_900001.json").is_file()
    assert (stores["cards"] / "group_900001.json").is_file()
    assert (stores["style"] / "group_900001.json").is_file()


def test_all_cleans_every_layer(tmp_path):
    stores = _seed(tmp_path)
    ctx = _make_ctx(config=_config(stores))
    _run(ctx, "全部")
    for key in ("memory", "cards", "style"):
        assert not (stores[key] / "group_900001.json").is_file(), key


def test_stickers_are_never_cleaned(tmp_path):
    """表情包不在清理范围内：图本身没有风格，是历史在带偏。"""
    stores = _seed(tmp_path)
    ctx = _make_ctx(config=_config(stores))
    _run(ctx, "全部")
    assert (stores["stickers"] / "group_900001.json").is_file()


# ---- 备份 ----

def test_cleaning_leaves_a_backup(tmp_path):
    stores = _seed(tmp_path)
    original = (stores["memory"] / "group_900001.json").read_text(encoding="utf-8")
    ctx = _make_ctx(config=_config(stores))
    _run(ctx)
    backups = list(stores["memory"].glob("group_900001.json.bak-*"))
    assert backups, "没有留下备份"
    assert backups[0].read_text(encoding="utf-8") == original


def test_repeated_cleaning_does_not_overwrite_backup(tmp_path):
    """同一秒内重复执行也要各留一份，不能互相覆盖。"""
    stores = _seed(tmp_path)
    ctx = _make_ctx(config=_config(stores))
    _run(ctx)
    (stores["memory"] / "group_900001.json").write_text("[]", encoding="utf-8")
    _run(ctx)
    assert len(list(stores["memory"].glob("group_900001.json.bak-*"))) == 2


def test_missing_store_is_not_an_error(tmp_path, _patch_reply):
    """目录不存在（新群没说过话）时应答"本来就是空的"，不抛异常。"""
    stores = _seed(tmp_path)
    ctx = _make_ctx(group_id="888888", config=_config(stores))
    _run(ctx)
    assert any("空" in s for s in _patch_reply)


def test_scope_key_matches_memory_naming():
    assert scope_key("123") == "group_123"


def test_resolve_target_group():
    from dl_senpai.reset_cmd import resolve_target

    t = resolve_target(group_id="123", user_id="999")
    assert t is not None
    assert (t.history_key, t.cards_key, t.style_key) == ("group_123", "group_123", "group_123")
    assert t.is_group is True
    assert t.label == "本群"


def test_resolve_target_private():
    from dl_senpai.reset_cmd import resolve_target

    t = resolve_target(group_id="", user_id="999")
    assert t is not None
    # 历史是 private_，卡片是 user_，风格为空（私聊不采样群风格）
    assert (t.history_key, t.cards_key, t.style_key) == ("private_999", "user_999", "")
    assert t.is_group is False


def test_resolve_target_without_ids_is_none():
    from dl_senpai.reset_cmd import resolve_target

    assert resolve_target(group_id="", user_id="") is None
    assert resolve_target(group_id="  ", user_id=" ") is None

# ---- 身份取账号，不取群名片（2026-10-13 线上事故回归）----
#
# 事故经过：管理员在群里发「reset喵」被拒，日志里 reset.denied 的 actor
# 明明就是管理员 QQ 号。根因是 is_admin 拿 context.actor 去比对管理员列表，
# 而 actor 是「发送者显示名（优先群名片）」——线上是昵称，永远比不中。
#
# 为什么之前的测试没发现：_make_ctx 造的 ctx 没有 actor 字段，
# 于是 is_admin 回退到 user_id，刚好走对；
# test_high_risk_tools.py 的辅助函数又把 actor 和 user_id 设成了同一个值，
# 把「两者不一致」这个真实情况完全盖住了。下面的用例专门补上这个缺口。

def _with_actor(ctx, actor):
    """给测试 ctx 挂一个群名片，模拟线上 MessageContext.actor。"""
    ctx.actor = actor
    return ctx


def test_admin_is_recognized_even_with_a_nickname(tmp_path):
    """管理员有群名片（actor 是昵称）时，依然要放行。

    这就是线上挂掉的那个场景。
    """
    stores = _seed(tmp_path)
    ctx = _with_actor(
        _make_ctx(user_id="10001", config=_config(stores)), "喵喵"
    )
    _run(ctx)
    assert not (stores["memory"] / "group_900001.json").is_file(), (
        "管理员因为群名片不是 QQ 号而被拒了"
    )


def test_nickname_cannot_impersonate_an_admin(tmp_path):
    """把群名片改成管理员的 QQ 号，不能因此获得权限。

    群名片是用户自己随便改的，一旦它被当作身份凭证，
    任何群友改个名片就能清空本群记忆。
    """
    stores = _seed(tmp_path)
    ctx = _with_actor(
        _make_ctx(user_id="99999", config=_config(stores)), "10001"
    )
    _run(ctx)
    assert (stores["memory"] / "group_900001.json").is_file(), (
        "群名片伪装成管理员 QQ 号就拿到了权限"
    )
