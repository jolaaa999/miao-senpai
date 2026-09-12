from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Literal

TriggerKind = Literal["mention", "interrupt", "none"]

DL_KEYWORDS = (
    "训练",
    "loss",
    "过拟合",
    "欠拟合",
    "梯度",
    "反传",
    "transformer",
    "attention",
    "cuda",
    "显存",
    "batch",
    "学习率",
    "epoch",
    "微调",
    "finetune",
    "扩散",
    "llm",
    "大模型",
    "卷积",
    "resnet",
    "pytorch",
    "tensor",
    "推理",
    "部署",
    "acm",
    "icpc",
    "codeforces",
    "cf",
    "leetcode",
    "洛谷",
    "算法竞赛",
    "竞赛题",
    "数据结构",
    "动态规划",
    "dp",
    "图论",
    "贪心",
    "二分",
    "并查集",
    "线段树",
    "树状数组",
    "最短路",
    "dfs",
    "bfs",
    "单调栈",
    "拓扑排序",
    "组合数学",
    "数论",
)


@dataclass
class TriggerDecision:
    kind: TriggerKind
    reason: str = ""


class InterruptCooldown:
    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def ready(self, session_id: str, cooldown_sec: int, now: float | None = None) -> bool:
        if session_id not in self._last:
            return True
        now = time.time() if now is None else now
        return (now - self._last[session_id]) >= cooldown_sec

    def mark(self, session_id: str, now: float | None = None) -> None:
        self._last[session_id] = time.time() if now is None else now


class AttentionWindow:
    """刚 @ 过学姐后，短时间内发表情包也算找她（QQ 难同时 @+表情）。"""

    def __init__(self) -> None:
        self._armed: dict[str, float] = {}

    @staticmethod
    def _key(session_id: str, user_id: int | str) -> str:
        return f"{session_id}:{user_id}"

    def arm(self, session_id: str, user_id: int | str, now: float | None = None) -> None:
        self._armed[self._key(session_id, user_id)] = (
            time.time() if now is None else now
        )

    def active(
        self,
        session_id: str,
        user_id: int | str,
        window_sec: int,
        now: float | None = None,
    ) -> bool:
        key = self._key(session_id, user_id)
        if key not in self._armed:
            return False
        now = time.time() if now is None else now
        return (now - self._armed[key]) <= max(0, int(window_sec))


def has_dl_keyword(text: str) -> bool:
    lower = text.lower()
    return any(k.lower() in lower for k in DL_KEYWORDS)


def should_ignore_text(text: str, min_len: int, *, has_images: bool = False) -> bool:
    stripped = text.strip()
    if not stripped:
        return not has_images
    if stripped.startswith(("/", "!", ".", "#", "。/")):
        return True
    # 去掉空白后过短
    compact = "".join(stripped.split())
    if len(compact) < min_len:
        return not has_images
    return False


def decide_trigger(
    *,
    is_mentioned: bool,
    text: str,
    session_id: str,
    interrupt_prob: float,
    cooldown_sec: int,
    min_msg_len: int,
    keyword_boost: float,
    cooldown: InterruptCooldown,
    has_images: bool = False,
    image_boost: float = 0.0,
    interrupt_prob_scale: float = 1.0,
    rng: random.Random | None = None,
    now: float | None = None,
) -> TriggerDecision:
    if is_mentioned:
        return TriggerDecision(kind="mention", reason="at_bot")

    if should_ignore_text(text, min_msg_len, has_images=has_images):
        return TriggerDecision(kind="none", reason="ignored")

    if not cooldown.ready(session_id, cooldown_sec, now=now):
        return TriggerDecision(kind="none", reason="cooldown")

    prob = interrupt_prob
    if has_dl_keyword(text):
        prob = min(1.0, prob + keyword_boost)
    if has_images:
        prob = min(1.0, prob + image_boost)
    scale = max(0.0, float(interrupt_prob_scale))
    prob = min(1.0, max(0.0, prob * scale))

    r = (rng or random).random()
    if r < prob:
        return TriggerDecision(kind="interrupt", reason=f"roll={r:.4f}<{prob:.4f}")

    return TriggerDecision(kind="none", reason=f"roll={r:.4f}>={prob:.4f}")
