"""Phase 6.5：把 _handle_chat_inner 从 handlers.py 抽取到 agent/legacy/orchestrator.py。

第五/七节要求：必须 MOVE 不能 COPY。
第一/二节要求：Move first，不改行为。

策略（保证"不改行为"）：
    1. 用 AST 精确定位 _handle_chat_inner 的完整源码
    2. 原样搬运到新模块，**一个字符都不改**函数体
    3. 它依赖的模块级名字（helper / 常量）通过显式 import 引入
    4. handlers.py 里留下极薄 forwarding wrapper

为什么"原样搬运"而不是"顺手重构"：
    777 行同时做搬运+重构会导致 1323 个测试的失败原因无法归因。
    Phase 6.5 第二节明确禁止这样做。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"
HANDLERS = PKG / "handlers.py"
TARGET_FUNC = "_handle_chat_inner"


def find_function(src: str) -> tuple[int, int]:
    """返回目标函数的 (起始行, 结束行)，1-based 且含首尾。"""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == TARGET_FUNC:
            return node.lineno, node.end_lineno or node.lineno
    raise SystemExit(f"找不到 {TARGET_FUNC}")


def collect_free_names(func_src: str) -> set[str]:
    """收集函数体内引用但不在此定义的**全局名字**（helper / 常量）。"""
    tree = ast.parse(func_src)
    fn = tree.body[0]

    # 函数内定义的名字（局部变量、参数、嵌套 def）
    local: set[str] = set()
    for arg in (
        list(getattr(fn.args, "posonlyargs", []))
        + list(fn.args.args)
        + list(fn.args.kwonlyargs)
    ):
        local.add(arg.arg)
    if fn.args.vararg:
        local.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        local.add(fn.args.kwarg.arg)

    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            local.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local.add(node.name)
        elif isinstance(node, ast.ClassDef):
            local.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                local.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.comprehension,)):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    local.add(sub.id)

    # 引用的全局名
    used: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)

    import builtins

    return used - local - set(dir(builtins))


def module_level_names(src: str) -> set[str]:
    """handlers.py 里定义的模块级名字（函数/类/赋值/import）。"""
    tree = ast.parse(src)
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
    return out


def main() -> int:
    src = HANDLERS.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    start, end = find_function(src)
    print(f"{TARGET_FUNC}: L{start}-{end}  ({end - start + 1} 行)")

    func_lines = lines[start - 1 : end]
    # 保持原缩进（顶层 async def，无缩进）
    func_src = "".join(func_lines)

    free = collect_free_names(func_src)
    module_names = module_level_names(src)
    # 只关心 handlers 内定义的（其它是 import 进来的，需要单独处理）
    handlers_defined = sorted(free & module_names)
    external = sorted(free - module_names)

    print(f"\n引用的 handlers 内定义名（{len(handlers_defined)} 个）：")
    for name in handlers_defined:
        print(f"    {name}")
    print(f"\n引用的外部名（import 或内置，{len(external)} 个）：")
    for name in external:
        print(f"    {name}")

    # 落盘供下一步使用
    out_dir = ROOT / "scripts" / "_phase65_work"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "handle_chat_inner.py.txt").write_text(func_src, encoding="utf-8")
    (out_dir / "handlers_defined.txt").write_text(
        "\n".join(handlers_defined), encoding="utf-8"
    )
    (out_dir / "external.txt").write_text("\n".join(external), encoding="utf-8")
    print(f"\n已导出到 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
