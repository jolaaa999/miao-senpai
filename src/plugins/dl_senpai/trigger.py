from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Literal

from .cache import BoundedTTLMap

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
    """插嘴冷却记录。

    Phase 2 治理：`_last` 原本是裸 dict，key 是 session_id，
    每个群/私聊一条且**永不释放**。长跑数月、群成员流动时内存只增不减。

    改为**容量有界**的 `BoundedTTLMap`（保留最近 `_MAX_SESSIONS` 个
    session，超出按最久未更新淘汰）。

    为什么只限容量、不设 TTL：`ready()`/`mark()` 支持注入 `now`
    （单测用它验证"冷却 90 秒后解禁"）。若按真实墙钟做 TTL 过期，
    注入的合成时间戳会被误判为过期记录而清除，**冷却会静默失效**。
    容量上限只约束内存，不改变任何冷却判定结果。
    """

    _MAX_SESSIONS = 8192

    def __init__(self) -> None:
        self._last: BoundedTTLMap = BoundedTTLMap(
            ttl=0.0, max_size=self._MAX_SESSIONS
        )

    def ready(self, session_id: str, cooldown_sec: int, now: float | None = None) -> bool:
        last = self._last.timestamp(session_id)
        if last is None:
            return True
        now = time.time() if now is None else now
        return (now - last) >= cooldown_sec

    def mark(self, session_id: str, now: float | None = None) -> None:
        self._last.set(session_id, time.time() if now is None else now)


class AttentionWindow:
    """刚 @ 过学姐后，短时间内发表情包也算找她（QQ 难同时 @+表情）。

    Phase 2 治理：`_armed` 原本无界。key 是 `session:user`，
    随群成员数量增长。改为容量有界的 `BoundedTTLMap`。

    与 `InterruptCooldown` 同理：只限容量不设 TTL，避免注入合成时间戳的
    测试与调用方被误判为过期。
    """

    _MAX_ENTRIES = 16384

    def __init__(self) -> None:
        self._armed: BoundedTTLMap = BoundedTTLMap(
            ttl=0.0, max_size=self._MAX_ENTRIES
        )

    @staticmethod
    def _key(session_id: str, user_id: int | str) -> str:
        return f"{session_id}:{user_id}"

    def arm(self, session_id: str, user_id: int | str, now: float | None = None) -> None:
        self._armed.set(
            self._key(session_id, user_id),
            time.time() if now is None else now,
        )

    def active(
        self,
        session_id: str,
        user_id: int | str,
        window_sec: int,
        now: float | None = None,
    ) -> bool:
        armed_at = self._armed.get(self._key(session_id, user_id))
        if armed_at is None:
            return False
        now = time.time() if now is None else now
        return (now - armed_at) <= max(0, int(window_sec))


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
    is_owner: bool = False,
    rng: random.Random | None = None,
    now: float | None = None,
) -> TriggerDecision:
    if is_mentioned:
        return TriggerDecision(kind="mention", reason="at_bot")

    if should_ignore_text(text, min_msg_len, has_images=has_images):
        return TriggerDecision(kind="none", reason="ignored")

    # 主人（或指定的人）说话：跳过冷却和骰子，一定接。
    # 为什么排在长度过滤**之后**：那样「嗯」「草」「/reset」这类仍然不走 AI，
    # 主人也不需要机器人对每个语气词都回一句。这里只解决「被概率/冷却晾着」。
    if is_owner:
        return TriggerDecision(kind="mention", reason="owner")

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
