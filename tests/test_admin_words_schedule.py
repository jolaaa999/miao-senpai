"""词库、定时消息、工具类指令的测试。

重点：
1. 词库只认整句（否则正常聊天里每个词都会被抢答）；
2. 词库命中要能真的接管消息，关掉后必须真的不接管；
3. 定时消息「到点发一次就好」——发完要从表里消失，不能重复发；
4. 计算器不是 eval（群友能碰到的东西不能有任意代码执行）。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from dl_senpai.admin_commands import handle_admin_command
from dl_senpai.admin_schedule import (
    TimerStore,
    fire_due,
    parse_timer_command,
)
from dl_senpai.admin_words import (
    WordStore,
    parse_word_command,
    word_guard,
    word_reply_for,
)
from dl_senpai.core.commands.admin import register_admin_commands
from dl_senpai.core.commands.registry import CommandRegistry

ADMIN_ID = "10001"
GROUP_ID = "900001"


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
            "DL_SENPAI_ADMIN_WORDS_PATH": str(tmp_path / "words.json"),
            "DL_SENPAI_ADMIN_SCHEDULE_PATH": str(tmp_path / "schedule.json"),
            "DL_SENPAI_ADMIN_SILENCE_PATH": str(tmp_path / "silence.json"),
            "DL_SENPAI_ADMIN_WARN_PATH": str(tmp_path / "warn.json"),
            "DL_SENPAI_ADMIN_BLACKLIST_PATH": str(tmp_path / "blacklist.json"),
        }
        env.update(overrides)
        return PluginConfig(**env)

    return _make


class FakeBot:
    self_id = "999999"

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_group_msg(self, **kwargs: Any) -> None:
        self.sent.append((int(kwargs.get("group_id") or 0), str(kwargs.get("message") or "")))

    async def send(self, event: Any, message: Any) -> None:
        self.sent.append((int(getattr(event, "group_id", 0) or 0), str(message)))


def _ctx(bot: Any, cfg: Any, *, text: str = "", user_id: str = ADMIN_ID):
    event = SimpleNamespace(
        group_id=int(GROUP_ID), user_id=int(user_id), message=[], reply=None,
        sender=SimpleNamespace(role="member"),
    )
    return SimpleNamespace(
        config=cfg, bot=bot, raw_event=event, user_id=user_id, group_id=GROUP_ID,
        self_id="999999", session_id=f"group:{GROUP_ID}", message_type="group",
        plain_text=text,
    )


def _run(reg: CommandRegistry, ctx: Any, text: str) -> str:
    inv = reg.match(text, ctx)
    assert inv is not None, text
    asyncio.run(handle_admin_command(ctx, inv.match))
    return inv.match.action


@pytest.fixture()
def registry() -> CommandRegistry:
    reg = CommandRegistry()
    register_admin_commands(reg, owner="admin_commands")
    return reg


# ---- 词库：匹配规则 ----

def test_word_store_exact_match_only(tmp_path):
    """整句匹配：含有关键词但整句不等，不算命中。"""
    store = WordStore(tmp_path / "w.json")
    store.add(GROUP_ID, "你好", "你好呀")
    assert store.lookup(GROUP_ID, "你好") == "你好呀"
    assert store.lookup(GROUP_ID, "你好啊今天天气不错") == ""


def test_word_store_prefix_wildcard(tmp_path):
    """触发词带 * 才是前缀包含，这是管理员显式选择放宽。"""
    store = WordStore(tmp_path / "w.json")
    store.add(GROUP_ID, "报时*", "我不报时，你自己看表")
    assert store.lookup(GROUP_ID, "报时一下") == "我不报时，你自己看表"
    assert store.lookup(GROUP_ID, "现在几点报时") == ""


def test_word_store_enabled_switch(tmp_path):
    store = WordStore(tmp_path / "w.json")
    store.add(GROUP_ID, "你好", "你好呀")
    store.set_enabled(GROUP_ID, False)
    assert store.enabled(GROUP_ID) is False


def test_word_guard_uses_config_path(config):
    cfg = config()
    bot = FakeBot()
    ctx = _ctx(bot, cfg, text="你好")
    assert word_guard(ctx) is False
    _run(registry_of(), ctx, "喵/词库 加 你好=你好呀")
    assert word_guard(ctx) is True
    assert word_reply_for(ctx) == "你好呀"


def registry_of() -> CommandRegistry:
    reg = CommandRegistry()
    register_admin_commands(reg, owner="admin_commands")
    return reg


def test_word_command_parsing():
    assert parse_word_command("加 你好=你好呀") == ("add", "你好", "你好呀")
    assert parse_word_command("你好=你好呀") == ("add", "你好", "你好呀")
    assert parse_word_command("删 你好") == ("del", "你好", "")
    assert parse_word_command("关") == ("off", "", "")
    assert parse_word_command("") == ("list", "", "")


def test_word_command_lifecycle(config, _patch_reply):
    cfg = config()
    bot = FakeBot()
    reg = registry_of()
    ctx = _ctx(bot, cfg)
    _run(reg, ctx, "喵/词库 加 你好=你好呀")
    assert "记下了" in _patch_reply[-1]
    _run(reg, ctx, "喵/词库")
    assert "你好" in _patch_reply[-1]
    _run(reg, ctx, "喵/词库 关")
    assert "关掉" in _patch_reply[-1]
    assert word_guard(ctx) is False
    _run(reg, ctx, "喵/词库 删 你好")
    assert "删掉了" in _patch_reply[-1]


def test_word_limits(config):
    cfg = config()
    from dl_senpai.admin_words import get_word_store

    store = get_word_store(cfg.admin_words_path)
    ok, _ = store.add(GROUP_ID, "x" * 100, "reply")
    assert ok is False
    ok, _ = store.add(GROUP_ID, "ok", "")
    assert ok is False


def test_blacklisted_user_gets_no_word_reply(config):
    """拉黑之后连词库也不能回——词库回话同样是「她的回应」。"""
    from dl_senpai.admin_control import get_blacklist_store

    cfg = config()
    bot = FakeBot()
    reg = registry_of()
    # 先用管理员身份挂词库，再用普通群友身份验证守卫
    _run(reg, _ctx(bot, cfg), "喵/词库 加 你好=你好呀")
    ctx = _ctx(bot, cfg, user_id="20002", text="你好")
    assert word_guard(ctx) is True
    get_blacklist_store(cfg.admin_blacklist_path).add(GROUP_ID, "20002", reason="广告")
    assert word_guard(ctx) is False


# ---- 定时消息 ----

def test_parse_timer_command():
    assert parse_timer_command("加 30m 记得交作业") == ("add", 1800, "记得交作业")
    assert parse_timer_command("30m记得交作业") == ("add", 1800, "记得交作业")
    assert parse_timer_command("列表") == ("list", 0, "")
    assert parse_timer_command("删 123456") == ("del", 123456, "")
    assert parse_timer_command("清空") == ("clear", 0, "")


def test_timer_add_and_fire_once(tmp_path):
    store = TimerStore(tmp_path / "s.json")
    task_id, why = store.add(GROUP_ID, 60, "记得交作业")
    assert why == ""
    assert task_id > 0
    assert len(store.list_group(GROUP_ID)) == 1
    # 现在还没到点
    assert store.due() == []
    # 跳到一分钟后：发一次
    bot = FakeBot()
    sent = asyncio.run(fire_due(bot, store=store, now=time.time() + 120))
    assert sent == 1
    assert bot.sent == [(int(GROUP_ID), "记得交作业")]
    # 再跑一次不能重复发
    assert asyncio.run(fire_due(bot, store=store, now=time.time() + 200)) == 0


def test_timer_rejects_bad_input(tmp_path):
    store = TimerStore(tmp_path / "s.json")
    assert store.add(GROUP_ID, 0, "x")[1]
    assert store.add(GROUP_ID, 60, "")[1]
    assert store.add(GROUP_ID, 40 * 86400, "x")[1]


def test_timer_command_lifecycle(config, _patch_reply):
    cfg = config()
    bot = FakeBot()
    reg = registry_of()
    ctx = _ctx(bot, cfg)
    _run(reg, ctx, "喵/定时 加 30m 记得交作业")
    assert "排好了" in _patch_reply[-1]
    _run(reg, ctx, "喵/定时")
    assert "记得交作业" in _patch_reply[-1]
    from dl_senpai.admin_schedule import get_timer_store

    store = get_timer_store(cfg.admin_schedule_path)
    task_id = int(store.list_group(GROUP_ID)[0]["id"])
    _run(reg, ctx, f"喵/定时 删 {task_id}")
    assert "删掉了" in _patch_reply[-1]
    assert store.list_group(GROUP_ID) == []


# ---- 工具类指令 ----

def test_dice_format(config, _patch_reply):
    cfg = config()
    reg = registry_of()
    ctx = _ctx(FakeBot(), cfg)
    _run(reg, ctx, "喵/骰子 2d6")
    text = _patch_reply[-1]
    assert text.startswith("🎲")
    assert "=" in text


def test_dice_rejects_absurd_input(config, _patch_reply):
    cfg = config()
    reg = registry_of()
    ctx = _ctx(FakeBot(), cfg)
    _run(reg, ctx, "喵/骰子 999d6")
    assert "1-20" in _patch_reply[-1]


def test_lots_returns_one_result(config, _patch_reply):
    cfg = config()
    reg = registry_of()
    ctx = _ctx(FakeBot(), cfg)
    _run(reg, ctx, "喵/抽签 明天的面试")
    assert "明天的面试" in _patch_reply[-1]


def test_calc_is_deterministic(config, _patch_reply):
    cfg = config()
    reg = registry_of()
    ctx = _ctx(FakeBot(), cfg)
    _run(reg, ctx, "喵/计算 (1+2)*3")
    assert _patch_reply[-1].endswith("= 9")


def test_calc_blocks_code_execution(config, _patch_reply):
    """群友能碰到的入口不能有任意代码执行。"""
    cfg = config()
    reg = registry_of()
    ctx = _ctx(FakeBot(), cfg)
    _run(reg, ctx, '喵/计算 __import__("os").system("echo hi")')
    assert "算不了" in _patch_reply[-1]


def test_calc_handles_division_by_zero(config, _patch_reply):
    cfg = config()
    reg = registry_of()
    ctx = _ctx(FakeBot(), cfg)
    _run(reg, ctx, "喵/计算 1/0")
    assert "除数" in _patch_reply[-1]


# ---- 新指令同样只认管理员 ----

@pytest.mark.parametrize("text", ["喵/词库", "喵/定时", "喵/骰子", "喵/计算 1+1"])
def test_non_admin_blocked(config, _patch_reply, text):
    from dl_senpai.admin_commands import AdminDenied

    cfg = config()
    reg = registry_of()
    ctx = _ctx(FakeBot(), cfg, user_id="99999")
    inv = reg.match(text, ctx)
    assert inv is not None
    with pytest.raises(AdminDenied):
        asyncio.run(handle_admin_command(ctx, inv.match))
