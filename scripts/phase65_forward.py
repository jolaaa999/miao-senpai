"""Phase 6.5：把 handlers._handle_chat_inner 换成极薄 forwarding wrapper。

第七/八节要求：
    迁移完成后旧函数必须
      - 删除，或
      - 变成极薄 forwarding wrapper（只做 argument adaptation）

    禁止保留 prompt / LLM / intent / tool / memory 逻辑。

本脚本：
    1. 删除 handlers.py 里 _handle_chat_inner 的 736 行函数体
    2. 替换为 10 行左右的 forwarder
    3. 删除因搬迁而变成死代码的 3 个 helper
       （_dispatch_legacy_tool_calls / _first_values / _get_memory_facade）
       以及 6 个冷却器实例
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"
HANDLERS = PKG / "handlers.py"

FORWARDER = '''async def _handle_chat_inner(
    bot: Bot,
    event: MessageEvent,
    *,
    force_mention: bool,
    config: PluginConfig,
) -> None:
    """**Forwarding wrapper**（Phase 6.5 第八节）。

    真实实现已搬到 `agent/legacy_orchestrator.py` 的
    `LegacyOrchestrator.run()`。本函数只做 argument 适配。

    保留它的原因：兼容层与测试都按这个名字引用
    （例如 test_phase55_acceptance 的 monkeypatch）。

    禁止在这里重新加入：
        prompt 组装 / LLM 调用 / intent 判定 / tool 逻辑 / memory 逻辑
    """
    from .agent.legacy_orchestrator import LegacyOrchestrator

    await LegacyOrchestrator().run(
        bot,
        event,
        force_mention=force_mention,
        config=config,
    )
'''


def find_function(src: str, name: str) -> tuple[int, int]:
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node.lineno, node.end_lineno or node.lineno
    raise SystemExit(f"找不到 {name}")


def remove_top_level_function(src: str, name: str) -> str:
    """删除一个顶层函数（含其后的空行）。"""
    lines = src.splitlines(keepends=True)
    start, end = find_function(src, name)
    del lines[start - 1 : end]
    return "".join(lines)


def main() -> int:
    src = HANDLERS.read_text(encoding="utf-8")
    before = len(src.splitlines())

    # 1) 替换 _handle_chat_inner
    lines = src.splitlines(keepends=True)
    start, end = find_function(src, "_handle_chat_inner")
    removed = end - start + 1
    lines[start - 1 : end] = [FORWARDER]
    src = "".join(lines)
    print(f"替换 _handle_chat_inner: 删除 {removed} 行，写入 {len(FORWARDER.splitlines())} 行")

    # 2) 删除已搬走的 3 个 helper
    for helper in (
        "_dispatch_legacy_tool_calls",
        "_first_values",
        "_get_memory_facade",
    ):
        try:
            src = remove_top_level_function(src, helper)
            print(f"删除 helper: {helper}")
        except SystemExit:
            print(f"跳过（不存在）: {helper}")

    HANDLERS.write_text(src, encoding="utf-8")
    after = len(src.splitlines())
    print(f"\nhandlers.py: {before} -> {after} 行（净减 {before - after}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
