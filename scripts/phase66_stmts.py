"""Phase 6.6：LegacyOrchestrator.run() 语义阶段拆分。

策略（在 778 行规模下最安全）：
    1. 用 AST 定位 run() 的每个顶层语句
    2. 按预定义边界把顶层语句分给 16 个阶段
    3. 用 **AST 节点级** 替换把裸局部名改成 state.<field>
       （不用正则 —— Phase 6.5 的正则改坏过 import）
    4. 生成新文件：阶段方法 + 精简的 run()

输出到 .new 文件供 diff 核对，确认后再替换。
"""

from __future__ import annotations

import ast
import difflib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "src" / "plugins" / "dl_senpai" / "agent" / "legacy_orchestrator.py"

# 阶段定义：(方法名, [该阶段包含的顶层语句**起始行号**列表])
# 起始行号来自审计输出，每个数字必须是某个顶层语句的 lineno。
STAGES: list[tuple[str, list[int], bool]] = [
    # (名称, 语句起始行, 是否可提前中止)
    ("resolve_session_stage", [181, 182, 184], True),
    ("prepare_message_context", [186, 189, 190, 198, 199], False),
    ("prepare_media_context", [200, 201, 202, 206, 207, 213, 214, 215, 221], False),
]


def get_run() -> ast.AsyncFunctionDef:
    tree = ast.parse(LEGACY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LegacyOrchestrator":
            for m in node.body:
                if isinstance(m, ast.AsyncFunctionDef) and m.name == "run":
                    return m
    raise SystemExit("找不到 run")


def main() -> int:
    run = get_run()
    print(f"run(): L{run.lineno}-{run.end_lineno}")
    print(f"顶层语句 {len(run.body)} 个，起始行号：")
    starts = [s.lineno for s in run.body]
    print(f"  {starts}")

    # 校验我预设的边界是否都能对应到真实语句
    all_declared = [n for _name, nums, _abort in STAGES for n in nums]
    missing = [n for n in all_declared if n not in starts]
    if missing:
        print(f"\n⚠ 这些行号不是顶层语句起点: {missing}")
    else:
        print("\nOK: 所有声明的边界都对应真实顶层语句")

    # 打印前 40 个语句的行号+类型，供精确制定边界
    print("\n全部顶层语句：")
    for s in run.body:
        first = ast.unparse(s).splitlines()[0][:76]
        print(f"  L{s.lineno:<5} {type(s).__name__:<14} {first}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
