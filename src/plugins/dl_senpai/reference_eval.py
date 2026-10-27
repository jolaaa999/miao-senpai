"""只有参考图策略才需要的一组评测：像不像本人 / 抄没抄构图 / 画得干不干净。

为什么不把这些塞进 CharacterIdentityEvaluator：

1. 身份分和「画面质量」是两件事。混在一起就会出现「这张更漂亮所以更像艾雅法拉」
   这种伪结论，而那正是本阶段要排除的偏差。
2. reference_identity_similarity 与 reference_leakage 只有 B 组（带参考图）才有，
   塞进共用评测器会让 A/B 两组的打分口径悄悄分叉。

## 盲评纪律

本模块所有公开函数的签名里**只有图和 config**，没有 strategy / prompt / model /
generation_mode。这不是风格问题：

    视觉模型看到 Prompt 写着"银白长发"→ 图有点灰 → 给 0.8

那就变成在测提示词，不是在测图。测试里有一条专门断言这些参数传不进来。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


def _clamp(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, num))


def _extract_json(text: str) -> dict[str, Any]:
    """从模型回复里挖出第一个 JSON 对象。挖不到就当空。"""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except Exception:  # noqa: BLE001 — 模型偶尔会带注释，解析失败不该炸
        return {}
    return data if isinstance(data, dict) else {}


# ---------------- 参考图身份相似度 ----------------

_SIMILARITY_PROMPT = """You are given two images of anime characters: IMAGE 1 is a reference
(the same character), IMAGE 2 is a generated image.

Question: do IMAGE 1 and IMAGE 2 depict the SAME character identity?

Important:
- Pose, camera angle, framing, background and art style MAY differ. Do not penalize those.
- Judge identity cues only: facial structure, hair (color/shape/length), eye color/shape,
  horns/ears/tail, outfit design, signature accessories, overall color palette.
- If IMAGE 2 is a generic anime character that merely shares one or two traits, score low.
- If they are clearly the same canonical design, score high even if the pose differs.

Reply with JSON only:
{"similarity": 0.0-1.0, "same_character": true/false, "issues": ["..."]}"""


@dataclass(frozen=True)
class ReferenceSimilarity:
    """生成图与原参考图「是不是同一个角色」的相似度。"""

    similarity: float | None = None
    same_character: bool | None = None
    issues: tuple[str, ...] = ()
    verdict: str = "not_evaluated"

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference_identity_similarity": (
                round(self.similarity, 3) if self.similarity is not None else None
            ),
            "same_character": self.same_character,
            "similarity_issues": list(self.issues) or None,
            "similarity_verdict": self.verdict,
        }


def _verdict(similarity: float | None) -> str:
    if similarity is None:
        return "not_evaluated"
    if similarity >= 0.85:
        return "same_character"
    if similarity >= 0.7:
        return "likely_same"
    if similarity >= 0.5:
        return "weak_resemblance"
    return "different_character"


async def evaluate_reference_similarity(
    config: Any, target: bytes, reference: bytes
) -> ReferenceSimilarity:
    """比较生成图与人工核验参考图的身份相似度。

    只接受两张图：不传 strategy、不传 prompt、不传模型名。
    """
    if not target or not reference:
        return ReferenceSimilarity()
    try:
        from .draw_enhance import _chat, _data_url

        text = await _chat(
            config,
            [
                {"type": "text", "text": _SIMILARITY_PROMPT},
                {"type": "text", "text": "IMAGE 1 (reference):"},
                {"type": "image_url", "image_url": {"url": _data_url(reference)}},
                {"type": "text", "text": "IMAGE 2 (generated):"},
                {"type": "image_url", "image_url": {"url": _data_url(target)}},
            ],
            max_tokens=400,
        )
    except Exception as exc:  # noqa: BLE001 — 评测是观测项，不能拖垮实验
        logger.warning(f"dl_senpai reference similarity failed: {type(exc).__name__}: {exc}")
        return ReferenceSimilarity()

    data = _extract_json(text)
    similarity = _clamp(data.get("similarity"))
    same = data.get("same_character")
    issues: tuple[str, ...] = ()
    if isinstance(data.get("issues"), list):
        issues = tuple(str(x).strip()[:80] for x in data["issues"][:3] if str(x).strip())
    return ReferenceSimilarity(
        similarity=similarity,
        same_character=bool(same) if isinstance(same, bool) else None,
        issues=issues,
        verdict=_verdict(similarity),
    )


# ---------------- 参考图泄漏 ----------------

_LEAKAGE_PROMPT = """You are given two images: IMAGE 1 is a reference image used as a
generation guide; IMAGE 2 is the generated output.

Question: how much did IMAGE 2 copy from IMAGE 1 beyond character identity?

Score each dimension from 0.0 to 1.0:
- pose_leakage:        0 = completely different pose, 1 = same pose copied
- composition_leakage: 0 = different framing/layout, 1 = same framing copied
- background_leakage:  0 = different background, 1 = same background copied
- style_leakage:       0 = different rendering style, 1 = same style copied

Copying the character's identity (hair, horns, outfit) is EXPECTED and must NOT count
as leakage. Only pose / composition / background / style count.

