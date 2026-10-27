"""角色身份注册表：把「艾雅法拉」这个名字变成可用的身份锚点。

为什么另起一层，而不是继续往 draw_cards 里塞：

1. CharacterCard 只有 name / aliases / spec 三段字符串——没有作品名（franchise）、
   没有 must_preserve 与 creative_freedom 的区分、也没有可信度。于是：
   - 名字没命中时**完全没有任何身份信息**（艾雅法拉压根不在卡里，这就是
     「画一张艾雅法拉」只能画出泛化羊角少女的直接原因）；
   - 命中了也只有一段中文描述，提示词里没有 Eyjafjalla / Arknights 这样的
     英文锚点，生图模型很容易顺着「羊角」滑到通用奇幻少女。
2. 数据要与代码分离：角色数据放 JSON，用户可以覆盖，不用改 Python。

**可信度是字段级的，不是角色级的**（2026-10-27 修正）：

上一版只有角色级的 verified: bool，而 Prompt Builder 无论真假都写
「Canonical visual identity (authoritative, do not contradict)」。这是个
逻辑错误——未人工确认的信息不是权威信息，写错了越强的措辞越稳定地画错。
现在每个视觉特征各自带 verified，Prompt Builder 按可信度分三档输出：
  Level A 已验证 → 硬约束
  Level B 未验证但非空 → supporting hints（不许用 authoritative 措辞）
  Level C 空/未知 → 完全不写（未知不等于「没有」）

数据来源三层（后者覆盖前者）：包内 data/characters.json → 用户目录
data/dl_senpai/characters/*.json。仍属于「本地 Profile」，不联网、不调模型。
"""

from __future__ import annotations

import json
import re
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nonebot import logger

# 包内种子数据（随代码走）
_BUILTIN_DATA = Path(__file__).with_name("data") / "characters.json"

# 视觉字段的展示名。顺序就是提示词里的书写顺序。
_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("race", "族属/种族"),
    ("face", "面部"),
    ("hair", "头发"),
    ("eyes", "眼睛"),
    ("horns", "角"),
    ("ears", "耳朵"),
    ("body_features", "身体特征"),
    ("clothing", "服装"),
    ("accessories", "配饰"),
    ("palette", "配色"),
    ("silhouette", "体型轮廓"),
)
_FIELD_KEYS = tuple(key for key, _ in _FIELD_LABELS)
_LABEL_OF = dict(_FIELD_LABELS)

# 状态推导用的必核字段（2026-10-27 修正）。
#
# 旧规则是 fixed 的 ("face","hair","horns","eyes","clothing") 且要求 4 项验证，
# 有两个硬伤：
#   1. face 被算成必核项 —— 但脸的身份基本无法用「圆脸/柔和五官」这种自然语言
#      可靠表达，硬要人工编一段描述反而会让生图模型重新设计人物。脸应该交给
#      已验证参考图去锚定，不该逼文本字段。
#   2. 要求角色级 verified=True 才能升 verified —— 那是人工开关，跟字段推导打架，
#      结果是「字段全验了也永远停在 partial」。
#
# 新规则（通用，无角色特判）：
#   必核 = hair + clothing + palette + 该角色**确实有值**的族属特征（角/耳/身体特征）
#   不够 4 项时用 eyes / accessories / silhouette 里真有值的补足
#   必核全中 → verified；命中任意一项 → partial_verified；否则 draft
_STATUS_BASE_FIELDS = ("hair", "clothing", "palette")
# 族属特征槽：有角的说角、有兽耳的说耳，谁有值谁进必核
_SPECIES_SLOTS = ("horns", "ears", "body_features")
_STATUS_FILL_FIELDS = ("eyes", "accessories", "silhouette")
_STATUS_MIN_REQUIRED = 4

# must_preserve 里的自然语言项 → 视觉字段 key。
# 为什么要这张表：must_preserve 是英文短语（"hairstyle and hair color"），
# 而可信度是按字段记的，不做映射就没法判断这一条到底是硬约束还是提示。
_PRESERVE_FIELD_HINTS: tuple[tuple[str, str], ...] = (
    ("facial", "face"),
    ("face", "face"),
    ("hair", "hair"),
    ("horn", "horns"),
    ("eye", "eyes"),
    ("ear", "ears"),
    ("tail", "body_features"),
    ("body feature", "body_features"),
    ("outfit", "clothing"),
    ("clothing", "clothing"),
    ("costume", "clothing"),
    ("accessor", "accessories"),
    ("palette", "palette"),
    ("color", "palette"),
    ("silhouette", "silhouette"),
    ("race", "race"),
)

