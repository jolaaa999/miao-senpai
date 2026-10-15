"""Phase 6.5 任务（十二）：handlers.py 依赖清理（第二十八节）。

编排搬走后，许多 import 变成死代码。第九节要求 handlers 理想只依赖：
    platform/gateway、runtime entry、reply/platform helpers、minimal config

本脚本只删除"仅出现在 import 语句中、无任何使用点"的名字，
并保留第三十六节要求不得误删的资产（persona 常量等）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "src" / "plugins" / "dl_senpai" / "handlers.py"

# 第三十六节：绝不可删（生产 prompt 资产 / 兼容函数）
PROTECTED = {
    "SYSTEM_PROMPT", "CARD_SYSTEM_EXTRA", "MUTE_SYSTEM_EXTRA",
    "DRAW_SYSTEM_EXTRA", "BROWSE_SYSTEM_EXTRA", "SEARCH_SYSTEM_EXTRA",
    "PERSON_MEMORY_SYSTEM_EXTRA", "VOICE_SYSTEM_EXTRA", "STICKER_SYSTEM_EXTRA",
    "build_system_prompt", "get_senpai_name",
    "polish_reply", "truncate_reply",
}


def used_names(tree: ast.Module, exclude_import_lines: set[int]) -> set[str]:
    """收集真实使用的名字（排除 import 行本身）。"""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.lineno not in exclude_import_lines:
            out.add(node.id)
        elif isinstance(node, ast.Attribute) and node.lineno not in exclude_import_lines:
            out.add(node.attr)
    return out


def main() -> int:
    src = HANDLERS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)

    # 找出所有 import 语句占据的行
    import_lines: set[int] = set()
    import_nodes: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_nodes.append(node)
            for i in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                import_lines.add(i)

    used = used_names(tree, import_lines)

    remove_lines: set[int] = set()
    modified_imports: list[str] = []

    for node in import_nodes:
        if not isinstance(node, ast.ImportFrom):
            continue
        kept: list[str] = []
        dropped: list[str] = []
        for alias in node.names:
            name = alias.asname or alias.name
            if name in PROTECTED:
                kept.append(f"{alias.name} as {alias.asname}" if alias.asname else alias.name)
                continue
            if name in used:
                kept.append(f"{alias.name} as {alias.asname}" if alias.asname else alias.name)
            else:
                dropped.append(alias.name)

        if not dropped:
            continue

        start, end = node.lineno, node.end_lineno or node.lineno
        if not kept:
            # 整个 import 语句删除
            for i in range(start, end + 1):
                remove_lines.add(i)
            modified_imports.append(
                f"删除整行 import: from {node.module} import {', '.join(dropped)}"
            )
        else:
            # 多行 import：重写为单行或折叠
            new_line = f"from {node.module} import {', '.join(kept)}\n"
            lines[start - 1] = new_line
            for i in range(start + 1, end + 1):
                remove_lines.add(i)
            modified_imports.append(
                f"精简 import: from {node.module} 去掉 {', '.join(dropped)}"
            )

    if not remove_lines and not modified_imports:
        print("没有需要清理的 import")
        return 0

    out = [
        line for i, line in enumerate(lines, start=1) if i not in remove_lines
    ]
    HANDLERS.write_text("".join(out), encoding="utf-8")

    for m in modified_imports:
        print(f"  {m}")
    print(f"\n处理 {len(modified_imports)} 处 import")
    print(f"handlers.py 现有 {len(HANDLERS.read_text(encoding='utf-8').splitlines())} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
