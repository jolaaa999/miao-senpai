"""通用实体解析：把「用户说的那个名字」变成可用于生图的身份依据。

## 为什么要有这一层

旧链路只有一条路：本地 characters.json 命中 → 走角色管线，否则 → 当普通图。
后果是**每个角色都要人工建档**才能保持身份，这条路不可能铺满用户会提到的所有
角色、生物、物体、场景。一旦 registry 里没有，用户的「画犬夜叉」就被悄悄降级
成一张泛泛的动漫人物。

新规则：
    cache 命中   → 直接用（0 次模型调用）
    cache 未命中 → 动态解析一次（最多 1 次轻量文本调用），拿不到就**保留原名**也照画

## 绝对不许做的事

- 不编视觉特征。模型不确定就不写 traits —— 写错的发色比不写更糟，
  因为提示词越具体，图片模型越会照着一个错误设定稳定作画。
- 不把动态结果标成 human_verified。模型猜的永远是 assistant_generated，
  这条与 Verification Trust Model 是同一条不变量。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

# 实体类型：不局限于「角色」。画坦克、画城市时也该走同一条链路。
ENTITY_TYPES = (
    "fictional_character",
    "real_person",
    "animal_species",
    "object",
    "vehicle",
    "location",
    "artwork",
    "franchise",
    "unknown_entity",
)

CACHE_VERSION = 1
# 缓存不是永久事实：模型昨天的猜测不该永远生效。
DEFAULT_TTL_SECONDS = 30 * 24 * 3600


@dataclass(frozen=True)
class Entity:
    """解析出来的一个实体。

    canonical_name / franchise 是**必须保留**的锚点：
    提示词里只要丢了拉丁名和作品名，模型就会开始按形容词猜人。
    """

    name: str                        # 用户原话里的写法（最高优先级保留）
    canonical_name: str = ""
    localized_name: str = ""
    franchise: str = ""
    type: str = "unknown_entity"
    traits: tuple[str, ...] = ()     # 高置信视觉特征，可为空
    confidence: float = 0.0
    source: str = "name_only"        # cache / dynamic_resolver / name_only
    cache_hit: bool = False
    trust: str = "assistant_generated"

    @property
    def anchor(self) -> str:
        """提示词里用来锁身份的那一行。拿不到信息就只留原名。"""
        head = self.canonical_name or self.name
        if self.localized_name and self.localized_name != head:
            head = f"{head} ({self.localized_name})"
        if self.franchise:
            return f"{head} from {self.franchise}"
        return head

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "canonical_name": self.canonical_name or self.name,
            "localized_name": self.localized_name,
            "franchise": self.franchise,
            "type": self.type,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "cache_hit": self.cache_hit,
            "traits": list(self.traits),
            "trust": self.trust,
        }


def cache_path(config: Any = None) -> Path:
    """缓存位置。运行时生成，不进 Git（见 .gitignore）。"""
    base = Path(str(getattr(config, "character_dir", "") or "")).parent if getattr(
        config, "character_dir", ""
    ) else Path(__file__).resolve().parent / "data"
    # 这里原本还有一行无条件覆盖：「base = Path(__file__).resolve().parent / "data"」，
    # 它把上面算出来的结果直接盖掉，于是 character_dir 配置从来没生效过，
    # 缓存和参考图黑名单全落在源码目录里（2026-10-27 查测试被真机数据污染时发现）。
    return base / "entity_cache.json"


class EntityCache:
    """模型生成的身份摘要缓存。可 TTL、可重解析、可被人工覆盖。"""

    def __init__(self, path: Path | None = None, *, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        self.path = Path(path) if path else cache_path()
        self.ttl = ttl
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._data = {"version": CACHE_VERSION, "entries": {}}
            return
        except Exception as exc:  # noqa: BLE001 — 缓存坏了不该拖垮生图
            logger.warning(f"dl_senpai entity cache unreadable: {exc}")
            self._data = {"version": CACHE_VERSION, "entries": {}}
            return
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            # 版本不合就丢弃重来：宁可多解析一次，也不要用结构不明的旧数据
            self._data = {"version": CACHE_VERSION, "entries": {}}
            return
        self._data = raw
        self._data.setdefault("entries", {})

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)   # 原子替换：避免半截 JSON 让下次启动全丢
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"dl_senpai entity cache write failed: {exc}")

    @staticmethod
    def key(name: str) -> str:
        return re.sub(r"\s+", "", str(name or "")).strip().lower()

    def get(self, name: str) -> Entity | None:
        key = self.key(name)
        entry = (self._data.get("entries") or {}).get(key)
        if not isinstance(entry, dict):
            return None
        created = float(entry.get("created_at") or 0)
        # 过期或时间戳缺失都当没有：缺时间戳说明数据损坏，不能当成"永不过期"
        if self.ttl > 0 and (not created or (time.time() - created) > self.ttl):
            return None
        canonical = str(entry.get("canonical_name") or "").strip()
        if not canonical:
            return None
        return Entity(
            name=str(entry.get("name") or name),
            canonical_name=canonical,
            localized_name=str(entry.get("localized_name") or ""),
            franchise=str(entry.get("franchise") or ""),
            type=str(entry.get("type") or "unknown_entity"),
            traits=tuple(str(x) for x in (entry.get("traits") or []) if str(x).strip()),
            confidence=float(entry.get("confidence") or 0.0),
            source="cache",
            cache_hit=True,
            trust=str(entry.get("trust") or "assistant_generated"),
        )

    def put(self, entity: Entity, *, model: str = "") -> None:
        """只缓存 assistant_generated。human_verified 的来源是人工核验，不走这里。"""
        key = self.key(entity.name or entity.canonical_name)
        if not key or not entity.canonical_name:
            return
        entries = self._data.setdefault("entries", {})
        entries[key] = {
            "name": entity.name,
            "canonical_name": entity.canonical_name,
            "localized_name": entity.localized_name,
            "franchise": entity.franchise,
            "type": entity.type,
            "traits": list(entity.traits),
            "confidence": round(entity.confidence, 3),
            "source": "dynamic_resolver",
            "model": model,
            "trust": "assistant_generated",   # 永远不升 human_verified
            "created_at": time.time(),
        }
        self._save()

    def clear(self) -> int:
        count = len(self._data.get("entries") or {})
        self._data = {"version": CACHE_VERSION, "entries": {}}
        self._save()
        return count


_RESOLVE_PROMPT = """Identify the entity the user is referring to in an image request.

