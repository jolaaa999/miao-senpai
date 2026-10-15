"""Phase 6A 静态检查：Parser 与 Executor 的单一来源验证。

第十八/十九节的强验收：
    Marker Parser implementation count = 1
    Tool production executor count = 8（每工具一个）

本脚本扫描生产代码，列出所有 parse_* 的直接调用者与 executor 定义，
用于生成 Convergence Checkpoint 报告。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"

# 8 个 marker 的解析函数
PARSERS = {
    "parse_card_actions",
    "parse_mute_actions",
    "parse_draw_request",
    "parse_browse_request",
    "parse_sticker_request",
    "parse_voice_request",
    "parse_impression_updates",
    "parse_search_request",
}

# 允许直接调用 parser 的模块（ToolDefinition / Marker adapter / 形状转换层）
ALLOWED_PARSER_CALLERS = {
    "agent/tools/builtin.py",     # ToolDefinition.parser
    "agent/tools/marker_adapter.py",  # 虽不直接调用，但持有委托逻辑
    "web_search.py",              # parse_search_request 内部递归/自用
    "group_card.py",              # 定义处
    "group_mute.py",
    "image_gen.py",
    "browser_agent.py",
    "stickers.py",
    "voice.py",
    "person_memory.py",
}

EXECUTOR_NAMES = {
    "card": "execute_card",
    "mute": "execute_mute",
    "draw": "execute_draw",
    "browse": "execute_browse",
    "sticker": "execute_sticker",
    "voice": "execute_voice",
    "impression": "execute_impression",
    "search": "execute_search",
}


def scan_parser_callers() -> dict[str, list[str]]:
    """找出每个 parser 的直接调用者（不含定义处）。"""
    out: dict[str, list[str]] = {}
    for path in PKG.rglob("*.py"):
        rel = str(path.relative_to(PKG)).replace("\\", "/")
        if rel in ALLOWED_PARSER_CALLERS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in PARSERS:
                out.setdefault(node.id, []).append(f"{rel}:{node.lineno}")
    return out


def scan_executor_definitions() -> dict[str, list[str]]:
    """找出每个 executor 的定义位置。"""
    out: dict[str, list[str]] = {}
    for path in PKG.rglob("*.py"):
        rel = str(path.relative_to(PKG)).replace("\\", "/")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in EXECUTOR_NAMES.values():
                    out.setdefault(node.name, []).append(f"{rel}:{node.lineno}")
    return out


def scan_legacy_tool_dispatch() -> dict[str, list[str]]:
    """找出 legacy orchestration 里直接调用业务执行函数的位置。"""
    legacy_targets = {
        "apply_card_actions": "card",
        "apply_mute_actions": "mute",
        "build_image_segment": "draw",
        "run_browse_tasks": "browse",
        "build_voice_segment": "voice",
        "maybe_pick_sticker_reply": "sticker",
        "searcher_from_config": "search",
    }
    out: dict[str, list[str]] = {}
    for path in PKG.rglob("*.py"):
        rel = str(path.relative_to(PKG)).replace("\\", "/")
        if rel == "agent/tools/executors.py":
            continue  # 新实现在这里，正是我们要的
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in legacy_targets:
                out.setdefault(legacy_targets[node.id], []).append(
                    f"{rel}:{node.lineno}"
                )
    return out


def main() -> int:
    print("=" * 62)
    print("Phase 6A Convergence Checkpoint — 静态检查")
    print("=" * 62)

    print("\n[1] Marker Parser 直接调用者（应只在新架构层）")
    callers = scan_parser_callers()
    if not callers:
        print("    无（全部经由 ToolDefinition / MarkerToolAdapter） OK")
    else:
        for name, sites in sorted(callers.items()):
            print(f"    {name}:")
            for s in sites:
                print(f"        {s}")

    print("\n[2] Tool Executor 定义（每个工具应恰好 1 处）")
    defs = scan_executor_definitions()
    for tool, fn in EXECUTOR_NAMES.items():
        sites = defs.get(fn, [])
        status = "OK" if len(sites) == 1 else f"COUNT={len(sites)}"
        print(f"    {tool:11} {fn:20} {status:8} {sites}")

    print("\n[3] Legacy tool dispatch 残留（业务执行函数的直接调用）")
    legacy = scan_legacy_tool_dispatch()
    if not legacy:
        print("    无残留 OK")
    else:
        total = 0
        for tool, sites in sorted(legacy.items()):
            print(f"    {tool}: {len(sites)} 处")
            for s in sites:
                print(f"        {s}")
            total += len(sites)
        print(f"    合计 {total} 处")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
