"""Phase 6.5 任务（一）：handlers.py 完整审计。

输出 top-level function / class 分类表：
    A. QQ / NoneBot 平台 helper
    B. Router / gateway 兼容
    C. Legacy orchestration
    D. 共享纯 helper
    E. 死代码

并标注：Runtime-used? / Legacy-only? / Platform-only? / Can move?
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"
HANDLERS = PKG / "handlers.py"


def top_level_defs(tree: ast.Module) -> list[tuple[str, str, int, int]]:
    """返回 (kind, name, lineno, end_lineno)。"""
    out: list[tuple[str, str, int, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(("func", node.name, node.lineno, node.end_lineno or node.lineno))
        elif isinstance(node, ast.ClassDef):
            out.append(("class", node.name, node.lineno, node.end_lineno or node.lineno))
    return out


def call_graph(tree: ast.Module) -> dict[str, set[str]]:
    """顶层函数 → 它调用的其它顶层函数名。"""
    defined = {name for _kind, name, _s, _e in top_level_defs(tree)}
    graph: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in defined:
                called.add(sub.id)
            if isinstance(sub, ast.Attribute) and sub.attr in defined:
                called.add(sub.attr)
        called.discard(node.name)
        graph[node.name] = called
    return graph


def external_refs(tree: ast.Module) -> set[str]:
    """收集所有 import 的模块名。"""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


# 分类关键词（按函数名与它调用/引用的模块推断）
PLATFORM_TOKENS = (
    "reply", "segment", "event", "at_", "_at", "mention", "extract_at",
    "is_private", "group_id", "user_id", "bot", "session_id_for",
)
SHARED_PURE_TOKENS = (
    "polish", "truncate", "clean", "normalize", "format_", "_plain_text",
    "strip_", "cooldown", "build_nickname",
)


def classify(
    name: str, source: str, refs: set[str], called: set[str], called_by: dict[str, list[str]]
) -> tuple[str, str, str]:
    """返回 (category, runtime_used, legacy_only)。

    category: A_platform / B_router_compat / C_legacy_orchestration /
              D_shared_pure / E_dead / Z_unknown
    """
    # E. 死代码：没有任何顶层函数调用它，且不在导出/装饰器里
    is_dead = not called_by.get(name) and not name.startswith("__")

    # C. legacy orchestration：chat 流程核心
    if name in {"_handle_chat", "_handle_chat_inner"} or name.startswith("_chat_"):
        return "C_legacy_orchestration", "no", "yes"

    # A. 平台 helper
    if any(tok in name for tok in PLATFORM_TOKENS):
        return "A_platform", "no", "shared"

    # D. 共享纯函数
    if any(tok in name for tok in SHARED_PURE_TOKENS):
        return "D_shared_pure", "no", "shared"

    # B. router 兼容
    if name.startswith("_build") or name in {"handle_agent_message"}:
        return "B_router_compat", "partial", "no"

    return ("E_dead" if is_dead else "Z_unknown"), "?", "?"


def main() -> int:
    text = HANDLERS.read_text(encoding="utf-8")
    tree = ast.parse(text)
    defs = top_level_defs(tree)
    graph = call_graph(tree)

    # 反向调用图
    called_by: dict[str, list[str]] = {}
    for caller, callees in graph.items():
        for callee in callees:
            called_by.setdefault(callee, []).append(caller)

    print("=" * 100)
    print(f"handlers.py 审计：{len(text.splitlines())} 行，{len(defs)} 个顶层定义")
    print("=" * 100)

    rows = []
    for kind, name, start, end in defs:
        called = graph.get(name, set())
        cat, rt, lo = classify(name, text, external_refs(tree), called, called_by)
        rows.append((cat, kind, name, start, end, end - start + 1, len(called_by.get(name, []))))

    # 按分类输出
    for cat_label, cat_key in [
        ("C. Legacy orchestration", "C_legacy_orchestration"),
        ("A. 平台 helper", "A_platform"),
        ("B. Router / gateway 兼容", "B_router_compat"),
        ("D. 共享纯 helper", "D_shared_pure"),
        ("E. 无调用者（候选死代码）", "E_dead"),
        ("Z. 未分类", "Z_unknown"),
    ]:
        subset = [r for r in rows if r[0] == cat_key]
        if not subset:
            continue
        total = sum(r[5] for r in subset)
        print(f"\n--- {cat_label}  ({len(subset)} 个, {total} 行) ---")
        for _c, kind, name, start, end, size, ncallers in sorted(subset, key=lambda r: -r[5]):
            print(f"    {size:5} 行  L{start:<5}-{end:<5} {kind:9} {name:44} 被调用 {ncallers} 次")

    print("\n" + "=" * 100)
    print("外部依赖（import 的模块）")
    print("=" * 100)
    for ref in sorted(external_refs(tree)):
        print(f"    {ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
