"""在场状态机单测：等下文 / 先听一会儿 / 潜水 / 追问上限。

这些规则全是「时间 + 阈值」，所以一律注入合成时间戳，一个 sleep 都不用。
"""

from __future__ import annotations

from types import SimpleNamespace

from dl_senpai.presence import (
    PresenceGate,
    build_presence_hint,
    looks_unfinished,
)


class _FixedRng:
    """固定返回同一个值的随机源：让潜水时长可预期。"""

    def __init__(self, value: int) -> None:
        self.value = value

    def randint(self, lo: int, hi: int) -> int:
        return self.value


def _cfg(**kw: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "presence_enable": True,
        "pending_wait_enable": True,
        "pending_wait_ms": 45000,
        "pending_wait_max_ms": 120000,
        "presence_observe_ms": 90000,
        "presence_dive_enable": True,
        "presence_dive_min_ms": 180000,
        "presence_dive_max_ms": 600000,
        "presence_pre_sleep_enable": True,
        "presence_pre_sleep_ms": 300000,
        "presence_topic_poke_max": 2,
        "presence_topic_window_ms": 600000,
        "presence_hint_enable": True,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _gate(**kw: object) -> PresenceGate:
    return PresenceGate(cfg_getter=lambda: _cfg(**kw), rng=_FixedRng(180000))


# -- 等下文 --

def test_unfinished_by_trailing_cue() -> None:
    assert looks_unfinished("我跟你讲") is True
    assert looks_unfinished("这个我知道，但是") is True
    assert looks_unfinished("然后") is True


def test_unfinished_by_trailing_comma() -> None:
    assert looks_unfinished("那个 PPT，") is True
    assert looks_unfinished("就是上次说的、") is True


def test_finished_sentences() -> None:
    assert looks_unfinished("那个 PPT 你有吗？") is False
    assert looks_unfinished("行。") is False
    assert looks_unfinished("") is False


def test_complete_short_not_unfinished() -> None:
    # 「好的，」看着虚，其实说完了；判成没说完会白等几十秒
    assert looks_unfinished("好的") is False
    assert looks_unfinished("好的，") is False
    assert looks_unfinished("算了") is False


def test_wait_window_extended_only_when_unfinished() -> None:
    g = _gate()
    assert g.wait_window_ms("那个 PPT 你有吗？", 2500) == 2500
    assert g.wait_window_ms("我跟你讲", 2500) == 45000


def test_wait_window_respects_switch_and_cap() -> None:
    assert _gate(pending_wait_enable=False).wait_window_ms("我跟你讲", 2500) == 2500
    # 基础窗口比等下文更长时，不缩回去
    assert _gate().wait_window_ms("我跟你讲", 60000) == 60000
    # 上限兜住异常配置
    assert _gate(pending_wait_ms=999999, pending_wait_max_ms=120000).wait_window_ms(
        "我跟你讲", 2500
    ) == 120000


# -- 潜水 --

def test_no_dive_without_having_spoken() -> None:
    g = _gate()
    g.note_incoming("g1", now=0.0)
    assert g.is_diving("g1", now=10_000.0) is False


def test_dive_after_quiet_observation() -> None:
    g = _gate()
    g.note_replied("g1", now=0.0)
    # 观察期内（90 秒）不潜水：刚说完话就消失最假
    assert g.is_diving("g1", now=60.0) is False
    assert g.is_diving("g1", now=100.0) is True
    assert g.dive_remaining_ms("g1", now=100.0) == 180000


def test_ordinary_message_does_not_wake_from_dive() -> None:
    g = _gate()
    g.note_replied("g1", now=0.0)
    assert g.is_diving("g1", now=100.0) is True
    g.note_incoming("g1", now=120.0)
    # 潜水里普通消息不叫醒她，否则「潜水」等于没实现
    assert g.is_diving("g1", now=130.0) is True


def test_addressed_message_wakes_from_dive() -> None:
    g = _gate()
    g.note_replied("g1", now=0.0)
    assert g.is_diving("g1", now=100.0) is True
    g.note_incoming("g1", addressed=True, now=120.0)
    assert g.is_diving("g1", now=130.0) is False
    assert g.dive_remaining_ms("g1", now=130.0) == 0


def test_dive_expires() -> None:
    g = _gate()
    g.note_replied("g1", now=0.0)
    assert g.is_diving("g1", now=100.0) is True
    # 180 秒潜水到期后回到「在场但没在潜水」（还要再安静一轮才会重新潜）
    assert g.is_diving("g1", now=281.0) is True  # 到期即刻重新进入下一段
    assert g.dive_remaining_ms("g1", now=281.0) > 0


def test_dive_disabled_by_config() -> None:
    g = _gate(presence_dive_enable=False)
    g.note_replied("g1", now=0.0)
    assert g.is_diving("g1", now=1000.0) is False


# -- 话题追问上限 --

def test_topic_pokes_exhausted() -> None:
    g = _gate()
    assert g.topic_pokes_exhausted("g1", now=0.0) is False
    g.mark_topic_poke("g1", now=1.0)
    assert g.topic_pokes_exhausted("g1", now=2.0) is False
    g.mark_topic_poke("g1", now=3.0)
    assert g.topic_pokes_exhausted("g1", now=4.0) is True


def test_topic_pokes_reset_when_addressed() -> None:
    g = _gate()
    g.mark_topic_poke("g1", now=1.0)
    g.mark_topic_poke("g1", now=2.0)
    assert g.topic_pokes_exhausted("g1", now=3.0) is True
    g.note_incoming("g1", addressed=True, now=4.0)
    assert g.topic_pokes_exhausted("g1", now=5.0) is False


def test_topic_pokes_reset_after_window() -> None:
    g = _gate()
    g.mark_topic_poke("g1", now=1.0)
    g.mark_topic_poke("g1", now=2.0)
    # 话题窗口（600 秒）过去后算新话题
    assert g.topic_pokes_exhausted("g1", now=700.0) is False


# -- 沉睡前观察 --

def test_pre_sleep_blocks_right_after_activity() -> None:
    g = _gate()
    g.note_incoming("g1", now=100.0)
    assert g.pre_sleep_remaining_ms(now=110.0) == 290000
    assert g.pre_sleep_remaining_ms(now=400.0) == 0


def test_pre_sleep_free_without_any_activity() -> None:
    assert _gate().pre_sleep_remaining_ms(now=10.0) == 0


def test_pre_sleep_can_be_disabled() -> None:
    g = _gate(presence_pre_sleep_enable=False)
    g.note_incoming("g1", now=100.0)
    assert g.pre_sleep_remaining_ms(now=101.0) == 0


# -- 提示词与配置 --

def test_presence_hint_toggle() -> None:
    assert "在场与节奏" in build_presence_hint(_cfg())
    assert build_presence_hint(_cfg(presence_hint_enable=False)) == ""


def test_config_defaults_exist() -> None:
    from dl_senpai.config import PluginConfig

    fields = PluginConfig.model_fields
    assert fields["presence_enable"].default is True
    assert fields["pending_wait_ms"].default == 45000
    assert fields["presence_observe_ms"].default == 90000
    assert fields["presence_pre_sleep_ms"].default == 300000
    assert fields["presence_topic_poke_max"].default == 2
    assert fields["bubble_lint_enable"].default is True
    assert fields["cjk_space_fix_enable"].default is True
