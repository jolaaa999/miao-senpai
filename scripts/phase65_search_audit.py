"""Phase 6.5 任务（八）：Search Audit —— 分析 llm.py 的 searcher_from_config 调用。

第十四/十五/十六/五十二节要求：必须真正分析，不能直接删除，
必须给**代码行为证据**，最终结论只能是 YES / NO / MIXED。

判断标准（第十五节）：
    如果逻辑是"在模型首次响应前根据 heuristic 决定是否预搜，
    然后把结果直接作为 context 给模型" → Model orchestration strategy（可保留）

    如果逻辑是"模型产出 SEARCH intent → 执行搜索 → 回填"
    → Tool execution（必须走 ToolExecutor）

本脚本提取每处调用的真实上下文，供人工核对。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"


def analyze(path: Path) -> list[dict]:
    """找出文件中所有 searcher_from_config 调用及其控制流证据。

    正确做法：从**最内层**函数往上找，而不是对每个函数向下 walk
    （后者会让每个函数都"包含"该调用）。
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()

    # 建 父节点 映射，用于向上查找
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def enclosing_function(node: ast.AST) -> str:
        cur = parents.get(id(node))
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur.name
            cur = parents.get(id(cur))
        return "<module>"

    def enclosing_loop(node: ast.AST) -> str:
        cur = parents.get(id(node))
        while cur is not None:
            if isinstance(cur, (ast.For, ast.While)):
                if isinstance(cur, ast.For) and isinstance(cur.target, ast.Name):
                    return f"for {cur.target.id} (L{cur.lineno})"
                return f"{type(cur).__name__.lower()} (L{cur.lineno})"
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break  # 不跨函数边界
            cur = parents.get(id(cur))
        return ""

    def enclosing_ifs(node: ast.AST) -> list[str]:
        out: list[str] = []
        cur = parents.get(id(node))
        while cur is not None:
            if isinstance(cur, ast.If):
                try:
                    out.append(ast.unparse(cur.test))
                except Exception:  # noqa: BLE001
                    out.append("<unparse failed>")
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break
            cur = parents.get(id(cur))
        return list(reversed(out))

    out: list[dict] = []
    for node in ast.walk(tree):
        is_call = False
        if isinstance(node, ast.Name) and node.id == "searcher_from_config":
            is_call = True
        elif isinstance(node, ast.Attribute) and node.attr == "searcher_from_config":
            is_call = True
        if not is_call:
            continue
        out.append(
            {
                "file": path.name,
                "function": enclosing_function(node),
                "line": node.lineno,
                "in_loop": enclosing_loop(node),
                "conditions": enclosing_ifs(node),
                "source": lines[node.lineno - 1].strip(),
            }
        )
    return out


def main() -> int:
    print("=" * 92)
    print("Search Audit: searcher_from_config 调用点")
    print("=" * 92)

    for rel in ("llm.py", "web_search.py", "handlers.py"):
        path = PKG / rel
        if not path.exists():
            continue
        hits = analyze(path)
        if not hits:
            continue
        print(f"\n### {rel}")
        for h in sorted(hits, key=lambda h: h["line"]):
            print(f"\n  L{h['line']}  在函数 {h['function']}() 内")
            print(f"      源码      : {h['source']}")
            print(f"      在循环内  : {h['in_loop'] or '否'}")
            for cond in h["conditions"]:
                print(f"      外层条件  : {cond}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
