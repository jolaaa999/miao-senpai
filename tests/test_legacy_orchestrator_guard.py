"""Legacy 路径的两个硬缺陷的守卫（2026-10 审计）。

审计发现 `agent/legacy_orchestrator.py` 在 Phase 6.6 的 state 重构里没做完：

1. `LegacyRunState` 从未定义（16 处引用，无定义也无导入），`run()` 必抛 NameError；
2. 各阶段方法读的是**未定义的局部变量**而不是 `state.X`（机械改写删掉了前缀）。

我们不修这个死代码（它已被 runtime 取代），但必须保证两件事：

  它失败时给出的信息是**可行动的**，而不是让人误以为只是少一行定义；
  它失败时**上层仍然能发出回复**——把 mode 设成 legacy 的人要的是回退保险，
  不是静默不回话。
"""

from __future__ import annotations

import inspect

import pytest


def test_legacy_orchestrator_fails_with_actionable_message() -> None:
    """run() 必须抛带说明的 RuntimeError，而不是 NameError。"""
    from dl_senpai.agent.legacy_orchestrator import LegacyOrchestrator

    async def _go() -> None:
        await LegacyOrchestrator().run(
            bot=None, event=None, force_mention=False, config=None
        )

    import asyncio

    with pytest.raises(RuntimeError) as exc:
        asyncio.get_event_loop().run_until_complete(_go()) if False else None
        asyncio.run(_go())

    message = str(exc.value)
    assert "legacy_orchestrator_unavailable" in message
    # 必须点名「改用哪个模式」，否则拿到报错的人不知道下一步做什么
    assert "runtime" in message
    assert "LegacyRunState" in message


def test_run_source_does_not_instantiate_missing_state() -> None:
    """结构断言：run() 里不得再直接实例化未定义的 LegacyRunState。

    一旦有人真的把状态容器补齐了，这条会失败——那时请把本测试连同
    legacy_bridge 的降级逻辑一起删掉，而不是放宽断言。
    """
    from dl_senpai.agent import legacy_orchestrator as mod

    src = inspect.getsource(mod.LegacyOrchestrator.run)
    assert "state = LegacyRunState()" not in src
    assert "raise RuntimeError" in src


@pytest.mark.asyncio
async def test_entry_falls_back_to_runtime_when_legacy_raises() -> None:
    """legacy handler 抛异常时必须降级到 runtime，而不是把异常抛上去。"""
    from dl_senpai.agent.context import AgentContext
    from dl_senpai.agent.entry import AgentEntry, AgentExecutionMode
    from dl_senpai.gateway import MessageContext

    class _FakeResult:
        text = "来自 runtime 的回复"
        steps = []
        tool_calls = []

    class _FakeRuntime:
        executor = None

        def __init__(self) -> None:
            self.called = False

        async def run(self, context, **kw):
            self.called = True
            return _FakeResult()

    async def _boom(context):
        raise RuntimeError("legacy_orchestrator_unavailable: 测试用")

    runtime = _FakeRuntime()
    entry = AgentEntry(
        runtime=runtime,
        legacy_handler=_boom,
        mode_resolver=lambda: AgentExecutionMode.LEGACY,
    )
    msg = MessageContext(message_type="group", group_id="1", user_id="2")
    ctx = AgentContext(session_id="group:1", message_context=msg, config=None)

    outcome = await entry.handle(ctx)

    assert runtime.called, "legacy 失败后必须真的跑了 runtime"
    assert outcome.used_runtime is True
    assert outcome.error in ("", None)