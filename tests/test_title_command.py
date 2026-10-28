"""喵title（专属头衔）的测试。

背景（2026-10-28 实机踩到）：QQ 的群专属头衔**只有群主能设**，
学姐是管理员时请求会被协议层直接拒（ActionFailed: OIDB error 1013），
而且被拒一次会写 6 小时冷却。结果是管理员反复发「喵title」都只收到
一句「头衔没设上，可能是协议端不支持」——看起来像指令坏了。

所以这里盯住三件事：
1. 身份不够时**不发请求**，直接把「得把我设成群主」说清楚；
2. 身份够（群主）时照常发，并验证回显；
3. 查不到身份时不能因为查不到就拦住——那会把一个可能成功的事变成一定失败。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from dl_senpai.admin_commands import handle_admin_command
from dl_senpai.core.commands.admin import register_admin_commands
from dl_senpai.core.commands.registry import CommandRegistry
from dl_senpai.group_card import _reset_title_refusal_cache

ADMIN_ID = "10001"
GROUP_ID = "980229149"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """每个用例都从「没有冷却」开始，否则一个用例会污染下一个。"""
    _reset_title_refusal_cache()
    sent: list[str] = []

    async def fake_reply(bot, event, text, **kwargs):
        sent.append(text)

    import dl_senpai.handlers as handlers

    monkeypatch.setattr(handlers, "_reply", fake_reply)
    yield sent
    _reset_title_refusal_cache()


@pytest.fixture()
def config(tmp_path):
    from dl_senpai.config import PluginConfig

    return PluginConfig(
        DL_SENPAI_ADMIN_QQ=ADMIN_ID,
        DL_SENPAI_ADMIN_SILENCE_PATH=str(tmp_path / "silence.json"),
        DL_SENPAI_ADMIN_WARN_PATH=str(tmp_path / "warn.json"),
    )


class TitleBot:
    """假 bot：能扮演「她是群主 / 管理员」，并记录真的发了什么请求。"""

    self_id = "999999"

    def __init__(self, *, role: str = "admin", info_raises: bool = False) -> None:
        self.role = role
        self.info_raises = info_raises
        self.title = ""
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_group_member_info(self, **kwargs: Any) -> dict[str, Any]:
        if self.info_raises:
            raise RuntimeError("协议端不给查")
        uid = str(kwargs.get("user_id") or "")
        if uid == self.self_id:
            return {"user_id": int(uid), "role": self.role, "title": self.title}
        return {"user_id": int(uid), "role": "member", "title": self.title}

    async def call_api(self, api: str, **kwargs: Any) -> None:
        self.calls.append((api, kwargs))
        if api == "set_group_special_title":
            self.title = str(kwargs.get("special_title") or "")

    def api_calls(self, api: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if name == api]


@pytest.fixture()
def registry() -> CommandRegistry:
    reg = CommandRegistry()
    register_admin_commands(reg, owner="admin_commands")
    return reg


def _ctx(bot: Any, cfg: Any, text: str = "喵title @某人 纯瘾大"):
    event = SimpleNamespace(
        group_id=int(GROUP_ID), user_id=int(ADMIN_ID), message_id="1",
        message=[SimpleNamespace(type="at", data={"qq": "20002"}),
                 SimpleNamespace(type="text", data={"text": text})],
        reply=None, sender=SimpleNamespace(role="member"),
    )
    return SimpleNamespace(
        config=cfg, bot=bot, raw_event=event, user_id=ADMIN_ID, group_id=GROUP_ID,
        self_id="999999", session_id=f"group:{GROUP_ID}", message_type="group",
        plain_text=text,
    )


def _run(reg: CommandRegistry, ctx: Any, text: str) -> None:
    invocation = reg.match(text, ctx)
    assert invocation is not None, f"没有指令匹配：{text}"
    asyncio.run(invocation.command.handler(ctx, invocation.match))


def test_refuses_without_calling_when_not_owner(registry, config, _clean_state):
    """她是管理员时：一个请求都不发，直接说清楚要群主。

    这就是实机上「喵title 好像有问题」的真相——请求必然被拒，
    发出去只是给腾讯刷一条 OIDB 错误、再锁 6 小时。
    """
    bot = TitleBot(role="admin")
    _run(registry, _ctx(bot, config), "喵title @某人 纯瘾大")

    assert bot.api_calls("set_group_special_title") == [], "不该发请求"
    joined = "\n".join(_clean_state)
    assert "群主" in joined and "管理员" in joined


def test_hint_mentions_card_as_alternative(registry, config, _clean_state):
    """用户真正的诉求可能是「换个显示名」，这条要给出替代方案，不然只能干瞪眼。"""
    _run(registry, _ctx(TitleBot(role="admin"), config), "喵title @某人 纯瘾大")
    assert "喵card" in "\n".join(_clean_state)


def test_sets_title_when_bot_is_owner(registry, config, _clean_state):
    """群主身份下正常设置，且要读到回显才算成功。"""
    bot = TitleBot(role="owner")
    _run(registry, _ctx(bot, config), "喵title @某人 纯瘾大")

    calls = bot.api_calls("set_group_special_title")
    assert calls, "群主身份该真的发请求"
    assert calls[0]["special_title"] == "纯瘾大"
    assert "头衔设成「纯瘾大」了" in "\n".join(_clean_state)


def test_cooldown_says_so_instead_of_generic_failure(registry, config, _clean_state):
    """6 小时冷却期间的提示要明确，否则管理员会以为指令本身坏了。"""
    import dl_senpai.group_card as gc

    gc._title_refusal_until[int(GROUP_ID)] = 9e18  # 直接丢进冷却

    bot = TitleBot(role="owner")
    _run(registry, _ctx(bot, config), "喵title @某人 纯瘾大")

    assert bot.api_calls("set_group_special_title") == [], "冷却期不该再发"
    assert "6 小时" in "\n".join(_clean_state)


def test_unknown_role_still_tries(registry, config, _clean_state):
    """身份查不到时不能拦住：查不到 ≠ 没权限，拦了就把可能成功的事变成必然失败。"""
    bot = TitleBot(role="owner", info_raises=True)
    _run(registry, _ctx(bot, config), "喵title @某人 纯瘾大")
    assert bot.api_calls("set_group_special_title"), "查不到身份也要发一次试试"
