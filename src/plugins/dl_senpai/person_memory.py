"""按 QQ 号跨群长时印象：像真人认识一个人那样积累画像。

不止记「主太刀 / 玩怪猎」这类事实标签，还会记：
- vibe：对面是什么样的人（气质/性格）
- style：说话习惯
- relation：跟学姐处成什么关系感
- moments：难忘的小瞬间
- traits / notes：事实标签与总备注
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from .storage import JsonStore

try:
    from nonebot import logger
except Exception:  # noqa: BLE001 — 单测/独立脚本可能无 nonebot

    class _Logger:
        def warning(self, msg: str) -> None:
            print(msg)

        def info(self, msg: str) -> None:
            print(msg)

        def exception(self, msg: str) -> None:
            print(msg)

    logger = _Logger()  # type: ignore[assignment]

from .group_card import loads_marker_json
from .style import MARKER_CLOSE_PATTERN

# 解析层**严格要求 >>>**：marker_baseline 把 IMPRESSION 的容错矩阵钉成
# 「只认 >>>」，那是历史调优的结果，不能为了统一而抹平。闭合符写错的标记
# 交给下面的残片正则从正文剥掉（正是 baseline 的「损坏 payload → 剥掉但不
# 产生 action」），只是不再写画像 —— 线上 7 处就是这种标记留在了群消息里。
_IMPRESSION_MARKER_RE = re.compile(
    r"<<<IMPRESSION\s*(\{.*?\})\s*>>>",
    re.DOTALL | re.IGNORECASE,
)
# 剥除层宽容错：闭合符写错（>>／> > >／＞＞／】】）、payload 损坏、整段截断，
# 一律从正文清掉。解析严格、剥除宽容，两层职责不同。
_IMPRESSION_RESIDUE_RE = re.compile(
    r"\[?\s*<<<IMPRESSION(?:\s*\{.*?\})?\s*" + MARKER_CLOSE_PATTERN + r"\s*\]?",
    re.DOTALL | re.IGNORECASE,
)
_TRAIT_SPLIT_RE = re.compile(r"[,，;；、|/]+")
_BANNED_TRAIT_RE = re.compile(
    r"(密码|token|api.?key|密钥|身份证|银行卡|手机号|验证码|住址)",
    re.IGNORECASE,
)


def _clean_text(raw: Any, *, max_len: int) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(raw or "").strip())
    text = re.sub(r"\s{2,}", " ", text)
    if _BANNED_TRAIT_RE.search(text):
        return ""
    if max_len > 0 and len(text) > max_len:
        text = text[:max_len].rstrip()
    return text


@dataclass
class PersonProfile:
    user_id: str
    display_names: list[str] = field(default_factory=list)
    traits: list[str] = field(default_factory=list)
    # 像真人认识一个人：气质、说话方式、关系感、难忘瞬间
    vibe: str = ""
    style: str = ""
    relation: str = ""
    moments: list[str] = field(default_factory=list)
    # 「她心里记着、但没说出口的事」。搬自 ai-companion-kit 的 pending：
    # 记忆光有档案没有心事，就只是数据库；有了这一层，下次见面她能自己
    # 想起来「他还欠我一个解释」，而不是干等着对方提。
    pending: list[str] = field(default_factory=list)
    notes: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, user_id: str, data: dict[str, Any]) -> PersonProfile:
        names_raw = data.get("display_names") or []
        traits_raw = data.get("traits") or []
        moments_raw = data.get("moments") or []
        names = (
            [str(n).strip() for n in names_raw if str(n).strip()]
            if isinstance(names_raw, list)
            else []
        )
        traits = (
            [str(t).strip() for t in traits_raw if str(t).strip()]
            if isinstance(traits_raw, list)
            else []
        )
        moments = (
            [str(m).strip() for m in moments_raw if str(m).strip()]
            if isinstance(moments_raw, list)
            else []
        )
        pending_raw = data.get("pending") or []
        pending = (
            [str(p).strip() for p in pending_raw if str(p).strip()]
            if isinstance(pending_raw, list)
            else []
        )
        return cls(
            user_id=str(user_id),
            display_names=names,
            traits=traits,
            vibe=str(data.get("vibe") or "").strip(),
            style=str(data.get("style") or "").strip(),
            relation=str(data.get("relation") or "").strip(),
            moments=moments,
            pending=pending,
            notes=str(data.get("notes") or "").strip(),
            updated_at=float(data.get("updated_at") or 0),
        )

    def has_content(self) -> bool:
        return bool(
            self.traits
            or self.notes
            or self.display_names
            or self.vibe
            or self.style
            or self.relation
            or self.moments
            or self.pending
        )


@dataclass(frozen=True)
class ImpressionUpdate:
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    notes: str | None = None  # None=不改；""=清空
    vibe: str | None = None
    style: str | None = None
    relation: str | None = None
    moment: str | None = None  # 追加一条难忘瞬间
    # 追加一条「没说出口的事」；传空串表示清空（事情说开了就不再挂着）
    pending: str | None = None


@dataclass
class ImpressionParseResult:
    text: str
    updates: list[ImpressionUpdate]


def sanitize_trait(raw: str, *, max_len: int = 24) -> str | None:
    text = _clean_text(raw, max_len=max_len)
    if not text or len(text) < 2:
        return None
    return text


def _normalize_trait_list(items: Any, *, max_len: int = 24) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(items, str):
        parts = _TRAIT_SPLIT_RE.split(items)
    elif isinstance(items, list):
        parts = []
        for item in items:
            if isinstance(item, str) and ("," in item or "，" in item or "、" in item):
                parts.extend(_TRAIT_SPLIT_RE.split(item))
            else:
                parts.append(item)
    else:
        return []
    for part in parts:
        cleaned = sanitize_trait(str(part), max_len=max_len)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def parse_impression_updates(raw_reply: str) -> ImpressionParseResult:
    updates: list[ImpressionUpdate] = []
    for match in _IMPRESSION_MARKER_RE.finditer(raw_reply or ""):
        data = loads_marker_json(match.group(1))
        if not data:
            continue
        add = _normalize_trait_list(data.get("add") or data.get("traits") or [])
        remove = _normalize_trait_list(data.get("remove") or data.get("del") or [])

        def opt_field(*keys: str, max_len: int) -> str | None:
            for key in keys:
                if key not in data:
                    continue
                raw = data.get(key)
                if raw is None:
                    return None
                return _clean_text(raw, max_len=max_len)
            return None

        notes = opt_field("notes", "note", max_len=400)
        vibe = opt_field("vibe", "personality", "portrait", max_len=160)
        style = opt_field("style", "talk", "tone", max_len=80)
        relation = opt_field("relation", "bond", "with_me", max_len=80)
        moment = opt_field("moment", "memory", "episode", max_len=80)
        pending = opt_field("pending", "unsaid", "heart", max_len=120)

        if (
            not add
            and not remove
            and notes is None
            and vibe is None
            and style is None
            and relation is None
            and moment is None
            and pending is None
        ):
            continue
        updates.append(
            ImpressionUpdate(
                add=add,
                remove=remove,
                notes=notes,
                vibe=vibe,
                style=style,
                relation=relation,
                moment=moment,
                pending=pending,
            )
        )

    cleaned = strip_impression_markers(raw_reply or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return ImpressionParseResult(text=cleaned, updates=updates)


def strip_impression_markers(text: str) -> str:
    """剥掉正文里的 IMPRESSION 标记，**包括解析失败的残片**。

    为什么这里比解析层宽：marker_baseline 规定的正是「损坏 payload 的 marker
    被剥掉，但不产生 action」。少写一个 > 不该让标记跟着回复发到群里，
    但也不该据此写画像 —— 两层职责不同，所以判据也不同。
    """
    cleaned = _IMPRESSION_MARKER_RE.sub("", text or "")
    cleaned = _IMPRESSION_RESIDUE_RE.sub('', cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


# 用户自我介绍兜底：模型漏写 <<<IMPRESSION>>> 时仍可记一点稳定特征
_SELF_INTRO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:我(?:是|叫)|我的名字(?:是|叫)?)\s*([^\s，。！？,.!?]{2,12})",
        re.I,
    ),
    re.compile(
        r"我(?:主玩|主力|主用|常用)?\s*([太刀大剑弓弩枪锤笛盾斧虫棍双刀斩斧铳枪轻弩重弩]{1,3})",
    ),
    re.compile(
        r"我(?:喜欢|爱玩|常玩|在玩|玩的是)\s*([^\s，。！？,.!?]{2,16})",
        re.I,
    ),
    re.compile(
        r"我(?:是|读|学)\s*([^\s，。！？,.!?]{2,12}(?:专业|系|方向)?)",
        re.I,
    ),
    re.compile(
        r"我(?:主|玩)\s*(太刀|大剑|弓|轻弩|重弩|铳枪|长枪|铳剑|片手|双刀|大锤|狩猎笛|斩斧|盾斧|操虫棍)",
    ),
)


def extract_traits_from_user_text(text: str, *, max_traits: int = 3) -> list[str]:
    """从用户话里粗提取可记特征（兜底，宁缺毋滥）。"""
    raw = (text or "").strip()
    if len(raw) < 4:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for pat in _SELF_INTRO_PATTERNS:
        for match in pat.finditer(raw):
            chunk = sanitize_trait(match.group(1))
            if not chunk:
                continue
            if chunk in {"学姐", "学长", "你", "你们", "机器人", "AI", "ai"}:
                continue
            key = chunk.casefold()
            if key in seen:
                continue
            seen.add(key)
            found.append(chunk)
            if len(found) >= max_traits:
                return found
    return found


def build_person_memory_brief(
    profile: PersonProfile,
    *,
    name: str = "",
    query: str = "",
) -> str:
    if not profile.has_content():
        return ""
    who = (name or "").strip() or (
        profile.display_names[0] if profile.display_names else f"QQ{profile.user_id}"
    )
    lines = [
        "【对这个人的长时印象】（按 QQ 跨群共用——像真人慢慢认识一个人）",
        f"对方：{who}（qq={profile.user_id}）",
    ]
    aliases = [n for n in profile.display_names if n and n != who][:4]
    if aliases:
        lines.append(f"还见过的名字：{'、'.join(aliases)}")
    if profile.vibe:
        lines.append(f"是什么样的人：{profile.vibe}")
    if profile.style:
        lines.append(f"说话习惯：{profile.style}")
    if profile.relation:
        lines.append(f"跟学姐的关系感：{profile.relation}")
    traits = profile.traits
    moments = profile.moments
    if query.strip():
        from .memory_recall import rank_person_items

        traits = rank_person_items(list(traits), query, keep=16)
        moments = rank_person_items(list(moments), query, keep=4)
    else:
        traits = list(traits)[:16]
        moments = list(moments)[-4:]
    if traits:
        lines.append(f"已知事实/兴趣：{'；'.join(traits)}")
    if moments:
        lines.append(f"难忘瞬间：{'；'.join(moments)}")
    if profile.pending:
        # 用「⚠️」而不是普通列表：这一条最容易在长上下文里被忽略，
        # 而它恰恰是「她心里挂着的事」——漏掉的代价比别的条目大。
        lines.append(f"⚠️ 你心里还记着、没说出口的事：{'；'.join(profile.pending[-4:])}")
    if profile.notes:
        lines.append(f"总备注：{profile.notes}")
    lines.append(
        "聊天时自然用上（像熟人接话），别念成档案、别每句都复读；"
        "有新认识或印象变了就写 <<<IMPRESSION ...>>> 更新。"
    )
    return "\n".join(lines)


class PersonMemoryStore:
    """一人一文件：user_{qq}.json。"""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        max_traits: int = 16,
        max_note_chars: int = 400,
        max_names: int = 6,
        max_moments: int = 8,
        max_vibe_chars: int = 160,
        max_pending: int = 6,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.max_traits = max(3, int(max_traits))
        self.max_note_chars = max(40, int(max_note_chars))
        self.max_names = max(2, int(max_names))
        self.max_moments = max(2, int(max_moments))
        self.max_vibe_chars = max(40, int(max_vibe_chars))
        # 心事留 6 条上限：再多就不是「挂着的事」，而是待办清单了
        self.max_pending = max(1, int(max_pending))
        self._lock = Lock()

    def _path(self, user_id: int | str) -> Path:
        return self.data_dir / f"user_{user_id}.json"

    def _store(self, user_id: int | str) -> JsonStore:
        return JsonStore(self._path(user_id), create_parent=False)

    def get(self, user_id: int | str) -> PersonProfile:
        raw = self._store(user_id).load(default=None)
        if not isinstance(raw, dict):
            return PersonProfile(user_id=str(user_id))
        return PersonProfile.from_dict(str(user_id), raw)

    def save(self, profile: PersonProfile) -> None:
        """原子写。修复重构前 `write_text` 可能写坏画像文件的问题。

        路径 `data/dl_senpai/person_memory/user_{qq}.json` 与 JSON 结构
        均未改变，历史画像文件可直接读取。
        """
        self._store(profile.user_id).save(profile.to_dict())

    def touch_name(self, user_id: int | str, display_name: str) -> PersonProfile:
        """记录见过的昵称/名片，跨群累积。"""
        name = re.sub(r"\s+", " ", (display_name or "").strip())
        profile = self.get(user_id)
        if not name:
            return profile
        if profile.display_names and profile.display_names[0] == name:
            return profile
        names = [n for n in profile.display_names if n != name]
        names.insert(0, name)
        profile.display_names = names[: self.max_names]
        profile.updated_at = time.time()
        self.save(profile)
        return profile

    def apply_updates(
        self,
        user_id: int | str,
        updates: list[ImpressionUpdate],
        *,
        display_name: str = "",
    ) -> PersonProfile:
        profile = self.get(user_id)
        if display_name.strip():
            name = re.sub(r"\s+", " ", display_name.strip())
            names = [n for n in profile.display_names if n != name]
            names.insert(0, name)
            profile.display_names = names[: self.max_names]

        traits = list(profile.traits)
        moments = list(profile.moments)
        pending_items = list(profile.pending)
        notes = profile.notes
        vibe = profile.vibe
        style = profile.style
        relation = profile.relation
        changed = False

        for upd in updates:
            for rem in upd.remove:
                before = len(traits)
                traits = [t for t in traits if t.casefold() != rem.casefold()]
                if len(traits) != before:
                    changed = True
            for add in upd.add:
                if any(t.casefold() == add.casefold() for t in traits):
                    continue
                traits.append(add)
                changed = True
            if upd.notes is not None:
                note = upd.notes
                if self.max_note_chars > 0 and len(note) > self.max_note_chars:
                    note = note[: self.max_note_chars].rstrip()
                if note != notes:
                    notes = note
                    changed = True
            if upd.vibe is not None:
                v = upd.vibe
                if self.max_vibe_chars > 0 and len(v) > self.max_vibe_chars:
                    v = v[: self.max_vibe_chars].rstrip()
                if v != vibe:
                    vibe = v
                    changed = True
            if upd.style is not None and upd.style != style:
                style = upd.style[:80]
                changed = True
            if upd.relation is not None and upd.relation != relation:
                relation = upd.relation[:80]
                changed = True
            if upd.moment:
                m = upd.moment
                if not any(x.casefold() == m.casefold() for x in moments):
                    moments.append(m)
                    changed = True
            if upd.pending is not None:
                # 空串 = 这件事说开了，从心里放下。不清掉的话，
                # 「没说出口的事」只增不减，几个月后她还挂着一堆过期心事。
                text = upd.pending.strip()
                if not text:
                    if pending_items:
                        pending_items = []
                        changed = True
                elif not any(x.casefold() == text.casefold() for x in pending_items):
                    pending_items.append(text)
                    changed = True

        if len(traits) > self.max_traits:
            traits = traits[-self.max_traits :]
            changed = True
        if len(moments) > self.max_moments:
            moments = moments[-self.max_moments :]
            changed = True
        if len(pending_items) > self.max_pending:
            pending_items = pending_items[-self.max_pending :]
            changed = True

        if display_name.strip() or changed:
            profile.traits = traits
            profile.moments = moments
            profile.pending = pending_items
            profile.notes = notes
            profile.vibe = vibe
            profile.style = style
            profile.relation = relation
            profile.updated_at = time.time()
            self.save(profile)
            logger.info(
                f"dl_senpai person_memory updated user={user_id} "
                f"vibe={profile.vibe!r} style={profile.style!r} "
                f"relation={profile.relation!r} traits={profile.traits!r}"
            )
        return profile


_store: PersonMemoryStore | None = None


def get_person_memory_store(
    data_dir: str | Path | None = None,
    *,
    max_traits: int = 16,
    max_note_chars: int = 400,
) -> PersonMemoryStore:
    global _store
    if data_dir is not None:
        return PersonMemoryStore(
            data_dir,
            max_traits=max_traits,
            max_note_chars=max_note_chars,
        )
    if _store is None:
        from .config import get_config

        cfg = get_config()
        _store = PersonMemoryStore(
            cfg.person_memory_dir,
            max_traits=cfg.person_memory_max_traits,
            max_note_chars=cfg.person_memory_max_note_chars,
        )
    return _store


def reset_person_memory_store() -> None:
    global _store
    _store = None
