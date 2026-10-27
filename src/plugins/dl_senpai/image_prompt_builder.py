"""把「身份」和「场景」拆开，再拼成送给图片模型的提示词。

为什么不能让聊天 Agent 自由发挥后直接扔给图片 API：

实测链路是「用户原话 → 聊天模型写 marker → 提示词增强模型扩写 → 图片模型」。
两次模型改写之后，原话里的**专有名词几乎必然被同义化**：艾雅法拉 → 羊角少女，
凯尔希 → 精灵耳医生。图片模型只认提示词，于是画出来的是一张"对得上形容词、
对不上人"的图。

所以这里的职责很明确：
1. 已知角色 —— 身份段落由 CharacterProfile 提供，**英文锚点（canonical name +
   franchise）必须原样出现**，并显式声明 identity preservation 优先、禁止改设计；
   场景部分只负责姿势/环境/光线/镜头，不许染指身份。
2. 普通图 —— 原样返回草稿，一个字都不加。这条是硬要求：不能为了修角色生图，
   把「画一只猫」也拖进角色管线。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .character_registry import CharacterProfile
from .character_resolver import CharacterResolution

# 请求里的动作词/客套词：从用户原话里剥出"场景"时用。
_REQUEST_NOISE_RE = re.compile(
    r"(帮我|给我|帮忙|麻烦|请|能不能|可以|画一?[张个幅]?|生成|来一?[张个]|制作|"
    r"的?图片|插画|壁纸|头像|图片|图|吧|呀|啊|嘛|好吗|行吗|谢谢)"
)

# 场景词太短就别写进提示词：只剩一个「的」或空串时写进去反而干扰模型。
_MIN_SCENE_CHARS = 3


@dataclass(frozen=True)
class ImagePrompt:
    """一次生图要用的提示词，以及它是怎么拼出来的。"""

    text: str
    kind: str = "general"            # known_character / general
    identity_block: str = ""
    scene_block: str = ""
    negatives: tuple[str, ...] = ()
    character: str = ""
    franchise: str = ""
    identity_priority: str = ""
    creative_freedom: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


class ImagePromptBuilder:
    """身份优先的提示词构建器。

    用法只有一句：build(user_request=..., draft_prompt=..., resolution=...)，
    返回的 .text 直接交给图片模型。
    """

    def build(
        self,
        *,
        user_request: str,
        draft_prompt: str = "",
        resolution: CharacterResolution | None = None,
    ) -> ImagePrompt:
        draft = (draft_prompt or "").strip()
        if resolution is None or not resolution.is_known or resolution.profile is None:
            # 普通图：原样透传，不做任何加工（不含任何角色约束）
            return ImagePrompt(
                text=draft or (user_request or "").strip(),
                kind="general",
                scene_block=(user_request or "").strip(),
            )
        return self._build_known_character(
            profile=resolution.profile,
            user_request=user_request,
            draft_prompt=draft,
        )

    # -- 已知角色 --

    def _build_known_character(
        self,
        *,
        profile: CharacterProfile,
        user_request: str,
        draft_prompt: str,
    ) -> ImagePrompt:
        identity = self._identity_block(profile)
        scene = self._scene_block(profile=profile, user_request=user_request, draft_prompt=draft_prompt)
        negatives = self._negatives(profile)

        parts = [identity]
        if scene:
            parts.append(f"Scene / composition (the only part that may change):\n{scene}")
        if negatives:
            parts.append("Do not:\n" + "\n".join(f"- {n}" for n in negatives))
        text = "\n\n".join(parts)

        return ImagePrompt(
            text=text,
            kind="known_character",
            identity_block=identity,
            scene_block=scene,
            negatives=negatives,
            character=profile.canonical_name,
            franchise=profile.franchise,
            identity_priority=profile.identity_priority,
            creative_freedom=profile.creative_freedom,
            notes=(
                ("profile_unverified",) if not profile.verified else ()
            ),
        )

    def _identity_block(self, profile: CharacterProfile) -> str:
        """身份段落：英文锚点 + 中文可见特征 + 硬约束。

        为什么英文锚点必须原样出现：图片模型对角色名的联想是按拉丁字母建立的，
        只给中文名时它会退化成按形容词作画（"羊角 + 银发 + 火山"）。
        同时保留中文名，是为了让模型把两条命名指向同一个人。
        """
        localized = profile.localized_names[0] if profile.localized_names else ""
        names = profile.canonical_name + (f" ({localized})" if localized else "")
        franchise = profile.franchise + (
            f" ({profile.franchise_localized})" if profile.franchise_localized else ""
        )

        lines = [
            f"Depict {names} from {franchise}.",
            "",
            "Character identity preservation is the highest priority.",
            "Preserve the canonical character identity, including:",
        ]
        for item in profile.must_preserve or ("facial identity", "signature outfit", "canonical color palette"):
            lines.append(f"- {item}")

        visual = profile.visual_identity.lines()
        if visual:
            lines.append("")
            lines.append("Canonical visual identity (authoritative, do not contradict):")
            lines.extend(f"- {v}" for v in visual)

        lines.append("")
        lines.append(
            f"The result must be immediately recognizable as {names} from {profile.franchise}."
        )
        lines.append("Do not redesign the character into a generic fantasy girl.")
        if profile.visual_identity.race:
            lines.append(
                "Do not replace her canonical design with a generic "
                f"{profile.visual_identity.race} anime girl."
            )
        allowed = ", ".join(profile.creative_freedom) or "pose, background, lighting, camera_angle, expression"
        lines.append(f"Allowed to change: {allowed}.")
        return "\n".join(lines)

    def _scene_block(
        self, *, profile: CharacterProfile, user_request: str, draft_prompt: str
    ) -> str:
        """场景段落：从用户原话里剥出"角色之外"的部分。

        注意这里**不删除用户原话**：剥掉的只是角色名与画图动词，
        剩下的环境/动作/情绪全部保留（用户要求：不要丢掉原始请求）。
        """
        rest = user_request or ""
        for name in profile.all_names():
            if name:
                rest = re.sub(re.escape(name), " ", rest, flags=re.IGNORECASE)
        rest = _REQUEST_NOISE_RE.sub(" ", rest)
        rest = re.sub(r"\s+", " ", rest).strip(" ，。、,.!！?？")

        parts: list[str] = []
        if len(rest) >= _MIN_SCENE_CHARS:
            parts.append(rest)

        # 草稿里通常已经有具体的画面描述（姿势/环境/光影）。
        # 先用**去掉角色名**的版本：否则「画艾雅法拉在咖啡店」会拼出
        # 「在咖啡店；艾雅法拉 咖啡店」这种把名字重复一遍的废话。
        draft = (draft_prompt or "").strip()
        if draft:
            stripped = draft
            for name in profile.all_names():
                if name:
                    stripped = re.sub(re.escape(name), " ", stripped, flags=re.IGNORECASE)
            stripped = re.sub(r"\s+", " ", stripped).strip(" ，。、,.!！?？")
            if len(re.sub(r"[\s，。、,.!！?？]", "", stripped)) >= _MIN_SCENE_CHARS:
                parts.append(stripped)
        # 两段内容高度重合时只留一段，避免同一句话在提示词里出现两次
        if len(parts) == 2 and (parts[0] in parts[1] or parts[1] in parts[0]):
            parts = [parts[0] if len(parts[0]) >= len(parts[1]) else parts[1]]
        return "；".join(dict.fromkeys(parts))

    def _negatives(self, profile: CharacterProfile) -> tuple[str, ...]:
        """禁止项：profile 自带的负向备注 + 两条通用护栏。"""
        out = list(profile.negative_notes)
        out.append("do not change hair color, horn/ear design or signature outfit structure")
        out.append("do not add or remove signature accessories")
        return tuple(dict.fromkeys(out))


__all__ = ["ImagePrompt", "ImagePromptBuilder"]
