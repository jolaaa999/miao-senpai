"""第三层：Behavior Compatibility Test —— 新旧路由决策等价性（Shadow 验证）。

Phase 3 第三十二/三十三节。用真实的 MessageContext 分别跑：
    - `legacy_route_for()`  ：复现 7 个 NoneBot matcher 的决策
    - `new_route_for()`     ：新 Router 的决策
断言两者等价。

这就是 shadow 模式在测试里的形态：不依赖真实流量也能验证一致性。
"""

from __future__ import annotations

import pytest

from dl_senpai.core.runtime import Runtime
from dl_senpai.core.shadow import (
    LegacyRoute,
    compare,
    legacy_route_for,
    new_route_for,
    shadow_enabled,
)
from dl_senpai.gateway import MessageContext


@pytest.fixture
def runtime() -> Runtime:
    return Runtime().build()


def group_ctx(text: str = "", **kw) -> MessageContext:
    base = dict(
        message_type="group",
        group_id="100",
        user_id="7",
        session_id="group:100",
        plain_text=text,
    )
    base.update(kw)
    return MessageContext(**base)


def private_ctx(text: str = "") -> MessageContext:
    return MessageContext(
        message_type="private",
        user_id="7",
        session_id="private:7",
        plain_text=text,
        is_to_me=True,
        is_private=True,
    )


# ================================================================
# 等价性：命令类
# ================================================================


@pytest.mark.parametrize(
    "text,expected",
    [
        ("签到", "checkin"),
        ("打卡", "checkin"),
        ("我的积分", "checkin"),
        ("积分排行", "checkin"),
        ("今日任务", "checkin"),
        ("完成任务", "checkin"),
        ("签到帮助", "checkin"),
        ("商店", "shop"),
        ("仓库", "shop"),
        ("商店帮助", "shop"),
        ("购买裙子", "shop"),
        ("穿上汉服", "shop"),
    ],
)
def test_command_routes_match_legacy(runtime: Runtime, text: str, expected: str) -> None:
    """命令类消息：新旧决策必须指向同一命令。

    注意 guard 要求 checkin 群白名单。这里显式把群配成签到群，
    否则两边都会判 none（那也是等价的，但不是这条测试想验证的）。
    """
    from dl_senpai.config import PluginConfig

    cfg = PluginConfig(
        DL_SENPAI_CHECKIN_ENABLE=True,
        DL_SENPAI_CHECKIN_GROUPS="100",
        DL_SENPAI_ALLOWED_GROUPS="",
    )
    ctx = group_ctx(text)
    legacy = legacy_route_for(ctx, config=cfg)
    new = new_route_for(runtime, ctx, config=cfg)
    assert new == expected, f"{text!r}: 新路由给出 {new}，期望 {expected}"
    assert legacy.value == expected, f"{text!r}: 旧路由给出 {legacy.value}，期望 {expected}"


# 好感度命令单独一组：checkin 与 affection 是**两个 namespace 但同一个
# handle_checkin 实现**（与原 matcher 一致）。谁先匹配取决于注册表内
# (priority, name) 排序，因此不能断言具体落在哪个 namespace——
# 只能断言"落在命令域且新旧一致"。
@pytest.mark.parametrize("text", ["好感度", "好感排行", "好感帮助"])
def test_affection_commands_route_to_command_stage(runtime: Runtime, text: str) -> None:
    from dl_senpai.config import PluginConfig

    cfg = PluginConfig(DL_SENPAI_CHECKIN_GROUPS="100")
    ctx = group_ctx(text)
    legacy = legacy_route_for(ctx, config=cfg)
    new = new_route_for(runtime, ctx, config=cfg)

    assert legacy.value == "checkin", f"旧实现把 {text!r} 归到了 {legacy.value}"
    assert new in {"checkin", "affection"}, f"新实现把 {text!r} 归到了 {new}"
    # 两者都必须在命令域（不能落进 AI）
    assert new not in {"none", "interrupt", "mention", "private"}


# ================================================================
# 等价性：Agent 类
# ================================================================


def test_private_always_routes_to_agent(runtime: Runtime) -> None:
    ctx = private_ctx("在吗")
    assert legacy_route_for(ctx) is LegacyRoute.PRIVATE
    assert new_route_for(runtime, ctx) == "private"


def test_mention_routes_to_agent(runtime: Runtime) -> None:
    ctx = group_ctx("@学姐 你好", is_to_me=True)
    assert legacy_route_for(ctx) is LegacyRoute.MENTION
    assert new_route_for(runtime, ctx) == "mention"


def test_short_mention_still_routes_to_agent(runtime: Runtime) -> None:
    """@ 必须必回，即使正文很短（短于 min_msg_len）。

    这是回归保护：旧 matcher 的 mention 走 force_mention=True，
    **绕过** should_ignore_text。如果新策略误加了长度过滤，
    "@学姐 在吗" 就会从"必回"变成"被忽略"。
    """
    ctx = group_ctx("@学姐 在吗", is_to_me=True)
    assert new_route_for(runtime, ctx) == "mention"


