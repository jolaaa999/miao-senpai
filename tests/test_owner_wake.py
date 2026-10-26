"""主人必醒：指定的人说话，跳过冷却和骰子。

搬自 ai-companion-kit 的 `wake.js`：

    if (this.ownerIds.includes(uid) && c.mode !== 'active')
      return { wake: true, reason: 'owner' };

为什么需要：群里她刚回过话、正在冷却时，你再说一句会被静默晾着。
「明明在却不理我」比不回更伤。

一个刻意的边界：主人也**不**跳过长度过滤。
「嗯」「草」「/reset」这种依然不走 AI——主人不需要机器人对每个语气词
都回一句。这里只解决「被概率和冷却晾着」。
"""

from __future__ import annotations

import random

from dl_senpai.config import PluginConfig
from dl_senpai.trigger import InterruptCooldown, decide_trigger


def _cfg(owner_ids: str = "") -> PluginConfig:
    # 别名注入：PluginConfig 的字段走 alias，直接传字段名不生效
    return PluginConfig(DL_SENPAI_OWNER_IDS=owner_ids)


def test_owner_parsing_and_matching():
    cfg = _cfg("10001, 10002")
    assert cfg.owner_id_set() == {"10001", "10002"}
    assert cfg.is_owner(10001) is True, "int 形态也要能匹配"
    assert cfg.is_owner("10002") is True
    assert cfg.is_owner("99999") is False


def test_no_owner_configured_matches_nobody():
    cfg = _cfg("")
    assert cfg.owner_id_set() == set()
    assert cfg.is_owner(10001) is False


def test_owner_wakes_even_when_cooldown_active():
    """冷却中别人叫不动她，主人可以。"""
    cooldown = InterruptCooldown()
    cooldown.mark("group:1")  # 刚好说过话，进入冷却

    stranger = decide_trigger(
        is_mentioned=False,
        text="今天天气不错啊",
        session_id="group:1",
        interrupt_prob=0.0,  # 概率为 0：普通人必不醒
        cooldown_sec=600,
        min_msg_len=2,
        keyword_boost=0.0,
        cooldown=cooldown,
        is_owner=False,
        rng=random.Random(1),
    )
    assert stranger.kind == "none"

    owner = decide_trigger(
        is_mentioned=False,
        text="今天天气不错啊",
        session_id="group:1",
        interrupt_prob=0.0,
        cooldown_sec=600,
        min_msg_len=2,
        keyword_boost=0.0,
        cooldown=cooldown,
        is_owner=True,
        rng=random.Random(1),
    )
    assert owner.kind == "mention"
    assert owner.reason == "owner"


def test_owner_still_respects_length_filter():
    """主人说「嗯」也不该触发——省得她对每个语气词都回一句。"""
    cooldown = InterruptCooldown()
    decision = decide_trigger(
        is_mentioned=False,
        text="嗯",
        session_id="group:1",
        interrupt_prob=0.0,
        cooldown_sec=600,
        min_msg_len=8,
        keyword_boost=0.0,
        cooldown=cooldown,
        is_owner=True,
    )
    assert decision.kind == "none"
    assert decision.reason == "ignored"


def test_owner_passes_through_with_empty_probability():
    """即使插嘴概率被配成 0，主人依旧叫得动（不受概率影响）。"""
    decision = decide_trigger(
        is_mentioned=False,
        text="学姐你在吗，问你个事",
        session_id="group:2",
        interrupt_prob=0.0,
        cooldown_sec=0,
        min_msg_len=2,
        keyword_boost=0.0,
        cooldown=InterruptCooldown(),
        is_owner=True,
    )
    assert decision.kind == "mention"
