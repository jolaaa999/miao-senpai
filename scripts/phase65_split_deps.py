"""Phase 6.5：精确区分"handlers 内定义"与"import 进来"的名字。

上一个脚本把 import 进来的名字也算进 handlers_defined，
导致依赖清单虚高。这里用"是否有 def/class/赋值节点"来精确判定。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "src" / "plugins" / "dl_senpai" / "handlers.py"
WORK = ROOT / "scripts" / "_phase65_work"


def main() -> int:
    src = HANDLERS.read_text(encoding="utf-8")
    tree = ast.parse(src)

    defined_funcs: dict[str, int] = {}
    defined_assigns: dict[str, int] = {}
    imported: dict[str, str] = {}

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined_funcs[node.name] = node.lineno
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined_assigns[t.id] = node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined_assigns[node.target.id] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported[a.asname or a.name] = node.module or ""
        elif isinstance(node, ast.Import):
            for a in node.names:
                imported[a.asname or a.name.split(".")[0]] = a.name

    used = set(
        (WORK / "handlers_defined.txt").read_text(encoding="utf-8").split()
    )

    print("=" * 88)
    print("A. handlers 内定义的函数/类（若被用则必须一起搬走）")
    print("=" * 88)
    must_move_funcs = sorted(n for n in used if n in defined_funcs)
    for name in must_move_funcs:
        print(f"    L{defined_funcs[name]:<6} {name}")

    print("\n" + "=" * 88)
    print("B. handlers 内定义的模块级变量（冷却器 / 单例）")
    print("=" * 88)
    must_move_vars = sorted(n for n in used if n in defined_assigns)
    for name in must_move_vars:
        print(f"    L{defined_assigns[name]:<6} {name}")

    print("\n" + "=" * 88)
    print("C. import 进来的名字（新模块自己 import 即可，不用搬）")
    print("=" * 88)
    from_imports = sorted(
        (n, imported[n]) for n in used if n in imported
    )
    for name, mod in from_imports:
        print(f"    {name:38} <- {mod}")

    print("\n" + "=" * 88)
    print("D. 未解析（内置或第三方直接引用）")
    print("=" * 88)
    rest = sorted(
        n for n in used if n not in defined_funcs and n not in defined_assigns
        and n not in imported
    )
    print(f"    {rest}")

    print("\n" + "=" * 88)
    print(f"汇总：需搬函数 {len(must_move_funcs)} 个，变量 {len(must_move_vars)} 个，"
          f"import {len(from_imports)} 个")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
