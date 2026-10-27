#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""角色生图 A/B 基准：一张人工核验的参考图到底有没有用？

固定实验，只改一个变量（有没有 human-verified 本地参考图），其余全部锁死：
同 prompt、同档案、同模型、同 provider、同 size、同 quality、同一份评测口径。

    # 1) dry-run（默认，不花钱）：打印闸门 + 分组配置 + Prompt 分层预览
    python scripts/character_generation_benchmark.py

    # 2) 评估已有图片（不花钱）
    python scripts/character_generation_benchmark.py --evaluate benchmark/eyjafjalla/<ts>

    # 3) 真实生成（三处闸门同时打开，且最多 2 次）
    $env:ALLOW_PAID_IMAGE_BENCHMARK="true"
    python scripts/character_generation_benchmark.py --run-paid --allow-paid

为什么闸门这么硬：一次 1536x2048 要一两分钟和真金白银，而且样本只有 n=1 pair。
「这张不好看再抽一次」会把实验变成挑图，结论立刻作废。所以 A 一次、B 一次，
失败就记失败，最多允许一次技术性重试（并打 technical_retry=true）。

盲评纪律：
- 身份/场景/质量评测只把「图片 + 身份依据」交给视觉模型，不告诉它哪张用了参考图；
- 参考图相似度与泄漏只对 B 组算，且签名里同样不接受 strategy / prompt / 模型名；
- human_review_report.md 里两张图先匿名成 Image 1 / Image 2，揭盲放在文末。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 固定实验场景（本阶段第五条）。A/B 必须逐字相同，所以它写死在这里，
# 不允许通过命令行给两组传不同 prompt。
SCENARIO = {
    "character": "艾雅法拉",
    "prompt": (
        "画一张艾雅法拉站在火山前的半身插画，自然表情，角色正面偏三分之二视角，"
        "背景能看出火山环境，但不要让背景遮挡人物主要特征。"
    ),
    # 场景要求显式列出：reference-guided 可能"人更像、场景更差"，必须一起量
    "scene_checks": (
        "火山/熔岩背景",
        "半身构图（腰部以上）",
        "自然表情",
        "正面偏三分之二视角",
        "背景没有遮挡人物主要特征",
    ),
}

# 只两组。策略名会出现在报告里，别改。
STRATEGIES: dict[str, dict[str, object]] = {
    "text_only": {
        "reference_policy": "text_only",   # 完全不碰参考图
        "auto_reference": False,
        "force_reference": "none",
        "generation_mode": "generate",
        "artifact": "A_text_only.png",
        "label": "仅文字档案",
    },
    "local_verified_1": {
        "reference_policy": "local_only",  # 只读本地库，禁止联网
        "auto_reference": True,
        "force_reference": "human_verified_local",
        "generation_mode": "edit",
        "artifact": "B_local_verified.png",
        "label": "1 张人工核验本地参考图",
    },
}

# 硬上限：本阶段最多 2 次真实生图。超过就拒绝执行，不静默加。
MAX_PAID_CALLS = 2
_ENV_FLAG = "ALLOW_PAID_IMAGE_BENCHMARK"
DEFAULT_OUT = REPO / "benchmark"


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="角色生图参考策略 A/B 基准")
    ap.add_argument("--character", default=SCENARIO["character"])
    ap.add_argument("--strategies", default="text_only,local_verified_1")
    ap.add_argument("--run-paid", action="store_true", help="真实生成（还需 env 闸 + --allow-paid）")
    ap.add_argument("--allow-paid", action="store_true", help="第二个付费闸")
    ap.add_argument("--evaluate", default="", help="评估已有产物的目录")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="产物根目录（默认 benchmark/）")
    ap.add_argument("--json", action="store_true")
    return ap.parse_args()


def _paid_allowed(args: argparse.Namespace) -> bool:
    """两个显式闸 + 一个环境变量闸，缺一不可。"""
    return bool(args.run_paid and args.allow_paid
                and os.environ.get(_ENV_FLAG, "").strip().lower() in {"1", "true", "yes"})


