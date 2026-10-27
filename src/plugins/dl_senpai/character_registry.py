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

# 身份核心字段：判断 profile 审核状态时看这几个有没有被验证
_CORE_FIELDS = ("face", "hair", "horns", "eyes", "clothing")

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

    def __bool__(self) -> bool:
        return bool(self.value.strip())


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
            else:
                value = str(raw or "").strip()
                verified = False
            if value:
                fields[key] = VisualField(value=value, verified=verified)
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
        return {k: {"value": v.value, "verified": v.verified} for k, v in self._fields.items()}


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

    def effective_status(self) -> str:
        """角色审核状态：显式声明优先，否则按字段推导。

        为什么推导规则要求「核心字段里有 4 个已验证」才算 verified：
        只有角验证了、脸没验证的档案直接标 verified 是自欺欺人。
        """
        explicit = (self.profile_status or "").strip().lower()
        if explicit in (STATUS_DRAFT, STATUS_PARTIAL, STATUS_VERIFIED):
            return explicit
        verified = set(self.verified_fields()) & set(_CORE_FIELDS)
        if self.verified and len(verified) >= 4:
            return STATUS_VERIFIED
        if verified:
            return STATUS_PARTIAL
        return STATUS_DRAFT

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


__all__ = [
    "DEFAULT_IDENTITY_SALIENCE",
    "STATUS_DRAFT",
    "STATUS_PARTIAL",
    "STATUS_VERIFIED",
    "CharacterMatch",
    "CharacterProfile",
    "CharacterRegistry",
    "VisualField",
    "VisualIdentity",
    "get_character_registry",
    "reset_character_registry",
]
