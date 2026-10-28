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

## 四层可信度（2026-10-27 修正）

上一版把「模型审阅过的资料」也写成了 authoritative 硬约束。那等于让 AI 给自己的
判断盖上人工核验的章，资料错的时候越强的措辞越稳定地画错。现在按来源分层：

    Level A  human_verified 字段      → 硬约束（must be preserved / do not contradict）
    Level B  human-verified 参考图    → 参考图是脸与整体外观的权威
    Level C  assistant_reviewed 字段  → supporting hints，**禁止** authoritative 措辞
    Level D  unverified / 空          → 不写入（未知 ≠ 没有，也不许模型自己编）

只有 Level A 才配得上「不得违背」这种话。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .character_registry import CharacterProfile
from .character_resolver import CharacterResolution

# 请求里的动作词/客套词：从用户原话里剥出"场景"时用。
_REQUEST_NOISE_RE = re.compile(
    r"(帮我|给我|帮忙|麻烦|请|能不能|可以|画一?[张个幅]?|生成|来一?[张个]|制作|"
    r"的?图片|插画|壁纸|头像|图片|图|吧|呀|啊|嘛|好吗|行吗|谢谢)"
)

# 场景词太短就别写进提示词：只剩一个「的」或空串时写进去反而干扰模型。
_MIN_SCENE_CHARS = 3

# 命中角色档案后，草稿里出现这些内容一律不采纳。
# 为什么必须过滤：Agent（聊天模型）写 draw prompt 时会凭记忆描述角色外貌，而且经常错——
# 实测把艾雅法拉写成「蓬松的深紫褐色短发」（她是银白偏浅紫长发），
# 把另一个角色写成「浅金棕长发、细框圆眼镜、羊尾巴」（那根本不是她）。
# 这类描述一旦作为"场景"拼进提示词，就和档案里的正确设定同场出现；
# 图片模型通常听更具体、更靠后的那段，画出来就不是本人。
# 身份只能有一个来源：档案。
_COLOR_WORDS = ("银白", "白", "黑", "金", "棕", "褐", "黄", "红", "紫", "蓝", "绿", "粉", "橙", "灰", "青")
_HAIR_WORDS = ("发", "马尾", "刘海")
_EYE_WORDS = ("瞳", "眼睛", "眼瞳")
_BODY_WORDS = ("角", "耳", "尾", "翼", "爪", "兽")
_CLOTH_WORDS = (
    "衣", "服", "裙", "鞋", "袜", "帽", "披风", "斗篷", "制服", "配饰", "法杖", "眼镜", "杖",
    "开衫", "内搭", "外套", "上衣", "裤子", "短裤", "长裤", "夹克", "衬衫", "毛衣", "袍",
    "泳装", "睡衣", "和服", "汉服", "西装", "礼服", "校服", "背带", "腰带",
)
# 泛化人物词：命中档案时，这个人是谁已经确定，再说「少女/少年」只会稀释身份锚点
_GENERIC_PERSON_WORDS = ("少女", "少年", "女孩", "男孩", "女性", "男性", "人物", "角色", "一位", "一个")


def _clause_conflicts_identity(clause: str) -> bool:
    """这个子句是不是在描述（可能与档案冲突的）长相或服装。"""
    text = str(clause or "")
    has_color = any(w in text for w in _COLOR_WORDS)
    # 发型词不要求带颜色：「额前碎发」照样和档案里的「长发」冲突
    if any(w in text for w in _HAIR_WORDS):
        return True
    if has_color and any(w in text for w in _EYE_WORDS):
        return True
    if any(w in text for w in _BODY_WORDS):
        return True
    if any(w in text for w in _CLOTH_WORDS):
        return True
    return any(w in text for w in _GENERIC_PERSON_WORDS)


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
    # 可信度快照：trace 与评估器都要知道这段提示词里哪些是真硬约束
    profile_status: str = ""
    human_verified_fields: tuple[str, ...] = ()
    assistant_reviewed_fields: tuple[str, ...] = ()
    unverified_fields: tuple[str, ...] = ()
    reference_verified: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def verified_fields(self) -> tuple[str, ...]:
        """兼容旧名：只含人工核验字段。"""
        return self.human_verified_fields

    @property
    def hint_fields(self) -> tuple[str, ...]:
        """兼容旧名：模型审阅过的字段。"""
        return self.assistant_reviewed_fields


