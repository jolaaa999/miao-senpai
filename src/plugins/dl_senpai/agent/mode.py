"""执行模式与停止原因：Phase 5 切流的两个核心枚举。

**为什么要用枚举而不是多个 bool**（第五节明确要求）：

重构风险最高的地方是"两条执行路径同时存在"。如果用
`RUNTIME_ENABLED` / `SHADOW_ENABLED` / `CANARY_ENABLED` 三个 bool，
会出现 `RUNTIME=true, SHADOW=true` 这种语义未定义的状态。

单一枚举让状态空间**互斥且完备**：

    LEGACY  → 100% 旧路径（紧急回滚）
    SHADOW  → 旧路径执行，新路径只算不做
    CANARY  → 白名单群走新路径，其余走旧路径
    RUNTIME → 新路径正式执行，legacy 仅作 fallback
"""

from __future__ import annotations

from ..config_env import get_env, get_flag

import os
from enum import Enum


class AgentExecutionMode(str, Enum):
    """Agent 执行模式。"""

    # 100% 旧路径。用于紧急回滚。
    #
    # **Phase 5.5 起标记为 deprecated**：它仍是合法的部署级回滚手段，
    # 但不再作为 RUNTIME 模式的自动 fallback 目标。
    # 计划在真实流量验证充分后（Phase 6/7）删除。
    LEGACY = "legacy"
    # 旧路径真正执行；AgentRuntime 只计算（dry_run，无副作用）。
    SHADOW = "shadow"
    # 白名单内的群走 AgentRuntime，其余走旧路径。
    CANARY = "canary"
    # AgentRuntime 正式执行。**不再 per-request fallback 到 legacy**。
    RUNTIME = "runtime"


def is_deprecated_mode(mode: AgentExecutionMode) -> bool:
    """该模式是否已标记废弃。"""
    return mode is AgentExecutionMode.LEGACY


def deprecation_warning(mode: AgentExecutionMode) -> str:
    """生成启动 warning 文本（第二十节要求 legacy mode 明确标记 deprecated）。

    文案要点：**legacy 不再是可用的回滚路径**。
    `LegacyOrchestrator` 在 Phase 6.6 的重构中未完成，run() 会直接抛异常，
    AgentEntry 捕获后降级到 runtime。也就是说配了 legacy 实际跑的还是 runtime，
    只是多一条 warning、多一次异常捕获。

    为什么保留这个模式而不是直接删掉：配置里可能还有旧值，
    删掉会变成启动期校验失败、bot 起不来；保留并降级更安全。
    """
    if not is_deprecated_mode(mode):
        return ""
    # 注意这是**相邻字符串字面量拼接**，各段之间不能有逗号——
    # 加一个逗号就变成返回 tuple 了，`"deprecated" in warning` 会直接为假，
    # 而函数签名写的还是 str，类型检查也未必拦得住。
    return (
        "legacy agent mode is deprecated and no longer functional: "
        "DL_SENPAI_AGENT_MODE=legacy 的旧实现已不可运行，"
        "本次启动实际会降级到 runtime 模式。建议把配置改为 runtime。"
    )


class StopReason(str, Enum):
    """Agent 一轮结束的原因（第四十六节：固定枚举，不用任意字符串）。

    关键区分：**哪些是"正常停止"，哪些是"基础设施故障"**。
    只有后者才允许触发 legacy fallback（第十一/十二节）——
    否则一次请求可能执行两遍副作用。
    """

    # -- 正常结束 --
    COMPLETED = "completed"              # 模型给出最终回复，流程走完
    NO_TOOL_CALL = "no_tool_call"        # 模型没有要求调用工具
    # -- 护栏正常拦截（不是故障）--
    MAX_STEPS = "max_steps"
    MAX_TOOL_CALLS = "max_tool_calls"
    REPEAT_LIMIT = "repeat_limit"
    TOKEN_BUDGET = "token_budget"
    TIMEOUT = "timeout"
    # -- 业务性结束（不是故障）--
    PERMISSION_DENIED = "permission_denied"
    TOOL_ERROR = "tool_error"            # 工具执行失败，但流程正常收尾
    CONFIRMATION_REQUIRED = "confirmation_required"
    # -- 故障（唯一允许 fallback 的一类）--
    MODEL_ERROR = "model_error"
    INFRA_ERROR = "infra_error"
    CANCELLED = "cancelled"


# 允许触发 legacy fallback 的停止原因。
#
# 为什么只有这三个：它们代表"新路径没能正常工作"，
# 而用户还没看到任何回复、也没有任何副作用发生。
FALLBACK_ALLOWED_REASONS: frozenset[str] = frozenset({
    StopReason.MODEL_ERROR.value,
    StopReason.INFRA_ERROR.value,
})

