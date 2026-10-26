"""共享 Tool Dispatch：legacy 与 runtime 共用的工具执行入口。

Phase 6A 第九/十/十二节。**禁止创建 LegacyToolExecutor** ——
legacy 必须调用与 RUNTIME 完全相同的 `ToolExecutor.execute()`。

本模块解决一个具体的工程问题：legacy 手里有 `ChatResult` 的 8 个字段
（已由 `LegacyChatResultAdapter` 从 ToolCall 转回来），
它需要把这些字段"送回" ToolExecutor 执行。

    legacy: result.card_actions  (旧形状)
            ↓ _tool_calls_from_card_actions()
    ToolCall (统一形状)
            ↓
    SharedToolDispatcher.dispatch()
            ↓ ToolExecutor.execute()      ← 与 RUNTIME 同一个实例
    ToolResult

这样权限、幂等、journal、观测全部自动共享，
legacy **不需要**再调 `apply_card_actions` / `apply_mute_actions`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .tools.models import ToolCall, ToolCallSource, ToolResult
from .tools.registry import get_tool_registry


@dataclass
class DispatchOutcome:
    """一次 legacy dispatch 的结果汇总。"""

    results: list[ToolResult] = field(default_factory=list)
    # 按工具名分组，便于 legacy 读取（如 applied_cards）
    by_tool: dict[str, list[ToolResult]] = field(default_factory=dict)

    def add(self, result: ToolResult) -> None:
        self.results.append(result)
        self.by_tool.setdefault(result.tool_name, []).append(result)

    @property
    def any_success(self) -> bool:
        return any(r.success for r in self.results)

    def successful(self, tool_name: str) -> list[ToolResult]:
        return [r for r in self.by_tool.get(tool_name, []) if r.success]

    def first_value(self, tool_name: str, default: Any = None) -> Any:
        for r in self.by_tool.get(tool_name, []):
            if r.success:
                return r.data.get("value", default)
        return default


class SharedToolDispatcher:
    """把 legacy 的旧形状转成 ToolCall 并交给共享 ToolExecutor。

    Args:
        executor: 共享 `ToolExecutor`（通常来自 Runtime）。
        journal: 可选 `ExecutionJournal`（幂等与 fallback 安全）。
    """

    def __init__(self, executor: Any = None, journal: Any = None) -> None:
        self._executor = executor
        self._journal = journal

    # -- executor 解析（懒加载）--

    def _get_executor(self) -> Any:
        if self._executor is not None:
            return self._executor
        from .tools.executor import ToolExecutor

        registry = get_tool_registry()
        if len(registry) == 0:
            from .tools.builtin import register_production_tools

            register_production_tools(registry)
        # 生成层（Word/PPT）单独注册：可见性对所有人开放，
        # 与「8 个内置工具」的数量契约解耦。
        from .tools.creator_tools import register_creator_tools

        register_creator_tools(registry)
        # 记心事工具也在这里补一次：本分发器可能在 Runtime 之前被调用
        # （legacy 形状的 card/sticker 分发），那时注册表里还没有它。
        from .tools.note_tool import register_note_tool
        from .tools.diary_tool import register_diary_tool

        register_note_tool(registry)
        register_diary_tool(registry)

        from .permissions import PermissionService

        self._executor = ToolExecutor(
            registry,
            permissions=PermissionService(),
            journal=self._journal,
        )
        return self._executor

    def set_journal(self, journal: Any) -> None:
        self._journal = journal
        executor = self._get_executor()
        if hasattr(executor, "set_journal"):
            executor.set_journal(journal)

    # -- 主入口 --

    async def dispatch(
        self,
        calls: list[ToolCall],
        context: Any,
        *,
        dry_run: bool = False,
    ) -> DispatchOutcome:
        """顺序执行一组 ToolCall（turn 内不并行，见第五十二节）。"""
        outcome = DispatchOutcome()
        executor = self._get_executor()
        for call in calls:
            # 幂等：同一 tool_call_id 只执行一次
            if self._journal is not None and self._journal.already_executed(call.id):
                continue
            result = await executor.execute(call, context, dry_run=dry_run)
            outcome.add(result)
        return outcome

    async def dispatch_one(
        self, call: ToolCall, context: Any, *, dry_run: bool = False
    ) -> ToolResult:
        executor = self._get_executor()
        return await executor.execute(call, context, dry_run=dry_run)


# ================================================================
# 旧形状 → ToolCall 转换
# ================================================================


def tool_calls_from_card_actions(actions: list[Any]) -> list[ToolCall]:
    """`ChatResult.card_actions` → `list[ToolCall]`。"""
    return [
        ToolCall(
            tool_name="card",
            arguments={
                "user_id": getattr(a, "user_id", None),
                "card": getattr(a, "card", ""),
                "kind": getattr(a, "kind", "request"),
            },
            source=ToolCallSource.INTERNAL,
            marker="CARD",
        )
        for a in actions or []
    ]


def tool_calls_from_mute_actions(actions: list[Any]) -> list[ToolCall]:
    return [
        ToolCall(
            tool_name="mute",
            arguments={
                "user_id": getattr(a, "user_id", None),
                "duration": getattr(a, "duration", 60),
                "kind": getattr(a, "kind", "request"),
            },
            source=ToolCallSource.INTERNAL,
            marker="MUTE",
        )
        for a in actions or []
    ]


def tool_calls_from_impressions(updates: list[Any]) -> list[ToolCall]:
    return [
        ToolCall(
            tool_name="impression",
            arguments={
                "add": list(getattr(u, "add", []) or []),
                "remove": list(getattr(u, "remove", []) or []),
                "notes": getattr(u, "notes", None),
            },
            source=ToolCallSource.INTERNAL,
            marker="IMPRESSION",
        )
        for u in updates or []
    ]


def tool_call_for_sticker(query: str, *, force: bool) -> ToolCall | None:
    if not force:
        return None
    return ToolCall(
        tool_name="sticker",
        arguments={"query": query or ""},
        source=ToolCallSource.INTERNAL,
        marker="STICKER",
    )


def tool_call_for_voice(*, force: bool, override: str) -> ToolCall | None:
    if not force:
        return None
    return ToolCall(
        tool_name="voice",
        arguments={"override": override or ""},
        source=ToolCallSource.INTERNAL,
        marker="VOICE",
    )


def tool_call_for_draw(prompt: str, *, force: bool) -> ToolCall | None:
    if not force:
        return None
    return ToolCall(
        tool_name="draw",
        arguments={"prompt": prompt or "", "force": True},
        source=ToolCallSource.INTERNAL,
        marker="DRAW",
    )


def tool_call_for_browse(tasks: list[Any], *, force: bool) -> ToolCall | None:
    if not force:
        return None
    return ToolCall(
        tool_name="browse",
        arguments={"tasks": list(tasks or [])},
        source=ToolCallSource.INTERNAL,
        marker="BROWSE",
    )


def tool_call_for_search(*, searched: bool, queries: list[str] | None = None) -> ToolCall | None:
    """检索已经由 llm 循环执行过，这里只用于记录/幂等。

    返回 None 表示"不需要再执行"——因为 search 与其它工具不同，
    它在 `llm.chat` 的多轮循环里已经执行完了。
    """
    return None


__all__ = [
    "SharedToolDispatcher",
    "DispatchOutcome",
    "tool_calls_from_card_actions",
    "tool_calls_from_mute_actions",
    "tool_calls_from_impressions",
    "tool_call_for_sticker",
    "tool_call_for_voice",
    "tool_call_for_draw",
    "tool_call_for_browse",
    "tool_call_for_search",
]
