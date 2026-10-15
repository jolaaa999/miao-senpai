"""Phase 6.5 任务（十一）：删除因搬迁而变成死代码的定义。

第三十五节：删除前必须确认「无生产引用 + 测试通过」。
第三十六节：不得误删 persona constants / legacy compatibility prompt /
          local model compatibility functions。

本脚本**只删**满足以下全部条件的顶层定义：
    1. 名字在候选清单里（人工确认过已搬走）
    2. 定义之外没有任何引用
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"


def count_refs(path: Path, name: str) -> tuple[int, list[int]]:
    """统计该文件里 name 的所有出现行（含定义行）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    hits = [
        i for i, line in enumerate(lines, start=1)
        if name in line and not line.strip().startswith("#")
    ]
    return len(hits), hits


def remove_def(path: Path, name: str) -> bool:
    """删除一个顶层 def / 赋值语句。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)

    for node in tree.body:
        names: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]

        if name not in names:
            continue

        start = node.lineno
        end = node.end_lineno or node.lineno
        # 连同后面的空行一起删，避免留下过多空行
        while end < len(lines) and not lines[end].strip():
            end += 1
        del lines[start - 1 : end]
        path.write_text("".join(lines), encoding="utf-8")
        return True
    return False


# 候选：搬迁后应无引用
HANDLERS_CANDIDATES = [
    "_get_memory_facade",
    "_first_values",
    "_card_cooldown",
    "_mute_cooldown",
    "_draw_cooldown",
    "_browse_cooldown",
    "_cooldown",
    "_attention",
]


def main() -> int:
    path = PKG / "handlers.py"
    removed = 0
    skipped: list[str] = []

    for name in HANDLERS_CANDIDATES:
        n, hits = count_refs(path, name)
        # 只允许"定义那一行"这唯一一次出现
        if n > 1:
            skipped.append(f"{name}（仍被引用 {n} 处: {hits}）")
            continue
        if remove_def(path, name):
            print(f"  删除死代码: {name}")
            removed += 1
        else:
            skipped.append(f"{name}（未找到定义）")

    print(f"\n删除 {removed} 个死定义")
    if skipped:
        print("跳过：")
        for s in skipped:
            print(f"    {s}")

    total = len(path.read_text(encoding="utf-8").splitlines())
    print(f"\nhandlers.py 现有 {total} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