def _strategies_of(args: argparse.Namespace) -> list[str]:
    """只允许定义过的策略，且最多 MAX_PAID_CALLS 个。"""
    names = [x.strip() for x in str(args.strategies).split(",") if x.strip()]
    unknown = [x for x in names if x not in STRATEGIES]
    if unknown:
        raise SystemExit(f"未知策略: {', '.join(unknown)}（可用: {', '.join(STRATEGIES)}）")
    if len(names) > MAX_PAID_CALLS:
        raise SystemExit(f"策略数 {len(names)} 超过上限 {MAX_PAID_CALLS}：本阶段只跑一次单样本对照")
    return names


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _gen_params(cfg) -> dict[str, str]:
    """生成参数快照。A/B 必须完全一致，所以生成前后各取一次做比对。"""
    return {
        "model": str(cfg.draw_model_resolved()),
        "provider": type(cfg).__name__,
        "base_url": str(cfg.draw_base_url_resolved()),
        "size": str(getattr(cfg, "draw_size", "") or ""),
        "quality": str(getattr(cfg, "draw_quality", "") or ""),
    }


def _profile(cfg):
    from plugins.dl_senpai.character_registry import CharacterRegistry

    registry = CharacterRegistry.load(cfg)
    profile = registry.resolve(SCENARIO["character"])
    if profile is None:
        raise SystemExit("内置数据里没有艾雅法拉")
    return profile


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
    """盲评：只给图片和身份依据，不给策略名、不给生成用的 Prompt。

    为什么不传 Prompt：模型看到"银白长发"会给图片的灰色头发打高分——
    那是在测提示词，不是测图。
    """
    from plugins.dl_senpai.character_references import count_verified_references
    from plugins.dl_senpai.identity_eval import CharacterIdentityEvaluator
    from plugins.dl_senpai.reference_eval import evaluate_visual_quality
    from plugins.dl_senpai.scene_eval import SceneComplianceEvaluator

    identity = await CharacterIdentityEvaluator(
        profile, verified_reference_count=count_verified_references(cfg, profile)
    ).evaluate(cfg, image)
    scene = await SceneComplianceEvaluator(scene_checks).evaluate(cfg, image)
    quality = await evaluate_visual_quality(cfg, image)
    return {
        "identity_score": None if identity.overall is None else round(identity.overall, 3),
        "identity_verdict": identity.verdict,
        "identity_basis": identity.basis,
        "identity_dimensions": {
            k: (None if v is None else round(v, 3)) for k, v in identity.dimensions.items()
        },
        "scene_score": None if scene.overall is None else round(scene.overall, 3),
        "scene_checks": {
            k: (None if v is None else round(v, 3)) for k, v in scene.checks.items()
        },
        "visual_quality": quality.overall,
        "visual_quality_dimensions": quality.dimensions,
        "issues": list(identity.issues) + list(scene.issues),
        **quality.as_dict(),
    }


async def _evaluate_reference_side(cfg, target: bytes, reference: bytes) -> dict:
    """B 组专属：像不像本人 + 抄没抄构图。A 组没有参考图，这两个指标必须是 null。"""
    from plugins.dl_senpai.reference_eval import (
        evaluate_reference_leakage,
        evaluate_reference_similarity,
    )

    similarity = await evaluate_reference_similarity(cfg, target, reference)
    leakage = await evaluate_reference_leakage(cfg, target, reference)
    return {
        "reference_identity_similarity": similarity.similarity if similarity.similarity is None
        else round(similarity.similarity, 3),
        "same_character": similarity.same_character,
        "similarity_verdict": similarity.verdict,
        "similarity_issues": list(similarity.issues),
        **leakage.as_dict(),
        "issues": list(similarity.issues) + list(leakage.notes),
    }


