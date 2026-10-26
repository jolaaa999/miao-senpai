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

**2026-10 补记**：曾经有两个实现——`AgentRuntime`（新）与
`LegacyOrchestrator`（旧）。旧实现已整体删除（见 handlers._handle_chat_inner），
现在本 Protocol 只有一个实现者。**但它仍然保留**：legacy_bridge 的注入点、
以及所有 `is_orchestrator()` 的结构化判定都依赖这层契约，
删掉等于把"可替换"这件事写死，而未来（如子 Agent）还会需要第二个实现。

历史上两者参数不对称的原因是：
    - AgentRuntime 是平台无关的（吃 AgentContext）
    - Legacy 深依赖 OneBot 的 Bot / MessageEvent（发消息、取群成员）

因此 Protocol 只约束**共同形状** `async run(*args, **kwargs)`，
由各自的调用方按自己的契约调用。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentOrchestrator(Protocol):
    """Agent 编排器的最小共同契约（第二十二节）。

    实现者：
        AgentRuntime        —— 唯一实现（RUNTIME / SHADOW / CANARY）
        （旧的 LegacyOrchestrator 已于 2026-10 删除）

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
