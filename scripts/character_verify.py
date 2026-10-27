#!/usr/bin/env python
"""角色档案人工核验 CLI。

为什么需要它：verified=true 必须来自「人看过原始资料并确认」，不能由程序、
视觉模型或生图结果自动写入。这个脚本就是那个人工动作的唯一入口。

用法：

    python scripts/character_verify.py list
    python scripts/character_verify.py show 艾雅法拉
    python scripts/character_verify.py set-value 艾雅法拉 hair "银白偏浅紫长发"
    python scripts/character_verify.py verify-field 艾雅法拉 hair
    python scripts/character_verify.py unverify-field 艾雅法拉 clothing
    python scripts/character_verify.py status-clear 艾雅法拉

约定：
- 空字段不能验证（先 set-value 填描述）；
- 撤销不删描述，只把 verified 打回 false；
- profile_status 不需要手写，永远由字段推导（status-clear 只清历史遗留值）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _registry():
    from plugins.dl_senpai.character_registry import CharacterRegistry, reset_character_registry

    reset_character_registry()
    return CharacterRegistry.load()


def _data_path(args: argparse.Namespace) -> Path:
    from plugins.dl_senpai.character_registry import builtin_data_path

    return Path(args.file) if getattr(args, "file", "") else builtin_data_path()


def _resolve(name: str):
    profile = _registry().resolve(name)
    if profile is None:
        print(f"未找到角色: {name}（用 list 看有哪些）", file=sys.stderr)
        raise SystemExit(2)
    return profile


def cmd_list(args: argparse.Namespace) -> int:
    registry = _registry()
    print(f"{'character':<18}{'status':<18}{'verified_fields':<18}required")
    for profile in sorted(registry.all(), key=lambda p: p.canonical_name):
        verified = ",".join(profile.verified_fields()) or "-"
        print(
            f"{profile.canonical_name:<18}"
            f"{profile.effective_status():<18}"
            f"{verified:<18}"
            f"{','.join(profile.required_fields())}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    profile = _resolve(args.character)
    localized = profile.localized_names[0] if profile.localized_names else ""
    print("Character:")
    print(f"  {profile.canonical_name}" + (f" / {localized}" if localized else ""))
    print("Franchise:")
    print(f"  {profile.franchise}" + (f" / {profile.franchise_localized}" if profile.franchise_localized else ""))
    print()
    print("Profile Status:")
    print(f"  {profile.effective_status()}")
    print()
    print("Required fields (升到 verified 需核验):")
    print(f"  {', '.join(profile.required_fields())}")
    print()
    print("Fields:")
    for key in profile.visual_identity.keys():
        fld = profile.visual_identity.field(key)
        mark = "verified" if fld.verified else "unverified"
        print(f"  {key}  [{mark}]")
        print(f"    value: {fld.value}")
        print(f"    verified: {str(fld.verified).lower()}")
        if fld.verified_at:
            print(f"    verified_at: {fld.verified_at}")
        if fld.verification_method:
            print(f"    verification_method: {fld.verification_method}")
        if fld.note:
            print(f"    note: {fld.note}")
    print()
    from plugins.dl_senpai.character_references import count_references, count_verified_references

    try:
        from plugins.dl_senpai.config import get_config

        cfg = get_config()
    except Exception:  # noqa: BLE001 — 没有 .env 也要能看档案
        cfg = None
    print("References:")
    print(f"  verified: {count_verified_references(cfg, profile)}")
    print(f"  files: {count_references(cfg, profile)}")
    print()
    print("Text Profile Verification ≠ Reference Identity Verification：")
    print("  face 不要求文本核验，脸的身份交给已验证参考图。")
    return 0


def cmd_verify_field(args: argparse.Namespace) -> int:
    from plugins.dl_senpai.character_registry import update_field_verification

    ok, message = update_field_verification(
        _data_path(args),
        args.character,
        args.field,
        verified=True,
        method=args.method,
        at=args.at,
        note=args.note,
        value=args.value,
    )
    print(message)
    if not ok:
        return 1
    profile = _resolve(args.character)
    print(f"profile_status → {profile.effective_status()}")
    print(f"verified_fields → {','.join(profile.verified_fields()) or '-'}")
    return 0


def cmd_unverify_field(args: argparse.Namespace) -> int:
    from plugins.dl_senpai.character_registry import update_field_verification

    ok, message = update_field_verification(
        _data_path(args),
        args.character,
        args.field,
        verified=False,
        note=args.note,
    )
    print(message)
    if not ok:
        return 1
    profile = _resolve(args.character)
    print(f"profile_status → {profile.effective_status()}")
    return 0


def cmd_set_value(args: argparse.Namespace) -> int:
    from plugins.dl_senpai.character_registry import update_field_verification

    ok, message = update_field_verification(
        _data_path(args),
        args.character,
        args.field,
        # 只改描述，不动核验状态：改文案不等于重新核验过
        verified=False,
        value=args.value,
    )
    print(message if ok else message)
    return 0 if ok else 1


def cmd_status_clear(args: argparse.Namespace) -> int:
    from plugins.dl_senpai.character_registry import update_profile_status_lock

    ok, message = update_profile_status_lock(_data_path(args), args.character, "")
    print(message)
    if not ok:
        return 1
    profile = _resolve(args.character)
    print(f"profile_status → {profile.effective_status()}（重新按字段推导）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="角色档案人工核验")
    parser.add_argument("--file", default="", help="角色数据文件（默认内置 data/characters.json）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有角色与状态").set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="查看一个角色的档案")
    p.add_argument("character")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("verify-field", help="人工确认一个字段")
    p.add_argument("character")
    p.add_argument("field")
    p.add_argument("--method", default="human_review")
    p.add_argument("--at", default="")
    p.add_argument("--note", default="")
    p.add_argument("--value", default=None, help="顺便修正描述")
    p.set_defaults(func=cmd_verify_field)

    p = sub.add_parser("unverify-field", help="撤销一个字段的核验")
    p.add_argument("character")
    p.add_argument("field")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_unverify_field)

    p = sub.add_parser("set-value", help="只改字段描述，不动核验状态")
    p.add_argument("character")
    p.add_argument("field")
    p.add_argument("value")
    p.set_defaults(func=cmd_set_value)

    p = sub.add_parser("status-clear", help="清掉手写的 profile_status，改回自动推导")
    p.add_argument("character")
    p.set_defaults(func=cmd_status_clear)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
