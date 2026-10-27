"""角色身份注册表：把「艾雅法拉」这个名字变成可用的身份锚点。

为什么另起一层，而不是继续往 draw_cards 里塞：

1. `CharacterCard` 只有 name / aliases / spec 三段字符串——没有作品名（franchise）、
   没有 must_preserve 与 creative_freedom 的区分、也没有置信度。于是：
   - 名字没命中时**完全没有任何身份信息**（艾雅法拉压根不在卡里，这就是
     「画一张艾雅法拉」只能画出泛化羊角少女的直接原因）；
   - 命中了也只有一段中文描述，提示词里没有 `Eyjafjalla / Arknights` 这样的
     英文锚点，生图模型很容易顺着「羊角」滑到通用奇幻少女。
2. 数据要与代码分离：角色数据放 JSON，运营/用户可以覆盖，不用改 Python。

数据来源分三层（后者覆盖前者）：包内 `data/characters.json` → 用户目录
`data/dl_senpai/characters/*.json`。仍属于用户所说的「本地 Profile」，
不联网、不调模型。
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


@dataclass(frozen=True)
class VisualIdentity:
    """一个角色的**可见**特征。空字段一律不写进提示词。

    为什么不填空值：提示词里出现「horns: 无」这种否定信息反而会让模型去画角。
    """

    race: str = ""
    hair: str = ""
    eyes: str = ""
    horns: str = ""
    ears: str = ""
    clothing: str = ""
    accessories: str = ""
    body_features: str = ""
    color_palette: str = ""
    silhouette: str = ""

    _LABELS: tuple[tuple[str, str], ...] = (
        ("race", "族属/种族"),
        ("hair", "头发"),
        ("eyes", "眼睛"),
        ("horns", "角"),
        ("ears", "耳朵"),
        ("body_features", "身体特征"),
        ("clothing", "服装"),
        ("accessories", "配饰"),
        ("color_palette", "配色"),
        ("silhouette", "体型轮廓"),
    )

    def lines(self) -> list[str]:
        """非空字段 → ["头发：…", "眼睛：…"]。"""
        out: list[str] = []
        for key, label in self._LABELS:
            value = str(getattr(self, key, "") or "").strip()
            if value:
                out.append(f"{label}：{value}")
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> VisualIdentity:
        data = data or {}
        return cls(**{k: str(data.get(k) or "") for k, _ in cls._LABELS})


@dataclass(frozen=True)
class CharacterProfile:
    """一个已知角色的身份档案。

    `verified=False` 表示没能逐条人工核对过——这类档案仍可用于提示词，
    但只会写里面确定的那几项，不会被当作"百科"去补全。
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
    verified: bool = False
    visual_identity: VisualIdentity = field(default_factory=VisualIdentity)
    must_preserve: tuple[str, ...] = ()
    creative_freedom: tuple[str, ...] = ("pose", "background", "lighting", "camera_angle", "expression")
    negative_notes: tuple[str, ...] = ()
    source: str = "builtin"

    @property
    def display_name(self) -> str:
        """优先用中文名，日志与提示词都更容易读。"""
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
        """参考图库的目录名：arknights/eyjafjalla。"""
        def _slug(text: str) -> str:
            return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

        return f"{_slug(self.franchise) or 'unknown'}/{_slug(self.canonical_name) or 'unknown'}"

    def all_names(self) -> tuple[str, ...]:
        return (self.canonical_name, *self.localized_names, *self.aliases)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str = "builtin") -> CharacterProfile:
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
            verified=bool(data.get("verified")),
            visual_identity=VisualIdentity.from_dict(data.get("visual_identity")),
            must_preserve=tuple(str(x) for x in data.get("must_preserve") or ()),
            creative_freedom=tuple(str(x) for x in data.get("creative_freedom") or ()),
            negative_notes=tuple(str(x) for x in data.get("negative_notes") or ()),
            source=source,
        )


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
    """角色档案集合 + 别名解析。

    `resolve(name)` 是精确解析（给「用户明确说了角色名」用），
    `find_in_text(*texts)` 是文本扫描（给「从一句话里找角色」用）。
    """

    def __init__(self, profiles: list[CharacterProfile] | None = None) -> None:
        self._profiles: dict[str, CharacterProfile] = {}
        for profile in profiles or []:
            if profile.canonical_name:
                self._profiles[profile.canonical_name.lower()] = profile

    # -- 加载 --

    @classmethod
    def from_file(cls, path: Path, *, source: str | None = None, base: CharacterRegistry | None = None) -> CharacterRegistry:
        """读一个 JSON 数据文件；`base` 存在时在它之上叠加（同名覆盖）。"""
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
        2. **弱别名必须带作品名**：`小羊` 只有在同一句里出现「明日方舟 / Arknights」
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
                # 只有弱别名、没有作品名：不认。留痕便于事后排查
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
    "CharacterMatch",
    "CharacterProfile",
    "CharacterRegistry",
    "VisualIdentity",
    "get_character_registry",
    "reset_character_registry",
]
