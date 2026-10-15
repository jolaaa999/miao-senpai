"""召唤词触发：群聊 / 私聊 / 临时会话里出现「喵喵」「喵学姐」时强制回复。

背景：原来的触发只有三种——私聊必回、@ 必回、未 @ 按概率插嘴。
`trigger.has_dl_keyword` 只是把插嘴概率 +0.05，**不保证回复**，所以
「喵喵」在群里大概率召唤不出学姐。本文件覆盖新增的 SUMMON 触发。

两条最容易写错、因此单独钉住的约束：
  1. 召唤词必须**绕过** `should_ignore_text` ——「喵喵」只有 2 字，
     短于 min_msg_len=8，否则会被当噪音丢掉；
  2. 召唤词必须排在**私聊分支之前**——否则私聊被 `enable_private` 关掉时
     召唤词会一起失效。
"""

from __future__ import annotations

from dl_senpai.config import PluginConfig
from dl_senpai.core.agent_trigger import AgentTrigger, AgentTriggerPolicy, has_summon_keyword


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
    base = {"DL_SENPAI_SUMMON_KEYWORDS": "喵喵,喵学姐"}
    base.update(kw)
    return PluginConfig(**base)


def _decide(text: str, *, group: bool = True, to_me: bool = False, config=None):
    return AgentTriggerPolicy().decide(
        _Ctx(text, group=group, to_me=to_me),
        config=config if config is not None else _cfg(),
    )


# ================================================================
# 1. 配置解析
# ================================================================


def test_parses_english_comma() -> None:
    assert _cfg().summon_keyword_list() == ["喵喵", "喵学姐"]


def test_parses_chinese_comma_too() -> None:
    """中文逗号必须认：`.env` 是手写的，中文输入法下极容易打出「，」。"""
    cfg = _cfg(DL_SENPAI_SUMMON_KEYWORDS="喵喵，喵学姐")
    assert cfg.summon_keyword_list() == ["喵喵", "喵学姐"]


def test_empty_and_whitespace_are_dropped() -> None:
    cfg = _cfg(DL_SENPAI_SUMMON_KEYWORDS=" 喵喵 , ,, 喵学姐 ")
    assert cfg.summon_keyword_list() == ["喵喵", "喵学姐"]


def test_empty_value_disables_feature() -> None:
    assert _cfg(DL_SENPAI_SUMMON_KEYWORDS="").summon_keyword_list() == []


# ================================================================
# 2. 匹配函数
# ================================================================


def test_matches_as_substring() -> None:
    assert has_summon_keyword("喵喵在吗", ["喵喵"]) is True
    assert has_summon_keyword("这道题怎么做喵学姐", ["喵学姐"]) is True


def test_ignores_empty_keywords() -> None:
    """空串不该"匹配一切"——否则留空配置会变成每条都回。"""
    assert has_summon_keyword("随便一句话", ["", "  "]) is False


# ================================================================
# 3. 触发判定
# ================================================================


def test_group_summon_forces_reply() -> None:
    d = _decide("喵喵", group=True, to_me=False)
    assert d.kind is AgentTrigger.SUMMON
    assert d.forced is True, "召唤词必须是强制回复，不能只提升插嘴概率"
    assert d.should_run is True


def test_summon_bypasses_min_msg_len() -> None:
    """「喵喵」2 字 < min_msg_len=8，必须绕过长度过滤。

    对照组：同样是 2 字但没召唤词的消息会被当噪音丢掉。
    """
    assert _cfg().min_msg_len == 8
    assert _decide("喵喵").kind is AgentTrigger.SUMMON
    assert _decide("在吗").kind is AgentTrigger.NONE
    assert _decide("在吗").reason == "ignored"


def test_summon_works_in_private() -> None:
    d = _decide("喵学姐", group=False)
    assert d.kind is AgentTrigger.SUMMON
    assert d.forced is True


def test_summon_works_even_when_private_disabled() -> None:
    """私聊被关闭时召唤词仍然有效——这是召唤词排在私聊分支之前的原因。"""
    cfg = _cfg(DL_SENPAI_ENABLE_PRIVATE=False)
    assert _decide("普通私聊", group=False, config=cfg).kind is AgentTrigger.NONE
    assert _decide("喵喵", group=False, config=cfg).kind is AgentTrigger.SUMMON


def test_without_summon_group_still_uses_probability() -> None:
    """没有召唤词的群消息仍然走原来的概率插嘴，行为不变。"""
    always = _cfg(DL_SENPAI_INTERRUPT_PROB=1.0)
    never = _cfg(DL_SENPAI_INTERRUPT_PROB=0.0)
    assert _decide("今天天气不错我们聊聊吧", config=always).kind is AgentTrigger.INTERRUPT
    assert _decide("今天天气不错我们聊聊吧", config=never).kind is AgentTrigger.NONE


def test_empty_keywords_restore_old_behavior() -> None:
    """留空 = 关闭召唤，退回原来的行为（可回滚）。"""
    cfg = _cfg(DL_SENPAI_SUMMON_KEYWORDS="", DL_SENPAI_INTERRUPT_PROB=0.0)
    assert _decide("喵喵", config=cfg).kind is AgentTrigger.NONE


def test_at_bot_still_forces_reply() -> None:
    """@ 学姐的行为完全不受影响；@ 且含召唤词时按召唤词记（同为强制回复）。"""
    d = _decide("随便说点什么", to_me=True)
    assert d.kind is AgentTrigger.MENTION
    assert d.forced is True
    assert _decide("喵喵", to_me=True).forced is True