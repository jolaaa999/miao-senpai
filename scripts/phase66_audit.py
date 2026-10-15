"""Phase 6.6 任务（三/四）：LegacyOrchestrator.run() 职责审计。

第三/四节要求：修改前先完整分析，输出内部阶段图与职责地图表。
**不得先写代码**，也**不得机械切成 10 个函数**。

本脚本输出：
    1. run() 的语句级结构（顶层语句 + 嵌套块边界）
    2. 每个语句区间的：行范围 / 类型 / 关键调用 / 局部变量写入
    3. 分支数与嵌套深度（第六十三节要求的复杂度指标）
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "src" / "plugins" / "dl_senpai" / "agent" / "legacy_orchestrator.py"


def get_run() -> ast.AsyncFunctionDef:
    tree = ast.parse(LEGACY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LegacyOrchestrator":
            for m in node.body:
                if isinstance(m, ast.AsyncFunctionDef) and m.name == "run":
                    return m
    raise SystemExit("找不到 LegacyOrchestrator.run")


def branch_count(fn: ast.AST) -> int:
    """统计分支语句数（if/for/while/except/with/match 分支）。"""
    n = 0
    for node in ast.walk(fn):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
            n += 1
        elif isinstance(node, ast.BoolOp):
            n += len(node.values) - 1
        elif isinstance(node, ast.IfExp):
            n += 1
    return n


def max_depth(fn: ast.AST, depth: int = 0) -> int:
    """最大嵌套深度。"""
    best = depth
    for child in ast.iter_child_nodes(fn):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.Try, ast.AsyncWith)):
            best = max(best, max_depth(child, depth + 1))
        else:
            best = max(best, max_depth(child, depth))
    return best


def calls_in(node: ast.AST) -> list[str]:
    """该节点内的函数调用名（去重保序）。"""
    out: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                out.append(f.id)
            elif isinstance(f, ast.Attribute):
                out.append(f.attr)
    seen: set[str] = set()
    uniq: list[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def assigns_in(node: ast.AST) -> list[str]:
    """该节点顶层写入的变量名（只统计直接子语句，不含更深块）。"""
    out: list[str] = []
    for sub in node.body if hasattr(node, "body") else []:
        if isinstance(sub, ast.Assign):
            for t in sub.targets:
                if isinstance(t, ast.Name):
                    out.append(t.id)
        elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
            out.append(sub.target.id)
        elif isinstance(sub, ast.AugAssign) and isinstance(sub.target, ast.Name):
            out.append(sub.target.id)
    return out


def describe(stmt: ast.stmt) -> str:
    """给语句一个简短描述。"""
    if isinstance(stmt, ast.If):
        try:
            return f"if {ast.unparse(stmt.test)[:70]}"
        except Exception:  # noqa: BLE001
            return "if ..."
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return f"for {ast.unparse(stmt.target)}"
    if isinstance(stmt, ast.While):
        return "while ..."
    if isinstance(stmt, ast.Try):
        return "try/except"
    if isinstance(stmt, ast.Assign):
        try:
            return f"= {ast.unparse(stmt.targets[0])[:60]}"
        except Exception:  # noqa: BLE001
            return "= ..."
    if isinstance(stmt, ast.Expr):
        try:
            return ast.unparse(stmt)[:80]
        except Exception:  # noqa: BLE001
            return "<expr>"
    if isinstance(stmt, (ast.Return,)):
        return "return"
    return type(stmt).__name__


def main() -> int:
    fn = get_run()
    print("=" * 108)
    print(f"LegacyOrchestrator.run  L{fn.lineno}-{fn.end_lineno}  "
          f"({fn.end_lineno - fn.lineno + 1} 行)")
    print(f"分支数(branch_count) = {branch_count(fn)}   "
          f"最大嵌套深度 = {max_depth(fn)}   "
          f"顶层语句数 = {len(fn.body)}")
    print("=" * 108)

    print(f"\n{'行范围':<16}{'类型':<12}{'写入变量':<28}描述")
    print("-" * 108)
    for stmt in fn.body:
        start = stmt.lineno
        end = stmt.end_lineno or stmt.lineno
        rng = f"{start}-{end}"
        assigns = ",".join(assigns_in(stmt))[:26]
        print(f"{rng:<16}{type(stmt).__name__:<12}{assigns:<28}{describe(stmt)}")

    print("\n" + "=" * 108)
    print("每个顶层语句块内的关键调用（用于判断职责）")
    print("=" * 108)
    for stmt in fn.body:
        start = stmt.lineno
        end = stmt.end_lineno or stmt.lineno
        cs = calls_in(stmt)
        if not cs:
            continue
        interesting = [
            c for c in cs
            if not c.startswith(("_", "len", "str", "int", "bool", "list", "dict", "set"))
        ]
        if not interesting:
            continue
        size = end - start + 1
        if size < 3:
            continue
        print(f"\n  L{start}-{end} ({size} 行):")
        print(f"      {', '.join(interesting[:14])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
