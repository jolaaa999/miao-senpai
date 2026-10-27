"""场景合规评分：只看「要求的东西画出来没有」。

为什么不能只评身份一致性：参考图引导（images.edit）有个已知副作用——
人物会更像，但构图/背景容易跟着参考图走，于是「站在火山前」的可能被画成
「站在白底立绘棚里」。只盯 IdentityScore 会漏掉这种退化，必须分开量。

与身份评分一样：只输出分数，不决定要不要重画。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from nonebot import logger

_PROMPT = """这是一张生成图。请逐项判断下列要求是否被满足，每项 0~1（1 = 完全满足）。
只判断列出的项目，不要评价角色长得像不像。

{checks}

只输出 JSON，不要别的话：
{{"checks": {{"项目": 0.0}}, "issues": ["未满足之处，中文，最多三条"]}}"""


@dataclass(frozen=True)
class SceneScore:
    """场景评分。checks 里 None 表示模型没给出判断（不猜）。"""

    overall: float | None = None
    checks: dict[str, float | None] = field(default_factory=dict)
    issues: tuple[str, ...] = ()
    verdict: str = "not_evaluated"


def _band(score: float | None) -> str:
    if score is None:
        return "not_evaluated"
    if score >= 0.9:
        return "excellent"
    if score >= 0.8:
        return "good"
    if score >= 0.7:
        return "acceptable"
    if score >= 0.55:
        return "weak"
    return "reject"


class SceneComplianceEvaluator:
    """按显式列出的场景要求打分。要求由调用方给，不猜用户想要什么。"""

    def __init__(self, requirements: tuple[str, ...]) -> None:
        self.requirements = tuple(r.strip() for r in requirements if str(r).strip())

    def as_text(self) -> str:
        return "、".join(self.requirements)

    async def evaluate(self, config: Any, image: bytes) -> SceneScore:
        if not self.requirements:
            return SceneScore(verdict="not_evaluated")
        checks_text = "\n".join(f"- {r}" for r in self.requirements)
        try:
            from .draw_enhance import _chat, _data_url

            text = await _chat(
                config,
                [
                    {"type": "text", "text": _PROMPT.format(checks=checks_text)},
                    {"type": "image_url", "image_url": {"url": _data_url(image)}},
                ],
                max_tokens=300,
            )
        except Exception as exc:  # noqa: BLE001 — 场景评分是观测项，不能拖垮生图
            logger.warning(f"dl_senpai scene eval failed: {type(exc).__name__}: {exc}")
            return SceneScore(verdict="not_evaluated")

        return self._parse(text)

    def _parse(self, text: str) -> SceneScore:
        data: dict[str, Any] = {}
        match = re.search(r"\{.*\}", text or "", re.S)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:  # noqa: BLE001
                data = {}

        raw = data.get("checks") if isinstance(data.get("checks"), dict) else {}
        checks: dict[str, float | None] = {req: None for req in self.requirements}
        for key, value in raw.items():
            # 模型可能改写项目名（加了标点/缩写），按子串回填，认不出来就丢
            target = None
            if key in checks:
                target = key
            else:
                for req in self.requirements:
                    if key and (key in req or req in key):
                        target = req
                        break
            if target is None:
                continue
            try:
                checks[target] = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                continue

        values = [v for v in checks.values() if isinstance(v, (int, float))]
        overall = sum(values) / len(values) if values else None
        issues: tuple[str, ...] = ()
        raw_issues = data.get("issues")
        if isinstance(raw_issues, list):
            issues = tuple(str(x).strip()[:80] for x in raw_issues[:3] if str(x).strip())
        score = SceneScore(overall=overall, checks=checks, issues=issues, verdict=_band(overall))
        shown = "n/a" if overall is None else f"{overall:.2f}"
        logger.info(f"dl_senpai scene eval overall={shown} verdict={score.verdict}")
        return score


__all__ = ["SceneComplianceEvaluator", "SceneScore"]
