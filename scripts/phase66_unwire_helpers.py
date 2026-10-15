"""Phase 6.6 任务（十四）：消除 legacy → handlers 反向依赖。

做法：
    1. 在 legacy_orchestrator.py 里把 9 个 _SharedHelper 描述符
       替换为从 gateway.platform / core.domain 的**直接 import**
    2. 函数体里的裸名调用（_reply / _fe / ...）改为新名字
       （reply / feature_enabled / ...）
    3. 删除 _SharedHelper 类与 9 个描述符属性

注意：本脚本只做**名字替换**，不改控制流。
每个替换都是 1:1 的符号改名。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "src" / "plugins" / "dl_senpai" / "agent" / "legacy_orchestrator.py"

# 旧名 → 新名（1:1 符号改名）
RENAMES: dict[str, str] = {
    "_plain_text": "plain_text",
    "_sender_name": "sender_name",
    "_resolve_session": "resolve_session",
    "_is_to_me": "is_to_me",
    "_reply": "reply",
    "_fe": "feature_enabled",
    "_affection_scope": "affection_scope",
    "_collect_stickers": "collect_stickers",
    "_finalize_affection_gain": "finalize_affection_gain",
}

IMPORT_BLOCK = '''

# Phase 6.6：平台与领域 helper 已从 handlers 抽到明确的归属模块。
# 依赖方向变为 legacy → gateway.platform / core.domain，
# 不再是 legacy → handlers。
from ..core.domain import (
    affection_scope,
    collect_stickers,
    feature_enabled,
    finalize_affection_gain,
)
from ..gateway.platform import (
    is_to_me,
    plain_text,
    reply,
    resolve_session,
    sender_name,
)
'''


def main() -> int:
    src = LEGACY.read_text(encoding="utf-8")

    # 1) 删除 _SharedHelper 类定义
    class_pat = re.compile(
        r"\nclass _SharedHelper:.*?(?=\n\w|\n#|\Z)", re.DOTALL
    )
    before = len(src)
    src, n_class = class_pat.subn("\n", src)
    print(f"删除 _SharedHelper 类: {'成功' if n_class else '未找到'}")

    # 2) 删除 9 个描述符属性行 + 其上的说明注释段
    desc_lines = []
    for line in src.splitlines(keepends=True):
        stripped = line.strip()
        is_desc = any(
            stripped == f"{old} = _SharedHelper(\"{old}\")" for old in RENAMES
        )
        if is_desc:
            continue
        desc_lines.append(line)
    src = "".join(desc_lines)
    print("删除 9 个描述符属性")

    # 3) 删除描述符的说明注释段（含"9 个与 handlers 共用"那段）
    src = re.sub(
        r"\n    # 9 个与 handlers 共用.*?(?=\n    async def run)",
        "\n",
        src,
        flags=re.DOTALL,
    )

    # 4) 插入新的 import 块（放在 _browse_cooldown 定义之后）
    anchor = "_browse_cooldown = BrowseCooldown()"
    if anchor in src:
        src = src.replace(anchor, anchor + "\n" + IMPORT_BLOCK, 1)
        print("插入新 import 块")
    else:
        print("FAIL: 找不到插入锚点")
        return 1

    # 5) 符号改名（用词边界，避免误伤子串）
    for old, new in RENAMES.items():
        pattern = re.compile(rf"(?<![\w.]){re.escape(old)}(?![\w])")
        src, n = pattern.subn(new, src)
        print(f"  改名 {old} -> {new}: {n} 处")

    LEGACY.write_text(src, encoding="utf-8")
    print(f"\nlegacy_orchestrator.py: {before} -> {len(src.splitlines())} 行")

    # 6) 自检：不应再有 handlers 引用
    remaining = [
        i for i, line in enumerate(src.splitlines(), start=1)
        if "handlers" in line
    ]
    if remaining:
        print(f"\n⚠ 仍含 'handlers' 的行: {remaining}")
    else:
        print("\nOK: 文件中已无 handlers 引用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
