"""轻量内心状态：mood / energy / social（摘自 MumuBot / elysia 情绪三维思路）。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path
from threading import Lock

_NEUTRAL = 55.0
_MIN = 0.0
_MAX = 100.0
# 每小时向中性回撤的比例
_DECAY_PER_HOUR = 0.08


# 心绪手札：只活三天的一句话情绪（2026-10-25 加，思路来自小黑盒「大肥鱼教程篇2」）。
#
# 为什么不直接算总分：把她心里的碎片压成一个数字，等于把「有点闷」变成
# 「心情 42 分」。分数不会让她说话变冷，只会让提示词里多一个数字。
# 手札保留原文 + 标签，注入时同类并排（「被冷落 ×3」），
# **系统不点火**——攒够了要不要发作由她自己决定，否则会出现
# 「无缘无故发脾气、你永远不知道火从哪来」。
_NOTE_LIFE_SEC = 3 * 86400
_NOTE_MAX_CHARS = 40


@dataclass
class InnerState:
    # 数值三维保留：只用于算「插嘴概率」，不再进提示词（见 brief）
    mood: float = _NEUTRAL
    energy: float = _NEUTRAL
    social: float = _NEUTRAL
    updated_at: float = 0.0
    # -- 心情分层（2026-10-25）--
    # 底色：凌晨结算定的那一层，决定她带着什么心情醒来
    base_word: str = ""
    base_reason: str = ""
    base_since: float = 0.0
    # 动态：白天被某句话改掉的那层（留空 = 跟底色一样）
    word: str = ""
    word_since: float = 0.0
    # 手札：[{t, tag, text}]
    notes: list[dict[str, Any]] = field(default_factory=list)

    def clamp(self) -> None:
        self.mood = max(_MIN, min(_MAX, float(self.mood)))
        self.energy = max(_MIN, min(_MAX, float(self.energy)))
        self.social = max(_MIN, min(_MAX, float(self.social)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mood": round(self.mood, 2),
            "energy": round(self.energy, 2),
            "social": round(self.social, 2),
            "updated_at": float(self.updated_at),
            "base_word": self.base_word,
            "base_reason": self.base_reason,
            "base_since": float(self.base_since),
            "word": self.word,
            "word_since": float(self.word_since),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> InnerState:
        st = cls(
            mood=float(data.get("mood", _NEUTRAL)),
            energy=float(data.get("energy", _NEUTRAL)),
            social=float(data.get("social", _NEUTRAL)),
            updated_at=float(data.get("updated_at") or 0.0),
            base_word=str(data.get("base_word") or "").strip(),
            base_reason=str(data.get("base_reason") or "").strip(),
            base_since=float(data.get("base_since") or 0.0),
            word=str(data.get("word") or "").strip(),
            word_since=float(data.get("word_since") or 0.0),
            notes=[x for x in (data.get("notes") or []) if isinstance(x, dict)],
        )
        st.clamp()
        return st


def _decay_toward_neutral(state: InnerState, *, now: float) -> InnerState:
    if state.updated_at <= 0:
        state.updated_at = now
        return state
    hours = max(0.0, (now - state.updated_at) / 3600.0)
    if hours <= 0:
        return state
    factor = min(1.0, hours * _DECAY_PER_HOUR)
    for attr in ("mood", "energy", "social"):
        cur = getattr(state, attr)
        setattr(state, attr, cur + (_NEUTRAL - cur) * factor)
    state.clamp()
    state.updated_at = now
    return state


def interrupt_prob_multiplier(state: InnerState) -> float:
    """social/energy 低时略降插嘴概率；不影响 @ 必回。"""
    social_f = 0.55 + 0.45 * (state.social / 100.0)
    energy_f = 0.65 + 0.35 * (state.energy / 100.0)
    return max(0.35, min(1.15, social_f * energy_f))


def current_word(state: InnerState) -> str:
    """她现在到底是什么心情：动态覆盖底色。"""
    return (state.word or state.base_word or "").strip()


def notes_summary(state: InnerState, *, now: float | None = None) -> str:
    """把三天内同类手札并排列出来，带计数。

    只摆事实，不点火：不写「她很生气」，也不提示该发作。
    真人憋着的时候不会突然爆炸，是一点点变冷的——这里只负责让「冷」
    有据可查。
    """
    moment = time.time() if now is None else now
    counts: dict[str, int] = {}
    order: list[str] = []
    for note in state.notes:
        try:
            ts = float(note.get("t") or 0)
        except (TypeError, ValueError):
            continue
        if moment - ts > _NOTE_LIFE_SEC:
            continue
        tag = str(note.get("tag") or "").strip()
        if not tag:
            continue
        if tag not in counts:
            order.append(tag)
            counts[tag] = 0
        counts[tag] += 1
    if not counts:
        return ""
    return "、".join(f"{tag}×{counts[tag]}" for tag in order)


def build_inner_state_brief(state: InnerState, *, now: float | None = None) -> str:
    """她此刻的心情。给的是话，不是分数。

    为什么去掉数字（2026-10-25）：以前尾部挂一行
    「mood=42 energy=38 social=55」，模型看到数字只会把它当参数复述，
    而「有点闷」才是能变成语气的东西。
    """
    lines: list[str] = []
    word = current_word(state)
    if word:
        head = f"现在{word}"
        if state.base_word and state.word and state.word != state.base_word:
            head = f"本来{state.base_word}，现在{state.word}"
        elif state.base_reason:
            head = f"现在{word}（{state.base_reason}）"
        lines.append(head)
    sticking = notes_summary(state, now=now)
    if sticking:
        lines.append(f"心里憋着的：{sticking}")
    if not lines:
        # 还没结算过心情时，退回原来的数值建议，别让这一段突然消失
        return _legacy_tips(state)
    return "【你现在的状态】（自己知道就行，别念出来）\n" + "；".join(lines)


def _legacy_tips(state: InnerState) -> str:
    tips: list[str] = []
    if state.energy < 35:
        tips.append("现在有点懒/累，回复偏短，别写小作文")
    elif state.energy > 75:
        tips.append("精力不错，可以多损两句或接梗")
    if state.mood < 35:
        tips.append("心情一般，少热情收尾，别假客服")
    elif state.mood > 75:
        tips.append("心情不错，可以轻快一点，仍保持学姐口吻")
    if state.social < 35:
        tips.append("社交意愿偏低，插嘴欲弱，被点名才认真回")
    elif state.social > 75:
        tips.append("比较想聊天，可以自然接话")
    if not tips:
        return ""
    return "【当前状态】（像真人当下的感觉，别念出来）\n" + "；".join(tips)


class InnerStateStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key) or "global"
        return self.data_dir / f"{safe}.json"

    def load(self, key: str = "global", *, now: float | None = None) -> InnerState:
        now = time.time() if now is None else now
        path = self._path(key)
        if not path.is_file():
            st = InnerState(updated_at=now)
            return st
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return InnerState(updated_at=now)
        if not isinstance(raw, dict):
            return InnerState(updated_at=now)
        st = InnerState.from_dict(raw)
        return _decay_toward_neutral(st, now=now)

    def save(self, key: str, state: InnerState) -> None:
        state.clamp()
        path = self._path(key)
        with self._lock:
            path.write_text(
                json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    # -- 心情分层（2026-10-25）--
    def _prune(self, state: InnerState, *, now: float) -> None:
        """只留三天内的手札。过期就是过期，不强化也不消除。"""
        kept: list[dict[str, Any]] = []
        for note in state.notes:
            try:
                ts = float(note.get("t") or 0)
            except (TypeError, ValueError):
                continue
            if ts and now - ts <= _NOTE_LIFE_SEC:
                kept.append(note)
        state.notes = kept[-40:]

    def add_note(self, key: str, text: str, tag: str = "", *, now: float | None = None) -> InnerState:
        """记一条心绪手札：那些不配写进日记、但确实硌在心里的小事。"""
        moment = time.time() if now is None else now
        body = str(text or "").strip()
        if not body:
            return self.load(key, now=moment)
        st = self.load(key, now=moment)
        st.notes.append({
            "t": moment,
            "tag": str(tag or "").strip()[:8],
            "text": body[:_NOTE_MAX_CHARS],
        })
        self._prune(st, now=moment)
        self.save(key, st)
        return st

    def set_base(self, key: str, word: str, reason: str = "", *, now: float | None = None) -> InnerState:
        """定底色。一天一次，不跟着每轮变。"""
        moment = time.time() if now is None else now
        st = self.load(key, now=moment)
        st.base_word = str(word or "").strip()[:12]
        st.base_reason = str(reason or "").strip()[:30]
        st.base_since = moment
        # 换底色时顺带清掉白天的临时心情，否则新底色被旧动态一直盖着
        st.word = ""
        st.word_since = 0.0
        self.save(key, st)
        return st

    def set_mood(self, key: str, word: str, *, now: float | None = None) -> InnerState:
        """白天被某句话改掉心情。她自己觉得真的变了才改。"""
        moment = time.time() if now is None else now
        st = self.load(key, now=moment)
        st.word = str(word or "").strip()[:12]
        st.word_since = moment
        self.save(key, st)
        return st

    def settle(self, key: str, *, now: float | None = None) -> InnerState:
        """结算底色：翻这三天的东西，问自己这几天到底什么状态。

        这里给的是**确定性兜底**：拿最近一条手札的标签当底色的理由。
        刻意不做平均、不做加权——昨天开心今天难过，平均出来叫「一般」，
        那是统计结果，不是心情。真想要更细腻的，让模型在结算时自己写。
        """
        moment = time.time() if now is None else now
        st = self.load(key, now=moment)
        self._prune(st, now=moment)
        if not st.notes:
            return st
        latest = max(st.notes, key=lambda x: float(x.get("t") or 0))
        tag = str(latest.get("tag") or "").strip()
        text = str(latest.get("text") or "").strip()
        if not tag and not text:
            return st
        st.base_word = tag or text[:6]
        st.base_reason = text[:30]
        st.base_since = moment
        st.word = ""
        st.word_since = 0.0
        self.save(key, st)
        return st

    def apply_after_reply(
        self,
        key: str,
        *,
        reply_len: int,
        mentioned: bool,
        interrupt: bool,
        private_chat: bool = False,
        now: float | None = None,
    ) -> InnerState:
        """发言后更新：长回复扣精力；被 @/私聊抬社交；插嘴略耗社交。"""
        now = time.time() if now is None else now
        st = self.load(key, now=now)
        drain = min(12.0, 1.5 + reply_len / 80.0)
        st.energy -= drain
        if mentioned or private_chat:
            st.social = min(_MAX, st.social + 4.0)
            st.mood = min(_MAX, st.mood + 1.5)
        if interrupt:
            st.social -= 3.0
            st.energy -= 2.0
        st.updated_at = now
        st.clamp()
        self.save(key, st)
        return st


def scope_key(
    *,
    per_group: bool,
    group_id: int | str | None,
    private: bool,
) -> str:
    if private:
        return "private"
    if per_group and group_id is not None:
        return f"group_{group_id}"
    return "global"


_store: InnerStateStore | None = None
_store_dir: str | None = None


def get_inner_state_store(data_dir: str | Path) -> InnerStateStore:
    global _store, _store_dir
    path = str(data_dir)
    if _store is None or _store_dir != path:
        _store = InnerStateStore(path)
        _store_dir = path
    return _store