# 通用身份显著性权重（不是为某个角色写死的）。
# 依据：决定「一眼认出是谁」的主要是脸、发型、角/耳这类族属特征；
# 配色和配饰权重低——它们错一点还认得出，脸错了就全错了。
DEFAULT_IDENTITY_SALIENCE: dict[str, float] = {
    "face": 1.4,
    "hair": 1.3,
    "horns": 1.4,
    "ears": 1.2,
    "eyes": 1.0,
    "clothing": 1.1,
    "accessories": 0.9,
    "palette": 0.8,
    "silhouette": 1.0,
    "race": 1.3,
    "body_features": 1.2,
}

# 核验方法。只有 METHOD_HUMAN 代表「人看过原始资料并确认」。
METHOD_HUMAN = "human_review"
# assistant_review：模型据训练知识核对。**永远不等于人工核验**。
METHOD_ASSISTANT = "assistant_review"
METHOD_REFERENCE = "reference_evidence"

# profile_status 四档。
#
# 为什么必须有 assistant_reviewed 这一档（2026-10-27 修正）：
# 上一阶段把「模型核对过公开设定」写成了 verified=true，于是那 8 个字段直接进了
# 提示词的 authoritative 硬约束段。这是语义污染——AI 给自己盖了「人工核验」的章。
# draft 又表达不了「审过了但没人确认」这个中间状态，所以补一档。
STATUS_DRAFT = "draft"
STATUS_ASSISTANT_REVIEWED = "assistant_reviewed"
STATUS_PARTIAL = "partial_verified"
STATUS_VERIFIED = "verified"


class VerificationStatus(str, Enum):
    """一个视觉字段（或一张参考图）的可信状态。

    这是本项目的信任模型核心：**只有 HUMAN_VERIFIED 才有资格进提示词的硬约束段**。

    HUMAN_VERIFIED must never be produced by automatic model review.
    也就是说：模型、后台任务、自动脚本、编码 Agent 都不许把状态写成 human_verified。
    唯一合法来源是人执行 scripts/character_verify.py verify-field。
    """

    UNVERIFIED = "unverified"
    ASSISTANT_REVIEWED = "assistant_reviewed"
    HUMAN_VERIFIED = "human_verified"

    @property
    def authoritative(self) -> bool:
        """能否作为权威约束写进提示词。"""
        return self is VerificationStatus.HUMAN_VERIFIED

    @property
    def counts_as_basis(self) -> bool:
        """能否作为身份评分的文字依据。assistant_reviewed 不算。"""
        return self is VerificationStatus.HUMAN_VERIFIED


def migrate_status(
    *, verified: bool = False, method: str = "", explicit: str = ""
) -> VerificationStatus:
    """把旧数据（verified: bool + verification_method）迁到三态。

    规则（保守方向：任何含糊情况都往低调）：
        verified=true  + human_review      → HUMAN_VERIFIED
        verified=true  + assistant_review  → ASSISTANT_REVIEWED
        verified=false + assistant_review  → ASSISTANT_REVIEWED
        其他                                → UNVERIFIED

    为什么 verified=true 配不出名的方法不算 human：
    「有人把 verified 设成 true 但没记来源」不能自动升级成人工核验，
    否则旧的误标会永久继承权威性。
    """
    raw = (explicit or "").strip().lower()
    if raw in (VerificationStatus.UNVERIFIED.value, VerificationStatus.ASSISTANT_REVIEWED.value,
               VerificationStatus.HUMAN_VERIFIED.value):
        return VerificationStatus(raw)

    meth = (method or "").strip().lower()
    if verified and meth == METHOD_HUMAN:
        return VerificationStatus.HUMAN_VERIFIED
    if meth == METHOD_ASSISTANT:
        return VerificationStatus.ASSISTANT_REVIEWED
    return VerificationStatus.UNVERIFIED