Reply with JSON only:
{"pose_leakage":0.0,"composition_leakage":0.0,"background_leakage":0.0,"style_leakage":0.0,"notes":["..."]}"""

_LEAKAGE_KEYS = ("pose_leakage", "composition_leakage", "background_leakage", "style_leakage")


@dataclass(frozen=True)
class LeakageScore:
    """参考图泄漏：0 = 没照抄，1 = 几乎照搬。"""

    dimensions: dict[str, float | None] = field(default_factory=dict)
    overall: float | None = None
    notes: tuple[str, ...] = ()
    verdict: str = "not_evaluated"

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference_leakage": (
                {k: (round(v, 3) if isinstance(v, (int, float)) else None)
                 for k, v in self.dimensions.items()}
                or None
            ),
            "reference_leakage_overall": (
                round(self.overall, 3) if self.overall is not None else None
            ),
            "reference_leakage_notes": list(self.notes) or None,
            "reference_leakage_verdict": self.verdict,
        }


def _leakage_verdict(overall: float | None) -> str:
    if overall is None:
        return "not_evaluated"
    if overall >= 0.6:
        return "heavy_leakage"
    if overall >= 0.4:
        return "noticeable_leakage"
    if overall >= 0.25:
        return "mild_leakage"
    return "acceptable"


async def evaluate_reference_leakage(
    config: Any, target: bytes, reference: bytes
) -> LeakageScore:
    """只有 B 组（带参考图）才调用。同样只看图，不看 strategy。"""
    if not target or not reference:
        return LeakageScore()
    try:
        from .draw_enhance import _chat, _data_url

        text = await _chat(
            config,
            [
                {"type": "text", "text": _LEAKAGE_PROMPT},
                {"type": "text", "text": "IMAGE 1 (reference):"},
                {"type": "image_url", "image_url": {"url": _data_url(reference)}},
                {"type": "text", "text": "IMAGE 2 (generated):"},
                {"type": "image_url", "image_url": {"url": _data_url(target)}},
            ],
            max_tokens=400,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai reference leakage failed: {type(exc).__name__}: {exc}")
        return LeakageScore()

    data = _extract_json(text)
    dims: dict[str, float | None] = {key: _clamp(data.get(key)) for key in _LEAKAGE_KEYS}
    values = [v for v in dims.values() if isinstance(v, (int, float))]
    overall = sum(values) / len(values) if values else None
    notes: tuple[str, ...] = ()
    if isinstance(data.get("notes"), list):
        notes = tuple(str(x).strip()[:80] for x in data["notes"][:3] if str(x).strip())
    return LeakageScore(
        dimensions=dims, overall=overall, notes=notes, verdict=_leakage_verdict(overall)
    )


# ---------------- 画面质量（非核心指标） ----------------

_QUALITY_PROMPT = """Rate this anime illustration on purely technical grounds.

Score each from 0.0 to 1.0:
- anatomy:        body structure, hands, proportions (no extra limbs, no melted faces)
- artifacts:      absence of rendering artifacts, warped lines, texture mush
- composition:    framing and balance (is the subject readable and well placed)
- coherence:      internal consistency of lighting, perspective and detail level

Do NOT judge whether it looks like any particular character, and do NOT reward prettiness
or style preference. Technical quality only.

Reply with JSON only:
{"anatomy":0.0,"artifacts":0.0,"composition":0.0,"coherence":0.0,"notes":["..."]}"""

_QUALITY_KEYS = ("anatomy", "artifacts", "composition", "coherence")


@dataclass(frozen=True)
class VisualQuality:
    dimensions: dict[str, float | None] = field(default_factory=dict)
    overall: float | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "visual_quality": round(self.overall, 3) if self.overall is not None else None,
            "visual_quality_dimensions": (
                {k: (round(v, 3) if isinstance(v, (int, float)) else None)
                 for k, v in self.dimensions.items()}
                or None
            ),
            "visual_quality_notes": list(self.notes) or None,
        }


async def evaluate_visual_quality(config: Any, image: bytes) -> VisualQuality:
    if not image:
        return VisualQuality()
    try:
        from .draw_enhance import _chat, _data_url

        text = await _chat(
            config,
            [
                {"type": "text", "text": _QUALITY_PROMPT},
                {"type": "image_url", "image_url": {"url": _data_url(image)}},
            ],
            max_tokens=400,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai visual quality failed: {type(exc).__name__}: {exc}")
        return VisualQuality()

    data = _extract_json(text)
    dims: dict[str, float | None] = {key: _clamp(data.get(key)) for key in _QUALITY_KEYS}
    values = [v for v in dims.values() if isinstance(v, (int, float))]
    overall = sum(values) / len(values) if values else None
    notes: tuple[str, ...] = ()
    if isinstance(data.get("notes"), list):
        notes = tuple(str(x).strip()[:80] for x in data["notes"][:3] if str(x).strip())
    return VisualQuality(dimensions=dims, overall=overall, notes=notes)


__all__ = [
    "LeakageScore",
    "ReferenceSimilarity",
    "VisualQuality",
    "evaluate_reference_leakage",
    "evaluate_reference_similarity",
    "evaluate_visual_quality",
]
