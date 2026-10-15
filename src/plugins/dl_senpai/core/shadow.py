"""Shadow Mode：新旧路由决策并行比对，只执行一个。

Phase 3 第三十三节。直接切流的风险在于：新 Router 的决策可能与 7 个
NoneBot matcher 的隐式 priority 顺序有细微差异，而这类差异往往只在
真实群里才暴露。

Shadow 模式的做法：

    真实消息
       ↓
    Legacy 决策（真正执行）
       ↓
    New Router 决策（**只计算，不执行**）
       ↓
    比对 → 不一致就记 router.shadow_mismatch

这样可以在零风险的前提下积累真实样本，用数据决定何时切流。

开关：环境变量 `DL_SENPAI_ROUTER_SHADOW`（默认关闭）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..observability.logging import log_event
from ..observability.metrics import metrics

SHADOW_ENV = "DL_SENPAI_ROUTER_SHADOW"
_TRUTHY = {"1", "true", "yes", "on"}


class LegacyRoute(str, Enum):
    """旧 matcher 会走的目标（按 priority 顺序的第一个命中）。"""

    VERIFY = "verify"
    CHECKIN = "checkin"
    SHOP = "shop"
    PRIVATE = "private"
    MENTION = "mention"
    INTERRUPT = "interrupt"
    STICKER_ONLY = "sticker_only"
    WELCOME = "welcome"
    LEAVE = "leave"
    NONE = "none"


@dataclass
class ShadowRecord:
    """一次 shadow 比对结果。"""

    legacy: str
    new: str
    match: bool
    session_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "legacy_route": self.legacy,
            "new_route": self.new,
            "match": self.match,
            "session_id": self.session_id or None,
        }


def shadow_enabled() -> bool:
    """Shadow 模式是否开启。"""
    return os.environ.get(SHADOW_ENV, "").strip().lower() in _TRUTHY


# -- 旧决策复现 ----------------------------------------------------------
#
# 严格按 Phase 1 记录的 priority 顺序判定，**不调用新 Router**。
# 这是 shadow 能发现问题的前提：两边必须是独立实现。


def legacy_route_for(ctx: Any, config: Any = None) -> LegacyRoute:
    """复现旧 matcher 的路由决策。

    顺序（Phase 1 审计确认）：
        verify(4) → welcome/leave(5) → checkin(5) → shop(5)
        → private(8) → mention(10) → interrupt(50) → sticker(100)

    注意：welcome/leave 是 notice 事件，与消息事件不冲突，因此这里
    先判 notice。checkin 与 shop 同为 priority 5，NoneBot 按注册顺序执行；
    handlers.py 里 checkin_matcher 先于 shop_matcher 注册，所以 checkin 优先。

    Args:
        config: 可选配置覆盖。旧 Rule 函数内部调 `get_config()`，
               因此测试注入配置时走这里的分支，避免污染全局。
    """
    from ..handlers import (
        _checkin_rule,
        _shop_rule,
        _verify_rule,
    )

    message_type = getattr(ctx, "message_type", "")
    raw_event = getattr(ctx, "raw_event", None)
    bot = getattr(ctx, "bot", None)
    if config is None:
        try:
            from ..config_runtime.runtime import get_config

            config = get_config()
        except Exception:  # noqa: BLE001
            config = None

    if message_type == "notice":
        notice_type = getattr(ctx, "notice_type", "")
        if notice_type == "notice.group_increase":
            return LegacyRoute.WELCOME
        if notice_type == "notice.group_decrease":
            return LegacyRoute.LEAVE
        return LegacyRoute.NONE

    # 命令类：直接用文本走现有 parse_* 函数 + 同样的 guard 条件。
    # 之所以不调 _checkin_rule/_shop_rule，是因为它们读全局 config；
    # 这里按它们的**判定条件**等价复现（正则来自同一批常量）。
    text = getattr(ctx, "plain_text", "") or ""
    if message_type == "group" and config is not None:
        from ..affection import parse_affection_command
        from ..checkin import parse_checkin_command
        from ..shop import parse_shop_command

        gid = getattr(ctx, "group_id", "")

        # verify(4)：需要 pending 状态，无 raw_event 时跳过
        if raw_event is not None and bot is not None:
            try:
                if _verify_rule(raw_event, bot):
                    return LegacyRoute.VERIFY
            except Exception:  # noqa: BLE001
                pass

        # checkin(5)
        try:
            from ..affection import parse_affection_command
            from ..checkin import parse_checkin_command
            from ..group_features import is_feature_enabled
            from ..shop import parse_shop_command

            if is_feature_enabled(config, gid, "checkin") and (
                parse_checkin_command(text) != "none"
                or parse_affection_command(text) != "none"
            ):
                return LegacyRoute.CHECKIN
            # shop(5)
            if (
                is_feature_enabled(config, gid, "shop")
                and config.is_checkin_group(gid)
                and parse_shop_command(text) != "none"
            ):
                return LegacyRoute.SHOP
        except Exception:  # noqa: BLE001
            pass

    # private(8)
    if message_type == "private":
        return LegacyRoute.PRIVATE

    # mention(10)
    if getattr(ctx, "is_to_me", False):
        return LegacyRoute.MENTION

    # interrupt(50) —— 必须过 should_ignore_text
    from ..trigger import should_ignore_text

    has_images = ctx.has_images() if hasattr(ctx, "has_images") else False
    if config is not None and should_ignore_text(
        text, config.min_msg_len, has_images=has_images
    ):
        return LegacyRoute.STICKER_ONLY if has_images else LegacyRoute.NONE

    # 概率判定在 shadow 里**不掷骰子**（否则每次比对都随机不同）。
    # 只要"够格进入插嘴候选"就算 INTERRUPT 候选，与实际是否掷中无关。
    return LegacyRoute.INTERRUPT


def new_route_for(runtime: Any, ctx: Any, config: Any = None) -> str:
    """计算新 Router 会走哪条路（**不执行 handler**）。

    实现方式：查命令注册表 + 查 Agent 触发策略，与 ``Runtime`` 里的
    实际路由逻辑保持同源，但只取决策不做副作用。
    """
    message_type = getattr(ctx, "message_type", "")
    if message_type == "notice":
        notice_type = getattr(ctx, "notice_type", "")
        if notice_type == "notice.group_increase":
            return LegacyRoute.WELCOME.value
        if notice_type == "notice.group_decrease":
            return LegacyRoute.LEAVE.value
        return LegacyRoute.NONE.value

    if message_type not in {"group", "private"}:
        return LegacyRoute.NONE.value

    # 命令 stage。guard 通过 ctx.extra["config"] 读取注入配置，
    # 保证 shadow 比对是可复现的纯函数（不依赖进程全局状态）。
    if config is not None and isinstance(getattr(ctx, "extra", None), dict):
        ctx.extra["config"] = config
    invocation = runtime.commands.match(getattr(ctx, "plain_text", "") or "", ctx)
    if invocation is not None:
        return invocation.command.name

    # agent stage
    decision = runtime.agent_trigger.decide(ctx, config=config)
    if not decision.should_run:
        # 未触发：与旧行为对齐——有图则等价于"只采集表情"
        if ctx.has_images() if hasattr(ctx, "has_images") else False:
            return LegacyRoute.STICKER_ONLY.value
        return LegacyRoute.NONE.value
    return decision.kind.value


# -- 比对 ----------------------------------------------------------------


def compare(runtime: Any, ctx: Any) -> ShadowRecord:
    """比对一次路由决策，并在不一致时记录。"""
    legacy = legacy_route_for(ctx)
    new = new_route_for(runtime, ctx)

    # 归一化：旧实现的 INTERRUPT 是"候选"，新实现只在掷中时才给 interrupt。
    # 两者语义不同层，不能直接判等——这类已知差异必须显式声明，
    # 否则 shadow 日志会被噪声淹没。
    legacy_norm = legacy.value
    match = legacy_norm == new

    if not match and legacy is LegacyRoute.INTERRUPT and new == LegacyRoute.NONE.value:
        # 旧：进入插嘴候选；新：本轮骰子没中。这是**预期差异**，
        # 由概率决定，不算路由错误。
        match = True

    record = ShadowRecord(
        legacy=legacy.value,
        new=new,
        match=match,
        session_id=getattr(ctx, "session_id", "") or "",
    )
    metrics.incr("router.shadow_compared")
    if not match:
        metrics.incr("router.shadow_mismatch")
        log_event("router.shadow_mismatch", level="warning", **record.as_log_fields())
    return record


__all__ = [
    "LegacyRoute",
    "ShadowRecord",
    "shadow_enabled",
    "legacy_route_for",
    "new_route_for",
    "compare",
    "SHADOW_ENV",
]
