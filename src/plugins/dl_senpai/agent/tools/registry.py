"""ToolDefinition 与 ToolRegistry。

设计目标（Phase 4 第六十六节）：新增一个工具只需注册 ToolDefinition，
**不需要改 persona / llm / handlers / agent runtime**。

一个 ToolDefinition 自描述四件事：
1. **怎么被模型看见**  —— `prompt_text`（承载现有 *_SYSTEM_EXTRA 原文）
2. **怎么被解析**      —— `marker` + `marker_parser`
3. **怎么被执行**      —— `executor`
4. **执行后怎么办**    —— `continuation`

可见性（第十五节）由 `visibility` 谓词决定，而不是让 PromptBuilder
写 `if draw_enable: prompt += DRAW_SYSTEM_EXTRA`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .models import Continuation, ToolCall, ToolResult

# 可见性谓词：(context) -> bool
VisibilityPredicate = Callable[[Any], bool]
# 执行器：(context, call) -> ToolResult（可同步或异步）
ToolExecutor = Callable[[Any, ToolCall], Any]
# marker 解析器：(text) -> (剩余文本, [ToolCall])
MarkerParser = Callable[[str], "tuple[str, list[ToolCall]]"]


class FailurePolicy(str, Enum):
    """工具失败后的处理策略（第四十七节）。

    AgentRuntime **不得**用 `if tool.name == "search"` 来判断，
    必须由工具自己声明。
    """

    # 把失败结果回填给模型，让它决定怎么继续（例如检索失败→换个说法答）
    RETURN_TO_MODEL = "return_to_model"
    # 直接停止本轮，用已有文本收尾
    STOP = "stop"
    # 失败但降级为纯文字继续（媒体类工具的默认行为）
    FALLBACK_TEXT = "fallback_text"


@dataclass
class ToolDefinition:
    """一个工具的自描述定义。

    Attributes:
        name: 工具名（唯一），如 `card` / `draw` / `search`。
        description: 人类可读说明（供 list() / 日志）。
        marker: marker 名（如 `CARD`）。空表示该工具没有 marker 协议。
        prompt_text: 注入 system prompt 的说明文本。
            **必须是 persona.py 里现有 *_SYSTEM_EXTRA 的原文**。
        executor: 执行器。
        parser: marker 解析器。
        parameter_schema: 参数 schema（用于校验与文档）。
        permission: 权限标识（Phase 4 只登记 + 走 PermissionService）。
        timeout: 执行超时秒数（0 = 不限制）。**Phase 5 真正 enforce**。
        rate_limit: 最小调用间隔秒数（0 = 不限制）。
        requires_confirmation: 是否需要用户确认（机制已建，暂不强制）。
        continuation: 执行后是否回模型。
        failure_policy: 失败后的处理策略。
        visibility: 可见性谓词；None 表示始终可见。
        owner: 归属者（插件 id），用于批量注销。
        enabled: 是否启用。
        priority: 解析顺序（数值小者先），用于保证多 marker 顺序稳定。
        side_effect: 是否产生**不可逆**副作用（决定 fallback 是否还允许）。
    """

    name: str
    description: str = ""
    marker: str = ""
    prompt_text: str = ""
    executor: ToolExecutor | None = None
    parser: MarkerParser | None = None
    parameter_schema: dict[str, Any] = field(default_factory=dict)
    permission: str = ""
    timeout: float = 0.0
    rate_limit: float = 0.0
    requires_confirmation: bool = False
    continuation: Continuation = Continuation.SIDE_EFFECT_ONLY
    failure_policy: FailurePolicy = FailurePolicy.FALLBACK_TEXT
    visibility: VisibilityPredicate | None = None
    owner: str = ""
    enabled: bool = True
    priority: int = 100
    # None 表示由 journal.is_irreversible 推断（默认判定表）
    side_effect: bool | None = None
    # Phase 5.5：漏写 marker 时的补救策略。
    # **必须挂在定义上**，而不是让 AgentRuntime 写
    # `if tool_name == "card"` —— 见第六节的硬性约束。
    recovery_policy: Any = None

    def is_visible(self, context: Any) -> bool:
        """该工具对当前上下文是否可见。

        可见性谓词异常时**视为不可见**——宁可少给模型一个工具，
        也不要因为配置读取失败而把不该给的工具暴露出去。
        """
        if not self.enabled:
            return False
        if self.visibility is None:
            return True
        try:
            return bool(self.visibility(context))
        except Exception:  # noqa: BLE001
            return False

    def has_side_effect(self) -> bool:
        """是否产生不可逆副作用。

        显式声明优先；未声明时用 `journal.IRREVERSIBLE_TOOLS` 推断。
        """
        if self.side_effect is not None:
            return self.side_effect
        from ..journal import is_irreversible

        return is_irreversible(self.name)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "tool": self.name,
            "marker": self.marker or None,
            "owner": self.owner or None,
            "continuation": self.continuation.value,
            "failure_policy": self.failure_policy.value,
        }


class ToolRegistry:
    """工具注册表。

    与 CommandRegistry 同构（Phase 3 已建立的模式）：注册 / 注销 /
    按 owner 批量清理 / 可见性过滤。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._ordered: list[ToolDefinition] | None = None
        # 权限服务（延迟绑定，见 _permission_service）。
        self._permissions: Any = None
        # 显式关掉权限层（仅供测试构造"纯注册表"场景）。
        self._permissions_disabled = False

    # -- 注册 --

    def register(self, tool: ToolDefinition) -> ToolDefinition:
        """注册工具；同名替换（防 reload 重复注册）。"""
        self._tools[tool.name] = tool
        self._ordered = None
        return tool

    def unregister(self, name: str, *, owner: str | None = None) -> bool:
        existing = self._tools.get(name)
        if existing is None:
            return False
        if owner is not None and existing.owner != owner:
            return False
        del self._tools[name]
        self._ordered = None
        return True

    def unregister_owner(self, owner: str) -> int:
        """按归属者批量注销（插件 disable 时用）。"""
        if not owner:
            return 0
        names = [n for n, t in self._tools.items() if t.owner == owner]
        for n in names:
            del self._tools[n]
        if names:
            self._ordered = None
        return len(names)

    # -- 启停 --

    def enable(self, name: str) -> bool:
        tool = self._tools.get(name)
        if tool is None:
            return False
        tool.enabled = True
        self._ordered = None
        return True

    def disable(self, name: str) -> bool:
        tool = self._tools.get(name)
        if tool is None:
            return False
        tool.enabled = False
        self._ordered = None
        return True

    # -- 查询 --

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list(self) -> list[ToolDefinition]:
        return list(self._sorted())

    def bind_permissions(self, service: Any) -> None:
        """绑定权限服务。绑定后 visible_tools 会叠加它的判定。"""
        self._permissions = service
        self._permissions_disabled = False

    def disable_permissions(self) -> None:
        """关掉权限层，退回"只看 ToolDefinition.visibility"。仅供测试。"""
        self._permissions = None
        self._permissions_disabled = True

    def permission_service(self) -> Any:
        """取（并惰性创建）权限服务；未启用时返回 None。

        公开这个入口是为了让 AgentRuntime 能在组装 prompt 前做异步预取 ——
        「机器人是不是群管理员」要网络往返，而 visible_tools 是同步的，
        预取只能由调用方在异步侧完成。
        """
        return self._permission_service()

    def _permission_service(self) -> Any:
        """惰性取权限服务。

        为什么要懒加载而不是在 __init__ 里直接 import：
        `permissions` 依赖 `tools`（ToolDefinition/ToolResult），
        在模块顶层互相 import 会成环。放在方法里 import 就避开了。
        """
        if self._permissions is not None:
            return self._permissions
        if self._permissions_disabled:
            return None
        try:
            from ..permissions import PermissionService

            self._permissions = PermissionService()
        except Exception:  # noqa: BLE001 — 权限层起不来时退回工具自身可见性
            return None
        return self._permissions

    def visible_tools(self, context: Any) -> list[ToolDefinition]:
        """当前上下文下模型可见的工具（按 priority 排序）。

        这是 PromptBuilder 的唯一工具来源——禁止再手写 if 分支。

        **必须经过 PermissionService**：
        `ToolDefinition.visibility` 只回答「这个功能开没开」，
        回答不了「这个人能不能用」。管理员门控（高风险工具）与
        群管理能力（MUTE/CARD 要求机器人本身就是管理员）都在
        PermissionService.visible 里，只在执行期检查是不够的——
        那样工具仍会出现在提示词里，模型会一次次去调、一次次被拒。
        """
        service = self._permission_service()
        if service is None:
            return [t for t in self._sorted() if t.is_visible(context)]
        return [t for t in self._sorted() if service.visible(t, context)]

    def markers(self) -> dict[str, ToolDefinition]:
        """marker 名 → 工具定义（供 MarkerToolAdapter 反查）。"""
        out: dict[str, ToolDefinition] = {}
        for tool in self._sorted():
            if tool.marker and tool.enabled and tool.parser is not None:
                out[tool.marker.upper()] = tool
        return out

    def parseable_tools(self) -> list[ToolDefinition]:
        """有 parser 且启用的工具，按 priority 排序。"""
        return [t for t in self._sorted() if t.enabled and t.parser is not None]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return [t.name for t in self._sorted()]

    def clear(self) -> None:
        self._tools.clear()
        self._ordered = None

    def _sorted(self) -> list[ToolDefinition]:
        if self._ordered is None:
            self._ordered = sorted(
                self._tools.values(), key=lambda t: (t.priority, t.name)
            )
        return self._ordered


# 全局单例（与 core.commands.get_registry 一致的模式）
_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    return _registry


__all__ = [
    "ToolDefinition",
    "ToolRegistry",
    "get_tool_registry",
    "ToolExecutor",
    "MarkerParser",
    "VisibilityPredicate",
]