def _load_verified_reference(cfg, profile) -> tuple[bytes | None, str]:
    """取那张人工核验过的本地参考图（并复核 sha256）。

    拿不到就返回 None —— B 组此时应当记为参考图缺失，而不是退回文字模式
    悄悄跑完、让人误以为做过对照。
    """
    from plugins.dl_senpai.character_references import (
        resolve_library_reference,
        verify_reference_integrity,
        profile_dir,
        load_manifest,
    )

    diag: list[str] = []
    picked = resolve_library_reference(cfg, profile, user_request=SCENARIO["prompt"], diagnostics=diag)
    if picked is None:
        return None, "; ".join(diag) or "no_verified_reference"
    manifest = load_manifest(cfg, profile)
    entry = next((e for e in (manifest.references if manifest else ()) if e.file == picked.filename), None)
    if entry is not None:
        ok, issue, raw = verify_reference_integrity(profile_dir(cfg, profile), entry)
        if not ok:
            return None, f"reference_integrity_failed: {issue}"
        return raw, picked.filename
    return picked.raw, picked.filename


async def _generate_one(
    cfg, name: str, spec: dict, prompt: str, scene_checks, outdir: Path, reference_raw: bytes | None
) -> dict:
    from plugins.dl_senpai.image_gen import build_image_segment

    params = _gen_params(cfg)
    started = time.monotonic()
    technical_retry = False
    seg = None
    try:
        seg = await build_image_segment(
            marker_prompt="",
            user_request=prompt,
            config=cfg,
            auto_reference=bool(spec["auto_reference"]),
            reference_policy=str(spec["reference_policy"]),
            archive_session_id=f"bench-{name}",
        )
    except Exception as exc:  # noqa: BLE001 — 网络/供应商错误要记下来，不能静默重生成
        # 只允许对"技术性失败"重试一次，且明确打标；成功图不重抽。
        technical_retry = True
        print(f"  ! {name} 首次失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            seg = await build_image_segment(
                marker_prompt="",
                user_request=prompt,
                config=cfg,
                auto_reference=bool(spec["auto_reference"]),
                reference_policy=str(spec["reference_policy"]),
                archive_session_id=f"bench-{name}-retry",
            )
        except Exception as exc2:  # noqa: BLE001
            return {
                "strategy": name,
                "status": "failed",
                "success": False,
                "technical_retry": technical_retry,
                "error": f"{type(exc2).__name__}: {exc2}",
                "duration": round(time.monotonic() - started, 1),
                "generation_params": params,
            }
    spent = round(time.monotonic() - started, 1)

    if seg is None:
        return {
            "strategy": name, "status": "no_image", "success": False,
            "technical_retry": technical_retry, "duration": spent, "generation_params": params,
        }

    path = _segment_path(seg)
    row: dict = {
        "strategy": name,
        "label": spec["label"],
        "status": "generated",
        "success": True,
        "technical_retry": technical_retry,
        "duration": spent,
        "generation_mode": spec["generation_mode"],
        "generation_params": params,
        "image": "",
        "image_sha256": "",
    }
    if not path or not path.is_file():
        return row

    raw = path.read_bytes()
    saved = outdir / str(spec["artifact"])
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_bytes(raw)
    row["image"] = saved.name
    row["image_sha256"] = _sha256(raw)

    profile = _profile(cfg)
    row.update(await _evaluate_image(cfg, profile, raw, scene_checks))
    # 只有带参考图的那组才评"像不像原图"和"抄没抄构图"
    if spec["force_reference"] == "human_verified_local" and reference_raw:
        row.update(await _evaluate_reference_side(cfg, raw, reference_raw))
    return row


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


