#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""角色字段核验 CLI —— 唯一能改「可信状态」的入口。

    python scripts/character_verify.py list
    python scripts/character_verify.py show 艾雅法拉
    python scripts/character_verify.py pending 艾雅法拉        # 人工交接面板
    python scripts/character_verify.py review-field 艾雅法拉 hair    # → assistant_reviewed
    python scripts/character_verify.py verify-field 艾雅法拉 hair --method human_review
    python scripts/character_verify.py unverify-field 艾雅法拉 hair
    python scripts/character_verify.py edit-field 艾雅法拉 hair "新的描述"
    python scripts/character_verify.py status-clear 艾雅法拉

## 为什么要分成 review-field 和 verify-field

旧版只有一个 verify-field，配上 --method 参数。问题是它的名字暗示了"核验完成"，
而模型跑一遍就能填 --method assistant_review 得到一个看起来 verified 的字段。
上一阶段就出了这个事故：8 个字段被模型核验后写成 verified=true，直接进了提示词的
authoritative 硬约束段。

现在按**谁做的核验**分两个命令：

    review-field  → assistant_reviewed，进 Level C（支撑性提示，禁止 authoritative 措辞）
    verify-field  → human_verified，  进 Level A（唯一可以写"不得违背"的层级）

verify-field 必须显式 --method human_review。传 assistant_review 会被拒绝：
程序绝不自动调用人工核验，HUMAN_VERIFIED must never be produced by automatic model review。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from plugins.dl_senpai.character_references import (  # noqa: E402
    count_references,
    count_verified_references,
    load_manifest,
)
from plugins.dl_senpai.character_registry import (  # noqa: E402
    METHOD_HUMAN,
    STATUS_ASSISTANT_REVIEWED,
    STATUS_DRAFT,
    STATUS_PARTIAL,
    STATUS_VERIFIED,
    VerificationStatus,
    builtin_data_path,
    editable_data_path,
    find_raw_entry,
    _load_raw,
    update_field_status,
    update_field_value,
    update_profile_status_lock,
)
from plugins.dl_senpai.character_registry import CharacterRegistry  # noqa: E402

_STATUS_NOTE = {
    VerificationStatus.HUMAN_VERIFIED: "已人工核验",
    VerificationStatus.ASSISTANT_REVIEWED: "仅模型审阅，待人工核验",
    VerificationStatus.UNVERIFIED: "未核验",
}


def _data_path() -> Path:
    from plugins.dl_senpai.config import get_config

    return editable_data_path(get_config())


def _load_profile(name: str):
    registry = CharacterRegistry.load()
    profile = registry.resolve(name)
    if profile is None:
        raise SystemExit(f"未找到角色: {name}")
    return profile


def cmd_list(args: argparse.Namespace) -> int:
    registry = CharacterRegistry.load()
    print(f"{'角色':<16}{'状态':<20}{'人工':<6}{'审阅':<6}{'未核验':<8}参考图")
    print("-" * 72)
    for profile in sorted(registry.all(), key=lambda p: p.canonical_name):
        refs = count_references(_config(), profile)
        ok_refs = count_verified_references(_config(), profile)
        print(
            f"{profile.canonical_name:<16}{profile.effective_status():<20}"
            f"{len(profile.human_verified_fields()):<6}"
            f"{len(profile.assistant_reviewed_fields()):<6}"
            f"{len(profile.unverified_fields()):<8}{ok_refs}/{refs}"
        )
    return 0


def _config():
    from plugins.dl_senpai.config import get_config

    return get_config()


def cmd_show(args: argparse.Namespace) -> int:
    profile = _load_profile(args.character)
    localized = profile.localized_names[0] if profile.localized_names else ""
    print(f"{profile.canonical_name} / {localized}")
    print(f"  Franchise      : {profile.franchise}")
    print(f"  Profile status : {profile.effective_status()}")
    required = profile.required_fields()
    print(f"  必核字段       : {', '.join(required) or '（无）'}")
    hit = [k for k in required if k in profile.human_verified_fields()]
    print(f"  必核命中       : {len(hit)}/{len(required)}（升到 verified 需要人工核验全部）")
    print(f"  标志性特征槽   : {profile.signature_feature_field() or '（该角色无此类字段）'}")

    print("\n  字段:")
    for key in profile.visual_identity.keys():
        fld = profile.visual_identity.field(key)
        mark = {VerificationStatus.HUMAN_VERIFIED: "✓", VerificationStatus.ASSISTANT_REVIEWED: "~"}.get(
            fld.verification_status, " "
        )
        print(f"    {mark} {key:<14}{fld.verification_status.value:<22}{fld.value[:44]}")

    cfg = _config()
    print(f"\n  参考图: {count_verified_references(cfg, profile)} 张人工核验 / {count_references(cfg, profile)} 张总计")

    print(
        "\n  说明：Text Profile Verification ≠ Reference Identity Verification\n"
        "        文字字段核验的是「描述是否准确」，脸和整体外观只能靠人工核验的参考图。\n"
        "        只有 human_verified 字段才进提示词的硬约束段；assistant_reviewed 只作支撑提示。"
    )
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    """人工交接面板：把「模型审过什么、还差什么」一次列清楚。"""
    profile = _load_profile(args.character)
    localized = profile.localized_names[0] if profile.localized_names else ""
    reviewed = profile.assistant_reviewed_fields()
    human = profile.human_verified_fields()
    pending = tuple(k for k in reviewed if k not in human)

    print(f"{profile.canonical_name} / {localized}\n")
    print("Assistant-reviewed:")
    for key in reviewed:
        print(f"✓ {key}")
    if not reviewed:
        print("none")

    print("\nHuman-verified:")
    for key in human:
        print(f"✓ {key}")
    if not human:
        print("none")

    print("\nPending human verification:")
    for key in pending:
        print(f"- {key}")
    if not pending:
        print("none")

    print(f"\nVerified references:\n{count_verified_references(_config(), profile)}")
    print(f"\nProfile status:\n{profile.effective_status()}")

    if pending:
        print("\n" + "=" * 64)
        print("人工核验清单（逐项确认，别整批盖章）")
        print("=" * 64)
        for key in pending:
            fld = profile.visual_identity.field(key)
            print(f"\n[ ] {key}\n")
            print(f"Candidate:\n{fld.value}\n")
            print(f"Current status:\n{fld.verification_status.value}\n")
            print("Action:")
            print("  确认无误： python scripts/character_verify.py "
                  f"verify-field {args.character} {key} --method human_review")
            print("  不确定  ： leave unchanged")
            print("  错误    ： python scripts/character_verify.py "
                  f"edit-field {args.character} {key} \"新的描述\"（改完自动变 unverified）")
    return 0


