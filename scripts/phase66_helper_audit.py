"""Phase 6.6 任务（十四/十五）：9 个共享 helper 的归属审计。

第十五节要求输出表格：
    Helper | Current location | Used by handlers | Used by legacy | Platform-specific? | New destination

第十六节要求分类：
    A. Platform helpers   → platform/ gateway/ services/
    B. Shared domain      → shared/ core/ services/
    C. Legacy-only        → agent/legacy/
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"
HANDLERS = PKG / "handlers.py"
LEGACY = PKG / "agent" / "legacy_orchestrator.py"

HELPERS = [
    "_affection_scope",
    "_collect_stickers",
    "_fe",
    "_finalize_affection_gain",
    "_is_to_me",
    "_plain_text",
    "_reply",
    "_resolve_session",
    "_sender_name",
]


def func_body_lines(path: Path, name: str) -> tuple[int, int, int] | None:
    """返回该函数的 (起, 止, 行数)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node.lineno, node.end_lineno or node.lineno, (node.end_lineno or node.lineno) - node.lineno + 1
    return None


def usage_outside_def(path: Path, name: str) -> list[tuple[str, int]]:
    """找出 name 在哪些**函数**里被使用（排除定义本身）。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: list[tuple[str, int]] = []

    def walk(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
            else:
                if isinstance(child, ast.Name) and child.id == name:
                    out.append((current, child.lineno))
                elif isinstance(child, ast.Attribute) and child.attr == name:
                    out.append((current, child.lineno))
                walk(child, current)

    walk(tree, "<module>")
    return out


def describe_helper(path: Path, name: str) -> str:
    """给 helper 一句话描述（取 docstring 首行或源码特征）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            doc = ast.get_docstring(node)
            if doc:
                return doc.strip().splitlines()[0][:60]
            # 无 docstring：看签名与首行
            if isinstance(node, ast.AsyncFunctionDef):
                return "async 函数（无 docstring）"
            return "同步函数（无 docstring）"
    return ""


def main() -> int:
    print("=" * 112)
    print("9 个共享 helper 归属审计")
    print("=" * 112)

    rows = []
    for name in HELPERS:
        info = func_body_lines(HANDLERS, name)
        if info is None:
            rows.append((name, None, [], []))
            continue
        start, end, size = info
        # handlers 内使用（排除定义所在行区间）
        h_use = [
            (fn, ln) for fn, ln in usage_outside_def(HANDLERS, name)
            if not (start <= ln <= end)
        ]
        l_use = usage_outside_def(LEGACY, name)
        # legacy 通过描述符访问，属性名会出现在 ClassDef 里
        rows.append((name, (start, end, size), h_use, l_use))

    print(f"\n{'Helper':<30}{'LOC':<6}{'handlers 用':<12}{'legacy 用':<10}说明")
    print("-" * 112)
    for name, info, h_use, l_use in rows:
        size = info[2] if info else 0
        hn = len({fn for fn, _ in h_use})
        ln = len({fn for fn, _ in l_use})
        print(f"{name:<30}{size:<6}{hn:<12}{ln:<10}{describe_helper(HANDLERS, name)}")

    print("\n" + "=" * 112)
    print("handlers 内的使用分布（判断是否真的共享）")
    print("=" * 112)
    for name, info, h_use, l_use in rows:
        fns = sorted({fn for fn, _ in h_use})
        print(f"\n  {name}:")
        print(f"      handlers 内: {fns if fns else '（无）'}")
        lfns = sorted({fn for fn, _ in l_use})
        print(f"      legacy 内  : {lfns if lfns else '（无）'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
