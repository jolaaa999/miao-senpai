"""扩展管理指令的测试（群管增强 + 风控 + 运维）。

这批指令的共同风险是「看起来执行了，其实什么都没做」：
拉黑没真的拦、警告没累计、撤回最近撤到别人头上。
所以每个用例都在断言**副作用**（store 内容、API 调用），
而不是只看回复文案是否好看。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from dl_senpai.admin_commands import handle_admin_command
from dl_senpai.core.commands.admin import register_admin_commands
from dl_senpai.core.commands.registry import CommandRegistry


ADMIN_ID = "10001"
GROUP_ID = "900001"
OTHER_ID = "20002"


class FakeBot:
    """记录调用 + 返回可控数据。"""

    self_id = "999999"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.members: list[dict[str, Any]] = []
        self.member_info: dict[int, dict[str, Any]] = {}
        self.group_info: dict[str, Any] = {}
        self.history: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []
        self.fail: set[str] = set()

    def _record(self, api: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((api, kwargs))

    def api_calls(self, api: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if name == api]

    def _maybe_fail(self, api: str) -> None:
        if api in self.fail:
            raise RuntimeError(f"{api} not supported")

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        self._record(api, kwargs)
        self._maybe_fail(api)
        if api in ("_get_group_notice", "get_group_notice"):
            return self.notices
        return None

    async def get_group_member_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_group_member_info", kwargs)
        self._maybe_fail("get_group_member_info")
        return self.member_info.get(int(kwargs.get("user_id") or 0), {})

    async def get_group_member_list(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("get_group_member_list", kwargs)
        self._maybe_fail("get_group_member_list")
        return self.members

    async def get_group_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_group_info", kwargs)
        self._maybe_fail("get_group_info")
        return self.group_info

    async def get_group_msg_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("get_group_msg_history", kwargs)
        self._maybe_fail("get_group_msg_history")
        return self.history

    async def delete_msg(self, **kwargs: Any) -> None:
        self._record("delete_msg", kwargs)

    async def set_group_ban(self, **kwargs: Any) -> None:
        self._record("set_group_ban", kwargs)

    async def set_group_leave(self, **kwargs: Any) -> None:
        self._record("set_group_leave", kwargs)

    async def send(self, event: Any, message: Any) -> None:
        self._record("send", {"message": message})


@pytest.fixture(autouse=True)
def _patch_reply(monkeypatch):
    sent: list[str] = []

    async def fake_reply(bot, event, text, **kwargs):
        sent.append(text)

    import dl_senpai.handlers as handlers

    monkeypatch.setattr(handlers, "_reply", fake_reply)
    return sent


@pytest.fixture()
def registry() -> CommandRegistry:
    reg = CommandRegistry()
    register_admin_commands(reg, owner="admin_commands")
    return reg


@pytest.fixture()
def config(tmp_path):
    from dl_senpai.config import PluginConfig

    def _make(**overrides):
        env = {
            "DL_SENPAI_ADMIN_QQ": ADMIN_ID,
            "DL_SENPAI_ADMIN_SILENCE_PATH": str(tmp_path / "silence.json"),
            "DL_SENPAI_ADMIN_WARN_PATH": str(tmp_path / "warn.json"),
            "DL_SENPAI_ADMIN_BLACKLIST_PATH": str(tmp_path / "blacklist.json"),
            "DL_SENPAI_MEMORY_DIR": str(tmp_path / "memory"),
        }
        env.update(overrides)
        return PluginConfig(**env)

    return _make


def _ctx(bot: FakeBot, cfg: Any, *, text: str = "", user_id: str = ADMIN_ID, at: tuple[str, ...] = ()):
    """造一个 MessageContext 替身（只需要管理指令用到的那几个字段）。"""
    segments = [SimpleNamespace(type="at", data={"qq": q}) for q in at]
    segments.append(SimpleNamespace(type="text", data={"text": text}))
    event = SimpleNamespace(
        group_id=int(GROUP_ID),
        user_id=int(user_id),
        message=segments,
        reply=None,
        sender=SimpleNamespace(role="member"),
    )
    return SimpleNamespace(
        config=cfg,
        bot=bot,
        raw_event=event,
        user_id=user_id,
        group_id=GROUP_ID,
        self_id=bot.self_id,
        session_id=f"group:{GROUP_ID}",
        message_type="group",
        plain_text=text,
    )


def _run(reg: CommandRegistry, ctx: Any, text: str) -> str:
    """匹配 + 执行，返回 action（顺便断言确实匹配上了）。"""
    inv = reg.match(text, ctx)
    assert inv is not None, text
    asyncio.run(handle_admin_command(ctx, inv.match))
    return inv.match.action


# ---- 注册表 ----

def test_all_extended_commands_registered(registry):
    """原有的 17 条 + 群管/风控/运维 24 条 + 工具/词库/定时 5 条 = 46 条。"""
    names = {cmd.name for cmd in registry.list(namespace="admin")}
    assert len(names) == 47, sorted(names)
    for expected in ("admin_whois", "admin_members", "admin_mutelist", "admin_blacklist",
                     "admin_warn", "admin_memory", "admin_health", "admin_reload"):
        assert expected in names, expected


@pytest.mark.parametrize(
    "text,action",
    [
        ("喵/查 20002", "whois"),
        ("喵/成员", "members"),
        ("喵/管理列表", "admins"),
        ("喵/禁言列表", "mutelist"),
        ("喵/群信息", "groupinfo"),
        ("喵/公告列表", "notices"),
        ("喵/撤回公告 1", "noticedel"),
        ("喵/群文件", "files"),
        ("喵/精华", "essence"),
        ("喵/相册", "albums"),
        ("喵/撤回最近 2", "recallrecent"),
        ("喵/退群 确认", "leave"),
        ("喵/警告 20002", "warn"),
        ("喵/警告列表", "warnlist"),
        ("喵/警告清零", "warnclear"),
        ("喵/拉黑 20002", "blacklist"),
        ("喵/解除拉黑 20002", "unblacklist"),
        ("喵/黑名单", "blacklistlist"),
        ("喵/记忆", "memory"),
        ("喵/忘记 全部", "forget"),
        ("喵/权限", "perm"),
        ("喵/健康", "health"),
        ("喵/重载配置", "reload"),
        ("喵/日志 5", "log"),
    ],
)
def test_alias_maps_to_action(registry, config, _patch_reply, text, action):
    bot = FakeBot()
    ctx = _ctx(bot, config(), text=text)
    assert _run(registry, ctx, text) == action


def test_existing_short_names_still_work(registry, config, _patch_reply):
    """新指令不能把老指令的短名字抢走（喵公告 vs 喵公告列表）。"""
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵/公告 大家好")
    assert _run(registry, ctx, "喵/公告 大家好") == "notice"


# ---- 群管查询 ----

def test_whois_reports_role_and_mute(registry, config, _patch_reply):
    bot = FakeBot()
    bot.member_info[20002] = {
        "user_id": 20002,
        "nickname": "小明",
        "card": "小明同学",
        "role": "admin",
        "join_time": 1700000000,
        "shut_up_timestamp": int(time.time()) + 300,
    }
    ctx = _ctx(bot, config(), at=("20002",))
    _run(registry, ctx, "喵/查 @小明")
    text = _patch_reply[-1]
    assert "小明同学" in text
    assert "管理员" in text
    assert "禁言" in text


def test_members_paginates(registry, config, _patch_reply):
    bot = FakeBot()
    bot.members = [
        {"user_id": 30000 + i, "nickname": f"m{i}", "role": "member", "join_time": i}
        for i in range(25)
    ]
    ctx = _ctx(bot, config())
    _run(registry, ctx, "喵/成员 1")
    assert "25 人" in _patch_reply[-1]
    assert "第 1/2 页" in _patch_reply[-1]


def test_members_explains_when_list_unavailable(registry, config, _patch_reply):
    """拿不到列表不能装作「群里没人」。"""
    bot = FakeBot()
    bot.fail.add("get_group_member_list")
    ctx = _ctx(bot, config())
    _run(registry, ctx, "喵/成员")
    assert "拿不到成员列表" in _patch_reply[-1]


def test_mutelist_finds_muted(registry, config, _patch_reply):
    now = int(time.time())
    bot = FakeBot()
    bot.members = [
        {"user_id": 20002, "nickname": "a", "role": "member", "shut_up_timestamp": now + 600},
        {"user_id": 20003, "nickname": "b", "role": "member", "shut_up_timestamp": 0},
    ]
    ctx = _ctx(bot, config())
    _run(registry, ctx, "喵/禁言列表")
    text = _patch_reply[-1]
    assert "20002" in text and "20003" not in text


def test_mutelist_refuses_to_lie(registry, config, _patch_reply):
    """协议端不返回禁言字段时，不能说「没人被禁言」。"""
    bot = FakeBot()
    bot.members = [{"user_id": 20002, "nickname": "a", "role": "member"}]
    ctx = _ctx(bot, config())
    _run(registry, ctx, "喵/禁言列表")
    assert "不返回禁言截止时间" in _patch_reply[-1]


def test_groupinfo_shows_counts(registry, config, _patch_reply):
    bot = FakeBot()
    bot.group_info = {"group_name": "测试群", "member_count": 12, "max_member_count": 200}
    bot.member_info[999999] = {"user_id": 999999, "role": "admin"}
    ctx = _ctx(bot, config())
    _run(registry, ctx, "喵/群信息")
    text = _patch_reply[-1]
    assert "测试群" in text and "12" in text and "管理员" in text


def test_recall_recent_only_touches_own_messages(registry, config, _patch_reply):
    bot = FakeBot()
    bot.history = [
        {"message_id": 1, "sender": {"user_id": 20002}},
        {"message_id": 2, "sender": {"user_id": 999999}},
        {"message_id": 3, "sender": {"user_id": 20002}},
        {"message_id": 4, "sender": {"user_id": 999999}},
    ]
    ctx = _ctx(bot, config())
    _run(registry, ctx, "喵/撤回最近 2")
    deleted = [kw["message_id"] for kw in bot.api_calls("delete_msg")]
    assert deleted == [2, 4]


def test_recall_recent_says_nothing_to_recall(registry, config, _patch_reply):
    bot = FakeBot()
    bot.history = [{"message_id": 1, "sender": {"user_id": 20002}}]
    ctx = _ctx(bot, config())
    _run(registry, ctx, "喵/撤回最近 3")
    assert "没得撤" in _patch_reply[-1]


# ---- 退群（危险操作） ----

def test_leave_requires_confirmation(registry, config, _patch_reply):
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵/退群")
    _run(registry, ctx, "喵/退群")
    assert bot.api_calls("set_group_leave") == []
    assert "确认" in _patch_reply[-1]


def test_leave_with_confirmation(registry, config, _patch_reply):
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵/退群 确认")
    _run(registry, ctx, "喵/退群 确认")
    assert bot.api_calls("set_group_leave")


# ---- 警告 ----

def test_warn_accumulates_and_auto_mutes(registry, config, _patch_reply):
    from dl_senpai.admin_commands_extra import WARN_LIMIT

    bot = FakeBot()
    cfg = config()
    for _ in range(WARN_LIMIT - 1):
        _run(registry, _ctx(bot, cfg), "喵/警告 20002 刷屏")
    assert bot.api_calls("set_group_ban") == []
    _run(registry, _ctx(bot, cfg), "喵/警告 20002 刷屏")
    bans = bot.api_calls("set_group_ban")
    assert len(bans) == 1
    assert bans[0]["user_id"] == 20002
    # 1 天 23 小时 59 分（2026-10-28 主人指定）
    assert bans[0]["duration"] == 23 * 3600 + 59 * 60 + 86400


def test_warn_list_and_clear(registry, config, _patch_reply):
    bot = FakeBot()
    cfg = config()
    _run(registry, _ctx(bot, cfg), "喵/警告 20002 骂人")
    _run(registry, _ctx(bot, cfg), "喵/警告列表")
    assert "20002" in _patch_reply[-1] and "1 次" in _patch_reply[-1]
    _run(registry, _ctx(bot, cfg), "喵/警告清零 20002")
    _run(registry, _ctx(bot, cfg), "喵/警告列表")
    assert "没有警告记录" in _patch_reply[-1]


# ---- 黑名单 ----

def test_blacklist_add_remove(registry, config, _patch_reply):
    from dl_senpai.admin_control import is_blacklisted

    bot = FakeBot()
    cfg = config()
    assert is_blacklisted(GROUP_ID, OTHER_ID, cfg=cfg) is False
    _run(registry, _ctx(bot, cfg), "喵/拉黑 20002 广告")
    assert is_blacklisted(GROUP_ID, OTHER_ID, cfg=cfg) is True
    _run(registry, _ctx(bot, cfg), "喵/黑名单")
    assert "20002" in _patch_reply[-1]
    _run(registry, _ctx(bot, cfg), "喵/解除拉黑 20002")
    assert is_blacklisted(GROUP_ID, OTHER_ID, cfg=cfg) is False


def test_blacklist_is_per_group(registry, config, _patch_reply):
    from dl_senpai.admin_control import is_blacklisted

    bot = FakeBot()
    cfg = config()
    _run(registry, _ctx(bot, cfg), "喵/拉黑 20002")
    assert is_blacklisted(GROUP_ID, 20002, cfg=cfg) is True
    assert is_blacklisted("900999", 20002, cfg=cfg) is False


# ---- 运维 ----

def test_memory_and_forget(registry, config, _patch_reply, tmp_path):
    from dl_senpai.handlers import _get_memory

    bot = FakeBot()
    cfg = config()
    memory = _get_memory(cfg)
    session = f"group:{GROUP_ID}"
    memory.save(session, [
        {"role": "user", "content": "今天好累"},
        {"role": "assistant", "content": "抱抱"},
        {"role": "user", "content": "明天考试"},
    ])
    _run(registry, _ctx(bot, cfg), "喵/记忆")
    assert "3 条" in _patch_reply[-1]

    _run(registry, _ctx(bot, cfg), "喵/忘记 考试")
    assert len(memory.load_all(session)) == 2

    _run(registry, _ctx(bot, cfg), "喵/忘记 全部")
    assert memory.load_all(session) == []


def test_perm_masks_numbers(registry, config, _patch_reply):
    bot = FakeBot()
    _run(registry, _ctx(bot, config()), "喵/权限")
    text = _patch_reply[-1]
    assert ADMIN_ID not in text
    assert "10****01" in text


def test_health_runs_without_crashing(registry, config, _patch_reply):
    bot = FakeBot()
    bot.member_info[999999] = {"user_id": 999999, "role": "admin"}
    _run(registry, _ctx(bot, config()), "喵/健康")
    text = _patch_reply[-1]
    assert "自检" in text and "数据目录" in text


def test_reload_reports_failure_clearly(registry, config, _patch_reply, monkeypatch):
    """重载失败也要说清，不能静默。"""
    from dl_senpai import config_watcher

    class Boom:
        def force_reload(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(config_watcher, "get_watcher", lambda: Boom())
    _run(registry, _ctx(FakeBot(), config()), "喵/重载配置")
    assert "重载失败" in _patch_reply[-1]


def test_log_command_is_friendly_without_files(registry, config, _patch_reply):
    _run(registry, _ctx(FakeBot(), config()), "喵/日志 5")
    assert _patch_reply[-1]


# ---- 鉴权：新指令同样只认管理员 ----

@pytest.mark.parametrize(
    "text",
    ["喵/拉黑 20002", "喵/退群 确认", "喵/重载配置", "喵/健康", "喵/忘记 全部"],
)
def test_non_admin_blocked(registry, config, _patch_reply, text):
    from dl_senpai.admin_commands import AdminDenied

    bot = FakeBot()
    ctx = _ctx(bot, config(), user_id="99999")
    inv = registry.match(text, ctx)
    assert inv is not None
    with pytest.raises(AdminDenied):
        asyncio.run(handle_admin_command(ctx, inv.match))
    # 一个接口都不该被调到
    assert bot.calls == []
