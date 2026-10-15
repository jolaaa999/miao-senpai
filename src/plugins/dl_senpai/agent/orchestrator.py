"""AgentOrchestrator Protocol（Phase 6.5 第二十二节）。

第二十二/二十三节要求：

    AgentEntry 只应该：
        select orchestrator
        run
        handle mode
        handle shadow/canary
        handle error boundary

    不能知道内部实现。

因此定义统一接口，让 Runtime 与 Legacy 都有相同形状。
第二十四节：`AgentRuntime` 若已符合接口则直接注册，
**不建空壳 wrapper**。

注意一个**真实的不对称**（诚实记录）：
    AgentRuntime.run(context: AgentContext, user_message: str) -> AgentResult
    LegacyOrchestrator.run(bot, event, *, force_mention, config) -> None

两者参数不同，无法用同一个 Protocol 直接约束。原因是：
    - AgentRuntime 是平台无关的（吃 AgentContext）
    - Legacy 深依赖 OneBot 的 Bot / MessageEvent（发消息、取群成员）

处理方式：Protocol 只约束**共同形状** `async run(*args, **kwargs)`，
由各自的调用方（AgentEntry / legacy_bridge）按自己的契约调用。
这是刻意的：强行统一签名会迫使其中一方做无意义的适配。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentOrchestrator(Protocol):
    """Agent 编排器的最小共同契约（第二十二节）。

    实现者：
        AgentRuntime        —— 新架构（RUNTIME 模式）
        LegacyOrchestrator  —— 旧编排（LEGACY 模式）

    两者**只要求**有一个可 await 的 run。
    参数契约各自不同，由调用方负责适配（见模块 docstring）。
    """

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        """执行一轮编排。"""
        ...


def is_orchestrator(obj: Any) -> bool:
    """结构化判定：任何有可等待 run 的对象都算编排器。"""
    return callable(getattr(obj, "run", None))


__all__ = ["AgentOrchestrator", "is_orchestrator"]
