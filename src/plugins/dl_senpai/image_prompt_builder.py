"""把「身份」和「场景」拆开，再拼成送给图片模型的提示词。

为什么不能让聊天 Agent 自由发挥后直接扔给图片 API：

实测链路是「用户原话 → 聊天模型写 marker → 提示词增强模型扩写 → 图片模型」。
两次模型改写之后，原话里的**专有名词几乎必然被同义化**：艾雅法拉 → 羊角少女，
凯尔希 → 精灵耳医生。图片模型只认提示词，于是画出来的是一张"对得上形容词、
对不上人"的图。

所以这里的职责很明确：
1. 已知角色 —— 身份段落由 CharacterProfile 提供，**英文锚点（canonical name +
   franchise）必须原样出现**；场景部分只负责姿势/环境/光线/镜头，不许染指身份。
2. 普通图 —— 原样返回草稿，一个字都不加。这条是硬要求：不能为了修角色生图，
   把「画一只猫」也拖进角色管线。

**三档可信度**（2026-10-27 修正）：

上一版不分青红皂白写「Canonical visual identity (authoritative, do not contradict)」，
把未经人工核对的设定当成权威。这是错的——资料写错时，越强的措辞越稳定地画错。
现在按字段可信度分层：

    Level A 已验证字段  → 硬约束（must be preserved / do not contradict）
    Level B 未验证非空  → supporting hints（只作参考，不许用权威措辞）
    Level C 空 / 未知   → 完全不写（未知 ≠ 没有）

只有 Level A 为空时才允许出现 supporting 分支里的"未人工核验"声明——否则
模型会把提示当权威，等于回到老问题。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .character_registry import CharacterProfile, VisualIdentity
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
    # 可信度快照：trace 与评估器都要知道这段提示词里哪些是硬约束
    profile_status: str = ""
    verified_fields: tuple[str, ...] = ()
    hint_fields: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


class ImagePromptBuilder:
    """身份优先的提示词构建器。"""

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
        tiers = profile.identity_tiers()

        parts = [identity]
        if scene:
            parts.append(f"Scene / composition (the only part that may change):\n{scene}")
        if negatives:
            header = (
                "Avoid (verified constraints and any supplied reference image win if they conflict):"
                if tiers["verified"]
                else "Avoid if possible (these notes are not verified; a supplied reference wins):"
            )
            parts.append(header + "\n" + "\n".join(f"- {n}" for n in negatives))
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
            profile_status=profile.effective_status(),
            verified_fields=tuple(key for key, _ in tiers["verified"]),
            hint_fields=tuple(key for key, _ in tiers["hints"]),
            notes=(f"profile_{profile.effective_status()}",),
        )

    def _identity_block(self, profile: CharacterProfile) -> str:
        """身份段落：分档输出，权威性只给已验证字段。

        英文锚点（canonical name + franchise）始终保留：图片模型对角色名的联想
        是按拉丁字母建立的，只给中文名时它会退化成按形容词作画。
        """
        localized = profile.localized_names[0] if profile.localized_names else ""
        names = profile.canonical_name + (f" ({localized})" if localized else "")
        franchise = profile.franchise + (
            f" ({profile.franchise_localized})" if profile.franchise_localized else ""
        )
        tiers = profile.identity_tiers()
        verified, hints = tiers["verified"], tiers["hints"]
        hard_items, soft_items = profile.split_preserve()

        lines = [
            f"Depict {names} from {franchise}.",
            "",
            "Character identity preservation is the highest priority.",
        ]

        # ---- Level A：硬约束 ----
        if verified:
            lines.append("")
            lines.append("Verified canonical identity constraints:")
            for key, value in verified:
                lines.append(f"- {profile.visual_identity.label(key)}：{value}")
            if hard_items:
                lines.append("Preserve these verified identity items: " + ", ".join(hard_items) + ".")
            lines.append("These verified features must be preserved. Do not contradict them.")

        # ---- Level B：支撑性提示（不许使用权威措辞）----
        if hints:
            lines.append("")
            if verified:
                lines.append(
                    "Supporting identity hints (not independently verified — follow them only "
                    "where they stay consistent with the verified constraints above and with any "
                    "supplied reference image, and never let them override those):"
                )
            else:
                # 全档都是 hints：先明确"这份档案还没被人工核验"，避免提示词自己造权威
                lines.append(
                    "Supporting identity hints (this profile has NOT been human-verified yet; "
                    "treat them as guidance only rather than recorded facts, and prefer any "
                    "supplied reference image wherever they differ):"
                )
            for key, value in hints:
                lines.append(f"- {profile.visual_identity.label(key)}：{value}")
            if soft_items:
                lines.append(
                    "Prefer preserving, where the reference and scene allow: "
                    + ", ".join(soft_items)
                    + "."
                )

        lines.append("")
        lines.append(
            f"The result must be immediately recognizable as {names} from {profile.franchise}."
        )
        lines.append("Do not redesign the character into a generic fantasy character.")
        # 这一条不引用任何具体字段（未验证的字段不该被写成硬约束）：
        # 只说"别用一个两个特征拼出来的泛化角色顶替她"，正好针对「羊角少女」这类退化。
        lines.append(
            "Do not replace the canonical design with a generic anime character that merely "
            "shares one or two of her traits (such as horns or hair color)."
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
        """禁止项：只输出档案自带的负向备注。

        为什么不在这里加「do not change hair color」这类生成式硬禁止：
        那些约束必须以「该字段已验证」为前提，否则又变成把未验证信息写成硬规则。
        已验证字段的硬约束由 Level A 段声明，这里不重复。
        """
        return tuple(dict.fromkeys(profile.negative_notes))


__all__ = ["ImagePrompt", "ImagePromptBuilder"]
