"""生图冷却：DL_SENPAI_DRAW_COOLDOWN 之前是个只存在于配置里的死项。

为什么要盯：图要 30~120 秒，同一会话连要两张时第二张必然排到超时，
而配置里明明写了冷却却从来没被读过——「配了没生效」比「没这个功能」更难查。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import dl_senpai.draw_worker as dw
from dl_senpai.agent.tools.executors import execute_draw, reset_draw_cooldown
from dl_senpai.agent.tools.models import ToolCall


def _ctx(**kw):
    base = dict(
        config=SimpleNamespace(draw_async=True, draw_cooldown=60),
        session_id="group:1",
        actor="小明",
        user_id="100",
        group_id="1",
        plain_text="画只猫",
        extra={},
        interrupt=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _call(prompt: str = "橘猫") -> ToolCall:
    return ToolCall(id="c1", tool_name="draw", arguments={"prompt": prompt})


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    """默认不真派单：这里只验证冷却判定。"""
    reset_draw_cooldown()
    monkeypatch.setattr(dw, "submit_draw_job", lambda context, prompt: "job-1")


@pytest.mark.asyncio
async def test_cooldown_zero_means_unlimited() -> None:
    ctx = _ctx(config=SimpleNamespace(draw_async=True, draw_cooldown=0))
    for _ in range(3):
        result = await execute_draw(ctx, _call())
        assert result.success is True, "关掉冷却时不该拦"


@pytest.mark.asyncio
async def test_second_draw_within_cooldown_is_blocked() -> None:
    ctx = _ctx()
    first = await execute_draw(ctx, _call())
    second = await execute_draw(ctx, _call("再画一只"))
    assert first.success is True
    assert second.success is False
    assert second.error_type == "draw_cooldown"
    # 提示里要有剩余秒数，模型才说得出「等一会儿」，而不是干巴巴地拒绝
    assert "还要等" in second.error


@pytest.mark.asyncio
async def test_other_session_is_not_blocked() -> None:
    await execute_draw(_ctx(), _call())
    elsewhere = await execute_draw(_ctx(session_id="private:999", group_id=""), _call())
    assert elsewhere.success is True, "别的群/私聊不该被这个群的冷却挡住"


@pytest.mark.asyncio
async def test_cooldown_expires(monkeypatch) -> None:
    clock = {"t": 1000.0}

    # executors 里是 `import time` + time.monotonic()，替换整个 time 属性最稳
    import dl_senpai.agent.tools.executors as ex

    class _FakeTime:
        @staticmethod
        def monotonic() -> float:
            return clock["t"]

    monkeypatch.setattr(ex, "time", _FakeTime)

    ctx = _ctx()
    assert (await execute_draw(ctx, _call())).success is True
    clock["t"] += 61.0   # 冷却 60 秒，过了一秒的余量
    assert (await execute_draw(ctx, _call())).success is True


@pytest.mark.asyncio
async def test_empty_prompt_still_rejected_first() -> None:
    """没写画面就该报 empty_prompt，别被冷却抢先拦住。"""
    result = await execute_draw(_ctx(), _call(""))
    assert result.error_type == "empty_prompt"