"""Phase 6A：删除因 parser 收敛而变成死代码的 import。

只删除"仅出现在 import 语句中、无任何调用点"的名字。
每个删除都先验证引用计数，避免误删。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"

# 目标文件 → 待检查的 import 名
TARGETS: dict[str, list[str]] = {
    "llm.py": [
        "parse_card_actions",
        "parse_mute_actions",
        "parse_sticker_request",
        "parse_voice_request",
        "parse_draw_request",
        "parse_browse_request",
        "parse_impression_updates",
        "parse_search_request",
    ],
    "handlers.py": [
        "parse_draw_request",
        "parse_browse_request",
    ],
}


def is_dead(text: str, name: str) -> bool:
    """该名字除 import 行外是否还有其他出现。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if name not in line:
            continue
        stripped = line.strip()
        # import 列表行 / from ... import 行
        if stripped == f"{name}," or stripped.startswith(f"from ") or stripped.startswith("import "):
            continue
        # 注释里提到不算使用
        if stripped.startswith("#"):
            continue
        return False
    return True


def main() -> int:
    total = 0
    for filename, names in TARGETS.items():
        path = PKG / filename
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        remove_indices: set[int] = set()

        for i, line in enumerate(lines):
            stripped = line.strip()
            for name in names:
                # 精确匹配 "    name," 形式的 import 条目
                if stripped == f"{name},":
                    if is_dead(text, name):
                        remove_indices.add(i)
                        print(f"  {filename}:{i + 1} 删除死 import {name}")
                        total += 1

        if remove_indices:
            new_lines = [
                line for i, line in enumerate(lines) if i not in remove_indices
            ]
            path.write_text("".join(new_lines), encoding="utf-8")

    print(f"\n共删除 {total} 个死 import")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