def cmd_review_field(args: argparse.Namespace) -> int:
    ok, message = update_field_status(
        _data_path(), args.character, args.field,
        status=VerificationStatus.ASSISTANT_REVIEWED,
        method="assistant_review",
        at=args.at or "",
        note=args.note or "模型据公开设定核对",
    )
    print(("✓ " if ok else "✗ ") + message)
    return 0 if ok else 1


def cmd_verify_field(args: argparse.Namespace) -> int:
    """人工核验。method 必须是 human_review —— 这是防伪造的第一道闸。"""
    method = (args.method or "").strip().lower()
    if method != METHOD_HUMAN:
        # 这里刻意报错而不是静默降级：静默降级会让人以为"核验成功了"
        print(
            "✗ assistant_review cannot create a verified field\n"
            "  human_verified 必须显式 --method human_review。\n"
            "  只是想让模型先审一遍：python scripts/character_verify.py "
            f"review-field {args.character} {args.field}"
        )
        return 2
    ok, message = update_field_status(
        _data_path(), args.character, args.field,
        status=VerificationStatus.HUMAN_VERIFIED,
        method=METHOD_HUMAN,
        at=args.at or "",
        note=args.note or "",
    )
    print(("✓ " if ok else "✗ ") + message)
    if ok:
        profile = _load_profile(args.character)
        print(f"  profile_status → {profile.effective_status()}")
    return 0 if ok else 1


def cmd_unverify_field(args: argparse.Namespace) -> int:
    ok, message = update_field_status(
        _data_path(), args.character, args.field,
        status=VerificationStatus.UNVERIFIED,
        note=args.note or "",
    )
    print(("✓ " if ok else "✗ ") + message)
    return 0 if ok else 1


def cmd_edit_field(args: argparse.Namespace) -> int:
    """改内容 → 核验自动失效。改完必须重新核验，不允许继承旧结论。"""
    ok, message = update_field_value(
        _data_path(), args.character, args.field, args.value, note=args.note or ""
    )
    print(("✓ " if ok else "✗ ") + message)
    return 0 if ok else 1


def cmd_status_clear(args: argparse.Namespace) -> int:
    ok, message = update_profile_status_lock(_data_path(), args.character, args.status or "")
    print(("✓ " if ok else "✗ ") + message)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="角色字段核验（人工核验的唯一入口）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出所有角色与可信状态").set_defaults(func=cmd_list)

    for name, func, help_text in (
        ("show", cmd_show, "查看某角色的字段与核验状态"),
        ("pending", cmd_pending, "人工交接面板：审阅过的、已核验的、还差哪些"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("character")
        sp.set_defaults(func=func)

    sp = sub.add_parser("review-field", help="标记为 assistant_reviewed（模型审阅，非人工核验）")
    sp.add_argument("character")
    sp.add_argument("field")
    sp.add_argument("--at", default="", help="日期，如 2026-10-27")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_review_field)

    sp = sub.add_parser("verify-field", help="标记为 human_verified（必须 --method human_review）")
    sp.add_argument("character")
    sp.add_argument("field")
    sp.add_argument("--method", default=METHOD_HUMAN, help="只接受 human_review")
    sp.add_argument("--at", default="")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_verify_field)

    sp = sub.add_parser("unverify-field", help="撤销核验（保留描述）")
    sp.add_argument("character")
    sp.add_argument("field")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_unverify_field)

    sp = sub.add_parser("edit-field", help="改描述（核验状态自动失效）")
    sp.add_argument("character")
    sp.add_argument("field")
    sp.add_argument("value")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_edit_field)

    # 旧名保留，避免已有脚本失效；语义与 edit-field 相同
    sp = sub.add_parser("set-value", help="edit-field 的旧名")
    sp.add_argument("character")
    sp.add_argument("field")
    sp.add_argument("value")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_edit_field)

    sp = sub.add_parser("status-clear", help="清除显式 profile_status 锁定")
    sp.add_argument("character")
    sp.add_argument("--status", default="", help="draft / assistant_reviewed / partial_verified / verified / 空")
    sp.set_defaults(func=cmd_status_clear)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