class VisualField:
    """一个视觉特征 + 它的可信状态。

    verified 已经不是可写字段，而是 verification_status 的**派生属性**。
    这是整条信任模型的地基：
        只有 HUMAN_VERIFIED 才 verified=True；
        ASSISTANT_REVIEWED 虽然「被审过」，但 verified 恒为 False。
    上一阶段允许 verified=True 与 method=assistant_review 并存，等于让模型
    给自己盖章，最终那 8 个字段直接进了提示词的 authoritative 段。
    """

    __slots__ = ("value", "verification_status", "verified_at", "verification_method", "note")

    def __init__(
        self,
        value: str = "",
        verification_status: VerificationStatus = VerificationStatus.UNVERIFIED,
        verified_at: str = "",
        verification_method: str = "",
        note: str = "",
    ) -> None:
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "verification_status", verification_status)
        object.__setattr__(self, "verified_at", verified_at)
        object.__setattr__(self, "verification_method", verification_method)
        object.__setattr__(self, "note", note)

    def __setattr__(self, name: str, value: Any) -> None:
        # 不可变：改值必须走 update_field_value，那里会顺手把核验状态打回 unverified
        raise AttributeError("VisualField 不可变，请用 character_registry 的写回函数")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VisualField):
            return NotImplemented
        return self.as_dict() == other.as_dict()

    def __repr__(self) -> str:
        return f"VisualField({self.value!r}, {self.verification_status.value})"

    def __bool__(self) -> bool:
        return bool(self.value.strip())

    @property
    def verified(self) -> bool:
        """兼容旧调用：派生值，不接受外部赋值。"""
        return self.verification_status is VerificationStatus.HUMAN_VERIFIED

    @property
    def reviewed(self) -> bool:
        """被模型审阅过（但没人工核验）。"""
        return self.verification_status is VerificationStatus.ASSISTANT_REVIEWED

    @property
    def status(self) -> str:
        return self.verification_status.value

    def as_dict(self) -> dict[str, Any]:
        """写回 JSON 用。新格式只写 verification_status，不再写 verified bool。"""
        out: dict[str, Any] = {
            "value": self.value,
            "verification_status": self.verification_status.value,
        }
        if self.verified_at:
            out["verified_at"] = self.verified_at
        if self.verification_method:
            out["verification_method"] = self.verification_method
        if self.note:
            out["note"] = self.note
        return out



class VisualIdentity:
    """角色的可见特征集合。

    数据写法：
        "hair": "银白偏浅紫的长发"                                    → unverified
        "hair": {"value": "...", "verification_status": "human_verified"}   → 新格式
        "hair": {"value": "...", "verified": true, "verification_method": "human_review"} → 旧格式，自动迁移

    旧格式一律走 migrate_status()：只有 verified=true 且方法明确是 human_review
    才迁成 HUMAN_VERIFIED，其余全部降级。
    """

    def __init__(self, fields: dict[str, VisualField]) -> None:
        self._fields = {k: v for k, v in fields.items() if k in _FIELD_KEYS}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> VisualIdentity:
        fields: dict[str, VisualField] = {}
        for key, raw in (data or {}).items():
            if key not in _FIELD_KEYS:
                continue
            if isinstance(raw, dict):
                value = str(raw.get("value") or "").strip()
                status = migrate_status(
                    verified=bool(raw.get("verified")),
                    method=str(raw.get("verification_method") or ""),
                    explicit=str(raw.get("verification_status") or ""),
                )
                verified_at = str(raw.get("verified_at") or "").strip()
                method = str(raw.get("verification_method") or "").strip()
                note = str(raw.get("note") or "").strip()
                # 只有人工核验才保留核验时间/方法，别让降级后的字段还挂着"核验过"的味道
                if status is not VerificationStatus.HUMAN_VERIFIED:
                    if status is not VerificationStatus.ASSISTANT_REVIEWED:
                        verified_at = ""
                        method = ""
            else:
                value = str(raw or "").strip()
                status = VerificationStatus.UNVERIFIED
                verified_at = method = note = ""
            if value:
                fields[key] = VisualField(
                    value=value,
                    verification_status=status,
                    verified_at=verified_at,
                    verification_method=method,
                    note=note,
                )
        return cls(fields)

    # -- 查询 --

    def __bool__(self) -> bool:
        return bool(self._fields)

    def __len__(self) -> int:
        return len(self._fields)

    def get(self, key: str) -> str:
        field = self._fields.get(key)
        return field.value if field else ""

    def field(self, key: str) -> VisualField:
        return self._fields.get(key) or VisualField()

    def label(self, key: str) -> str:
        return _LABEL_OF.get(key, key)

    def keys(self) -> tuple[str, ...]:
        """按展示顺序返回非空字段。"""
        return tuple(k for k in _FIELD_KEYS if k in self._fields)

    def keys_with(self, status: VerificationStatus) -> tuple[str, ...]:
        return tuple(k for k in self.keys() if self._fields[k].verification_status is status)

    def human_verified_keys(self) -> tuple[str, ...]:
        return self.keys_with(VerificationStatus.HUMAN_VERIFIED)

    def assistant_reviewed_keys(self) -> tuple[str, ...]:
        return self.keys_with(VerificationStatus.ASSISTANT_REVIEWED)

    def verified_keys(self) -> tuple[str, ...]:
        """兼容旧名：**只**指人工核验过的字段。"""
        return self.human_verified_keys()

    def unverified_keys(self) -> tuple[str, ...]:
        """既没人工核验、也没被模型审阅过的字段。"""
        return self.keys_with(VerificationStatus.UNVERIFIED)

    def lines(self, *, status: VerificationStatus | None = None) -> list[str]:
        """拼成 ["头发：…", …]。status=None 取全部。"""
        out: list[str] = []
        for key in self.keys():
            if status is not None and self._fields[key].verification_status is not status:
                continue
            out.append(f"{self.label(key)}：{self._fields[key].value}")
        return out

    def as_dict(self) -> dict[str, Any]:
        return {k: v.as_dict() for k, v in self._fields.items()}