def _classify(rows: list[dict]) -> dict[str, str]:
    """本次实验的临时分类（不是生产规则）。

    Strong Win / Weak Win / Tradeoff / No Benefit，阈值写死在报告里，
    免得事后按结果调整口径。
    """
    by = {r["strategy"]: r for r in rows if r.get("success")}
    a, b = by.get("text_only"), by.get("local_verified_1")
    if not a or not b:
        return {"verdict": "INCOMPLETE", "reason": "缺一组结果，不能下结论"}
    ai, bi = a.get("identity_score"), b.get("identity_score")
    asc, bsc = a.get("scene_score"), b.get("scene_score")
    if ai is None or bi is None:
        return {"verdict": "NOT_EVALUATED", "reason": "身份分未产出（缺人工核验依据）"}
    delta = bi - ai
    leakage = b.get("reference_leakage_overall")
    scene_ok = asc is None or bsc is None or bsc >= asc - 0.05
    leakage_ok = leakage is None or leakage < 0.4
    if delta >= 0.10 and scene_ok and leakage_ok:
        return {"verdict": "STRONG_WIN", "identity_delta": f"{delta:+.3f}"}
    if delta > 0 and scene_ok and leakage_ok:
        return {"verdict": "WEAK_WIN", "identity_delta": f"{delta:+.3f}"}
    if delta > 0 and not (scene_ok and leakage_ok):
        return {"verdict": "TRADEOFF", "identity_delta": f"{delta:+.3f}"}
    return {"verdict": "NO_BENEFIT", "identity_delta": f"{delta:+.3f}"}


def _fmt(value) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _print_table(rows: list[dict]) -> None:
    """第二十七条那张表。缺的指标写 N/A，不写 0——0 是"测了但很差"，N/A 是"没法测"。"""
    by = {r["strategy"]: r for r in rows}
    a = by.get("text_only", {})
    b = by.get("local_verified_1", {})
    ad = a.get("identity_dimensions") or {}
    bd = b.get("identity_dimensions") or {}

    def dim(key: str, side: dict) -> str:
        return _fmt(side.get(key))

    print()
    print("| Metric | Text Only | Local Verified |")
    print("|---|---:|---:|")
    print(f"| IdentityScore | {_fmt(a.get('identity_score'))} | {_fmt(b.get('identity_score'))} |")
    for key, label in (
        ("face", "Face"), ("hair", "Hair"), ("horns", "Horns"),
        ("clothing", "Clothing"), ("palette", "Palette"),
    ):
        print(f"| {label} | {dim(key, ad)} | {dim(key, bd)} |")
    print(f"| Reference Identity Similarity | N/A | {_fmt(b.get('reference_identity_similarity'))} |")
    print(f"| SceneScore | {_fmt(a.get('scene_score'))} | {_fmt(b.get('scene_score'))} |")
    print(f"| VisualQuality | {_fmt(a.get('visual_quality'))} | {_fmt(b.get('visual_quality'))} |")
    print(f"| Duration | {a.get('duration', 0)} s | {b.get('duration', 0)} s |")
    print(f"| Generation Mode | generate | edit |")
    print(f"| Reference Leakage | N/A | {_fmt(b.get('reference_leakage_overall'))} |")
    print()
    print("A issues:", "; ".join(a.get("issues") or [])[:200] or "-")
    print("B issues:", "; ".join(b.get("issues") or [])[:200] or "-")


