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

# profile_status 三档
STATUS_DRAFT = "draft"
STATUS_PARTIAL = "partial_verified"
STATUS_VERIFIED = "verified"


@dataclass(frozen=True)
class VisualField:
    """一个视觉特征 + 它自己的可信度。

    verified=True 只应该出现在「人工核对过」或「有已验证参考图佐证」的字段上。
    默认 False 是刻意的：宁可少写一条硬约束，也不要凭空给权威性。
    """

    value: str = ""
    verified: bool = False
    # 核验时间与方法：只允许人工审核流程写入（scripts/character_verify.py）。
    # 程序、视觉模型、生图结果都无权把 verified 从 false 改成 true。
    verified_at: str = ""
    verification_method: str = ""
    note: str = ""

    def __bool__(self) -> bool:
        return bool(self.value.strip())

    def as_dict(self) -> dict[str, Any]:
        """写回 JSON 用。空元数据不写，避免数据文件被无意义字段淹没。"""
        out: dict[str, Any] = {"value": self.value, "verified": self.verified}
        if self.verified_at:
            out["verified_at"] = self.verified_at
        if self.verification_method:
            out["verification_method"] = self.verification_method
        if self.note:
            out["note"] = self.note
        return out


class VisualIdentity:
    """角色的可见特征集合。

    兼容两种数据写法（旧数据不用改）：
        "hair": "银白偏浅紫的长发"                       → verified=False
        "hair": {"value": "...", "verified": true}      → 按标注
    字符串一律当未验证，这是保守方向的默认值。
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
                verified = bool(raw.get("verified"))
                verified_at = str(raw.get("verified_at") or "").strip()
                method = str(raw.get("verification_method") or "").strip()
                note = str(raw.get("note") or "").strip()
            else:
                value = str(raw or "").strip()
                verified = False
                verified_at = method = note = ""
            if value:
                fields[key] = VisualField(
                    value=value,
                    verified=verified,
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

    def verified_keys(self) -> tuple[str, ...]:
        return tuple(k for k in self.keys() if self._fields[k].verified)

    def unverified_keys(self) -> tuple[str, ...]:
        return tuple(k for k in self.keys() if not self._fields[k].verified)

    def lines(self, *, verified: bool | None = None) -> list[str]:
        """拼成 ["头发：…", …]。

        verified=None 取全部（默认，兼容旧调用）；True/False 只取对应档。
        """
        out: list[str] = []
        for key in self.keys():
            if verified is not None and self._fields[key].verified is not verified:
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

    def verified_fields(self) -> tuple[str, ...]:
        return self.visual_identity.verified_keys()

    def unverified_fields(self) -> tuple[str, ...]:
        return self.visual_identity.unverified_keys()

    def required_fields(self) -> tuple[str, ...]:
        """升到 verified 需要核验哪些字段（按这个角色的实际数据算）。"""
        required: list[str] = list(_STATUS_BASE_FIELDS)
        required += [slot for slot in _SPECIES_SLOTS if self.visual_identity.get(slot)]
        for extra in _STATUS_FILL_FIELDS:
            if len(required) >= _STATUS_MIN_REQUIRED:
                break
            if self.visual_identity.get(extra):
                required.append(extra)
        return tuple(dict.fromkeys(required))

    def effective_status(self) -> str:
        """角色审核状态：**始终**按字段推导，不接受人工直接写字符串。

        唯一例外是显式声明 draft —— 那是人工主动锁定「这角色别用」，
        属于保守方向，允许覆盖推导结果。反方向的升级一律由字段决定。
        """
        explicit = (self.profile_status or "").strip().lower()
        verified = set(self.verified_fields())
        required = self.required_fields()
        hit = [key for key in required if key in verified]

        if not hit:
            return STATUS_DRAFT

        # 完整核验：必核项全中且数量够。此前还要角色级 verified=True 才能升，
        # 那个开关让「字段全验了也停在 partial」，已经删掉。
        if len(hit) == len(required) and len(required) >= _STATUS_MIN_REQUIRED:
            return STATUS_VERIFIED if explicit != STATUS_DRAFT else STATUS_PARTIAL
        return STATUS_PARTIAL

    def split_preserve(self) -> tuple[list[str], list[str]]:
        """把 must_preserve 拆成（硬约束, 提示）。

        规则：能映射到某个视觉字段、且该字段已验证 → 硬约束；
        其余（含映射不到字段的抽象项，如「facial identity」但 face 为空）→ 提示。
        """
        hard: list[str] = []
        soft: list[str] = []
        for item in self.must_preserve:
            key = _preserve_field_of(item)
            if key is not None and self.visual_identity.field(key).verified:
                hard.append(item)
            else:
                soft.append(item)
        return hard, soft

    def salience(self, key: str) -> float:
        """某字段的身份显著性权重。"""
        own = self.identity_salience.get(key)
        if isinstance(own, (int, float)) and own > 0:
            return float(own)
        return float(DEFAULT_IDENTITY_SALIENCE.get(key, 1.0))

    def identity_tiers(self) -> dict[str, list[tuple[str, str]]]:
        """分三档返回视觉特征：verified / hints /（空的不出现）。

        返回 [(字段key, 值), …]，顺序按 _FIELD_KEYS。
        """
        verified: list[tuple[str, str]] = []
        hints: list[tuple[str, str]] = []
        for key in self.visual_identity.keys():
            fld = self.visual_identity.field(key)
            (verified if fld.verified else hints).append((key, fld.value))
        return {"verified": verified, "hints": hints}

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

# 允许的核验方法。human_review 才代表「人类看过原始资料并确认」。
METHOD_HUMAN = "human_review"
# assistant_review：模型根据训练知识核对。**不等同人工核验**，只作为占位，
# 方便人在没有原始资料时先跑通链路，之后必须由人复核或撤销。
METHOD_ASSISTANT = "assistant_review"
METHOD_REFERENCE = "reference_evidence"


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
    """写入某个视觉字段的核验状态。返回 (ok, 说明)。

    约束（都是刻意的）：
    - 只能验证**有内容**的字段。空值字段不能靠"验证"变成事实，那会写出
      「角：无 verified=true」这种比不写更糟的数据。
    - 撤销时不删除 value，只把 verified 打回 false —— 描述留着当下次核验的起点。
    """
    if field not in _FIELD_KEYS:
        return False, f"未知字段: {field}（可用: {', '.join(_FIELD_KEYS)}）"

    data = _load_raw(path)
    entry = find_raw_entry(data, character)
    if entry is None:
        return False, f"未找到角色: {character}"

    raw = _raw_field(entry, field)
    if value is not None:
        raw["value"] = str(value).strip()
    current = str(raw.get("value") or "").strip()
    if verified and not current:
        return False, f"{field} 没有内容，不能验证空字段（先 set-value 填描述）"

    raw["verified"] = bool(verified)
    if verified:
        raw["verified_at"] = str(at or "").strip()
        raw["verification_method"] = str(method or METHOD_HUMAN).strip()
        if note:
            raw["note"] = str(note).strip()
    else:
        # 保留历史痕迹：什么时候撤的、为什么撤，比"悄悄变回 false"有用
        raw.pop("verified_at", None)
        raw["verification_method"] = ""
        if note:
            raw["note"] = str(note).strip()

    _save_raw(path, data)
    state = "verified=true" if verified else "verified=false"
    return True, f"{entry.get('canonical_name')}.{field} → {state}"


def update_profile_status_lock(path: Path, character: str, status: str) -> tuple[bool, str]:
    """设置/清除 profile_status 的显式声明。

    正常流程不需要它：状态由字段推导。只用于两种情况——人工强行把角色锁回
    draft（保守方向），或者清掉历史遗留的手写值让推导生效（传空字符串）。
    """
    value = (status or "").strip().lower()
    if value not in ("", STATUS_DRAFT, STATUS_PARTIAL, STATUS_VERIFIED):
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
    "_SPECIES_SLOTS",
    "_STATUS_BASE_FIELDS",
    "_STATUS_MIN_REQUIRED",
    "STATUS_DRAFT",
    "STATUS_PARTIAL",
    "STATUS_VERIFIED",
    "CharacterMatch",
    "CharacterProfile",
    "CharacterRegistry",
    "VisualField",
    "VisualIdentity",
    "METHOD_ASSISTANT",
    "METHOD_HUMAN",
    "METHOD_REFERENCE",
    "builtin_data_path",
    "editable_data_path",
    "find_raw_entry",
    "update_field_verification",
    "update_profile_status_lock",
    "get_character_registry",
    "reset_character_registry",
]
