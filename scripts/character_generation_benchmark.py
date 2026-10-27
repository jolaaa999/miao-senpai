#!/usr/bin/env python
"""角色生图 A/B 基准：比较不同参考策略的身份一致性。

用法（默认**只评估已有图片**，不花钱）：

    python scripts/character_generation_benchmark.py \
        --prompt "画一张艾雅法拉站在火山前" \
        --images data/dl_senpai/bench/ --character 艾雅法拉

目录里按策略前缀放图，例如：

    data/dl_senpai/bench/text_only_01.png
    data/dl_senpai/bench/local_verified_01.png
    data/dl_senpai/bench/web_verified_01.png

要真实生成必须显式开两个闸（缺一不可）：

    set ALLOW_PAID_IMAGE_BENCHMARK=true
    python scripts/character_generation_benchmark.py --allow-paid ...

为什么默认不生成：一次 1536x2048 大约 130 秒、要花钱。基准是拿来做判断的，
不是拿来自动烧预算的。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

STRATEGIES = ("text_only", "local_verified", "web_verified")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="角色生图参考策略基准")
    parser.add_argument("--prompt", default="画一张艾雅法拉站在火山前", help="用户原话")
    parser.add_argument("--character", default="艾雅法拉", help="角色名（用于解析档案）")
    parser.add_argument("--images", default="data/dl_senpai/bench", help="已有图片目录")
    parser.add_argument("--strategies", default=",".join(STRATEGIES), help="逗号分隔的策略名")
    parser.add_argument("--allow-paid", action="store_true", help="允许真实调用生图 API")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")
    return parser.parse_args()


def _paid_allowed(args: argparse.Namespace) -> bool:
    """两个闸都开才允许花钱：显式参数 + 显式环境变量。"""
    env = str(os.environ.get("ALLOW_PAID_IMAGE_BENCHMARK", "")).strip().lower()
    return bool(args.allow_paid) and env in ("1", "true", "yes", "on")


async def _evaluate_existing(cfg, profile, strategy: str, image_dir: Path) -> dict:
    from plugins.dl_senpai.character_references import count_verified_references
    from plugins.dl_senpai.identity_eval import CharacterIdentityEvaluator

    matches = sorted(image_dir.glob(f"{strategy}*.png")) if image_dir.is_dir() else []
    if not matches:
        return {"strategy": strategy, "status": "no_image", "identity_score": None,
                "duration": 0.0, "success": None, "issues": []}

    evaluator = CharacterIdentityEvaluator(
        profile, verified_reference_count=count_verified_references(cfg, profile)
    )
    rows = []
    for path in matches:
        started = time.monotonic()
        score = await evaluator.evaluate(cfg, path.read_bytes())
        rows.append({
            "strategy": strategy,
            "image": path.name,
            "status": "evaluated",
            "identity_score": None if score.overall is None else round(score.overall, 3),
            "verdict": score.verdict,
            "dimensions": {k: (None if v is None else round(v, 3)) for k, v in score.dimensions.items()},
            "duration": round(time.monotonic() - started, 2),
            "success": True,
            "issues": list(score.issues),
        })
    return rows


async def _generate(cfg, strategy: str, prompt: str) -> dict:
    """真实生成（仅在双闸都开时调用）。"""
    from plugins.dl_senpai.image_gen import build_image_segment

    started = time.monotonic()
    segment = await build_image_segment(
        resolved_prompt="",
        user_request=prompt,
        config=cfg,
        auto_reference=strategy != "text_only",
        archive_session_id="",
    )
    return {
        "strategy": strategy,
        "status": "generated" if segment else "no_image",
        "identity_score": None,
        "duration": round(time.monotonic() - started, 1),
        "success": segment is not None,
        "issues": [],
    }


async def main() -> int:
    args = _parse_args()
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    from plugins.dl_senpai.character_registry import CharacterRegistry
    from plugins.dl_senpai.config import get_config

    cfg = get_config()
    registry = CharacterRegistry.load(cfg)
    profile = registry.resolve(args.character)
    if profile is None:
        print(f"未找到角色: {args.character}（可先用 character_registry 确认名字）", file=sys.stderr)
        return 2

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    image_dir = Path(args.images)
    paid = _paid_allowed(args)

    rows: list[dict] = []
    for strategy in strategies:
        if paid:
            rows.append(await _generate(cfg, strategy, args.prompt))
        else:
            result = await _evaluate_existing(cfg, profile, strategy, image_dir)
            rows.extend(result if isinstance(result, list) else [result])

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f"\n角色: {profile.display_name}  策略: {','.join(strategies)}  付费: {paid}")
    print(f"{'strategy':<16}{'score':<8}{'verdict':<16}{'duration':<10}{'success':<8}issues")
    for row in rows:
        score = row.get("identity_score")
        print(
            f"{row['strategy']:<16}"
            f"{('n/a' if score is None else f'{score:.2f}'):<8}"
            f"{row.get('verdict', row['status']):<16}"
            f"{row['duration']:<10}"
            f"{str(row.get('success')):<8}"
            f"{'; '.join(row.get('issues') or [])[:40]}"
        )
    if not paid:
        print("\n（未开启付费生成：以上仅评估 --images 目录里已有的图）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
