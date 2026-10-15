"""Phase 3 交付物自检：验证 matcher 迁移与各层接口齐全。"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "plugins"))

LEGACY_MATCHERS = [
    "verify_matcher",
    "checkin_matcher",
    "shop_matcher",
    "private_matcher",
    "mention_matcher",
    "interrupt_matcher",
    "sticker_collector",
    "welcome_matcher",
    "leave_matcher",
]

BUSINESS_FNS = [
    "handle_checkin",
    "handle_shop",
    "handle_group_verify",
    "handle_group_increase",
    "handle_group_decrease",
    "_handle_chat",
]

ADAPTER_FNS = [
    "dispatch_checkin_command",
    "dispatch_shop_command",
    "dispatch_verify_command",
    "observe_stickers",
    "observe_notice_welcome",
    "observe_notice_leave",
    "guard_checkin",
    "guard_shop",
    "guard_verify",
]


def main() -> int:
    import dl_senpai.handlers as h

    ok = True
    print("=== 旧 matcher 是否全部停用 ===")
    for name in LEGACY_MATCHERS:
        active = hasattr(h, name)
        ok &= not active
        print(f"  {name:20} {'ACTIVE (FAIL)' if active else 'disabled OK'}")

    print("\n=== 新 gateway matcher ===")
    for name in ("gateway_message_matcher", "gateway_notice_matcher"):
        present = hasattr(h, name)
        ok &= present
        print(f"  {name:24} {present}")

    print("\n=== 业务函数保留 ===")
    for fn in BUSINESS_FNS:
        present = hasattr(h, fn)
        ok &= present
        print(f"  {fn:24} {present}")

    print("\n=== 适配层函数 ===")
    for fn in ADAPTER_FNS:
        present = hasattr(h, fn)
        ok &= present
        print(f"  {fn:26} {present}")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
