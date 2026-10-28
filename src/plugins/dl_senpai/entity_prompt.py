"""通用实体提示词：没有 CharacterProfile 也能锁住身份。

## 为什么不能只用 ImagePromptBuilder

ImagePromptBuilder 的前提是「本地有档案」。可用户会提到几千个角色，档案永远不可能
铺满。档案缺失时旧行为是退回普通生图 —— 于是「画犬夜叉」出来一张泛泛的古风少年。

这一层只依赖 Entity（名字 + 作品 + 少量高置信特征），有档案时那些字段更硬，
没档案时至少还有**名字和作品名**这个锚点：图片模型对知名角色的拉丁名识别度
远高于任何形容词堆砌。

## 两条不能破的规矩

1. 实体锚点永不丢失：canonical_name / localized_name / franchise 至少保留一个，
   原话里的写法优先。
2. 用户显式修改压过默认身份约束：说了「穿西装」就不再强塞原制服，
   说了「不要羊角」就不再写角。身份的核心（脸/发/种族特征）仍然保留。
"""

from __future__ import annotations

from typing import Any

from .image_intent import ImageIntent
from .image_prompt_builder import ImagePrompt

# 多实体时最容易被模型糊成一个人的地方，单独强调
_MULTI_LOCK = (
    "Multiple characters must appear together and remain visually distinct.",
    "Do not blend their traits into a single character.",
    "Do not swap their traits with each other.",
)

# 这些维度是身份核心：即使用户改服装，也不该动
_CORE_DIMENSIONS = ("face", "hair", "horns", "ears")


class UniversalEntityPromptBuilder:
    """Entity → ImagePrompt。不认识 CharacterProfile，只认识 Entity。"""

    def build(
        self,
        *,
        user_request: str,
        intent: ImageIntent,
        draft_prompt: str = "",
    ) -> ImagePrompt:
        entities = list(intent.entities)
        if not entities:
            raise ValueError("UniversalEntityPromptBuilder 需要至少一个实体")

        overrides = intent.override_map()
        blocks: list[str] = []
        negatives: list[str] = []
        notes: list[str] = []

        if len(entities) > 1:
            blocks.append("\n".join(_MULTI_LOCK))
            negatives.extend(_MULTI_LOCK)

        for index, entity in enumerate(entities, start=1):
            blocks.append(self._entity_block(entity, index, len(entities), overrides))

        # 场景与身份分开写：混在一句里，模型会为了"构图好看"牺牲身份
        scene = self._scene_block(user_request, draft_prompt)
        text = "\n\n".join([b for b in blocks if b] + ([scene] if scene else []))

        if len(entities) > 1:
            notes.append(f"multi_entity:{len(entities)}")
        for dimension, action in overrides.items():
            notes.append(f"override:{dimension}={action}")

        return ImagePrompt(
            text=text,
            kind="known_entity",
            identity_block="\n\n".join(b for b in blocks if b),
            scene_block=scene,
            negatives=tuple(dict.fromkeys(negatives)),
            character=" / ".join(e.canonical_name or e.name for e in entities),
            franchise=" / ".join(dict.fromkeys(e.franchise for e in entities if e.franchise)),
            identity_priority="highest",
            # 没有人工核验字段：这里全部是模型侧摘要，不能冒充硬约束
            profile_status="dynamic_entity" if not any(e.cache_hit for e in entities) else "cached_entity",
            human_verified_fields=(),
            assistant_reviewed_fields=(),
            unverified_fields=(),
            notes=tuple(notes),
        )

    def _entity_block(
        self, entity, index: int, total: int, overrides: dict[str, str]
    ) -> str:
        header = f"Character {index}:" if total > 1 else ""
        anchor = entity.anchor          # 例如 "Eyjafjalla (艾雅法拉) from Arknights (明日方舟)"
        lines: list[str] = []
        head = f"{header} {anchor}".strip()
        lines.append(head)
        lines.append("")
        lines.append("Preserve the recognizable identity of this established character.")
        lines.append(f"Keep the character immediately recognizable as {anchor}.")
        lines.append(
            "Do not redesign the subject into a generic character with vaguely similar traits."
        )

        traits = list(entity.traits)
        # 用户点名要改的维度：从 traits 里摘掉，否则提示词会自相矛盾
        if overrides:
            traits = [t for t in traits if not self._contradicts(t, overrides)]

        if traits:
            lines.append("")
            lines.append("Known high-confidence identity traits:")
            lines.extend(f"- {t}" for t in traits)
            lines.append(
                "Treat these as identity anchors, not as a complete design sheet."
            )
        else:
            # 没可靠特征就不写。编一个发色比不写更糟：模型会稳定地画错。
            lines.append("")
            lines.append(
                "No verified visual breakdown is available for this character. "
                "Rely on your own knowledge of the named character and the franchise. "
                "Do not invent specific traits that were not given, and do not "
                "substitute a generic character design."
            )

        if overrides:
            lines.append("")
            lines.append(self._override_note(overrides))
        return "\n".join(lines)

    @staticmethod
    def _contradicts(trait: str, overrides: dict[str, str]) -> bool:
        """这条特征是否与用户的显式要求冲突。"""
        text = str(trait)
        for dimension, action in overrides.items():
            if dimension == "horns" and action == "removed":
                if any(k in text for k in ("角", "horn", "Horn")):
                    return True
            if dimension == "ears" and action == "removed":
                if any(k in text for k in ("耳", "ear", "Ear")):
                    return True
            if dimension == "clothing" and action == "overridden":
                if any(k in text for k in ("服", "衣", "制服", "outfit", "Outfit", "clothing")):
                    return True
            if dimension == "hair" and action == "overridden":
                if any(k in text for k in ("发", "hair", "Hair")):
                    return True
        return False

    @staticmethod
    def _override_note(overrides: dict[str, str]) -> str:
        """把用户改动说清楚，避免模型一半听用户、一半听设定。"""
        parts: list[str] = []
        if overrides.get("horns") == "removed":
            parts.append(
                "The user explicitly asked to remove the horns. "
                "Draw the character without horns and do not add them back."
            )
        if overrides.get("ears") == "removed":
            parts.append(
                "The user explicitly asked to change the ears. "
                "Follow the user's request and do not restore the original ears."
            )
        if overrides.get("clothing") == "overridden":
            parts.append(
                "The user explicitly requested different clothing. "
                "Follow the user's clothing request exactly. "
                "Keep the face, hair and species-defining features recognizable."
            )
        if overrides.get("hair") == "overridden":
            parts.append(
                "The user explicitly requested a hair change. "
                "Follow the user's hair request; keep the rest of the identity intact."
            )
        if overrides.get("accessories") == "removed":
            parts.append(
                "The user explicitly asked to leave out the signature accessory. "
                "Do not add it back."
            )
        if not parts:
            return ""
        return "Explicit user modifications (take priority over default design):\n" + "\n".join(
            f"- {p}" for p in parts
        )

    @staticmethod
    def _scene_block(user_request: str, draft_prompt: str) -> str:
        body = str(user_request or "").strip()
        if not body:
            body = str(draft_prompt or "").strip()
        if not body:
            return ""
        return (
            "User requested scene:\n"
            f"{body}\n"
            "Follow the scene and composition request. "
            "Allowed to change: pose, background, lighting, camera angle, clothing "
            "when the user asked for it."
        )


__all__ = ["UniversalEntityPromptBuilder"]