def _write_artifacts(outdir: Path, rows: list[dict], meta: dict) -> None:
    """把结果落盘。图片与 result.json 都不进 Git（见 .gitignore）。"""
    outdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "sample_size": "1 pair",
        "note": "n=1，禁止外推为总体效果；本文件只描述这一次实验。",
        **meta,
        "rows": rows,
        "decision": _classify(rows),
    }
    (outdir / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 盲评报告：先把两张图复制成中性文件名。
    #
    # 为什么不能直接引用 A_text_only.png / B_local_verified.png：
    # 文件名本身就剧透了哪张用了参考图，人工盲评会立刻失效。
    # 目录里的正式产物仍按第九条命名（方便对账），blind/ 下才是给人看的。
    by = {r["strategy"]: r for r in rows}
    a, b = by.get("text_only", {}), by.get("local_verified_1", {})
    pair = [("A_text_only", a), ("B_local_verified", b)]
    # 用图片 hash 排序决定匿名顺序：可复现，但看报告的人推不出来
    pair.sort(key=lambda kv: kv[1].get("image_sha256") or kv[0])

    blind_dir = outdir / "blind"
    blind_dir.mkdir(parents=True, exist_ok=True)
    for idx, (name, row) in enumerate(pair, start=1):
        src_name = row.get("image") or ""
        if not src_name:
            row["blind_name"] = ""
            continue
        src = outdir / src_name
        if not src.is_file():
            row["blind_name"] = ""
            continue
        neutral = f"image_{idx}.png"
        (blind_dir / neutral).write_bytes(src.read_bytes())
        row["blind_name"] = f"blind/{neutral}"
    lines = [
        "# 盲评报告（n = 1 pair）",
        "",
        "> 先只看图，别往下翻到揭盲节。",
        "",
    ]
    for idx, (name, row) in enumerate(pair, start=1):
        lines.append(f"## Image {idx}")
        lines.append("")
        lines.append(f"文件：{row.get('blind_name') or '(缺失)'}")
        lines.append("")
        for key, label in (
            ("identity_score", "IdentityScore"), ("scene_score", "SceneScore"),
            ("visual_quality", "VisualQuality"),
        ):
            lines.append(f"- {label}: {_fmt(row.get(key))}")
        dims = row.get("identity_dimensions") or {}
        shown = ", ".join(f"{k}={_fmt(v)}" for k, v in dims.items() if v is not None) or "-"
        lines.append(f"- Identity dimensions: {shown}")
        issues = "; ".join(row.get("issues") or [])[:300] or "-"
        lines.append(f"- Issues: {issues}")
        lines.append("")
    lines += [
        "## 请你先回答",
        "",
        "更像艾雅法拉的是： 1 / 2 / 差不多",
        "",
        "---",
        "",
        "<details><summary>揭盲（回答完再展开）</summary>",
        "",
    ]
    for idx, (name, row) in enumerate(pair, start=1):
        lines.append(f"- Image {idx} = {name}（mode={row.get('generation_mode')}）")
    lines += ["", "</details>", ""]
    (outdir / "human_review_report.md").write_text("\n".join(lines), encoding="utf-8")


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
        return await _dry_run(cfg, names, SCENARIO["prompt"])

    if not paid:
        print(f"拒绝：真实生成需要 --allow-paid 且环境变量 {_ENV_FLAG}=true", file=sys.stderr)
        return 2

    # 闸门放在付费之前：宁可一次都不生成，也不要在没有人工核验依据的情况下
    # 生成两张图然后得出一个看起来有依据的结论。
    if not _print_gate(cfg):
        print("BLOCKED_BY_HUMAN_REFERENCE：未执行任何真实生成，付费调用 0 次。", file=sys.stderr)
        return 3

    profile = _profile(cfg)
    reference_raw, reference_note = _load_verified_reference(cfg, profile)
    if reference_raw is None:
        # B 组拿不到核验图就整组不做——不许退回文字模式悄悄跑完
        print(f"REFERENCE_INTEGRITY_FAILED: {reference_note}", file=sys.stderr)
        print("BLOCKED_BY_HUMAN_REFERENCE：未执行任何真实生成，付费调用 0 次。", file=sys.stderr)
        return 4

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = Path(args.out) / "eyjafjalla" / stamp
    meta = {
        "character": profile.canonical_name,
        "prompt": SCENARIO["prompt"],
        "scene_checks": list(SCENARIO["scene_checks"]),
        "generation_params": _gen_params(cfg),
        "reference_file": reference_note,
        "reference_sha256": _sha256(reference_raw),
        "profile_status": profile.effective_status(),
        "human_verified_fields": list(profile.human_verified_fields()),
        "timestamp": stamp,
    }

    rows: list[dict] = []
    for name in names:
        print(f"生成 {name} …", file=sys.stderr)
        rows.append(
            await _generate_one(
                cfg, name, STRATEGIES[name], SCENARIO["prompt"],
                SCENARIO["scene_checks"], outdir, reference_raw,
            )
        )

    _write_artifacts(outdir, rows, meta)
    if args.json:
        print(json.dumps({"rows": rows, "decision": _classify(rows)}, ensure_ascii=False, indent=2))
    else:
        _print_table(rows)
        print()
        print(f"样本量：{len(rows)} 张 / 1 pair（单样本，不构成统计结论，不可外推）")
        print(f"决策分类：{_classify(rows)}")
        print(f"产物目录：{outdir}（不进 Git）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
