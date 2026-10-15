"""Phase 6A：精确清理 llm.py 中的死 parser import（内联形式）。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LLM = ROOT / "src" / "plugins" / "dl_senpai" / "llm.py"

# 目标 import 行（原始文本）→ 替换为（去掉死名字）
REPLACEMENTS: list[tuple[str, str, str]] = [
    (
        "from .group_card import CardAction, parse_card_actions",
        "from .group_card import CardAction",
        "parse_card_actions",
    ),
    (
        "from .group_mute import MuteAction, parse_mute_actions",
        "from .group_mute import MuteAction",
        "parse_mute_actions",
    ),
    (
        "from .stickers import parse_sticker_request",
        "",  # 整行删除
        "parse_sticker_request",
    ),
    (
        "from .voice import parse_voice_request",
        "",
        "parse_voice_request",
    ),
    (
        "from .image_gen import DrawRequest, parse_draw_request, strip_draw_markers",
        "from .image_gen import DrawRequest, strip_draw_markers",
        "parse_draw_request",
    ),
    (
        "from .browser_agent import BrowseRequest, parse_browse_request, strip_browse_markers, wants_browser_help",
        "from .browser_agent import BrowseRequest, strip_browse_markers, wants_browser_help",
        "parse_browse_request",
    ),
    (
        "from .person_memory import ImpressionUpdate, parse_impression_updates, strip_impression_markers",
        "from .person_memory import ImpressionUpdate, strip_impression_markers",
        "parse_impression_updates",
    ),
]


def main() -> int:
    text = LLM.read_text(encoding="utf-8")
    removed = 0
    for old, new, name in REPLACEMENTS:
        # 确认该名字真的没有调用点（除 import 行外）
        call_sites = [
            (i, line) for i, line in enumerate(text.splitlines(), start=1)
            if name in line and not line.strip().startswith(("from ", "import ", "#"))
        ]
        if call_sites:
            print(f"  SKIP {name}: 仍有调用点 {[i for i, _ in call_sites]}")
            continue
        if old not in text:
            print(f"  MISS {old!r}")
            continue
        if new:
            text = text.replace(old, new)
        else:
            # 整行删除（含换行）
            text = text.replace(old + "\n", "")
        removed += 1
        print(f"  OK 移除 {name}")

    LLM.write_text(text, encoding="utf-8")
    print(f"\n移除 {removed} 个死 import")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