User request: {request}
Candidate name(s): {names}

Return JSON only:
{{"entities":[{{"name":"<as user wrote>","canonical_name":"<latin/native canonical>",
"localized_name":"<Chinese name if any>","franchise":"<work/series, empty if none>",
"type":"fictional_character|real_person|animal_species|object|vehicle|location|artwork|franchise|unknown_entity",
"traits":["<only high-confidence visual traits you are sure about>"],
"confidence":0.0}}]}}

Rules:
- Only list entities that are genuinely NAMED in the request. Do not invent.
- traits: list ONLY traits you are highly confident about. If unsure, use an empty list.
  A wrong trait is worse than no trait.
- If the candidate is not a real named entity (it is a generic description), return
  type "unknown_entity" with confidence 0 and empty traits.
- Never translate a named character into a generic description."""


class UniversalEntityResolver:
    """cache 优先，miss 时最多解析一次。"""

    def __init__(self, config: Any = None, *, cache: EntityCache | None = None) -> None:
        self.config = config
        self.cache = cache or EntityCache(
            ttl=float(getattr(config, "draw_entity_cache_ttl", DEFAULT_TTL_SECONDS) or DEFAULT_TTL_SECONDS)
        )
        self.last_call_count = 0     # 供 trace/测试断言：解析花了多少次模型调用

    def from_cache(self, names: list[str]) -> list[Entity]:
        out: list[Entity] = []
        for name in names:
            hit = self.cache.get(name)
            if hit is not None:
                out.append(hit)
        return out

    async def resolve(self, names: list[str], *, request: str = "") -> list[Entity]:
        """把候选名解析成实体。任何一步失败都退回「只有名字」，绝不阻断生图。"""
        self.last_call_count = 0
        names = [n for n in dict.fromkeys(names) if str(n).strip()]
        if not names:
            return []

        entities: list[Entity] = []
        missing: list[str] = []
        for name in names:
            hit = self.cache.get(name)
            if hit is not None:
                entities.append(hit)
            else:
                missing.append(name)

        if not missing:
            return entities

        if not bool(getattr(self.config, "draw_entity_dynamic_resolve", True)):
            return entities + [Entity(name=n) for n in missing]

        resolved, ok = await self._dynamic(request or " ".join(names), missing)
        self.last_call_count = 1 if ok else 0
        if not resolved:
            # 解析失败/低置信：只留名字。宁可信息少，也不要编。
            return entities + [Entity(name=n) for n in missing]

        model = ""
        try:
            model = str(getattr(self.config, "draw_vision_model", "") or "")
        except Exception:  # noqa: BLE001
            model = ""
        for entity in resolved:
            # 低置信不进缓存：一次含糊的猜测没有复用价值，只会污染后续请求
            if entity.confidence >= 0.5 and entity.canonical_name:
                self.cache.put(entity, model=model)
        return entities + resolved

    async def _dynamic(self, request: str, names: list[str]) -> tuple[list[Entity], bool]:
        try:
            from .draw_enhance import _chat

            text = await _chat(
                self.config,
                [{"type": "text", "text": _RESOLVE_PROMPT.format(
                    request=request[:400], names=", ".join(names)[:200])}],
                max_tokens=500,
            )
        except Exception as exc:  # noqa: BLE001 — 解析是增强项，失败就降级
            logger.warning(f"dl_senpai entity resolve failed: {type(exc).__name__}: {exc}")
            return [], False

        data = {}
        match = re.search(r"\{.*\}", text or "", re.S)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:  # noqa: BLE001
                data = {}
        raw = data.get("entities") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return [], True

        out: list[Entity] = []
        for item in raw[:4]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            canonical = str(item.get("canonical_name") or "").strip()
            etype = str(item.get("type") or "unknown_entity").strip()
            if etype not in ENTITY_TYPES:
                etype = "unknown_entity"
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
            except (TypeError, ValueError):
                confidence = 0.0
            traits = tuple(
                str(x).strip() for x in (item.get("traits") or [])[:6] if str(x).strip()
            )
            if not name and not canonical:
                continue
            # 低置信 / 泛化描述：只保留名字，特征一律丢弃
            if confidence < 0.5 or etype == "unknown_entity":
                traits = ()
            out.append(Entity(
                name=name or canonical,
                canonical_name=canonical,
                localized_name=str(item.get("localized_name") or "").strip(),
                franchise=str(item.get("franchise") or "").strip(),
                type=etype,
                traits=traits,
                confidence=confidence,
                source="dynamic_resolver",
            ))
        return out, True


__all__ = [
    "CACHE_VERSION",
    "DEFAULT_TTL_SECONDS",
    "ENTITY_TYPES",
    "Entity",
    "EntityCache",
    "UniversalEntityResolver",
]
