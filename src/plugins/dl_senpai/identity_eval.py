"""角色身份评分：把「像不像」变成可记录的分数。

为什么需要它：judge_image 只会回一句「不合格 + 问题描述」，粒度太粗——
「服装差一点」和「脸完全不是她」在它那里是同一件事。要改进生图，得先能
分辨是哪一维在拖后腿。

三条硬规矩：

1. **只评有依据的字段**。没有验证过的档案字段、也没有已验证参考图支撑时，
   该维度返回 None（not_evaluated），而不是让模型凭记忆自说自话打分。
   未知不是 0 分，也不是满分。
2. **总分不是简单平均**。脸/发型/角这类决定「一眼认出是谁」的维度权重更高，
   权重来自 CharacterProfile.salience（先验默认表，不为某个角色写死）。
3. **只测量，不驱动重画**。这里绝不返回「要不要重画」，也不会去调生图接口——
   无限重画会把费用和时间拖成黑洞。重画策略要单独设计（bounded retry）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from nonebot import logger

from .character_registry import CharacterProfile

# 分数 → 结论。区间是通用约定，与具体角色无关。
_VERDICT_BANDS: tuple[tuple[float, str], ...] = (
    (0.90, "excellent"),
    (0.80, "good"),
    (0.70, "acceptable"),
    (0.55, "weak"),
    (0.00, "reject"),
)

_PROMPT = """这是角色「{name}」（{franchise}）的一张生成图。请**只**依据下面列出的项目打分，
每项 0~1（1 = 完全一致）。不要评价未列出的项目，也不要用你自己的记忆补充设定。

{facts}

只输出 JSON，不要别的话：
{{"dimensions": {{"字段key": 0.0}}, "issues": ["问题，中文，最多三条"]}}"""


def verdict_for(score: float | None) -> str:
    """分数 → 结论档位。"""
    if score is None:
        return "not_evaluated"
    for threshold, name in _VERDICT_BANDS:
        if score >= threshold:
            return name
    return "reject"


@dataclass(frozen=True)
class IdentityScore:
    """一次身份评分。dimensions 里 None 表示「没有依据，不评」。"""

    character: str = ""
    overall: float | None = None
    dimensions: dict[str, float | None] = field(default_factory=dict)
    issues: tuple[str, ...] = ()
    verdict: str = "not_evaluated"
    evaluated_fields: tuple[str, ...] = ()
    not_evaluated: tuple[str, ...] = ()
    basis: str = "none"      # verified_profile / verified_profile+reference / verified_reference / none


class CharacterIdentityEvaluator:
    """按档案可信度决定「哪些维度有资格被打分」。"""

    def __init__(
        self,
        profile: CharacterProfile,
        *,
        verified_reference_count: int = 0,
    ) -> None:
        self.profile = profile
        self.verified_reference_count = max(0, int(verified_reference_count))

    def evaluable_fields(self) -> tuple[str, ...]:
        """可评分的字段。

        - 有已验证字段时：只评这些（有文字依据）。
        - 一条都没验证、但有已验证参考图时：可以评档案里所有非空字段
          （依据是那张图，不是模型记忆）。
        - 两者都没有：什么都不评，返回空。
        """
        verified = self.profile.verified_fields()
        if verified:
            return tuple(k for k in verified if self.profile.visual_identity.get(k))
        if self.verified_reference_count > 0:
            return tuple(self.profile.visual_identity.keys())
        return ()

    def _basis(self) -> str:
        has_verified = bool(self.profile.verified_fields())
        if has_verified and self.verified_reference_count:
            return "verified_profile+reference"
        if has_verified:
            return "verified_profile"
        if self.verified_reference_count:
            return "verified_reference"
        return "none"

    def weighted_overall(self, dimensions: dict[str, float | None]) -> float | None:
        """按身份显著性加权求总分（不是算术平均）。"""
        total_w = 0.0
        total = 0.0
        for key, value in dimensions.items():
            if not isinstance(value, (int, float)):
                continue
            weight = self.profile.salience(key)
            total += float(value) * weight
            total_w += weight
        if total_w <= 0:
            return None
        return max(0.0, min(1.0, total / total_w))

    async def evaluate(self, config: Any, image: bytes) -> IdentityScore:
        """给一张图打分。任何异常都退回 not_evaluated，绝不影响出图。"""
        fields = self.evaluable_fields()
        if not fields:
            logger.info(
                f"dl_senpai identity eval skipped character={self.profile.canonical_name} "
                f"reason=no_verified_basis"
            )
            return IdentityScore(
                character=self.profile.canonical_name,
                verdict="not_evaluated",
                basis="none",
                not_evaluated=tuple(self.profile.visual_identity.keys()),
            )

        facts = "\n".join(
            f"- {key}（{self.profile.visual_identity.label(key)}）：{self.profile.visual_identity.get(key)}"
            for key in fields
        )
        try:
            from .draw_enhance import _chat, _data_url

            text = await _chat(
                config,
                [
                    {
                        "type": "text",
                        "text": _PROMPT.format(
                            name=self.profile.display_name,
                            franchise=self.profile.franchise_display,
                            facts=facts,
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": _data_url(image)}},
                ],
                max_tokens=400,
            )
        except Exception as exc:  # noqa: BLE001 — 评分是观测项，不能拖垮生图
            logger.warning(f"dl_senpai identity eval failed: {type(exc).__name__}: {exc}")
            return IdentityScore(
                character=self.profile.canonical_name,
                verdict="not_evaluated",
                basis=self._basis(),
            )

        return self._parse(text, fields)

    def _parse(self, text: str, fields: tuple[str, ...]) -> IdentityScore:
        dimensions: dict[str, float | None] = {key: None for key in self.profile.visual_identity.keys()}
        issues: tuple[str, ...] = ()
        data: dict[str, Any] = {}
        match = re.search(r"\{.*\}", text or "", re.S)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:  # noqa: BLE001
                data = {}

        raw_dims = data.get("dimensions") if isinstance(data.get("dimensions"), dict) else {}
        for key, value in raw_dims.items():
            if key not in dimensions:
                continue          # 模型冒出来的字段一律忽略：它没有依据
            if key not in fields:
                continue          # 不在可评列表里的也不接受
            try:
                dimensions[key] = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                continue

        raw_issues = data.get("issues")
        if isinstance(raw_issues, list):
            issues = tuple(str(x).strip()[:80] for x in raw_issues[:3] if str(x).strip())

        overall = self.weighted_overall(dimensions)
        evaluated = tuple(k for k, v in dimensions.items() if isinstance(v, (int, float)))
        not_evaluated = tuple(k for k in dimensions if k not in evaluated)
        score = IdentityScore(
            character=self.profile.canonical_name,
            overall=overall,
            dimensions=dimensions,
            issues=issues,
            verdict=verdict_for(overall),
            evaluated_fields=evaluated,
            not_evaluated=not_evaluated,
            basis=self._basis(),
        )
        shown = "n/a" if score.overall is None else f"{score.overall:.2f}"
        logger.info(
            f"dl_senpai identity eval character={score.character} overall={shown} "
            f"verdict={score.verdict} evaluated={','.join(score.evaluated_fields) or '-'} "
            f"skipped={len(score.not_evaluated)}"
        )
        return score


__all__ = ["CharacterIdentityEvaluator", "IdentityScore", "verdict_for"]
