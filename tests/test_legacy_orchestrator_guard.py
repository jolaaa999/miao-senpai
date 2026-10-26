"""旧编排（legacy_orchestrator）删除后的行为契约。

背景：2026-10 审计发现 `agent/legacy_orchestrator.py` 从 Phase 6.6 起就没做完
—— 它引用 `LegacyRunState` 却从未 import，`run()` 必然抛错，
因此 `DL_SENPAI_AGENT_MODE=legacy` 从来不是真正的回滚路径，
只是"先抛一次异常、再降级回 runtime"。

处理：连同 `legacy_state.py` / `legacy_context.py`（无任何生产引用）一起删除，
并把 `handlers._handle_chat_inner` 改成**显式抛错**的桩。

本文件守三件事：
  1. 旧实现不再存在（防止有人再塞一个"看起来能回滚"的假入口）；
  2. 配成 legacy 时**不能**让机器人哑掉 —— 必须降级到 runtime 并给出回复；
  3. 启动期的 deprecated warning 仍然存在（配置里写着 legacy 的人要知道）。
"""

from __future__ import annotations

import importlib
import textwrap
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "src" / "plugins" / "dl_senpai" / "agent"


# ================================================================
# 1. 旧实现确实不在了
# ================================================================


@pytest.mark.parametrize(
    "name",
    ["legacy_orchestrator.py", "legacy_state.py", "legacy_context.py"],
)
def test_legacy_modules_are_deleted(name: str) -> None:
    """三个旧模块必须已删除。

    为什么用"文件不存在"而不是"函数不存在"：删除的动机是**没人能维护它**，
    只删函数留下文件，等于把同样的问题换个位置。
    """
    assert not (AGENT / name).exists(), f"{name} 又回来了？旧路径已判定不可维护"


def test_legacy_orchestrator_import_fails() -> None:
    """导入必须失败 —— 有任何代码偷偷 import 它都会当场炸出来。"""
    with pytest.raises(ImportError):
        importlib.import_module("dl_senpai.agent.legacy_orchestrator")


def test_legacy_adapter_is_kept() -> None:
    """`legacy_adapter.py` 要**留着**：它是 ToolCall → ChatResult 的形状转换，

    llm.py 的生产路径仍在用。删旧编排时不要顺手把它一起删掉。
    """
    from dl_senpai.agent.legacy_adapter import LegacyChatResultAdapter

    assert callable(LegacyChatResultAdapter)


# ================================================================
# 2. 桩：明确抛错，而不是悄悄消失
# ================================================================


async def test_handle_chat_inner_raises_actionable_error() -> None:
    """旧入口必须抛**可行动**的错误，而不是 ImportError 或静默 no-op。

    为什么保留这个桩：legacy_bridge 仍把它当 legacy_handler 注入，
    AgentEntry 靠捕获它来走降级分支。让它消失反而更危险
    （会变成"没有 handler"，可能直接不回复）。
    """
    from dl_senpai import handlers

    with pytest.raises(RuntimeError) as excinfo:
        await handlers._handle_chat_inner(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            force_mention=False,
            config=None,  # type: ignore[arg-type]
        )
    message = str(excinfo.value)
    assert "legacy_path_removed" in message
    assert "runtime" in message


def test_handle_chat_inner_stayed_thin() -> None:
    """桩不许重新长胖：它是"旧路径已删除"的声明，不是编排入口。

    用 AST 判定而不是数行数：**函数体必须恰好是一条 raise**。
    数行数会把签名和 docstring 也算进去，改个参数名就误报。
    """
    import ast
    import inspect

    from dl_senpai import handlers

    src = inspect.getsource(handlers._handle_chat_inner)
    # 去掉装饰器缩进影响，整体 dedent 后再解析
    tree = ast.parse(textwrap.dedent(src))
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    body = list(fn.body)
    # docstring 不算逻辑：第一个节点是裸字符串常量时跳过
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    assert len(body) == 1, f"桩里多了别的语句（{len(body)} 条）"
    stmt = body[0]
    assert isinstance(stmt, ast.Raise), "桩必须是 raise，不能是 pass/return"
    assert isinstance(stmt.exc, ast.Call)
    assert getattr(stmt.exc.func, "id", "") == "RuntimeError"


# ================================================================
# 3. 配成 legacy 也不能让机器人哑掉
# ================================================================


@pytest.mark.asyncio
async def test_legacy_mode_degrades_to_runtime(monkeypatch) -> None:
    """真实场景：`DL_SENPAI_AGENT_MODE=legacy` + 抛错的旧入口 → 仍回复。

    这条是删除旧实现之后**最重要的行为保险**。它掉了就意味着
    "配置里残留 legacy 的部署会变成机器人完全不回话"。
    """
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "legacy")

    from dl_senpai import handlers
    from dl_senpai.agent.context import AgentContext, AgentResult
    from dl_senpai.agent.entry import AgentEntry
    from dl_senpai.gateway import MessageContext

    class RT:
        executor = None

        async def run(self, *a: Any, **k: Any) -> Any:
            return AgentResult(text="runtime 兜住了")

    async def legacy_handler(context: Any) -> None:
        # 走的就是那条已经删掉的旧路径
        await handlers._handle_chat_inner(
            None, None, force_mention=False, config=None,  # type: ignore[arg-type]
        )

    msg = MessageContext(
        message_type="group", group_id="900001", user_id="10001",
        session_id="group:900001", plain_text="你好",
    )
    ctx = AgentContext(session_id="group:900001", message_context=msg)
    outcome = await AgentEntry(runtime=RT(), legacy_handler=legacy_handler).handle(ctx)

    # 没有把异常抛给上层，而是降级到 runtime 并产出回复
    assert outcome.used_runtime is True
    assert outcome.result.text == "runtime 兜住了"
    assert outcome.mode == "legacy"  # 模式照实记录，便于线上查"谁配错了"


# ================================================================
# 4. 启动期仍要提醒
# ================================================================


def test_deprecation_warning_still_fires() -> None:
    """配成 legacy 时启动必须 warning，并且**说清实际跑的是 runtime**。"""
    from dl_senpai.agent.mode import AgentExecutionMode, deprecation_warning, is_deprecated_mode

    assert is_deprecated_mode(AgentExecutionMode.LEGACY) is True
    warning = deprecation_warning(AgentExecutionMode.LEGACY)
    assert "deprecated" in warning
    assert "runtime" in warning
    # 非 legacy 模式不得有 warning
    assert deprecation_warning(AgentExecutionMode.RUNTIME) == ""
