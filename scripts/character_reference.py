#!/usr/bin/env python
"""参考图库导入 CLI（人工流程）。

只处理本地文件：绝不联网下载，也绝不因为域名看起来像官网就自动 verified。
source_url 只是记录，不等于核验。

用法：

    python scripts/character_reference.py add 艾雅法拉 "C:\\refs\\eyja.png" --role fullbody --source-type official --source-url "..." --verified
    python scripts/character_reference.py list 艾雅法拉
    python scripts/character_reference.py check 艾雅法拉
    python scripts/character_reference.py unverify 艾雅法拉 ref_fullbody_01.png
    python scripts/character_reference.py remove 艾雅法拉 ref_fullbody_01.png --delete-file

注意：图片二进制不进 Git（第三方版权资产）。这里也不会自动 git add。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _profile(name: str):
    from plugins.dl_senpai.character_registry import CharacterRegistry

    profile = CharacterRegistry.load().resolve(name)
    if profile is None:
        print(f"未找到角色: {name}", file=sys.stderr)
        raise SystemExit(2)
    return profile


def _cfg():
    from plugins.dl_senpai.config import get_config

    return get_config()


def cmd_add(args: argparse.Namespace) -> int:
    from plugins.dl_senpai.character_references import add_reference_to_library

    profile = _profile(args.character)
    source = Path(args.path)
    if not source.is_file():
        print(f"文件不存在: {source}", file=sys.stderr)
        return 1
    raw = source.read_bytes()
    ok, message = add_reference_to_library(
        _cfg(),
        profile,
        raw,
        original_name=source.name,
        role=args.role,
        priority=args.priority,
        verified=args.verified,
        reviewed=getattr(args, "reviewed", False),
        method=args.method,
        source_url=args.source_url,
        source_type=args.source_type,
        notes=args.notes,
        at=args.at,
    )
    print(message)
    if ok:
        print("提示：图片不会自动 git add（第三方版权资产，只存在运行环境）")
    return 0 if ok else 1


def cmd_list(args: argparse.Namespace) -> int:
    from plugins.dl_senpai.character_references import (
        load_manifest,
        profile_dir,
        reference_integrity_report,
    )
    from plugins.dl_senpai.character_references import _MANIFEST_NAME  # noqa: PLC2701

    profile = _profile(args.character)
    cfg = _cfg()
    manifest_path = profile_dir(cfg, profile) / _MANIFEST_NAME
    manifest = load_manifest(cfg, profile)
    print(f"Character: {profile.canonical_name}")
    print(f"Directory: {profile_dir(cfg, profile)}")
    print(f"Manifest:  {'有' if manifest else '无'}  ({manifest_path})")
    rows = reference_integrity_report(cfg, profile)
    if not rows:
        print("References: 0")
        return 0
    print(f"{'file':<28}{'verified':<10}{'role':<10}{'prio':<6}{'ok':<6}issue")
    for row in rows:
        print(
            f"{row['file']:<28}"
            f"{str(row['verified']).lower():<10}"
            f"{row['role']:<10}"
            f"{row['priority']:<6}"
            f"{str(row['ok']).lower():<6}"
            f"{row['issue']}"
        )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from plugins.dl_senpai.character_references import reference_integrity_report

    rows = reference_integrity_report(_cfg(), _profile(args.character))
    if not rows:
        print("没有可检查的参考图条目")
        return 0
    bad = [r for r in rows if not r["ok"]]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    print()
    print(f"合计 {len(rows)} 条，异常 {len(bad)} 条")
    return 0 if not bad else 1


def cmd_verify(args: argparse.Namespace) -> int:
    from plugins.dl_senpai.character_references import set_reference_verified

    ok, message = set_reference_verified(
        _cfg(), _profile(args.character), args.file,
        verified=True, method=getattr(args, "method", "human_review"), note=args.note, at=args.at,
    )
    print(message)
    return 0 if ok else 1


def cmd_unverify(args: argparse.Namespace) -> int:
    from plugins.dl_senpai.character_references import set_reference_verified

    ok, message = set_reference_verified(
        _cfg(), _profile(args.character), args.file,
        verified=False, note=args.note,
    )
    print(message)
    return 0 if ok else 1


def cmd_remove(args: argparse.Namespace) -> int:
    from plugins.dl_senpai.character_references import (
        _MANIFEST_NAME,  # noqa: PLC2701
        _load_raw_manifest,  # noqa: PLC2701
        _save_raw_manifest,  # noqa: PLC2701
        profile_dir,
    )

    cfg = _cfg()
    profile = _profile(args.character)
    directory = profile_dir(cfg, profile)
    path = directory / _MANIFEST_NAME
    data = _load_raw_manifest(path)
    entries = [x for x in data.get("references") or [] if isinstance(x, dict)]
    kept = [x for x in entries if str(x.get("file")) != args.file]
    if len(kept) == len(entries):
        print(f"manifest 里没有 {args.file}", file=sys.stderr)
        return 1
    data["references"] = kept
    data["verified"] = any(bool(x.get("verified")) for x in kept)
    _save_raw_manifest(path, data)
    print(f"已从 manifest 移除: {args.file}")
    if args.delete_file:
        target = directory / args.file
        if target.is_file():
            target.unlink()
            print(f"已删除文件: {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="参考图库导入（人工流程）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="导入一张本地图片")
    p.add_argument("character")
    p.add_argument("path")
    p.add_argument("--role", default="identity", choices=["identity", "fullbody", "portrait"])
    p.add_argument("--priority", type=int, default=100)
    p.add_argument("--source-type", default="", help="official / wiki / fan_art …（只记录，不决定 verified）")
    p.add_argument("--source-url", default="")
    p.add_argument("--notes", default="")
    # 人工核验必须是显式动作：--verified 不写 --method human_review 会被拒绝，
    # 免得"顺手加个 --verified"就把一张图升级成身份权威。
    p.add_argument("--verified", action="store_true",
                   help="人工核验过这张图（必须同时 --method human_review）")
    p.add_argument("--reviewed", action="store_true",
                   help="模型审阅过但没人确认（assistant_reviewed，不用于 images.edit）")
    p.add_argument("--method", default="human_review")
    p.add_argument("--at", default="")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="列出参考图与检查结果")
    p.add_argument("character")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("check", help="完整性校验（存在/头/大小/sha256）")
    p.add_argument("character")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("verify", help="人工核验某张图")
    p.add_argument("character")
    p.add_argument("file")
    p.add_argument("--method", default="human_review")
    p.add_argument("--at", default="")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("unverify", help="撤销某张图的核验")
    p.add_argument("character")
    p.add_argument("file")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_unverify)

    p = sub.add_parser("remove", help="从清单移除（--delete-file 才删图）")
    p.add_argument("character")
    p.add_argument("file")
    p.add_argument("--delete-file", action="store_true")
    p.set_defaults(func=cmd_remove)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
