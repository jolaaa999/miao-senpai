"""群友指令（不带「喵」前缀）的测试。

这批指令的风险全在**误吞**上：一条正则写宽了，正常聊天就被短路掉，
用户永远等不到回复，而且他不知道发生了什么。所以这里的负例比正例更重要。

另外盯住一条结构规则：这些指令**不认喵前缀**——
「喵签到」不是签到，被喵前缀命中的消息只走管理指令。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from dl_senpai.core.commands.builtin import build_builtin_commands
from dl_senpai.core.commands.registry import CommandRegistry
from dl_senpai.member_commands import MEMBER_PATTERNS, handle_member_command

GROUP_ID = "900001"
MEMBER_ID = "20002"


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
    for cmd in build_builtin_commands():
        reg.register(cmd)
    return reg


def _match(reg: CommandRegistry, text: str):
    return reg.match(text, None)


# ---- 1. 触发词 ----


@pytest.mark.parametrize(
    "text,action,arg",
    [
        ("抽签", "lots", ""),
        ("运势", "lots", ""),
        ("抽签 今天面试", "lots", "今天面试"),
        ("骰子", "dice", ""),
        ("骰子 2d6", "dice", "2d6"),
        ("roll 3d10", "dice", "3d10"),
        ("计算 1+1", "calc", "1+1"),
        ("计算 (17.6*23.4+8)/3", "calc", "(17.6*23.4+8)/3"),
        ("算一下 2^10", "calc", "2^10"),
        ("我的信息", "me", ""),
        ("我是谁", "me", ""),
        ("群信息", "groupinfo", ""),
        ("群公告", "notices", ""),
        ("群文件", "files", ""),
        ("禁言名单", "mutelist", ""),
        ("管理名单", "admins", ""),
        ("我的警告", "mywarn", ""),
        ("改名片 小花", "card", "小花"),
    ],
)
def test_trigger_words_match_expected_action(registry, text, action, arg):
    invocation = _match(registry, text)
    assert invocation is not None, f"「{text}」没匹配到任何指令"
    assert invocation.command.name == "member"
    assert invocation.match.action == action
    assert invocation.match.arg(0) == arg


# ---- 2. 负例：正常聊天不能被吞 ----


@pytest.mark.parametrize(
    "text",
    [
        "今天天气不错",
        "算一下他会来吗",          # 「算一下」+ 非算式 → 必须放行
        "群公告里写了什么",         # 「群公告」+ 后缀 → 必须放行
        "我的信息好多啊",           # 不是整句「我的信息」
        "运势真好",                 # 日常说话，不是求签
        "改名片",                   # 只有指令词、没内容 → 不吃
        "骰子好好玩",
        "我的警告是不是有点多",
    ],
)
def test_normal_chat_is_not_swallowed(registry, text):
    """这些必须落到别处（AI 闲聊），不能被群友指令截走。"""
    invocation = _match(registry, text)
    assert invocation is None or invocation.command.name != "member", (
        f"「{text}」被群友指令误吞了"
    )


# ---- 3. 结构规则：不认喵前缀 ----


def test_mao_prefixed_text_is_not_a_member_command(registry):
    """「喵群信息」不是群友指令：带喵的走管理指令通道（这里是 admin_groupinfo）。"""
    from dl_senpai.core.commands.admin import register_admin_commands

    register_admin_commands(registry, owner="admin_commands")
    invocation = _match(registry, "喵群信息")
    assert invocation is not None
    assert invocation.command.name != "member"


# ---- 4. 分派表不能和正则脱节 ----


def test_every_action_has_a_handler():
    """正则里的每个 action 都要能分派到 handler，否则群友收到「还没接上」。"""
    from dl_senpai.member_commands import _HANDLERS

    declared = {action for action, _pattern in MEMBER_PATTERNS}
    assert declared == set(_HANDLERS), declared ^ set(_HANDLERS)


def test_unknown_action_answers_instead_of_silence():
    """真的脱节了也要说话——管理/指令最糟的失败方式是「没反应」。"""
    sent: list[str] = []

    async def fake_reply(ctx, text):
        sent.append(text)

    import dl_senpai.admin_commands as ac

    original = ac._reply
    ac._reply = fake_reply
    try:
        asyncio.run(handle_member_command(None, SimpleNamespace(action="nope", arg=lambda i=0, d="": d)))
    finally:
        ac._reply = original
    assert sent and "没接上" in sent[0]


# ---- 5. 两个有真实行为的指令 ----


class FakeBot:
    self_id = "999999"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def set_group_card(self, **kwargs: Any) -> None:
        self.calls.append(("set_group_card", kwargs))


def _ctx(bot: Any, cfg: Any, *, text: str, user_id: str = MEMBER_ID):
    event = SimpleNamespace(
        group_id=int(GROUP_ID), user_id=int(user_id), message_id="1",
        message=[], reply=None, sender=SimpleNamespace(role="member"),
    )
    return SimpleNamespace(
        config=cfg, bot=bot, raw_event=event, user_id=user_id, group_id=GROUP_ID,
        self_id="999999", session_id=f"group:{GROUP_ID}", message_type="group",
        plain_text=text,
    )


@pytest.fixture()
def config(tmp_path):
    from dl_senpai.config import PluginConfig

    return PluginConfig(
        DL_SENPAI_ADMIN_QQ="10001",
        DL_SENPAI_ADMIN_WARN_PATH=str(tmp_path / "warn.json"),
    )


def _run(reg: CommandRegistry, ctx: Any, text: str) -> None:
    invocation = reg.match(text, ctx)
    assert invocation is not None, f"没有指令匹配：{text}"
    asyncio.run(invocation.command.handler(ctx, invocation.match))


def test_card_changes_own_name(registry, config, _patch_reply):
    """群友改自己的名片：只能改自己，uid 必须来自 ctx 而不是别人@他。"""
    bot = FakeBot()
    _run(registry, _ctx(bot, config, text="改名片 小花"), "改名片 小花")
    calls = [kw for name, kw in bot.calls if name == "set_group_card"]
    assert calls and calls[0]["user_id"] == int(MEMBER_ID)
    assert calls[0]["card"] == "小花"
    assert "小花" in _patch_reply[-1]


def test_mywarn_shows_own_record(registry, config, _patch_reply):
    """自己的警告账本要能自己看——不给看的话，被禁言时就只剩吵架。"""
    from dl_senpai.admin_commands_extra import _warn_store

    cfg = config
    _warn_store(cfg).add(GROUP_ID, MEMBER_ID, reason="违禁词：傻逼", by="999999")
    _warn_store(cfg).add(GROUP_ID, MEMBER_ID, reason="刷屏", by="999999")

    _run(registry, _ctx(FakeBot(), cfg, text="我的警告"), "我的警告")
    joined = "\n".join(_patch_reply)
    assert "2 次警告" in joined
    assert "刷屏" in joined or "违禁词" in joined


def test_mywarn_says_clean_when_no_record(registry, config, _patch_reply):
    _run(registry, _ctx(FakeBot(), config, text="我的警告"), "我的警告")
    assert "没有警告记录" in _patch_reply[-1]