def test_plain_short_message_not_routed(runtime: Runtime) -> None:
    """普通短消息（未 @）应被忽略，不进 AI。"""
    ctx = group_ctx("嗯")
    assert legacy_route_for(ctx) is LegacyRoute.NONE
    assert new_route_for(runtime, ctx) == "none"


def test_plain_long_message_is_interrupt_candidate(runtime: Runtime) -> None:
    """够长的普通消息进入插嘴候选（是否掷中由概率决定）。"""
    ctx = group_ctx("今天天气真的挺不错的啊")
    legacy = legacy_route_for(ctx)
    new = new_route_for(runtime, ctx)
    # 旧实现给 INTERRUPT（候选），新实现按骰子给出 interrupt 或 none
    assert legacy is LegacyRoute.INTERRUPT
    assert new in {"interrupt", "none"}


# ================================================================
# 等价性：通知类
# ================================================================


def test_group_increase_routes_to_welcome(runtime: Runtime) -> None:
    ctx = MessageContext(message_type="notice", group_id="100", notice_type="notice.group_increase")
    assert legacy_route_for(ctx) is LegacyRoute.WELCOME
    assert new_route_for(runtime, ctx) == "welcome"


def test_group_decrease_routes_to_leave(runtime: Runtime) -> None:
    ctx = MessageContext(message_type="notice", group_id="100", notice_type="notice.group_decrease")
    assert legacy_route_for(ctx) is LegacyRoute.LEAVE
    assert new_route_for(runtime, ctx) == "leave"


def test_other_notice_routes_to_none(runtime: Runtime) -> None:
    ctx = MessageContext(message_type="notice", group_id="100", notice_type="notice.other")
    assert legacy_route_for(ctx) is LegacyRoute.NONE
    assert new_route_for(runtime, ctx) == "none"


# ================================================================
# Shadow compare
# ================================================================


def test_shadow_disabled_by_default() -> None:
    assert shadow_enabled() is False


def test_shadow_env_enables(monkeypatch) -> None:
    monkeypatch.setenv("DL_SENPAI_ROUTER_SHADOW", "1")
    assert shadow_enabled() is True


def test_compare_records_match_for_command(runtime: Runtime) -> None:
    from dl_senpai.config import PluginConfig

    cfg = PluginConfig(DL_SENPAI_CHECKIN_GROUPS="100")
    ctx = group_ctx("签到")
    # compare 内部各自取配置，这里只验证它能跑并给出结论
    record = compare(runtime, ctx)
    assert record.legacy in {r.value for r in LegacyRoute}
    assert isinstance(record.match, bool)


def test_compare_tolerates_interrupt_probability(runtime: Runtime) -> None:
    """插嘴是概率性的，shadow 必须容忍"旧候选 vs 新未掷中"。"""
    ctx = group_ctx("这是一条足够长的普通群聊消息内容")
    record = compare(runtime, ctx)
    # 不应因为概率差异被标记为 mismatch
    assert record.match is True, (
        f"概率差异被误判为不匹配: legacy={record.legacy} new={record.new}"
    )


def test_shadow_metrics_recorded(runtime: Runtime) -> None:
    from dl_senpai.observability import metrics

    metrics.reset()
    ctx = group_ctx("签到")
    compare(runtime, ctx)
    assert metrics.counter("router.shadow_compared") > 0


# ================================================================
# 全量样本一致性
# ================================================================


SHADOW_SAMPLES: list[tuple[str, MessageContext]] = [
    ("签到命令", group_ctx("签到")),
    ("打卡命令", group_ctx("打卡")),
    ("商店命令", group_ctx("商店")),
    ("仓库命令", group_ctx("仓库")),
    ("好感度命令", group_ctx("好感度")),
    ("@机器", group_ctx("@学姐 你好", is_to_me=True)),
    ("@机器短句", group_ctx("@学姐 在吗", is_to_me=True)),
    ("普通短消息", group_ctx("嗯")),
    ("普通长消息", group_ctx("今天天气真的挺不错的啊")),
    ("私聊", private_ctx("在吗")),
    ("入群通知", MessageContext(message_type="notice", group_id="100",
                            notice_type="notice.group_increase")),
    ("退群通知", MessageContext(message_type="notice", group_id="100",
                            notice_type="notice.group_decrease")),
]


def test_all_shadow_samples_consistent(runtime: Runtime) -> None:
    """批量比对：所有样本的 legacy 与 new 决策必须一致。

    这是 Phase 3 报告里 shadow 验证数据的来源。
    """
    mismatches: list[str] = []
    for label, ctx in SHADOW_SAMPLES:
        legacy = legacy_route_for(ctx)
        new = new_route_for(runtime, ctx)
        if legacy.value == new:
            continue
        # 唯一允许的差异：旧实现给"插嘴候选"，新实现因概率未中给 none
        if legacy is LegacyRoute.INTERRUPT and new == "none":
            continue
        mismatches.append(f"{label}: legacy={legacy.value} new={new}")

    assert mismatches == [], "发现路由决策不一致：\n" + "\n".join(mismatches)
