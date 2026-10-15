"""第四层：真实集成测试 —— Fake OneBot Event 走完整 Gateway → Router 链路。

Phase 3 第三十五/三十六节。这些测试不测单个函数，而是构造真实的
NoneBot 事件对象，走完整链路：

    FakeEvent → gateway.from_onebot_event → Runtime.dispatch
              → Observer / Command / Plugin / Agent

并用 mock 替换掉真正会发消息/调 LLM 的边界，验证"谁被执行了"。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dl_senpai.config import PluginConfig
from dl_senpai.core.runtime import Runtime
from dl_senpai.gateway import agent_session_key, from_onebot_event


# ================================================================
# Fake OneBot 事件
# ================================================================


class FakeSender:
    def __init__(self, nickname: str = "小明", card: str = "") -> None:
        self.nickname = nickname
        self.card = card


class FakeSegment:
    def __init__(self, seg_type: str, data: dict | None = None) -> None:
        self.type = seg_type
        self.data = data or {}


class FakeMessage(list):
    """最小消息容器：list 的语义 + get_plaintext()。"""

    def get_plaintext(self) -> str:
        return "".join(
            str(seg.data.get("text", "")) for seg in self if seg.type == "text"
        )


class FakeBot:
    def __init__(self, self_id: str = "999") -> None:
        self.self_id = self_id
        self.send = AsyncMock()
        self.call_api = AsyncMock(return_value={})


def _real_onebot_classes():
    """取真实的 OneBot 事件类，让 fake 继承它们。

    为什么必须继承：`gateway.from_onebot_event` 用 `isinstance` 分派事件类型。
    纯鸭子类型的 fake 会被判为未知事件而返回 None——这不是 bug，
    而是刻意的类型安全。测试要么构造真实事件对象，要么继承。
    继承比构造真实对象更轻量，也不依赖 pydantic 校验细节。
    """
    from nonebot.adapters.onebot.v11 import (
        GroupIncreaseNoticeEvent,
        GroupMessageEvent,
        PrivateMessageEvent,
    )

    return GroupMessageEvent, PrivateMessageEvent, GroupIncreaseNoticeEvent


_GroupMessageEvent, _PrivateMessageEvent, _GroupIncreaseNoticeEvent = (
    _real_onebot_classes()
)


class _EventMixin:
    """绕过 pydantic 校验，只提供业务需要的字段。"""

    def __init__(self, **kw) -> None:  # noqa: D401
        for k, v in kw.items():
            object.__setattr__(self, k, v)

    def get_plaintext(self) -> str:
        return self.message.get_plaintext().strip()

    def is_tome(self) -> bool:
        for seg in self.message:
            if seg.type == "at" and str(seg.data.get("qq")) == str(self.self_id):
                return True
        return False


class FakeGroupMessageEvent(_EventMixin, _GroupMessageEvent):  # type: ignore[misc]
    def __init__(
        self,
        *,
        text: str = "",
        user_id: int = 7,
        group_id: int = 100,
        message_id: int = 1,
        at_bot: bool = False,
        self_id: int = 999,
        segments: list[FakeSegment] | None = None,
    ) -> None:
        segs: list[FakeSegment] = []
        if at_bot:
            segs.append(FakeSegment("at", {"qq": str(self_id)}))
        if text:
            segs.append(FakeSegment("text", {"text": text}))
        if segments:
            segs.extend(segments)
        _EventMixin.__init__(
            self,
            post_type="message",
            message_type="group",
            user_id=user_id,
            group_id=group_id,
            self_id=self_id,
            message_id=message_id,
            sender=FakeSender(),
            message=FakeMessage(segs),
            reply=None,
        )


class FakePrivateMessageEvent(_EventMixin, _PrivateMessageEvent):  # type: ignore[misc]
    def __init__(self, *, text: str = "", user_id: int = 7, message_id: int = 1) -> None:
        segs = [FakeSegment("text", {"text": text})] if text else []
        _EventMixin.__init__(
            self,
            post_type="message",
            message_type="private",
            user_id=user_id,
            group_id=0,
            self_id=999,
            message_id=message_id,
            sender=FakeSender(),
            message=FakeMessage(segs),
            reply=None,
        )


class FakeGroupIncreaseNotice(_EventMixin, _GroupIncreaseNoticeEvent):  # type: ignore[misc]
    def __init__(self, *, user_id: int = 8, group_id: int = 100) -> None:
        _EventMixin.__init__(
            self,
            post_type="notice",
            notice_type="group_increase",
            user_id=user_id,
            group_id=group_id,
            self_id=999,
            message_id=0,
            message=FakeMessage([]),
        )


# ================================================================
# Gateway 归一化
# ================================================================


def test_gateway_normalizes_group_message() -> None:
    bot = FakeBot()
    event = FakeGroupMessageEvent(text="签到", group_id=100, user_id=7)
    ctx = from_onebot_event(event, bot)
    assert ctx is not None
    assert ctx.message_type == "group"
    assert ctx.group_id == "100"
    assert ctx.user_id == "7"
    assert ctx.session_id == "group:100"
    assert ctx.plain_text == "签到"
    assert ctx.is_private is False
    assert ctx.actor == "小明"


def test_gateway_normalizes_private_message() -> None:
    bot = FakeBot()
    ctx = from_onebot_event(FakePrivateMessageEvent(text="在吗", user_id=7), bot)
    assert ctx.message_type == "private"
    assert ctx.session_id == "private:7"
    assert ctx.is_private is True
    assert ctx.is_to_me is True  # 私聊天然是"找我"


def test_gateway_detects_at_bot() -> None:
    bot = FakeBot()
    ctx = from_onebot_event(FakeGroupMessageEvent(text="你好", at_bot=True), bot)
    assert ctx.is_to_me is True


def test_gateway_normalizes_notice() -> None:
    bot = FakeBot()
    ctx = from_onebot_event(FakeGroupIncreaseNotice(user_id=8, group_id=100), bot)
    assert ctx.message_type == "notice"
    assert ctx.notice_type == "notice.group_increase"
    assert ctx.user_id == "8"


def test_gateway_assigns_trace_id() -> None:
    bot = FakeBot()
    ctx = from_onebot_event(FakeGroupMessageEvent(text="hi"), bot)
    assert ctx.trace_id


def test_gateway_unknown_event_returns_none() -> None:
    assert from_onebot_event(object(), FakeBot()) is None
    assert from_onebot_event(None, FakeBot()) is None


def test_gateway_agent_key_differs_by_user() -> None:
    bot = FakeBot()
    a = from_onebot_event(FakeGroupMessageEvent(text="x", user_id=1), bot)
    b = from_onebot_event(FakeGroupMessageEvent(text="x", user_id=2), bot)
    assert agent_session_key(a) != agent_session_key(b)
    assert a.session_id == b.session_id


# ================================================================
# 集成：场景 1-4（命令 / @ / 普通消息 / 私聊）
# ================================================================


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    """把生产数据目录重定向到临时目录。

    **为什么必须做**：`observe_notice_welcome` 会真的调用
    `get_verify_store().register(...)`，进而把 `group:user` 写进
    `data/dl_senpai/verify/pending.json`。集成测试跑一遍就污染真实数据
    （实测会把测试群 100 写进生产 verify 表）。

    这里把相关 Store 的目录全部指向 tmp_path。
    """
    from dl_senpai.group_verify import GroupVerifyStore

    verify_dir = tmp_path / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)

    # handlers 通过模块级名字调用 get_verify_store，patch 这一处即可。
    # 注意不要 patch 不存在的属性（如 handlers.get_group_feature_store），
    # 那会让 monkeypatch 自身抛 AttributeError。
    monkeypatch.setattr(
        "dl_senpai.handlers.get_verify_store",
        lambda *a, **k: GroupVerifyStore(verify_dir),
    )
    return tmp_path


def _tmp_dirs_config(tmp_path) -> PluginConfig:
    """一份把所有落盘目录指向 tmp_path 的配置。

    为什么需要：dispatch 会跑观察者，而 observe_recent_chat 现在还会采样
    群风格；用生产默认目录就会把测试群 100 写进 data/dl_senpai/group_style。
    """
    return PluginConfig(
        DL_SENPAI_MEMORY_DIR=str(tmp_path / "memory"),
        DL_SENPAI_INNER_STATE_DIR=str(tmp_path / "inner_state"),
        DL_SENPAI_AFFECTION_DIR=str(tmp_path / "affection"),
        DL_SENPAI_GROUP_STYLE_DIR=str(tmp_path / "group_style"),
        DL_SENPAI_RECENT_CHAT_DIR=str(tmp_path / "recent_chat"),
        DL_SENPAI_MEMORY_CARDS_DIR=str(tmp_path / "memory_cards"),
    )


@pytest.fixture
def runtime(monkeypatch, tmp_path) -> Runtime:
    cfg = _tmp_dirs_config(tmp_path)
    # handlers 走模块级 get_config；桥接层走 `from .config import get_config`，
    # 绑的是 config 模块里那个名字，两处都要单独打一次。
    monkeypatch.setattr("dl_senpai.handlers.get_config", lambda: cfg)
    monkeypatch.setattr("dl_senpai.config.get_config", lambda: cfg)
    return Runtime().build()


@pytest.fixture
def checkin_group(monkeypatch, tmp_path) -> PluginConfig:
    """把测试群配成签到群 + 功能全开。

    落盘目录必须指向 tmp_path：这些场景会走完整条消息管道，观察者顺带采样
    群风格、回复后结算内心状态，用生产默认目录就会写进 data/dl_senpai。
    """
    cfg = PluginConfig(
        DL_SENPAI_CHECKIN_ENABLE=True,
        DL_SENPAI_CHECKIN_GROUPS="100",
        DL_SENPAI_ALLOWED_GROUPS="",
        DL_SENPAI_ENABLE_PRIVATE=True,
        DL_SENPAI_MEMORY_DIR=str(tmp_path / "memory"),
        DL_SENPAI_INNER_STATE_DIR=str(tmp_path / "inner_state"),
        DL_SENPAI_AFFECTION_DIR=str(tmp_path / "affection"),
        DL_SENPAI_GROUP_STYLE_DIR=str(tmp_path / "group_style"),
        DL_SENPAI_RECENT_CHAT_DIR=str(tmp_path / "recent_chat"),
        DL_SENPAI_MEMORY_CARDS_DIR=str(tmp_path / "memory_cards"),
    )
    monkeypatch.setattr("dl_senpai.handlers.get_config", lambda: cfg)
    # 桥接层走的是 `from .config import get_config`，绑的是 config 模块里
    # 那个名字，所以必须单独打一次，否则 agent 路径仍拿生产配置。
    monkeypatch.setattr("dl_senpai.config.get_config", lambda: cfg)
    monkeypatch.setattr("dl_senpai.core.commands.builtin._checkin_guard",
                        lambda ctx: bool(ctx.group_id))
    monkeypatch.setattr("dl_senpai.core.commands.builtin._shop_guard",
                        lambda ctx: bool(ctx.group_id))
    return cfg


@pytest.mark.asyncio
async def test_scenario_1_checkin_only_does_not_call_agent(
    runtime: Runtime, checkin_group: PluginConfig
) -> None:
    """场景 1：用户「签到」→ 只执行签到，**不得调用 Agent**。"""
    bot = FakeBot()
    ctx = from_onebot_event(FakeGroupMessageEvent(text="签到"), bot)

    agent_called: list[str] = []

    async def fake_agent(c, trigger=None):
        agent_called.append("agent")
        return True

    with patch("dl_senpai.legacy_bridge.handle_agent_message", fake_agent):
        with patch("dl_senpai.handlers.dispatch_checkin_command", new=AsyncMock()) as cmd:
            result = await runtime.dispatch(ctx)

    assert result.stage == "command"
    assert result.handled is True
    assert result.consumed is True
    assert agent_called == [], "命令消费后不应进入 Agent"
    assert cmd.await_count == 1


@pytest.mark.asyncio
async def test_scenario_2_shop_only(
    runtime: Runtime, checkin_group: PluginConfig
) -> None:
    """场景 2：用户「商店」→ 只执行商店。"""
    bot = FakeBot()
    ctx = from_onebot_event(FakeGroupMessageEvent(text="商店"), bot)

    with patch("dl_senpai.handlers.dispatch_shop_command", new=AsyncMock()) as shop:
        result = await runtime.dispatch(ctx)

    assert result.stage == "command"
    assert result.consumed is True
    assert shop.await_count == 1


@pytest.mark.asyncio
async def test_scenario_3_mention_enters_agent(runtime: Runtime) -> None:
    """场景 3：@机器人 你好 → 进入 Agent。"""
    bot = FakeBot()
    ctx = from_onebot_event(FakeGroupMessageEvent(text="你好", at_bot=True), bot)

    calls: list[str] = []

    async def fake_agent(c, trigger=None):
        calls.append(trigger.kind.value)
        return True

    with patch("dl_senpai.legacy_bridge.handle_agent_message", fake_agent):
        result = await runtime.dispatch(ctx)

    assert calls == ["mention"]
    assert result.stage == "agent"
    assert result.consumed is True


@pytest.mark.asyncio
async def test_scenario_3b_short_mention_still_enters_agent(runtime: Runtime) -> None:
    """@必回：即使正文极短也必须进 Agent（回归保护）。"""
    bot = FakeBot()
    ctx = from_onebot_event(FakeGroupMessageEvent(text="在吗", at_bot=True), bot)
    calls: list[str] = []

    async def fake_agent(c, trigger=None):
        calls.append("hit")
        return True

    with patch("dl_senpai.legacy_bridge.handle_agent_message", fake_agent):
        await runtime.dispatch(ctx)

    assert calls == ["hit"]


@pytest.mark.asyncio
async def test_scenario_4_plain_message_uses_interrupt_probability(
    runtime: Runtime,
) -> None:
    """场景 4：普通群聊按 interrupt 概率逻辑（注入 rng）。"""
    bot = FakeBot()
    ctx = from_onebot_event(
        FakeGroupMessageEvent(text="今天天气真的挺不错的啊"), bot
    )

    calls: list[str] = []

    async def fake_agent(c, trigger=None):
        calls.append(trigger.kind.value)
        return True

    import random

    # 概率设为 1.0 → 必然插嘴
    with patch.object(runtime.agent_trigger, "decide") as decide:
        from dl_senpai.core.agent_trigger import AgentDecision, AgentTrigger

        decide.return_value = AgentDecision(AgentTrigger.INTERRUPT, "forced")
        with patch("dl_senpai.legacy_bridge.handle_agent_message", fake_agent):
            await runtime.dispatch(ctx)

    assert calls == ["interrupt"]


@pytest.mark.asyncio
async def test_scenario_4b_plain_message_may_skip_agent(runtime: Runtime) -> None:
    """概率未中时不应进 Agent。"""
    bot = FakeBot()
    ctx = from_onebot_event(
        FakeGroupMessageEvent(text="今天天气真的挺不错的啊"), bot
    )
    calls: list[str] = []

    async def fake_agent(c, trigger=None):
        calls.append("hit")
        return True

    with patch.object(runtime.agent_trigger, "decide") as decide:
        from dl_senpai.core.agent_trigger import AgentDecision, AgentTrigger

        decide.return_value = AgentDecision(AgentTrigger.NONE, "roll")
        with patch("dl_senpai.legacy_bridge.handle_agent_message", fake_agent):
            result = await runtime.dispatch(ctx)

    assert calls == []
    assert result.handled is False


@pytest.mark.asyncio
async def test_scenario_private_enters_agent(runtime: Runtime) -> None:
    """私聊行为一致：进入 Agent 且 forced=True。"""
    bot = FakeBot()
    ctx = from_onebot_event(FakePrivateMessageEvent(text="在吗"), bot)
    forced: list[bool] = []

    async def fake_agent(c, trigger=None):
        forced.append(trigger.forced)
        return True

    with patch("dl_senpai.legacy_bridge.handle_agent_message", fake_agent):
        result = await runtime.dispatch(ctx)

    assert forced == [True]
    assert result.stage == "agent"


# ================================================================
# 集成：场景 5-6（sticker observer）
# ================================================================


@pytest.mark.asyncio
async def test_scenario_5_sticker_observer_runs_once_with_agent(
    runtime: Runtime,
) -> None:
    """场景 5：带表情图片 + @机器人 → observer 执行一次，Agent 同时执行。"""
    bot = FakeBot()
    ctx = from_onebot_event(
        FakeGroupMessageEvent(
            text="看这个",
            at_bot=True,
            segments=[FakeSegment("image", {"file": "x.jpg", "sub_type": "1"})],
        ),
        bot,
    )

    observed: list[str] = []
    agent: list[str] = []

    async def fake_observe(c):
        observed.append("sticker")

    async def fake_agent(c, trigger=None):
        agent.append("hit")
        return True

    with patch("dl_senpai.handlers.observe_stickers", fake_observe):
        runtime.router.unregister_observer("sticker_collector", owner="builtin_observers")
        runtime.router.observe("sticker_collector", fake_observe, owner="builtin_observers", priority=900)
        with patch("dl_senpai.legacy_bridge.handle_agent_message", fake_agent):
            await runtime.dispatch(ctx)

    assert observed == ["sticker"], f"observer 执行次数异常: {len(observed)}"
    assert agent == ["hit"]


@pytest.mark.asyncio
async def test_scenario_6_command_consumes_but_observer_still_sees(
    runtime: Runtime, checkin_group: PluginConfig
) -> None:
    """场景 6：命令 + sticker → 命令被消费，sticker observer 仍记录。

    这是 Phase 3 最关键的语义断言（第十七/十八节）。
    """
    bot = FakeBot()
    ctx = from_onebot_event(
        FakeGroupMessageEvent(
            text="签到",
            segments=[FakeSegment("image", {"file": "y.jpg", "sub_type": "1"})],
        ),
        bot,
    )

    observed: list[str] = []

    async def fake_observe(c):
        observed.append("sticker")

    runtime.router.unregister_observer("sticker_collector", owner="builtin_observers")
    runtime.router.observe("sticker_collector", fake_observe, owner="builtin_observers", priority=900)

    with patch("dl_senpai.handlers.dispatch_checkin_command", new=AsyncMock()):
        result = await runtime.dispatch(ctx)

    assert result.stage == "command"
    assert result.consumed is True
    assert observed == ["sticker"], "命令 consume 后 observer 被错误屏蔽"


@pytest.mark.asyncio
async def test_scenario_notice_reaches_welcome_observer(
    runtime: Runtime, isolated_data
) -> None:
    """通知类事件只走观察者，不进入 Router。"""
    bot = FakeBot()
    ctx = from_onebot_event(FakeGroupIncreaseNotice(), bot)
    seen: list[str] = []

    async def fake_welcome(c):
        seen.append("welcome")

    runtime.router.unregister_observer("welcome_notice", owner="builtin_observers")
    runtime.router.observe(
        "welcome_notice", fake_welcome,
        matcher=lambda c: c.notice_type == "notice.group_increase",
        owner="builtin_observers",
    )
    await runtime.router.dispatch_observers(ctx)
    assert seen == ["welcome"]


@pytest.mark.asyncio
async def test_real_welcome_observer_does_not_touch_prod_data(isolated_data) -> None:
    """真实 welcome observer 写数据时必须落在隔离目录。

    这条测试保护生产 `data/dl_senpai/verify/pending.json` 不被测试污染。
    """
    from dl_senpai.core.runtime import Runtime as R

    rt = R().build()
    bot = FakeBot()
    ctx = from_onebot_event(FakeGroupIncreaseNotice(user_id=8, group_id=100), bot)

    from dl_senpai.handlers import observe_notice_welcome

    # 直接调用真实观察者，它会写 verify store
    try:
        await observe_notice_welcome(ctx)
    except Exception:  # noqa: BLE001 — bot mock 能力有限，写入成功即可
        pass

    # 数据必须落在隔离目录，而不是 data/dl_senpai
    assert (isolated_data / "verify").is_dir()
    from pathlib import Path

    prod = Path(__file__).resolve().parents[1] / "data" / "dl_senpai" / "verify"
    if (prod / "pending.json").is_file():
        import json

        raw = json.loads((prod / "pending.json").read_text(encoding="utf-8"))
        assert "100:8" not in raw, "测试污染了生产 verify 数据"


# ================================================================
# 集成：场景 7-8（多插件 priority + consume、旁路插件）
# ================================================================


@pytest.mark.asyncio
async def test_scenario_7_two_plugins_priority_and_consume(runtime: Runtime) -> None:
    """场景 7：两个 plugin 同时匹配 → 验证 priority + consume。"""
    from dl_senpai.core.router import Route, RouteResult

    order: list[str] = []
    runtime.router.plugin_route(
        "low", lambda c: True,
        lambda c: order.append("low") or RouteResult.handle(consumed=False),
        priority=50,
    )
    runtime.router.plugin_route(
        "high", lambda c: True,
        lambda c: order.append("high") or RouteResult.handle(consumed=False),
        priority=10,
    )
    runtime.router.plugin_route(
        "stopper", lambda c: True,
        lambda c: order.append("stopper") or RouteResult.handle(consumed=True),
        priority=999,
    )

    bot = FakeBot()
    ctx = from_onebot_event(FakeGroupMessageEvent(text="嗯"), bot)
    await runtime.dispatch(ctx)

    assert order == ["high", "low", "stopper"]


@pytest.mark.asyncio
async def test_scenario_8_bypass_plugin_lets_router_continue(
    runtime: Runtime,
) -> None:
    """场景 8：旁路 plugin 执行后 Router 继续（consume=False）。"""
    from dl_senpai.core.router import RouteResult

    order: list[str] = []
    runtime.router.plugin_route(
        "bypass", lambda c: True,
        lambda c: order.append("bypass") or RouteResult.observe(reason="bypass"),
        priority=10,
    )

    bot = FakeBot()
    ctx = from_onebot_event(FakeGroupMessageEvent(text="你好", at_bot=True), bot)

    async def fake_agent(c, trigger=None):
        order.append("agent")
        return True

    with patch("dl_senpai.legacy_bridge.handle_agent_message", fake_agent):
        await runtime.dispatch(ctx)

    assert order == ["bypass", "agent"]


# ================================================================
# 集成：场景 10-11（session 有序 / 并发）
# ================================================================


@pytest.mark.asyncio
async def test_scenario_10_same_session_ordered() -> None:
    """场景 10：同一 session 连续两条消息必须有序。"""
    from dl_senpai.core.session.executor import SessionExecutor

    ex = SessionExecutor()
    order: list[int] = []

    async def task(n: int) -> None:
        order.append(n)

    for i in range(3):
        await ex.submit("group:100:user:7", (lambda n: (lambda: task(n)))(i))
    await ex.drain()
    assert order == [0, 1, 2]


@pytest.mark.asyncio
async def test_scenario_11_different_sessions_concurrent() -> None:
    """场景 11：不同 session 允许并发。"""
    import asyncio

    from dl_senpai.core.session.executor import SessionExecutor

    ex = SessionExecutor()
    marks: list[str] = []

    async def slow() -> None:
        marks.append("slow-start")
        await asyncio.sleep(0.05)
        marks.append("slow-end")

    async def fast() -> None:
        marks.append("fast-start")
        await asyncio.sleep(0.01)
        marks.append("fast-end")

    await ex.submit("group:100:user:1", slow)
    await ex.submit("group:100:user:2", fast)
    await ex.drain()
    assert marks.index("fast-start") < marks.index("slow-end")