class ImagePromptBuilder:
    """身份优先的提示词构建器。"""

    def build(
        self,
        *,
        user_request: str,
        draft_prompt: str = "",
        resolution: CharacterResolution | None = None,
        reference_verified: bool = False,
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
            reference_verified=reference_verified,
        )

    # -- 已知角色 --

    def _build_known_character(
        self,
        *,
        profile: CharacterProfile,
        user_request: str,
        draft_prompt: str,
        reference_verified: bool,
    ) -> ImagePrompt:
        identity = self._identity_block(profile, reference_verified=reference_verified)
        scene = self._scene_block(profile=profile, user_request=user_request, draft_prompt=draft_prompt)
        negatives = self._negatives(profile)
        tiers = profile.identity_tiers()
        human = tuple(key for key, _ in tiers["human"])
        reviewed = tuple(key for key, _ in tiers["reviewed"])
        unverified = tuple(key for key, _ in tiers["unverified"])

        parts = [identity]
        if scene:
            parts.append(f"Scene / composition (the only part that may change):\n{scene}")
        if negatives:
            header = (
                "Avoid (human-verified constraints and the reference image win if they conflict):"
                if human or reference_verified
                else "Avoid if possible (these notes are not human-verified; a reference image wins):"
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
            human_verified_fields=human,
            assistant_reviewed_fields=reviewed,
            unverified_fields=unverified,
            reference_verified=reference_verified,
            notes=(f"profile_{profile.effective_status()}",),
        )

    def _identity_block(self, profile: CharacterProfile, *, reference_verified: bool) -> str:
        """身份段落：四层输出，权威性只给人工核验的东西。

        英文锚点（canonical name + franchise）始终保留：图片模型对角色名的联想
        是按拉丁字母建立的，只给中文名时它会退化成按形容词作画。
        """
        localized = profile.localized_names[0] if profile.localized_names else ""
        names = profile.canonical_name + (f" ({localized})" if localized else "")
        franchise = profile.franchise + (
            f" ({profile.franchise_localized})" if profile.franchise_localized else ""
        )
        tiers = profile.identity_tiers()
        human, reviewed, unverified = tiers["human"], tiers["reviewed"], tiers["unverified"]
        hard_items, reviewed_items, soft_items = profile.split_preserve()

        lines = [
            f"Depict {names} from {franchise}.",
            "",
            "Character identity preservation is the highest priority.",
        ]

        # ---- Level A：人工核验 → 硬约束 ----
        if human:
            lines.append("")
            lines.append("Human-verified identity constraints:")
            for key, value in human:
                lines.append(f"- {profile.visual_identity.label(key)}：{value}")
            if hard_items:
                lines.append("Preserve these verified identity items: " + ", ".join(hard_items) + ".")
            lines.append("These features are human-verified. Do not contradict them.")

        # ---- Level B：人工核验参考图 → 脸与整体外观的权威 ----
        if reference_verified:
            lines.append("")
            lines.append(
                "Human-verified reference image supplied: follow it for facial identity, "
                "proportions and overall appearance. Where this description and the reference "
                "image disagree, follow the reference image."
            )

        # ---- Level C：模型审阅 → 仅支撑性提示 ----
        if reviewed:
            lines.append("")
            lines.append(
                "Assistant-reviewed identity hints (not human-verified; use as supporting "
                "guidance only):"
            )
            for key, value in reviewed:
                lines.append(f"- {profile.visual_identity.label(key)}：{value}")
            if reviewed_items:
                lines.append("Likely identity traits, in order of confidence: " + ", ".join(reviewed_items) + ".")
            lines.append(
                "These hints are not authoritative. Do not let them override the reference "
                "image or any human-verified identity fields."
            )

        # ---- Level D：未核验 / 未知 → 不写具体值，但明确「不要自己编」 ----
        if unverified:
            lines.append("")
            lines.append(
                "Some visual traits are unrecorded or unverified and are intentionally left "
                "unspecified. Do not invent them and do not quietly substitute generic defaults."
            )

        lines.append("")
        lines.append(
            f"The result must be immediately recognizable as {names} from {profile.franchise}."
        )
        lines.append("Do not redesign the character into a generic fantasy character.")
        # 这一条不引用任何具体字段（未核验的字段不该被写成硬约束）：
        # 只说"别用一个两个特征拼出来的泛化角色顶替她"，正好针对「羊角少女」这类退化。
        lines.append(
            "Do not replace the canonical design with a generic anime character that merely "
            "shares one or two of her traits (such as horns or hair color)."
        )
        allowed = ", ".join(profile.creative_freedom) or "pose, background, lighting, camera_angle, expression"
        lines.append(f"Allowed to change: {allowed}.")
        if soft_items:
            lines.append("Minor items to keep in mind if the scene allows: " + ", ".join(soft_items) + ".")
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
            # 先剔掉描述长相/服装的子句，只留场景、动作、光影、构图
            kept = [
                c.strip()
                for c in re.split(r"[，,。；;、\n]", draft)
                if c.strip() and not _clause_conflicts_identity(c)
            ]
            stripped = "；".join(kept)
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
        那些约束必须以「该字段人工核验过」为前提，否则又变成把未核验信息写成硬规则。
        """
        return tuple(dict.fromkeys(profile.negative_notes))


__all__ = ["ImagePrompt", "ImagePromptBuilder"]
