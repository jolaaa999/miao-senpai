"""插嘴总开关：DL_SENPAI_INTERRUPT_ENABLE=false 后群里不再主动搭话。

为什么不能靠 INTERRUPT_PROB=0 关掉：trigger.decide_trigger 会在概率上再叠加
keyword_boost（命中关键词 +0.05）和 image_interrupt_boost（带图 +0.06），
概率永远压不到 0，照样会插嘴。本开关在掷骰子之前就直接返回。

本文件同时钉住"关掉插嘴不能误伤其他触发"：@ / 私聊 / 召唤词都必须照常回。
"""

from __future__ import annotations

from dl_senpai.config import PluginConfig
from dl_senpai.core.agent_trigger import AgentTrigger, AgentTriggerPolicy


class _Ctx:
    """够用的 MessageContext 替身（decide 只读这几个字段）。"""

    def __init__(self, text: str, *, group: bool = True, to_me: bool = False) -> None:
        self.plain_text = text
        self.is_private = not group
        self.is_to_me = to_me
        self.group_id = "100" if group else ""
        self.user_id = "9"
        self.session_id = "group:100" if group else "private:9"


def _cfg(**kw):
    return PluginConfig(**kw)


def _decide(text: str, *, group: bool = True, to_me: bool = False, config=None):
    return AgentTriggerPolicy().decide(
        _Ctx(text, group=group, to_me=to_me),
        config=config if config is not None else _cfg(),
    )


def test_default_is_enabled() -> None:
    """默认必须是开的，否则等于悄悄改掉了原有行为。"""
    assert _cfg().interrupt_enable is True


def test_disabled_blocks_probability_path() -> None:
    """概率拉满也不插嘴。"""
    cfg = _cfg(DL_SENPAI_INTERRUPT_ENABLE=False, DL_SENPAI_INTERRUPT_PROB=1.0)
    d = _decide("今天天气不错我们聊聊吧", config=cfg)
    assert d.kind is AgentTrigger.NONE
    assert d.reason == "interrupt_disabled"


def test_disabled_beats_keyword_boost() -> None:
    """关键词加成不能再把概率顶起来——这正是 PROB=0 关不干净的原因。"""
    cfg = _cfg(
        DL_SENPAI_INTERRUPT_ENABLE=False,
        DL_SENPAI_INTERRUPT_PROB=0.0,
        DL_SENPAI_KEYWORD_BOOST=1.0,
    )
    d = _decide("帮我画个图吧我们聊聊", config=cfg)
    assert d.kind is AgentTrigger.NONE


def test_enabled_keeps_old_behavior() -> None:
    """开关打开时行为与改动前一致，保证可回滚。"""
    cfg = _cfg(DL_SENPAI_INTERRUPT_ENABLE=True, DL_SENPAI_INTERRUPT_PROB=1.0)
    assert _decide("今天天气不错我们聊聊吧", config=cfg).kind is AgentTrigger.INTERRUPT


def test_disabled_still_replies_when_at() -> None:
    """关掉插嘴只影响"未 @"的场景，@ 必须照常回。"""
    cfg = _cfg(DL_SENPAI_INTERRUPT_ENABLE=False)
    d = _decide("随便说点什么", to_me=True, config=cfg)
    assert d.kind is AgentTrigger.MENTION
    assert d.forced is True


def test_disabled_still_replies_in_private() -> None:
    cfg = _cfg(DL_SENPAI_INTERRUPT_ENABLE=False)
    d = _decide("你好呀", group=False, config=cfg)
    assert d.kind is AgentTrigger.PRIVATE
    assert d.forced is True


def test_disabled_still_replies_on_summon_keyword() -> None:
    """召唤词是明确的"来找我"，不能被插嘴开关连坐。"""
    cfg = _cfg(
        DL_SENPAI_INTERRUPT_ENABLE=False,
        DL_SENPAI_SUMMON_KEYWORDS="喵喵,喵学姐",
    )
    d = _decide("喵喵", config=cfg)
    assert d.kind is AgentTrigger.SUMMON
    assert d.forced is True