# 明确禁止 fallback 的原因（即使它们看起来像"失败"）。
#
# MAX_STEPS / TOKEN_BUDGET / TIMEOUT / PERMISSION_DENIED 都是
# **护栏正常工作**的结果，不是故障。若对这些做 fallback，
# 等于绕过护栏再跑一遍，可能重复执行副作用。
FALLBACK_FORBIDDEN_REASONS: frozenset[str] = frozenset({
    StopReason.MAX_STEPS.value,
    StopReason.MAX_TOOL_CALLS.value,
    StopReason.REPEAT_LIMIT.value,
    StopReason.TOKEN_BUDGET.value,
    StopReason.TIMEOUT.value,
    StopReason.PERMISSION_DENIED.value,
    StopReason.TOOL_ERROR.value,
    StopReason.CONFIRMATION_REQUIRED.value,
    StopReason.CANCELLED.value,
    StopReason.COMPLETED.value,
    StopReason.NO_TOOL_CALL.value,
})


# -- 环境变量 --

ENV_MODE = "DL_SENPAI_AGENT_MODE"
ENV_CANARY_GROUPS = "DL_SENPAI_AGENT_RUNTIME_GROUPS"
# 兼容 Phase 4 的旧开关（避免用户配置突然失效）
ENV_SHADOW_LEGACY = "DL_SENPAI_AGENT_SHADOW"


def resolve_mode(explicit: str | None = None) -> AgentExecutionMode:
    """决定当前执行模式。

    解析顺序：
    1. 显式参数（测试/调用方直接指定）
    2. `DL_SENPAI_AGENT_MODE`
    3. 兼容 Phase 4 的 `DL_SENPAI_AGENT_SHADOW=1` → SHADOW
    4. 默认 **RUNTIME**（Phase 5 的切流目标）
    """
    raw = (explicit or get_env(ENV_MODE, "")).strip().lower()
    if raw:
        for mode in AgentExecutionMode:
            if mode.value == raw:
                return mode
        # 容错别名
        if raw in {"on", "true", "runtime", "new"}:
            return AgentExecutionMode.RUNTIME
        if raw in {"off", "false", "old"}:
            return AgentExecutionMode.LEGACY
        # 未识别的值（含测试隔离哨兵 __UNSET__）→ 视为未配置，
        # 继续往下走兼容开关与默认值，而不是直接返回某个模式。
        # 这样 Phase 4 的 DL_SENPAI_AGENT_SHADOW=1 仍能生效。

    if get_flag(ENV_SHADOW_LEGACY):
        return AgentExecutionMode.SHADOW

    return AgentExecutionMode.RUNTIME


def canary_groups() -> set[str]:
    """Canary 白名单群号。

    刻意**不做随机采样**（第七节）：随机切流导致问题难以复现。
    按群号切则同一群的行为稳定，出问题能立刻定位。
    """
    raw = get_env(ENV_CANARY_GROUPS, "").strip()
    if not raw:
        return set()
    return {g.strip() for g in raw.split(",") if g.strip()}


def mode_for_group(mode: AgentExecutionMode, group_id: str | None) -> AgentExecutionMode:
    """把全局模式解析成具体某个群的模式。"""
    if mode is not AgentExecutionMode.CANARY:
        return mode
    if group_id and str(group_id) in canary_groups():
        return AgentExecutionMode.RUNTIME
    return AgentExecutionMode.LEGACY


def should_fallback(stop_reason: str, *, side_effect_committed: bool) -> bool:
    """是否允许触发 legacy fallback。

    Args:
        stop_reason: Agent 的停止原因。
        side_effect_committed: 本轮是否已经发生**不可逆副作用**。

    第二道闸门是关键（第十三节）：即使原因是"故障"，
    只要已经禁言/改名片/写 memory，就**绝不能**再跑一次 legacy，
    否则会执行两遍。
    """
    if side_effect_committed:
        return False
    return stop_reason in FALLBACK_ALLOWED_REASONS


__all__ = [
    "AgentExecutionMode",
    "StopReason",
    "FALLBACK_ALLOWED_REASONS",
    "FALLBACK_FORBIDDEN_REASONS",
    "resolve_mode",
    "canary_groups",
    "mode_for_group",
    "should_fallback",
    "is_deprecated_mode",
    "deprecation_warning",
    "ENV_MODE",
    "ENV_CANARY_GROUPS",
    "ENV_SHADOW_LEGACY",
]
