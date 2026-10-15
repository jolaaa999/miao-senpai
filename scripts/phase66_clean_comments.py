"""Phase 6.6：清理 legacy_orchestrator.py 中已失效的注释。

Phase 6.5 留下的注释仍在说"本模块会 import handlers / 临时反向依赖"，
但代码已经改成直接 import gateway.platform / core.domain。
按 AGENTS 规则「修改已有代码时，若原有注释已失效，一并更新」必须清掉。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "src" / "plugins" / "dl_senpai" / "agent" / "legacy_orchestrator.py"

# 模块 docstring 中关于 handlers 反向依赖的整段说明
OLD_DOC = re.compile(
    r"所有 helper 若在被搬函数之外也有使用.*?(?=\n\"\"\")",
    re.DOTALL,
)

NEW_DOC = """Phase 6.6 更新：本模块**不再依赖 handlers**。
    9 个原共享 helper 已抽到明确的归属模块：
        gateway/platform.py  —— plain_text / sender_name / resolve_session
                                / is_to_me / reply
        core/domain.py       —— feature_enabled / affection_scope
                                / collect_stickers / finalize_affection_gain
    依赖方向变为 legacy → gateway|core，而不是 legacy → handlers。
"""

# 描述符段落（已删除代码，注释也应删）
OLD_DESC = re.compile(
    r"\n# -- 与 handlers 共用的 helper（延迟解析，避免模块级循环依赖）--.*?(?=\n\n\nclass LegacyOrchestrator)",
    re.DOTALL,
)


def main() -> int:
    src = LEGACY.read_text(encoding="utf-8")

    src, n1 = OLD_DOC.subn(NEW_DOC.rstrip(), src)
    print(f"更新模块 docstring: {n1}")

    src, n2 = OLD_DESC.subn("\n", src)
    print(f"删除描述符说明段: {n2}")

    LEGACY.write_text(src, encoding="utf-8")
    print(f"legacy_orchestrator.py 现有 {len(src.splitlines())} 行")

    # 自检：注释里也不该再提 handlers
    hits = [
        (i, line.strip())
        for i, line in enumerate(src.splitlines(), start=1)
        if "handlers" in line
    ]
    if hits:
        print("\n仍提及 handlers 的行：")
        for i, line in hits:
            print(f"    {i}: {line[:88]}")
    else:
        print("\nOK: 全文（含注释）已无 handlers 引用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
