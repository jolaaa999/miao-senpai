"""子 agent 调度器的单元测试。

盯三件事，都是「不测就会静默坏」的：
1. 派单立即返回（主对话不被阻塞）——坏了就是她又卡两分钟才说话；
2. 作业失败/超时也一定回调——坏了就是「她说画了，然后永远没有图」；
3. 并发上限生效——坏了是群里连刷时几个请求一起超时，全废。
"""

from __future__ import annotations

import asyncio

import pytest

from dl_senpai.subagent import (
    SubAgentError,
    SubAgentRunner,
    SubAgentSpec,
    _extract_json,
    get_subagent_runner,
    reset_subagent_runner,
    run_llm_agent,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    # 单例是进程级的：不重置会让用例之间的并发计数互相串
    reset_subagent_runner()


def test_extract_json_tolerates_fences_and_chatter() -> None:
    assert _extract_json('{"action":"finish"}') == {"action": "finish"}
    assert _extract_json('好的\n{"action":"finish"}\n') == {"action": "finish"}
    # 模型爱用三个反引号的 json 代码块包一层（这里用 \x60 拼，避免源码里出现反引号）
    fence = "\x60" * 3
    fenced = fence + 'json\n{"action":"tool","tool":"image_search"}\n' + fence
    assert _extract_json(fenced)["tool"] == "image_search"
    assert _extract_json("我不知道") == {}
    assert _extract_json("") == {}


@pytest.mark.asyncio
async def test_submit_returns_immediately_and_calls_back() -> None:
    runner = SubAgentRunner(max_concurrent=2)
    seen: list[str] = []

    async def _slow() -> str:
        await asyncio.sleep(0.05)
        return "done"

    job_id = runner.submit("draw", _slow, timeout=2.0, on_done=lambda r: seen.append(r.value))
    # 关键断言：submit 立刻返回，作业还没跑完
    assert job_id
    assert seen == []
    await runner.drain(timeout=3.0)
    assert seen == ["done"]
    assert runner.pending() == 0


@pytest.mark.asyncio
async def test_callback_still_fires_on_failure() -> None:
    """作业炸了也必须回调：调用方靠它决定要不要发降级文案。"""
    runner = SubAgentRunner(max_concurrent=1)
    got: list[tuple[str, str]] = []

    async def _boom() -> None:
        raise ValueError("上游 502")

    runner.submit(
        "draw",
        _boom,
        timeout=2.0,
        on_done=lambda r: got.append((r.status, r.error)),
    )
    await runner.drain(timeout=3.0)
    assert got and got[0][0] == "failed"
    assert "上游 502" in got[0][1]


@pytest.mark.asyncio
async def test_timeout_is_reported_as_timeout() -> None:
    runner = SubAgentRunner(max_concurrent=1)
    statuses: list[str] = []

    async def _hang() -> None:
        await asyncio.sleep(5)

    runner.submit("draw", _hang, timeout=0.1, on_done=lambda r: statuses.append(r.status))
    await runner.drain(timeout=3.0)
    assert statuses == ["timeout"]


@pytest.mark.asyncio
async def test_concurrency_is_capped() -> None:
    """并发上限：群里连刷时不能同时把上游打满。"""
    runner = SubAgentRunner(max_concurrent=2)
    running = 0
    peak = 0

    async def _work() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1

    for _ in range(5):
        runner.submit("draw", _work, timeout=5.0)
    await runner.drain(timeout=5.0)
    assert peak <= 2
    assert runner.stats()["total"] == 5


@pytest.mark.asyncio
async def test_llm_agent_runs_tool_then_finishes() -> None:
    """智能型任务：先调工具拿结果，再带着结果收工。"""
    script = [
        {"action": "tool", "tool": "image_search", "args": {"query": "凯尔希 立绘"}},
        {"action": "finish", "answer": "找到了", "payload": {"url": "https://example.com/a.png"}},
    ]
    calls: list[list[dict]] = []

    async def _fake_chat(config, messages):
        calls.append(list(messages))
        return script[len(calls) - 1]

    async def _fake_search(args):
        return {"hits": [{"image_url": "u"}]}

    import dl_senpai.subagent as sub

    spec = SubAgentSpec(name="ref", goal="找一张凯尔希的官方立绘", max_steps=3)
    original = dict(sub._TOOLS)
    sub._TOOLS["image_search"] = _fake_search
    try:
        answer = await run_llm_agent(None, spec, chat=_fake_chat)
    finally:
        # 工具表是模块级共享的：用例结束必须还原，否则后面的用例会拿到假实现
        sub._TOOLS.clear()
        sub._TOOLS.update(original)

    assert answer.answer == "找到了"
    assert answer.payload["url"] == "https://example.com/a.png"
    assert answer.steps == 2
    # 第二轮上下文里必须带着工具结果，否则模型下一步就是瞎猜
    assert "工具 image_search 的结果" in calls[1][-1]["content"]


@pytest.mark.asyncio
async def test_budget_exhausted_falls_back_to_last_tool_result() -> None:
    """走完预算还没收工，也要把最后拿到的工具结果带回来。

    实测模型常在最后一步纠结「再搜一次」，前面搜到的图不该跟着作废。
    """

    async def _fake_chat(config, messages):
        return {"action": "tool", "tool": "image_find", "args": {"query": "凯尔希"}}

    import dl_senpai.subagent as sub

    async def _fake_find(args):
        return {"url": "https://example.com/k.png", "candidates": [{"url": "https://example.com/k.png"}]}

    original = dict(sub._TOOLS)
    sub._TOOLS["image_find"] = _fake_find
    try:
        answer = await run_llm_agent(
            None, SubAgentSpec(name="ref", goal="找图", max_steps=2), chat=_fake_chat
        )
    finally:
        sub._TOOLS.clear()
        sub._TOOLS.update(original)

    assert answer.payload["url"] == "https://example.com/k.png"
    assert answer.steps == 2


@pytest.mark.asyncio
async def test_llm_agent_raises_only_without_any_tool_result() -> None:
    """一步都没走成（max_steps=0 被抬到 1 且没结果）才抛错。"""

    async def _fake_chat(config, messages):
        return {}  # 空回复，也没调工具

    spec = SubAgentSpec(name="ref", goal="找图", max_steps=1)
    with pytest.raises(SubAgentError):
        await run_llm_agent(None, spec, chat=_fake_chat)


@pytest.mark.asyncio
async def test_tolerates_name_and_parameters_key_spellings() -> None:
    """模型把工具名写成 name、参数写成 parameters 时也必须能用。

    实测 deepseek 就这么写：只认 tool/args 时它永远「协议违规」，
    三步预算全烧在改键名上。
    """
    script = [
        {"action": "tool", "name": "image_find", "parameters": {"query": "凯尔希"}},
        {"action": "finish", "answer": "ok", "payload": {"url": "u"}},
    ]
    calls: list[list[dict]] = []

    async def _fake_chat(config, messages):
        calls.append(list(messages))
        return script[len(calls) - 1]

    async def _fake_find(args):
        # 参数必须真的传进来，否则模型会一直换着名字重试
        assert args.get("query") == "凯尔希"
        return {"url": "u"}

    import dl_senpai.subagent as sub

    original = dict(sub._TOOLS)
    sub._TOOLS["image_find"] = _fake_find
    try:
        answer = await run_llm_agent(
            None, SubAgentSpec(name="ref", goal="找图", max_steps=3), chat=_fake_chat
        )
    finally:
        sub._TOOLS.clear()
        sub._TOOLS.update(original)

    assert answer.answer == "ok"
    assert answer.steps == 2


def test_system_prompt_lists_tool_signatures() -> None:
    """工具清单必须带参数示例：只列名字时模型会说「没有这个工具」然后收工。"""
    from dl_senpai.subagent import _PERSONA, _TOOL_DOCS

    assert "image_find" in _TOOL_DOCS
    desc, example = _TOOL_DOCS["image_find"]
    assert "query" in example
    assert "{tool_lines}" in _PERSONA


def test_singleton_is_reused() -> None:
    a = get_subagent_runner()
    b = get_subagent_runner()
    assert a is b
    reset_subagent_runner()
    assert get_subagent_runner() is not a
