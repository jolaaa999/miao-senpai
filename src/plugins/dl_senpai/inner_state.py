"""轻量内心状态：mood / energy / social（摘自 MumuBot / elysia 情绪三维思路）。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

_NEUTRAL = 55.0
_MIN = 0.0
_MAX = 100.0
# 每小时向中性回撤的比例
_DECAY_PER_HOUR = 0.08


@dataclass
class InnerState:
    mood: float = _NEUTRAL
    energy: float = _NEUTRAL
    social: float = _NEUTRAL
    updated_at: float = 0.0

    def clamp(self) -> None:
        self.mood = max(_MIN, min(_MAX, float(self.mood)))
        self.energy = max(_MIN, min(_MAX, float(self.energy)))
        self.social = max(_MIN, min(_MAX, float(self.social)))

    def to_dict(self) -> dict[str, float]:
        return {
            "mood": round(self.mood, 2),
            "energy": round(self.energy, 2),
            "social": round(self.social, 2),
            "updated_at": float(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict) -> InnerState:
        st = cls(
            mood=float(data.get("mood", _NEUTRAL)),
            energy=float(data.get("energy", _NEUTRAL)),
            social=float(data.get("social", _NEUTRAL)),
            updated_at=float(data.get("updated_at") or 0.0),
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


def build_inner_state_brief(state: InnerState) -> str:
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
    return (
        "【当前状态】（像真人当下的感觉，别念出来）\n"
        + "；".join(tips)
        + f"\n（mood={state.mood:.0f} energy={state.energy:.0f} social={state.social:.0f}）"
    )


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
