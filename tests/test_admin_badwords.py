"""违禁词测试。

重点：
1. 违禁词是「包含」匹配，不是整句——这是它和词库最像、也最容易搞错的地方；
2. 踩中要记警告、满三次禁言 1 天 23 小时 59 分，且禁完清零（不然第四次
   会立刻又被禁，等于永久封禁）；
3. 主人和群管不被自己的词表误伤——这是「配错就锁死自己」的事故源；
4. 关掉开关后词表还在，只是不再记警告（可以原地停用而不丢配置）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from dl_senpai.admin_badwords import (
    BadwordStore,
    badword_guard,
    badword_handler,
    parse_badword_command,
)
from dl_senpai.admin_commands import handle_admin_command
from dl_senpai.core.commands.admin import register_admin_commands
from dl_senpai.core.commands.registry import CommandRegistry

ADMIN_ID = "10001"
MEMBER_ID = "20002"
GROUP_ID = "900001"
#: 1 天 23 小时 59 分。写成算式，别让读测试的人自己去数秒
MUTE_SECONDS = 23 * 3600 + 59 * 60 + 86400


@pytest.fixture(autouse=True)
def _patch_reply(monkeypatch):
    sent: list[str] = []

    async def fake_reply(bot, event, text, **kwargs):
        sent.append(text)

    import dl_senpai.handlers as handlers

    monkeypatch.setattr(handlers, "_reply", fake_reply)
    return sent


@pytest.fixture()
def config(tmp_path):
    from dl_senpai.config import PluginConfig

    def _make(**overrides):
        env = {
            "DL_SENPAI_ADMIN_QQ": ADMIN_ID,
            "DL_SENPAI_ADMIN_BADWORDS_PATH": str(tmp_path / "badwords.json"),
            "DL_SENPAI_ADMIN_WARN_PATH": str(tmp_path / "warn.json"),
            "DL_SENPAI_ADMIN_BLACKLIST_PATH": str(tmp_path / "blacklist.json"),
            "DL_SENPAI_ADMIN_SILENCE_PATH": str(tmp_path / "silence.json"),
        }
        env.update(overrides)
        return PluginConfig(**env)

    return _make


class FakeBot:
    self_id = "999999"

    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def delete_msg(self, **kwargs: Any) -> None:
        self.calls.append(("delete_msg", kwargs))
        if not self.allow:
            raise RuntimeError("no permission")

    async def set_group_ban(self, **kwargs: Any) -> None:
        self.calls.append(("set_group_ban", kwargs))
        if not self.allow:
            raise RuntimeError("no permission")

    async def send_group_msg(self, **kwargs: Any) -> None:
        self.calls.append(("send_group_msg", kwargs))


def _ctx(bot: Any, cfg: Any, *, text: str = "", user_id: str = ADMIN_ID):
    event = SimpleNamespace(
        group_id=int(GROUP_ID), user_id=int(user_id), message_id="4242",
        message=[], reply=None, sender=SimpleNamespace(role="member"),
    )
    return SimpleNamespace(
        config=cfg, bot=bot, raw_event=event, user_id=user_id, group_id=GROUP_ID,
        self_id="999999", session_id=f"group:{GROUP_ID}", message_type="group",
        plain_text=text,
    )


def _run(reg: CommandRegistry, ctx: Any, text: str) -> None:
    invocation = reg.match(text, ctx)
    assert invocation is not None, f"没有指令匹配：{text}"
    asyncio.run(invocation.command.handler(ctx, invocation.match))


@pytest.fixture()
def registry() -> CommandRegistry:
    reg = CommandRegistry()
    register_admin_commands(reg, owner="admin_commands")
    return reg


def _add_words(store: BadwordStore, *words: str) -> None:
    for word in words:
        store.add(GROUP_ID, word, by=ADMIN_ID)


# ---- 1. 存储 ----


def test_store_matches_substring_not_whole_sentence(tmp_path):
    """违禁词是「包含」匹配——和词库的整句匹配是反的，这里最容易搞错。"""
    store = BadwordStore(tmp_path / "bw.json")
    _add_words(store, "傻逼")
    assert store.hit(GROUP_ID, "你个傻逼") == "傻逼"
    assert store.hit(GROUP_ID, "傻逼") == "傻逼"
    assert store.hit(GROUP_ID, "今天天气不错") == ""


def test_store_prefers_longest_match(tmp_path):
    """同时配了「傻」和「傻逼」时记更精确的那个，否则日志看不出骂了什么。"""
    store = BadwordStore(tmp_path / "bw.json")
    _add_words(store, "傻", "傻逼")
    assert store.hit(GROUP_ID, "你个傻逼") == "傻逼"


def test_store_english_is_case_insensitive(tmp_path):
    store = BadwordStore(tmp_path / "bw.json")
    _add_words(store, "spam")
    assert store.hit(GROUP_ID, "别 SPAM 了") == "spam"


def test_store_rejects_duplicate_and_empty(tmp_path):
    store = BadwordStore(tmp_path / "bw.json")
    ok, why = store.add(GROUP_ID, "傻逼")
    assert ok and "1 条" in why
    ok, why = store.add(GROUP_ID, "傻逼", by=ADMIN_ID)
    assert not ok and "本来就在" in why
    ok, _ = store.add(GROUP_ID, "   ")
    assert not ok


def test_store_is_per_group(tmp_path):
    """按群存：一个群的尺度不该影响另一个群。"""
    store = BadwordStore(tmp_path / "bw.json")
    _add_words(store, "菜")
    assert store.hit(GROUP_ID, "你好菜") == "菜"
    assert store.hit("888888", "你好菜") == ""


def test_store_disable_keeps_words(tmp_path):
    """关掉检测只是不再记警告，词表要留着——重新打开不用再录一遍。"""
    store = BadwordStore(tmp_path / "bw.json")
    _add_words(store, "菜")
    store.set_enabled(GROUP_ID, False)
    assert store.enabled(GROUP_ID) is False
    assert store.list_group(GROUP_ID) == ["菜"]


# ---- 2. guard ----


def test_guard_blocks_only_when_hit(config):
    cfg = config()
    from dl_senpai.admin_badwords import get_badword_store

    _add_words(get_badword_store(cfg.admin_badwords_path), "傻逼")
    assert badword_guard(_ctx(FakeBot(), cfg, text="你个傻逼", user_id=MEMBER_ID))
    assert not badword_guard(_ctx(FakeBot(), cfg, text="你好呀", user_id=MEMBER_ID))


def test_guard_exempts_admin(config):
    """主人骂人不该被自己禁言——这是最难排查的事故。"""
    cfg = config()
    from dl_senpai.admin_badwords import get_badword_store

    _add_words(get_badword_store(cfg.admin_badwords_path), "傻逼")
    assert not badword_guard(_ctx(FakeBot(), cfg, text="你个傻逼", user_id=ADMIN_ID))


def test_guard_ignores_mao_prefixed_commands(config):
    """「喵bad add 傻逼」这种管理指令不能被自己的词拦下来。"""
    cfg = config()
    from dl_senpai.admin_badwords import get_badword_store

    _add_words(get_badword_store(cfg.admin_badwords_path), "傻逼")
    assert not badword_guard(_ctx(FakeBot(), cfg, text="喵bad add 傻逼", user_id=MEMBER_ID))


def test_guard_respects_off_switch(config):
    cfg = config()
    from dl_senpai.admin_badwords import get_badword_store

    store = get_badword_store(cfg.admin_badwords_path)
    _add_words(store, "傻逼")
    store.set_enabled(GROUP_ID, False)
    assert not badword_guard(_ctx(FakeBot(), cfg, text="你个傻逼", user_id=MEMBER_ID))


# ---- 3. 命中处理：警告与禁言 ----


def _hit(config, bot, text="你个傻逼", user_id=MEMBER_ID):
    cfg = config()
    from dl_senpai.admin_badwords import get_badword_store

    _add_words(get_badword_store(cfg.admin_badwords_path), "傻逼")
    ctx = _ctx(bot, cfg, text=text, user_id=user_id)
    asyncio.run(badword_handler(ctx, None))
    return ctx


def test_hit_recalls_and_warns_once(config, _patch_reply):
    bot = FakeBot()
    cfg = config()
    _hit(config, bot)
    assert any(name == "delete_msg" for name, _ in bot.calls), "要撤回违规消息"
    from dl_senpai.admin_commands_extra import _warn_store

    assert _warn_store(cfg).get(GROUP_ID, MEMBER_ID)["count"] == 1
    assert "1/3" in "\n".join(_patch_reply)


def test_three_hits_ban_for_almost_two_days(config, _patch_reply):
    """满三次禁言 1 天 23 小时 59 分，并清零警告。

    清零很关键：不清的话第四次立刻又被禁，等于判了无期。
    """
    from dl_senpai.admin_commands_extra import _warn_store

    cfg = config()
    bot = FakeBot()
    for _ in range(3):
        _hit(config, bot)

    bans = [kwargs for name, kwargs in bot.calls if name == "set_group_ban"]
    assert bans, "第三次必须禁言"
    assert bans[-1]["duration"] == MUTE_SECONDS
    assert int(_warn_store(cfg).get(GROUP_ID, MEMBER_ID).get("count") or 0) == 0
    assert "1 天 23 小时 59 分" in "\n".join(_patch_reply)


def test_recall_failure_still_warns(config, _patch_reply):
    """撤回失败（她不是管理员）不能连警告一起丢掉。"""
    from dl_senpai.admin_commands_extra import _warn_store

    cfg = config()
    _hit(config, FakeBot(allow=False))
    assert _warn_store(cfg).get(GROUP_ID, MEMBER_ID)["count"] == 1


def test_warn_limit_constant_is_almost_two_days():
    """警告时长常量就是主人要的那个数，别在别处偷偷改回去。"""
    from dl_senpai.admin_commands_extra import WARN_AUTO_MUTE_SECONDS

    assert WARN_AUTO_MUTE_SECONDS == MUTE_SECONDS == 172740


# ---- 4. 指令 ----


def test_parse_badword_command():
    assert parse_badword_command("") == ("list", [])
    assert parse_badword_command("列表") == ("list", [])
    assert parse_badword_command("开") == ("on", [])
    assert parse_badword_command("off") == ("off", [])
    assert parse_badword_command("清空") == ("clear", [])
    assert parse_badword_command("add 傻逼") == ("add", ["傻逼"])
    # 一次加多个：管理员导一批词时一条条发最容易半途而废
    assert parse_badword_command("加 傻逼 脑残") == ("add", ["傻逼", "脑残"])
    assert parse_badword_command("删 傻逼") == ("del", ["傻逼"])


def test_badword_command_lifecycle(registry, config, _patch_reply):
    """一条指令走完：加 → 列表 → 删 → 清空。"""
    cfg = config()
    ctx = _ctx(FakeBot(), cfg, text="喵bad add 傻逼 脑残")
    _run(registry, ctx, "喵bad add 傻逼 脑残")
    joined = "\n".join(_patch_reply)
    assert "加上了「傻逼」" in joined and "加上了「脑残」" in joined

    _run(registry, _ctx(FakeBot(), cfg), "喵bad 列表")
    assert "傻逼" in _patch_reply[-1] and "脑残" in _patch_reply[-1]

    _run(registry, _ctx(FakeBot(), cfg), "喵bad del 脑残")
    assert "删掉了「脑残」" in _patch_reply[-1]

    _run(registry, _ctx(FakeBot(), cfg), "喵bad 清空")
    assert "清空了 1 条" in _patch_reply[-1]


def test_badword_command_chinese_alias(registry, config, _patch_reply):
    """「喵/违禁词」也认——中文别名没删。"""
    cfg = config()
    _run(registry, _ctx(FakeBot(), cfg), "喵/违禁词 add 傻逼")
    assert "加上了「傻逼」" in "\n".join(_patch_reply)


def test_badword_command_ignores_non_admin(registry, config):
    """群友发「喵bad」要被静默跳过，不能让他们自己改词表。"""
    from dl_senpai.admin_commands import AdminDenied

    cfg = config()
    ctx = _ctx(FakeBot(), cfg, text="喵bad add 学长", user_id=MEMBER_ID)
    invocation = registry.match("喵bad add 学长", ctx)
    assert invocation is not None
    with pytest.raises(AdminDenied):
        asyncio.run(invocation.command.handler(ctx, invocation.match))
