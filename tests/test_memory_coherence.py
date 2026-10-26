"""记忆一致性：三条抱怨对应的机制守卫（2026-10-25）。

线上评价原话：
    「就是感觉好奇怪，说不上来，感觉在一本正经地重复废话」
    「而且怎么不读上面的聊天记录了」
    「但是之前的聊天记录又记得，又很混乱」

对应的三个机制问题，这里各钉一条测试：
    1. 她自己的旧回复（判词腔 / 自我点评）被当范文回灌
    2. 当天流水与近期历史重叠，同一段话进提示词两遍
    3. 跨场合流水把别处的对话（含她自己的原话）塞进来
"""

from __future__ import annotations

from dl_senpai.config import PluginConfig
from dl_senpai.cross_chat import build_cross_brief, get_cross_store
from dl_senpai.memory_recall import (
    limit_assistant_turns,
    messages_for_llm_with_recall,
    select_recent_and_recall,
)


def test_limit_assistant_turns_cuts_from_head():
    """只留最近 N 条她的回复，并且切完必须以 user 开头。"""
    msgs: list[dict[str, str]] = []
    for i in range(10):
        msgs.append({"role": "user", "content": f"问{i}"})
        msgs.append({"role": "assistant", "content": f"答{i}"})
    out = limit_assistant_turns(msgs, 3)
    assert sum(1 for m in out if m["role"] == "assistant") == 3
    assert out[0]["role"] == "user"
    assert out[-1]["content"] == "答9"


def test_limit_assistant_turns_zero_is_noop():
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert limit_assistant_turns(msgs, 0) == msgs


def test_recall_quotes_only_the_other_side():
    """召回的旧聊天只引对方的话：她自己那些旧回复是旧人设产物，回灌只会更乱。"""
    history: list[dict[str, str]] = []
    for i in range(30):
        history.append({"role": "user", "content": f"闲聊{i} 天气不错"})
        history.append({"role": "assistant", "content": f"嗯{i}"})
    # 两条命中关键词的旧回复：一条是判词腔，一条是正常话
    history[2] = {"role": "user", "content": "德育线怎么算"}
    history[3] = {"role": "assistant", "content": "这一刀就变得很致命：绩点是软排序，德育线是硬门槛。"}  # 判词腔旧回复
    history[4] = {"role": "user", "content": "德育线又是啥"}
    history[5] = {"role": "assistant", "content": "呜……才不是猫娘啦！德育线的事等会儿再说。"}  # 旧人设旧回复
    _recent, brief = select_recent_and_recall(
        history, "德育线怎么算的", recent_turns=6, recall_top=5
    )
    assert "德育线怎么算" in brief   # 对方说过的话要留住
    assert "致命" not in brief       # 她的判词腔旧回复不引
    assert "猫娘" not in brief       # 她的旧人设回复也不引


def test_messages_for_llm_trims_assistant_turns():
    hist: list[dict[str, str]] = []
    for i in range(12):
        hist.append({"role": "user", "content": f"问{i}"})
        hist.append({"role": "assistant", "content": f"答{i}"})
    out, _brief = messages_for_llm_with_recall(
        hist, "问11", recent_turns=24, recall_top=2, enable=True, max_assistant_turns=4
    )
    assert sum(1 for m in out if m["role"] == "assistant") == 4
    assert out[0]["role"] == "user"


def test_cross_chat_omits_her_own_reply_by_default(tmp_path):
    """跨场合流水只记「对方说了什么」，不再把她自己的原话当范文。"""
    store = get_cross_store(str(tmp_path))
    store.record(where="group:1", who="洛希极限", text="然后再划一个更高的德育线",
                 reply="这一刀就变得很致命：绩点是软排序，德育线是硬门槛。")
    brief = build_cross_brief(current_where="group:2", n=8, directory=str(tmp_path))
    assert "德育线" in brief          # 对方说的话要留住
    assert "致命" not in brief        # 她自己的句子不再回灌
    assert "你：" not in brief


def test_cross_chat_enable_defaults_off():
    """默认关：它是「一本正经重复废话」最直接的自产污染源。"""
    assert PluginConfig().cross_chat_enable is False


def test_recall_defaults_are_tight():
    cfg = PluginConfig()
    assert cfg.memory_recall_top <= 2
    assert cfg.history_assistant_turns == 8
