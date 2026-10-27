#!/usr/bin/env python
"""角色生图 A/B 基准：参考图到底有没有用？

固定场景，只改一个变量（reference strategy），其余全部相同：
同 prompt、同模型、同尺寸、同 quality、同档案、同参考图策略之外的参数。

三种模式：

    # 1) dry-run（默认，不花钱）：只打印将要执行的配置 + Prompt 分层预览
    python scripts/character_generation_benchmark.py

    # 2) 评估已有图片（不花钱）
    python scripts/character_generation_benchmark.py --evaluate data/dl_senpai/bench

    # 3) 真实生成（必须双闸同时打开，且最多 2 次）
    set ALLOW_PAID_IMAGE_BENCHMARK=true
    python scripts/character_generation_benchmark.py --run-paid --allow-paid

为什么要双闸：一次 1536x2048 要 1~2 分钟和真金白银。基准是用来做判断的，
不是拿来自动烧预算的。两次也够——A 一次 B 一次，不许"再抽一张挑好看的"，
那会让对比失去意义。

盲评：评估阶段只把「待评图片 + 身份依据」交给视觉模型，**不告诉它哪张用了
参考图**。否则模型会顺着提示夸reference 版。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 固定实验场景。为什么不让人自由输入：变量越少越能说明问题。
SCENARIO = {
    "character": "艾雅法拉",
    "prompt": "画一张艾雅法拉站在火山前，半身构图，自然表情",
    # 场景要求显式列出：reference-guided 生成可能"人更像、场景更差"，必须一起量
    "scene_checks": ("火山/熔岩背景", "半身构图（腰部以上）", "自然表情"),
}

# 两组。策略名会出现在报告里，别改。
STRATEGIES: dict[str, dict[str, object]] = {
    "text_only": {"reference_policy": "text_only", "auto_reference": False, "label": "无参考图"},
    "local_verified_1": {"reference_policy": "local_only", "auto_reference": True, "label": "1 张本地核验图"},
}

# 硬上限：本阶段最多 2 次真实生图。超过就拒绝执行，不静默加。
MAX_PAID_CALLS = 2
_ENV_FLAG = "ALLOW_PAID_IMAGE_BENCHMARK"


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="角色生图参考策略 A/B 基准")
    ap.add_argument("--character", default=SCENARIO["character"])
    ap.add_argument("--prompt", default=SCENARIO["prompt"])
    ap.add_argument("--strategies", default="text_only,local_verified_1")
    ap.add_argument("--run-paid", action="store_true", help="真实生成（还需环境变量闸）")
    ap.add_argument("--allow-paid", action="store_true", help="第二个付费闸")
    ap.add_argument("--evaluate", default="", help="只评估该目录下已有图片")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    return ap.parse_args()


def _paid_allowed(args: argparse.Namespace) -> bool:
    """两个闸都开才算允许花钱。缺一个都不行。"""
    env = str(os.environ.get(_ENV_FLAG, "")).strip().lower()
    return bool(args.allow_paid) and env in ("1", "true", "yes", "on")


def _strategies_of(args: argparse.Namespace) -> list[str]:
    names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    unknown = [n for n in names if n not in STRATEGIES]
    if unknown:
        raise SystemExit(f"未知策略: {', '.join(unknown)}（可用: {', '.join(STRATEGIES)}）")
    if len(names) > MAX_PAID_CALLS:
        raise SystemExit(f"策略数 {len(names)} 超过付费上限 {MAX_PAID_CALLS}")
    return names


def _segment_path(seg) -> Path | None:
    """从 MessageSegment 里挖出本地文件路径。"""
    try:
        uri = str(seg.data.get("file") or "")
    except Exception:  # noqa: BLE001
        return None
    if uri.startswith("file:"):
        return Path(unquote(urlparse(uri).path.lstrip("/"))) if os.name == "nt" else Path(unquote(urlparse(uri).path))
    return None


async def _evaluate_image(cfg, profile, image: bytes, scene_checks: tuple[str, ...]) -> dict:
    """盲评：只给图片和依据，不给策略名。"""
    from plugins.dl_senpai.character_references import count_verified_references
    from plugins.dl_senpai.identity_eval import CharacterIdentityEvaluator
    from plugins.dl_senpai.scene_eval import SceneComplianceEvaluator

    identity = await CharacterIdentityEvaluator(
        profile, verified_reference_count=count_verified_references(cfg, profile)
    ).evaluate(cfg, image)
    scene = await SceneComplianceEvaluator(scene_checks).evaluate(cfg, image)
    return {
        "identity_score": None if identity.overall is None else round(identity.overall, 3),
        "identity_verdict": identity.verdict,
        "identity_dimensions": {
            k: (None if v is None else round(v, 3)) for k, v in identity.dimensions.items()
        },
        "scene_score": None if scene.overall is None else round(scene.overall, 3),
        "scene_checks": {
            k: (None if v is None else round(v, 3)) for k, v in scene.checks.items()
        },
        "issues": list(identity.issues) + list(scene.issues),
    }


async def _generate_one(cfg, name: str, spec: dict, prompt: str, scene_checks) -> dict:
    from plugins.dl_senpai.image_gen import build_image_segment

    started = time.monotonic()
    seg = await build_image_segment(
        marker_prompt="",
        user_request=prompt,
        config=cfg,
        auto_reference=bool(spec["auto_reference"]),
        reference_policy=str(spec["reference_policy"]),
        archive_session_id=f"bench-{name}",
    )
    spent = round(time.monotonic() - started, 1)
    if seg is None:
        return {"strategy": name, "status": "no_image", "duration": spent, "success": False}

    path = _segment_path(seg)
    row = {
        "strategy": name,
        "status": "generated",
        "duration": spent,
        "success": True,
        "image": str(path) if path else "",
    }
    if path and path.is_file():
        row.update(await _evaluate_image(cfg, _profile(cfg), path.read_bytes(), scene_checks))
    return row


def _profile(cfg):
    from plugins.dl_senpai.character_registry import CharacterRegistry

    registry = CharacterRegistry.load(cfg)
    profile = registry.resolve(SCENARIO["character"])
    if profile is None:
        raise SystemExit("内置数据里没有艾雅法拉")
    return profile


# A/B 前置闸门：只认事实，不认 profile_status。
#
# 为什么不再看 profile_status == "verified"：上一阶段就是 status 被错误语义污染
# （assistant_review 写成 verified），如果闸门只看 status，一条被污染的字段就能
# 放行整个真实生成。所以这里直接数：人工核验了几个字段、覆盖了哪几类、有几张
# 人工核验参考图。
MIN_HUMAN_VERIFIED_FIELDS = 4
MIN_HUMAN_VERIFIED_REFERENCES = 1
# 必须覆盖的类别：头发、服装、配色（身份辨识度最高的三项）
COVERAGE_REQUIRED = ("hair", "clothing", "palette")


def ab_gate(cfg) -> tuple[bool, list[str]]:
    """返回 (是否放行, 阻塞原因列表)。"""
    from plugins.dl_senpai.character_references import count_verified_references

    profile = _profile(cfg)
    human = set(profile.human_verified_fields())
    reasons: list[str] = []

    if len(human) < MIN_HUMAN_VERIFIED_FIELDS:
        reasons.append(
            f"human_verified_fields = {len(human)} < {MIN_HUMAN_VERIFIED_FIELDS}"
        )
    missing = [key for key in COVERAGE_REQUIRED if key not in human]
    # 标志性特征（角/耳/尾…）由数据推导，不写死某个字段：
    # 「有角的角色要核验角」是对的，「所有角色都必须有角」是错的。
    signature = profile.signature_feature_field()
    if signature and signature not in human:
        missing.append(f"{signature}（标志性特征）")
    if missing:
        reasons.append("未人工核验的关键项: " + ", ".join(missing))

    refs = count_verified_references(cfg, profile)
    if refs < MIN_HUMAN_VERIFIED_REFERENCES:
        reasons.append(
            f"human_verified_references = {refs} < {MIN_HUMAN_VERIFIED_REFERENCES}"
        )
    return (not reasons), reasons


def _print_gate(cfg) -> bool:
    """打印闸门状态。返回是否可以继续真实 A/B。"""
    profile = _profile(cfg)
    ok, reasons = ab_gate(cfg)
    human = profile.human_verified_fields()
    print("=== A/B 前置闸门 ===")
    print(f"  profile_status           : {profile.effective_status()}")
    print(f"  human_verified_fields    : {len(human)}/{MIN_HUMAN_VERIFIED_FIELDS}  {', '.join(human) or '-'}")
    print(f"  assistant_reviewed_fields: {', '.join(profile.assistant_reviewed_fields()) or '-'}")
    for key in COVERAGE_REQUIRED:
        print(f"    {key:<10}: {'✓ 已人工核验' if key in human else '✗ 未人工核验'}")
    signature = profile.signature_feature_field()
    if signature:
        print(f"    {signature:<10}: {'✓ 已人工核验' if signature in human else '✗ 未人工核验（标志性特征）'}")
    if ok:
        print("  → 闸门通过，可以执行真实 A/B")
    else:
        print()
        print("  BLOCKED_BY_HUMAN_REFERENCE")
        for reason in reasons:
            print(f"    - {reason}")
        print()
        print("  解锁步骤：")
        print("    python scripts/character_verify.py pending 艾雅法拉")
        print("    python scripts/character_verify.py verify-field 艾雅法拉 <field> --method human_review")
        print("    python scripts/character_reference.py add 艾雅法拉 <图片路径> --role fullbody "
              "--source-type official --verified --method human_review")
    print()
    return ok


async def _dry_run(cfg, names: list[str], prompt: str) -> int:
    """不生成任何图片：把两组会实际发生的事情摊开给人看。"""
    from plugins.dl_senpai.character_references import (
        count_references,
        count_verified_references,
        reference_integrity_report,
    )
    from plugins.dl_senpai.character_resolver import resolve_character
    from plugins.dl_senpai.image_prompt_builder import ImagePromptBuilder

    profile = _profile(cfg)
    resolution = resolve_character(prompt, config=cfg)
    built = ImagePromptBuilder().build(user_request=prompt, draft_prompt=prompt, resolution=resolution)
    verified_n = count_verified_references(cfg, profile)
    web_mode = str(getattr(cfg, "character_web_reference_mode", "verify_only"))

    print("=== 场景 ===")
    print(f"  character : {profile.canonical_name} / {(profile.localized_names or [''])[0]}")
    print(f"  prompt    : {prompt}")
    print(f"  scene     : {', '.join(SCENARIO['scene_checks'])}")
    print("=== 档案 ===")
    print(f"  profile_status            : {profile.effective_status()}")
    print(f"  human_verified_fields     : {', '.join(profile.human_verified_fields()) or '-'}")
    print(f"  assistant_reviewed_fields : {', '.join(profile.assistant_reviewed_fields()) or '-'}")
    print(f"  unverified_fields         : {', '.join(profile.unverified_fields()) or '-'}")
    print(f"  required                  : {', '.join(profile.required_fields())}")
    print("=== 参考图 ===")
    print(f"  files            : {count_references(cfg, profile)}")
    print(f"  verified         : {verified_n}")
    print(f"  web_reference    : {web_mode}")
    for row in reference_integrity_report(cfg, profile):
        print(f"  - {row['file']} ok={row['ok']} issue={row['issue'] or '-'}")
    print("=== Prompt 分层 ===")
    low = built.text.lower()
    print(f"  has_human_verified_section   : {'human-verified identity constraints' in low}")
    print(f"  has_reference_section        : {'human-verified reference image supplied' in low}")
    print(f"  has_assistant_reviewed_section: {'assistant-reviewed identity hints' in low}")
    print(f"  has_forbidden_authority_leak : "
          f"{'assistant-reviewed' in low and 'do not contradict them' in low}")
    print(f"  human fields                 : {', '.join(built.human_verified_fields) or '-'}")
    print(f"  reviewed fields              : {', '.join(built.assistant_reviewed_fields) or '-'}")
    print(f"  unverified fields (不写入)    : {', '.join(built.unverified_fields) or '-'}")
    print("--- identity block ---")
    print(built.identity_block)
    print("=== 将执行的策略 ===")
    for name in names:
        spec = STRATEGIES[name]
        ref_mode = "local_verified" if spec["reference_policy"] == "local_only" else "none"
        if ref_mode == "local_verified" and verified_n == 0:
            ref_mode = "none（无人工核验图，实际会退化成 text_only）"
        print(f"  {name:<18} policy={spec['reference_policy']:<10} ref={ref_mode}")
    print()
    print(_print_gate(cfg) and "闸门已通过" or "闸门未通过：真实 A/B 仍然阻塞")
    print("未生成任何图片（dry-run）。要真实跑：--run-paid --allow-paid 且环境变量 "
          f"{_ENV_FLAG}=true")
    return 0



async def _evaluate_dir(cfg, directory: Path) -> list[dict]:
    from plugins.dl_senpai.scene_eval import SceneComplianceEvaluator  # noqa: F401

    profile = _profile(cfg)
    rows: list[dict] = []
    for name in STRATEGIES:
        for path in sorted(directory.glob(f"{name}*.png")):
            row = {"strategy": name, "image": path.name, "status": "evaluated",
                   "duration": 0.0, "success": True}
            row.update(await _evaluate_image(cfg, profile, path.read_bytes(), SCENARIO["scene_checks"]))
            rows.append(row)
    return rows


def _print_table(rows: list[dict]) -> None:
    print()
    print("| Strategy | Identity | Scene | Time | Reference | Main Issues |")
    print("|---|---:|---:|---:|---|---|")
    for row in rows:
        ident = row.get("identity_score")
        scene = row.get("scene_score")
        issues = "; ".join(row.get("issues") or [])[:60] or "-"
        ref = "verified" if row.get("strategy") == "local_verified_1" else "none"
        print(
            f"| {row['strategy']} | "
            f"{('n/a' if ident is None else f'{ident:.2f}')} | "
            f"{('n/a' if scene is None else f'{scene:.2f}')} | "
            f"{row.get('duration', 0)} s | {ref} | {issues} |"
        )


async def main() -> int:
    args = _parse_args()
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    from plugins.dl_senpai.config import get_config

    cfg = get_config()
    names = _strategies_of(args)
    paid = _paid_allowed(args)

    if args.evaluate:
        rows = await _evaluate_dir(cfg, Path(args.evaluate))
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            _print_table(rows)
        return 0

    if not args.run_paid:
        return await _dry_run(cfg, names, args.prompt)

    if not paid:
        print(f"拒绝：真实生成需要 --allow-paid 且环境变量 {_ENV_FLAG}=true", file=sys.stderr)
        return 2

    # 闸门放在付费之前：宁可一次都不生成，也不要在没有人工核验依据的情况下
    # 生成两张图然后得出一个看起来有依据的结论。
    if not _print_gate(cfg):
        print("BLOCKED_BY_HUMAN_REFERENCE：未执行任何真实生成，付费调用 0 次。", file=sys.stderr)
        return 3

    rows: list[dict] = []
    for name in names:
        rows.append(await _generate_one(cfg, name, STRATEGIES[name], args.prompt, SCENARIO["scene_checks"]))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        _print_table(rows)
        print()
        print(f"样本量：{len(rows)} 张（单样本，不构成统计结论）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
