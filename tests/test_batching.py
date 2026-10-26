"""消息合并窗口单测。

核心行为只有两条，但都很容易写错：
  1. 连着发的几条，只有**最后一条**负责回复；
  2. 中间那些必须安静退出，否则会回好几次。
"""

from __future__ import annotations

import asyncio

from dl_senpai.batching import MessageBatcher


def _run(coro):
    return asyncio.run(coro)


def test_single_message_passes_through_unchanged():
    b = MessageBatcher()
    out = _run(b.wait_and_merge("group:1", "在吗", "老王", window_ms=0))
    assert out == "在吗"


def test_burst_is_merged_and_only_last_returns():
    """三条连着发 → 只有最后一条带合并结果，前两条返回 None。"""
    b = MessageBatcher()

    async def scenario():
        first = asyncio.create_task(
            b.wait_and_merge("group:1", "那个 PPT", "小李", window_ms=60)
        )
        await asyncio.sleep(0.01)
        second = asyncio.create_task(
            b.wait_and_merge("group:1", "就是上次说的", "小李", window_ms=60)
        )
        await asyncio.sleep(0.01)
        third = asyncio.create_task(
            b.wait_and_merge("group:1", "你有吗", "小李", window_ms=60)
        )
        return await asyncio.gather(first, second, third)

    a, c, d = _run(scenario())
    assert a is None and c is None, "前两条应安静退出"
    assert d == "那个 PPT\n就是上次说的\n你有吗"
    assert b.pending("group:1") == 0, "合并后应清空缓冲"


def test_same_speaker_has_no_name_prefix():
    """同一个人连打三行，标三遍名字读起来太吵。"""
    b = MessageBatcher()

    async def scenario():
        t1 = asyncio.create_task(b.wait_and_merge("g", "第一句", "老王", window_ms=50))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(b.wait_and_merge("g", "第二句", "老王", window_ms=50))
        return await asyncio.gather(t1, t2)

    _, last = _run(scenario())
    assert last == "第一句\n第二句"
    assert "老王：" not in last


def test_different_speakers_are_labelled():
    """两个人接话时必须带名字，否则分不清谁说的。"""
    b = MessageBatcher()

    async def scenario():
        t1 = asyncio.create_task(b.wait_and_merge("g", "吃啥", "小李", window_ms=50))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(b.wait_and_merge("g", "随便", "老王", window_ms=50))
        return await asyncio.gather(t1, t2)

    _, last = _run(scenario())
    assert "小李：吃啥" in last
    assert "老王：随便" in last


def test_sessions_do_not_mix():
    """两个群同时说话不能串味。"""
    b = MessageBatcher()
    assert _run(b.wait_and_merge("group:1", "群一的话", "a", window_ms=0)) == "群一的话"
    assert _run(b.wait_and_merge("group:2", "群二的话", "b", window_ms=0)) == "群二的话"


def test_max_items_flushes_immediately():
    """攒满上限就立刻交出去，别让人一直等不到回应。"""
    b = MessageBatcher(max_items=2)

    async def scenario():
        t1 = asyncio.create_task(b.wait_and_merge("g", "一", "a", window_ms=5000))
        await asyncio.sleep(0.01)
        # 第二条到达时已满 2 条，应当立刻 flush，不再等 5 秒
        return await asyncio.wait_for(
            b.wait_and_merge("g", "二", "a", window_ms=5000), timeout=1.0
        )

    out = _run(scenario())
    assert out == "一\n二"


def test_reset_clears_pending():
    b = MessageBatcher()
    _run(b.wait_and_merge("g", "x", "a", window_ms=0))
    b.reset()
    assert b.pending("g") == 0