@dataclass(frozen=True)
class CharacterProfile:
    """一个已知角色的身份档案。

    verified 字段保留但**不再决定权威性**：权威性由「字段级 verified +
    profile_status + 已验证参考图」共同决定，见 identity_tiers()。
    """

    canonical_name: str
    franchise: str = ""
    franchise_localized: str = ""
    localized_names: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    # 弱别名（例如「小羊」= 艾雅法拉）：必须同时出现作品名才敢认，
    # 否则「画一只小羊」会被强行变成艾雅法拉。
    weak_aliases: tuple[str, ...] = ()
    franchise_aliases: tuple[str, ...] = ()
    identity_priority: str = "HIGH"
    confidence: float = 0.0
    # 角色级审核状态。留空则按字段自动推导（见 effective_status）。
    profile_status: str = ""
    verified: bool = False
    visual_identity: VisualIdentity = field(default_factory=lambda: VisualIdentity({}))
    must_preserve: tuple[str, ...] = ()
    creative_freedom: tuple[str, ...] = ("pose", "background", "lighting", "camera_angle", "expression")
    negative_notes: tuple[str, ...] = ()
    # 角色自己的身份显著性权重（留空用 DEFAULT_IDENTITY_SALIENCE）
    identity_salience: dict[str, float] = field(default_factory=dict)
    source: str = "builtin"

    @property
    def display_name(self) -> str:
        if self.localized_names:
            return f"{self.localized_names[0]}（{self.canonical_name}）"
        return self.canonical_name

    @property
    def franchise_display(self) -> str:
        if self.franchise_localized:
            return f"{self.franchise_localized}（{self.franchise}）"
        return self.franchise

    @property
    def slug(self) -> str:
        def _slug(text: str) -> str:
            return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

        return f"{_slug(self.franchise) or 'unknown'}/{_slug(self.canonical_name) or 'unknown'}"

    def all_names(self) -> tuple[str, ...]:
        return (self.canonical_name, *self.localized_names, *self.aliases)

    # -- 可信度 --

    def human_verified_fields(self) -> tuple[str, ...]:
        """人工核验过的字段。**只有这些**能进提示词的硬约束段。"""
        return self.visual_identity.human_verified_keys()

    def assistant_reviewed_fields(self) -> tuple[str, ...]:
        """模型审阅过、但没人确认的字段。只能做支撑性提示。"""
        return self.visual_identity.assistant_reviewed_keys()

    def verified_fields(self) -> tuple[str, ...]:
        """兼容旧名。语义已收紧：只含人工核验字段。"""
        return self.human_verified_fields()

    def unverified_fields(self) -> tuple[str, ...]:
        """既没人工核验也没被审阅：不写进提示词（未知 ≠ 没有）。"""
        return self.visual_identity.unverified_keys()

    def required_fields(self) -> tuple[str, ...]:
        """升到 verified 需要**人工核验**哪些字段（按这个角色的实际数据算）。

        通用规则，不含任何角色特判：
            必核 = hair + clothing + palette + 该角色确实有值的族属特征
            不足 4 项时用 eyes / accessories / silhouette 里真有值的补足
        """
        required: list[str] = list(_STATUS_BASE_FIELDS)
        required += [slot for slot in _SPECIES_SLOTS if self.visual_identity.get(slot)]
        for extra in _STATUS_FILL_FIELDS:
            if len(required) >= _STATUS_MIN_REQUIRED:
                break
            if self.visual_identity.get(extra):
                required.append(extra)
        return tuple(dict.fromkeys(required))

    def signature_feature_field(self) -> str:
        """这个角色的「标志性特征」落在哪个字段（角/耳/尾/身体特征…）。

        为什么不在规则里写死 horns：不是所有角色都有角。这里只回答「哪个槽有值」，
        具体是角还是兽耳由数据决定。
        """
        for slot in _SPECIES_SLOTS:
            if self.visual_identity.get(slot):
                return slot
        return ""

    def effective_status(self) -> str:
        """角色审核状态：**始终**由字段状态推导，不接受手写字符串。

        四档：
            verified            必核字段全部人工核验
            partial_verified    有人工核验，但必核没全中
            assistant_reviewed  只被模型审阅过，无任何人工核验
            draft               什么都没有

        例外只有一个：显式写 draft（人工主动锁定），允许把 verified 压到 partial。
        反方向的升级一律看字段。
        """
        explicit = (self.profile_status or "").strip().lower()
        human = set(self.human_verified_fields())
        required = self.required_fields()
        hit = [key for key in required if key in human]

        if len(hit) == len(required) and len(required) >= _STATUS_MIN_REQUIRED:
            return STATUS_VERIFIED if explicit != STATUS_DRAFT else STATUS_PARTIAL
        if human:
            return STATUS_PARTIAL
        if self.assistant_reviewed_fields():
            return STATUS_ASSISTANT_REVIEWED
        return STATUS_DRAFT

    def split_preserve(self) -> tuple[list[str], list[str], list[str]]:
        """把 must_preserve 拆成（人工硬约束, 模型审阅提示, 其余提示）。

        规则只看字段状态，不看措辞：「该条能映射到某字段」+「字段是人工核验的」
        才升级成硬约束。assistant_reviewed 一律只能当提示。
        """
        hard: list[str] = []
        reviewed: list[str] = []
        soft: list[str] = []
        for item in self.must_preserve:
            key = _preserve_field_of(item)
            status = (
                self.visual_identity.field(key).verification_status
                if key is not None
                else VerificationStatus.UNVERIFIED
            )
            if status is VerificationStatus.HUMAN_VERIFIED:
                hard.append(item)
            elif status is VerificationStatus.ASSISTANT_REVIEWED:
                reviewed.append(item)
            else:
                soft.append(item)
        return hard, reviewed, soft


    def salience(self, key: str) -> float:
        """某字段的身份显著性权重。"""
        own = self.identity_salience.get(key)
        if isinstance(own, (int, float)) and own > 0:
            return float(own)
        return float(DEFAULT_IDENTITY_SALIENCE.get(key, 1.0))

    def identity_tiers(self) -> dict[str, list[tuple[str, str]]]:
        """分档返回视觉特征，与提示词四层模型一一对应。

        human      → Level A，可以写 authoritative / must preserve
        reviewed   → Level C，只能写 supporting guidance
        unverified → Level D，不写进提示词

        （Level B 是参考图派生，不在文字字段里，由 Prompt Builder 单独处理。）
        """
        human: list[tuple[str, str]] = []
        reviewed: list[tuple[str, str]] = []
        unverified: list[tuple[str, str]] = []
        for key in self.visual_identity.keys():
            fld = self.visual_identity.field(key)
            bucket = {
                VerificationStatus.HUMAN_VERIFIED: human,
                VerificationStatus.ASSISTANT_REVIEWED: reviewed,
            }.get(fld.verification_status, unverified)
            bucket.append((key, fld.value))
        return {"human": human, "reviewed": reviewed, "unverified": unverified}

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str = "builtin") -> CharacterProfile:
        salience_raw = data.get("identity_salience") or {}
        salience: dict[str, float] = {}
        if isinstance(salience_raw, dict):
            for k, v in salience_raw.items():
                try:
                    salience[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
        return cls(
            canonical_name=str(data.get("canonical_name") or "").strip(),
            franchise=str(data.get("franchise") or "").strip(),
            franchise_localized=str(data.get("franchise_localized") or "").strip(),
            localized_names=tuple(str(x).strip() for x in data.get("localized_names") or () if str(x).strip()),
            aliases=tuple(str(x).strip() for x in data.get("aliases") or () if str(x).strip()),
            weak_aliases=tuple(str(x).strip() for x in data.get("weak_aliases") or () if str(x).strip()),
            franchise_aliases=tuple(str(x).strip() for x in data.get("franchise_aliases") or () if str(x).strip()),
            identity_priority=str(data.get("identity_priority") or "HIGH"),
            confidence=float(data.get("confidence") or 0.0),
            profile_status=str(data.get("profile_status") or "").strip(),
            verified=bool(data.get("verified")),
            visual_identity=VisualIdentity.from_dict(data.get("visual_identity")),
            must_preserve=tuple(str(x) for x in data.get("must_preserve") or ()),
            creative_freedom=tuple(str(x) for x in data.get("creative_freedom") or ()),
            negative_notes=tuple(str(x) for x in data.get("negative_notes") or ()),
            identity_salience=salience,
            source=source,
        )


def _preserve_field_of(item: str) -> str | None:
    """把 must_preserve 的一条英文短语映射到视觉字段 key。"""
    text = (item or "").lower()
    for hint, key in _PRESERVE_FIELD_HINTS:
        if hint in text:
            return key
    return None


@dataclass(frozen=True)
class CharacterMatch:
    """一次命中的结果（带上「凭什么认出来」）。"""

    profile: CharacterProfile
    matched_alias: str
    confidence: float
    weak: bool = False
    franchise_hit: bool = False

    def reason(self) -> str:
        kind = "弱别名" if self.weak else "名字"
        extra = "+作品名" if self.franchise_hit else ""
        return f"{kind}「{self.matched_alias}」{extra}"


class CharacterRegistry:
    """角色档案集合 + 别名解析。"""

    def __init__(self, profiles: list[CharacterProfile] | None = None) -> None:
        self._profiles: dict[str, CharacterProfile] = {}
        for profile in profiles or []:
            if profile.canonical_name:
                self._profiles[profile.canonical_name.lower()] = profile

    # -- 加载 --

    @classmethod
    def from_file(cls, path: Path, *, source: str | None = None, base: CharacterRegistry | None = None) -> CharacterRegistry:
        """读一个 JSON 数据文件；base 存在时在它之上叠加（同名覆盖）。"""
        registry = cls(list(base.all()) if base else [])
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return registry
        except Exception as exc:  # noqa: BLE001 — 数据坏了不该让生图挂掉
            logger.warning(f"dl_senpai character data unreadable path={path}: {exc}")
            return registry
        items = raw.get("characters") if isinstance(raw, dict) else raw
        for item in items or []:
            if not isinstance(item, dict):
                continue
            profile = CharacterProfile.from_dict(item, source=source or str(path))
            if profile.canonical_name:
                registry._profiles[profile.canonical_name.lower()] = profile
        return registry

    @classmethod
    def load(cls, config: Any = None) -> CharacterRegistry:
        """内置数据 → 用户目录覆盖。用户目录按文件名排序，后者覆盖前者。"""
        registry = cls.from_file(_BUILTIN_DATA, source="builtin")
        user_dir = _user_dir(config)
        if user_dir is not None and user_dir.is_dir():
            for path in sorted(user_dir.glob("*.json")):
                registry = cls.from_file(path, source=str(path), base=registry)
        return registry

    # -- 查询 --

    def all(self) -> list[CharacterProfile]:
        return list(self._profiles.values())

    def __len__(self) -> int:
        return len(self._profiles)

    def resolve(self, name: str) -> CharacterProfile | None:
        """精确解析：名字/别名完全对应（不猜）。"""
        key = (name or "").strip().lower()
        if not key:
            return None
        for profile in self._profiles.values():
            if any(alias.lower() == key for alias in profile.all_names()):
                return profile
        return None

    def find_in_text(self, *texts: str) -> CharacterMatch | None:
        """在一段文本里找角色名。

        两个关键约束：
        1. **长名优先**：先试「初音未来」再试「初音」，否则短名会先命中，
           把「雷电将军」这类更具体的名字盖掉。
        2. **弱别名必须带作品名**：小羊 只有在同一句里出现「明日方舟 / Arknights」
           时才认作艾雅法拉，否则一律不认——不能因为用户说「画一只小羊」就擅自
           把人家的羊变成游戏角色。
        """
        haystack = " ".join(str(t or "") for t in texts).lower()
        if not haystack.strip():
            return None

        strong: list[tuple[str, CharacterProfile]] = []
        weak: list[tuple[str, CharacterProfile]] = []
        for profile in self._profiles.values():
            for alias in profile.all_names():
                if alias.strip():
                    strong.append((alias.strip(), profile))
            for alias in profile.weak_aliases:
                if alias.strip():
                    weak.append((alias.strip(), profile))

        strong.sort(key=lambda item: len(item[0]), reverse=True)
        for alias, profile in strong:
            if alias.lower() in haystack:
                return CharacterMatch(
                    profile=profile,
                    matched_alias=alias,
                    confidence=profile.confidence,
                    weak=False,
                    franchise_hit=_franchise_hit(profile, haystack),
                )

        weak.sort(key=lambda item: len(item[0]), reverse=True)
        for alias, profile in weak:
            if alias.lower() not in haystack:
                continue
            if not _franchise_hit(profile, haystack):
                logger.info(
                    f"dl_senpai character weak alias ignored alias={alias} "
                    f"character={profile.canonical_name} reason=no_franchise"
                )
                continue
            return CharacterMatch(
                profile=profile,
                matched_alias=alias,
                confidence=min(profile.confidence, 0.8),
                weak=True,
                franchise_hit=True,
            )
        return None


def _franchise_hit(profile: CharacterProfile, haystack: str) -> bool:
    words = [profile.franchise, profile.franchise_localized, *profile.franchise_aliases]
    return any(word and word.lower() in haystack for word in words)


def _user_dir(config: Any) -> Path | None:
    try:
        raw = str(getattr(config, "character_dir", "") or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return Path(raw)
    except Exception:  # noqa: BLE001
        return None


# 单例：注册表是只读数据，每轮重建等于反复读盘。
_registry: CharacterRegistry | None = None


def get_character_registry(config: Any = None) -> CharacterRegistry:
    global _registry
    if _registry is None:
        _registry = CharacterRegistry.load(config)
    return _registry


def reset_character_registry() -> None:
    """测试与热重载用：丢掉缓存，下次重新读盘。"""
    global _registry
    _registry = None


# ---------------- 人工核验写回 ----------------
#
# 这里是**唯一**能把 verified 从 false 改成 true 的入口。调用它的是
# scripts/character_verify.py，也就是「人说这张档案对」那一刻。
# 程序、视觉模型、生图结果都不该走到这里——把生成结果学回档案，等于让模型
# 自己给自己出题再自己判卷。


def builtin_data_path() -> Path:
    """内置角色数据文件（随代码版本走，可提交可回滚）。"""
    return _BUILTIN_DATA


def editable_data_path(config: Any = None) -> Path:
    """默认写内置数据；角色数据是项目自带的角色知识，不是运行时私料。"""
    return _BUILTIN_DATA


def _load_raw(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _save_raw(path: Path, data: dict[str, Any]) -> None:
    # 先写临时文件再替换：CLI 中途崩掉不该留下半截 JSON
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def find_raw_entry(data: dict[str, Any], character: str) -> dict[str, Any] | None:
    """在原始 JSON 里按规范名/本地名/别名找角色条目（忽略大小写）。"""
    key = (character or "").strip().lower()
    if not key:
        return None
    for item in data.get("characters") or []:
        if not isinstance(item, dict):
            continue
        names = [item.get("canonical_name") or ""]
        names += list(item.get("localized_names") or [])
        names += list(item.get("aliases") or [])
        if any(str(n).strip().lower() == key for n in names if n):
            return item
    return None


def _raw_field(entry: dict[str, Any], field: str) -> dict[str, Any]:
    """把 visual_identity[field] 统一成 dict 形式（旧数据是纯字符串）。"""
    identity = entry.setdefault("visual_identity", {})
    raw = identity.get(field)
    if isinstance(raw, dict):
        return raw
    out: dict[str, Any] = {"value": str(raw or "").strip(), "verified": False}
    identity[field] = out
    return out


def _normalize_status(status: VerificationStatus | str) -> VerificationStatus | None:
    if isinstance(status, VerificationStatus):
        return status
    raw = str(status or "").strip().lower()
    try:
        return VerificationStatus(raw)
    except ValueError:
        return None


def update_field_status(
    path: Path,
    character: str,
    field: str,
    *,
    status: VerificationStatus | str,
    method: str = "",
    at: str = "",
    note: str = "",
) -> tuple[bool, str]:
    """写入字段的可信状态。返回 (ok, 说明)。

    这是**唯一**能产生 HUMAN_VERIFIED 的入口，规则刻意收得很紧：

    1. HUMAN_VERIFIED 必须同时传 method=human_review。
       模型审阅（assistant_review）想写成 human_verified 会被直接拒绝——
       否则「AI 给自己的判断盖人工章」这件事会在数据层重新出现。
    2. 空字段不能核验。写出「角：无 verified=true」比不写更糟。
    3. 撤销时保留 value 与 note，只把状态打回 unverified。
    """
    target = _normalize_status(status)
    if target is None:
        return False, f"非法状态: {status}（可用: unverified / assistant_reviewed / human_verified）"
    if field not in _FIELD_KEYS:
        return False, f"未知字段: {field}（可用: {', '.join(_FIELD_KEYS)}）"

    meth = (method or "").strip().lower()
    if target is VerificationStatus.HUMAN_VERIFIED and meth != METHOD_HUMAN:
        return False, (
            "assistant_review cannot create a verified field"
            if meth == METHOD_ASSISTANT
            else "human_verified 必须显式传 --method human_review"
        )

    data = _load_raw(path)
    entry = find_raw_entry(data, character)
    if entry is None:
        return False, f"未找到角色: {character}"

    raw = _raw_field(entry, field)
    current = str(raw.get("value") or "").strip()
    if target is not VerificationStatus.UNVERIFIED and not current:
        return False, f"{field} 没有内容，不能核验空字段（先 edit-field 填描述）"

    # 旧字段一律清掉，避免 verified=true 与 verification_status 打架
    raw.pop("verified", None)
    raw["verification_status"] = target.value

    if target is VerificationStatus.UNVERIFIED:
        raw.pop("verified_at", None)
        raw.pop("verification_method", None)
    else:
        raw["verification_method"] = meth or (
            METHOD_HUMAN if target is VerificationStatus.HUMAN_VERIFIED else METHOD_ASSISTANT
        )
        if at:
            raw["verified_at"] = at.strip()
        elif target is VerificationStatus.HUMAN_VERIFIED and not raw.get("verified_at"):
            raw["verified_at"] = ""
    if note:
        raw["note"] = note.strip()

    _save_raw(path, data)
    return True, f"{entry.get('canonical_name')}.{field} → {target.value}"


def update_field_value(
    path: Path,
    character: str,
    field: str,
    value: str,
    *,
    note: str = "",
) -> tuple[bool, str]:
    """改字段内容。**任何改动都会让核验状态失效**（打回 unverified）。

    为什么必须这样：否则「A 内容被人工核验过」+「改成 B」会让 B 自动继承 A 的
    核验结论。那是最阴险的信任污染——数据看起来永远是 verified，但内容早就换了。
    """
    if field not in _FIELD_KEYS:
        return False, f"未知字段: {field}（可用: {', '.join(_FIELD_KEYS)}）"
    data = _load_raw(path)
    entry = find_raw_entry(data, character)
    if entry is None:
        return False, f"未找到角色: {character}"

    raw = _raw_field(entry, field)
    old = str(raw.get("value") or "").strip()
    new = str(value or "").strip()
    if not new:
        return False, "不能把字段改成空值（要删除请显式告诉我，不要用空字符串）"

    previous = raw.get("verification_status") or ""
    raw["value"] = new
    raw.pop("verified", None)
    raw["verification_status"] = VerificationStatus.UNVERIFIED.value
    raw.pop("verified_at", None)
    raw.pop("verification_method", None)
    if note:
        raw["note"] = note.strip()
    elif previous:
        # 留痕：为什么这个字段突然变 unverified，否则以后没人知道是改过内容
        raw["note"] = f"内容已修改（原状态 {previous}），核验失效"
    _save_raw(path, data)

    if previous and previous != VerificationStatus.UNVERIFIED.value:
        return True, f"{entry.get('canonical_name')}.{field} 已更新，核验状态 {previous} → unverified"
    return True, f"{entry.get('canonical_name')}.{field} 已更新（unverified）"


def update_field_verification(
    path: Path,
    character: str,
    field: str,
    *,
    verified: bool,
    method: str = "",
    at: str = "",
    note: str = "",
    value: str | None = None,
) -> tuple[bool, str]:
    """兼容旧签名（测试与旧脚本在用）。语义已收紧：

    verified=True 只有配 method=human_review 才会变成 HUMAN_VERIFIED，
    其余 combination 一律降为 ASSISTANT_REVIEWED 或拒绝。
    """
    if value is not None:
        ok, message = update_field_value(path, character, field, value, note=note)
        if not ok:
            return ok, message
        if not verified:
            return True, message
    if verified:
        meth = (method or "").strip().lower()
        if meth and meth != METHOD_HUMAN:
            # 旧调用想用 assistant_review 冒充 verified：降级，不报错
            return update_field_status(
                path, character, field,
                status=VerificationStatus.ASSISTANT_REVIEWED, method=meth, at=at, note=note,
            )
        return update_field_status(
            path, character, field,
            status=VerificationStatus.HUMAN_VERIFIED, method=METHOD_HUMAN, at=at, note=note,
        )
    return update_field_status(
        path, character, field, status=VerificationStatus.UNVERIFIED, note=note
    )



def update_profile_status_lock(path: Path, character: str, status: str) -> tuple[bool, str]:
    """设置/清除 profile_status 的显式声明。

    正常流程不需要它：状态由字段推导。只用于两种情况——人工强行把角色锁回
    draft（保守方向），或者清掉历史遗留的手写值让推导生效（传空字符串）。
    """
    value = (status or "").strip().lower()
    if value not in ("", STATUS_DRAFT, STATUS_ASSISTANT_REVIEWED, STATUS_PARTIAL, STATUS_VERIFIED):
        return False, f"非法状态: {status}"
    data = _load_raw(path)
    entry = find_raw_entry(data, character)
    if entry is None:
        return False, f"未找到角色: {character}"
    if value:
        entry["profile_status"] = value
    else:
        entry.pop("profile_status", None)
    _save_raw(path, data)
    return True, f"{entry.get('canonical_name')}.profile_status → {value or '(自动推导)'}"


__all__ = [
    "DEFAULT_IDENTITY_SALIENCE",
    "METHOD_ASSISTANT",
    "METHOD_HUMAN",
    "METHOD_REFERENCE",
    "STATUS_ASSISTANT_REVIEWED",
    "STATUS_DRAFT",
    "STATUS_PARTIAL",
    "STATUS_VERIFIED",
    "VerificationStatus",
    "migrate_status",
    "update_field_status",
    "update_field_value",
    "_SPECIES_SLOTS",
    "_STATUS_BASE_FIELDS",
    "_STATUS_MIN_REQUIRED",
    "CharacterMatch",
    "CharacterProfile",
    "CharacterRegistry",
    "VisualField",
    "VisualIdentity",
    "builtin_data_path",
    "editable_data_path",
    "find_raw_entry",
    "update_field_verification",
    "update_profile_status_lock",
    "get_character_registry",
    "reset_character_registry",
]
