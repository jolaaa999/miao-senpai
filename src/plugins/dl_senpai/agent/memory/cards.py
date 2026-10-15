"""Advanced JSON Cards：常驻上下文的结构化长期记忆。

参考《深入理解 AI Agent》第 3 章「用户记忆和知识库」的双层记忆架构：

    上层 = 少量关键事实结构化后**常驻上下文**，提供随时可见的「概览」；
    下层 = 上下文感知检索，按需从海量原始对话里取回「细节」。

为什么现有实现不够：`person_memory` 只存「印象标签」，`affection` 只存一个数值，
`ChatMemory` 只存原始轮次。它们都回答不了「这个人上次说的那件事后来怎么样了」
这类需要**事实 + 时间 + 来源**的问题。本模块补的就是这一层。

为什么是「只增不改」（Mem0 v3 的做法）：v2 在写入阶段做 ADD/UPDATE/DELETE 消歧，
一次误判就不可逆地丢历史；用户先说「我住北京」后说「搬到上海」，v2 会把北京那条
覆盖掉。v3 改成只追加、把冲突留给**检索时按时间排序**解决，LoCoMo 从 71.4 涨到
92.5。这里照 v3 做：新事实不删除旧事实，只标记取代关系。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from ...storage.json_store import JsonStore

__all__ = [
    "CardStore",
    "MemoryCard",
    "build_cards_brief",
    "get_card_store",
    "reset_card_store",
    "normalize_fact",
    "card_importance",
]

# 认知科学的三类记忆（第三章 3.1.5）。存哪一类由写入方决定：
#   semantic   —— 从多次交互抽象出的稳定特征（「用户是素食者」）
#   episodic   —— 带时间地点的具体事件（「上周三订了去东京的机票」）
#   procedural —— 可复用的行为流程（「先搜直飞 → 确认座位 → 用餐号」）
CARD_KINDS = ("semantic", "episodic", "procedural")

# 时间衰减半衰期：30 天。取值理由见 importance() 的注释。
_HALF_LIFE_DAYS = 30.0


def normalize_fact(text: str, *, max_len: int = 200) -> str:
    """归一化事实文本，用于去重比对。

    只做空白与会话内标点的收敛，**不改写内容**——第四章 4.2.3 明确禁止
    静默转换：把弯引号改成直引号这种「顺手规范化」会让后续匹配莫名失败。
    """
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    return cleaned[:max_len]


def _fingerprint(card: MemoryCard) -> str:
    """去重指纹：同一类、同一主体、同一事实视为同一条。"""
    raw = f"{card.kind}|{card.subject}|{normalize_fact(card.fact).lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class MemoryCard:
    """一条结构化长期记忆。

    Attributes:
        kind: semantic / episodic / procedural。
        subject: 主体（人物、群、话题），检索时的实体匹配维度。
        fact: 事实本身，一句话。
        evidence: 来源（哪一轮、谁说的），用于回答「这条从哪来」。
        qualifiers: 适用场景限定。冲突不删只限定，是第三章 3.3.3.2 的要求：
            两条互相矛盾的说法若各自在不同条件下成立，就把条件写进知识，
            而不是强行收敛成一条。
        created_at: 首次写入时间戳（秒）。
        last_seen: 最近一次被复述/命中的时间戳。
        hits: 被命中次数，重要性评分的「访问频率」因子。
        emotion: 情感强度 0~1，重要的「情感强度」因子。
        superseded_by: 被哪条取代（card_id）。为空表示当前有效。
    """

    kind: str = "semantic"
    subject: str = ""
    fact: str = ""
    evidence: str = ""
    qualifiers: str = ""
    created_at: float = 0.0
    last_seen: float = 0.0
    hits: int = 0
    emotion: float = 0.0
    superseded_by: str = ""
    card_id: str = ""

    def __post_init__(self) -> None:
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.last_seen:
            self.last_seen = self.created_at
        if self.kind not in CARD_KINDS:
            self.kind = "semantic"
        self.emotion = max(0.0, min(1.0, float(self.emotion or 0.0)))
        self.fact = normalize_fact(self.fact)
        if not self.card_id:
            self.card_id = _fingerprint(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "kind": self.kind,
            "subject": self.subject,
            "fact": self.fact,
            "evidence": self.evidence,
            "qualifiers": self.qualifiers,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "hits": self.hits,
            "emotion": self.emotion,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "MemoryCard | None":
        if not isinstance(raw, dict):
            return None
        fact = normalize_fact(str(raw.get("fact") or ""))
        if not fact:
            return None
        return cls(
            kind=str(raw.get("kind") or "semantic"),
            subject=str(raw.get("subject") or ""),
            fact=fact,
            evidence=str(raw.get("evidence") or ""),
            qualifiers=str(raw.get("qualifiers") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            last_seen=float(raw.get("last_seen") or 0.0),
            hits=int(raw.get("hits") or 0),
            emotion=float(raw.get("emotion") or 0.0),
            superseded_by=str(raw.get("superseded_by") or ""),
            card_id=str(raw.get("card_id") or ""),
        )


def card_importance(card: MemoryCard, *, now: float | None = None, duplicates: int = 0) -> float:
    """重要性评分（第三章 3.1.7 第一层）。

    四个因子按第三章给的四种思路取：
        访问频率 hits     —— 经常被检索的记忆更重要
        时间衰减 decay    —— 越久远越容易被遗忘
        情感强度 emotion  —— 带强情感标记的更易保留
        信息独特性 uniq   —— 重复信息重要性降低

    为什么半衰期取 30 天：记忆是陪聊用的，一个月前的闲聊偏好仍值得记得，
    但三个月前的一句牢骚不该一直占着常驻位置。
    权重 0.35/0.30/0.20/0.15 是工程取值：频率与时间主导，情感次之，
    独特性只做微调（它更多是「压缩候选」的判据，不是「保留」的判据）。
    """
    moment = now if now is not None else time.time()
    age_days = max(0.0, (moment - (card.created_at or moment)) / 86400.0)
    decay = 0.5 ** (age_days / _HALF_LIFE_DAYS)
    freq = min(1.0, max(0, card.hits) / 5.0)
    emo = max(0.0, min(1.0, card.emotion))
    uniq = 1.0 / (1.0 + max(0, duplicates))
    return round(0.35 * freq + 0.30 * decay + 0.20 * emo + 0.15 * uniq, 4)


class CardStore:
    """按作用域一文件的结构化事实卡存储。

    文件布局 `{data_dir}/{scope}.json`，scope 形如 `user_123` / `group_456`。
    和作用域键都做过文件名净化，避免 `..` 之类的路径穿越。
    """

    def __init__(self, data_dir: str | Path, *, max_cards: int = 60) -> None:
        self.data_dir = Path(data_dir)
        self.max_cards = max(8, int(max_cards))
        self._lock = Lock()

    def _safe(self, scope: str) -> str:
        """净化作用域键：只留字母数字下划线与短横。"""
        cleaned = re.sub(r"[^0-9A-Za-z_\-]", "_", str(scope or ""))[:64]
        return cleaned or "default"

    def _path(self, scope: str) -> Path:
        return self.data_dir / f"{self._safe(scope)}.json"

    def _store(self, scope: str) -> JsonStore:
        return JsonStore(self._path(scope), create_parent=True)

    def load(self, scope: str) -> list[MemoryCard]:
        raw = self._store(scope).load(default=None)
        if not isinstance(raw, dict):
            return []
        cards = [MemoryCard.from_dict(item) for item in raw.get("cards") or []]
        return [c for c in cards if c is not None]

    def save(self, scope: str, cards: list[MemoryCard]) -> None:
        self._store(scope).save({"cards": [c.to_dict() for c in cards]})

    def add(self, scope: str, card: MemoryCard, *, supersedes: str = "") -> MemoryCard:
        """追加一条事实。

        **不做 UPDATE/DELETE**：已存在同样事实时只更新命中计数与时间；
        传入 supersedes 时把被取代的那条标记上，但**保留在库里**，
        这样「以前住北京、现在住上海」两条都在，检索时按时间给出当前事实。
        """
        with self._lock:
            cards = self.load(scope)
            incoming = MemoryCard.from_dict(card.to_dict())
            assert incoming is not None
            for existing in cards:
                if existing.card_id == incoming.card_id:
                    existing.hits += 1
                    existing.last_seen = time.time()
                    # 补齐新带来的信息，但不是覆盖：空字段才填。
                    if not existing.evidence and incoming.evidence:
                        existing.evidence = incoming.evidence
                    if not existing.qualifiers and incoming.qualifiers:
                        existing.qualifiers = incoming.qualifiers
                    existing.emotion = max(existing.emotion, incoming.emotion)
                    self.save(scope, cards)
                    return existing
            for existing in cards:
                if supersedes and existing.card_id == supersedes:
                    existing.superseded_by = incoming.card_id
            cards.append(incoming)
            cards = self._enforce_capacity(cards)
            self.save(scope, cards)
            return incoming

    def touch(self, scope: str, card_id: str) -> None:
        """标记一次命中（供重要性评分的访问频率因子）。"""
        with self._lock:
            cards = self.load(scope)
            changed = False
            for card in cards:
                if card.card_id == card_id:
                    card.hits += 1
                    card.last_seen = time.time()
                    changed = True
                    break
            if changed:
                self.save(scope, cards)

    def _enforce_capacity(self, cards: list[MemoryCard]) -> list[MemoryCard]:
        """超容量时按重要性淘汰——**淘汰的是表达，不是证据**。

        第三章 3.3.3.2 的纪律：删掉的是可服务化的知识表达，下层只增不改的
        原始证据（ChatMemory 里的轮次）不动。
        """
        if len(cards) <= self.max_cards:
            return cards
        now = time.time()
        # 被取代的优先出局，其次才是低重要性
        ranked = sorted(
            cards,
            key=lambda c: (bool(c.superseded_by), card_importance(c, now=now)),
        )
        survivors = ranked[len(ranked) - self.max_cards :]
        keep = {c.card_id for c in survivors}
        return [c for c in cards if c.card_id in keep]

    def active(self, scope: str, *, include_superseded: bool = False) -> list[MemoryCard]:
        """取出当前有效的卡片；默认滤掉已被取代的。"""
        cards = self.load(scope)
        if include_superseded:
            return cards
        return [c for c in cards if not c.superseded_by]


def build_cards_brief(
    cards: list[MemoryCard],
    *,
    max_cards: int = 12,
    max_chars: int = 700,
    now: float | None = None,
    query: str = "",
) -> str:
    """把卡片渲染成常驻上下文的「概览」文本。

    为什么带 query 排序：常驻位有限，有当前话题时优先放相关卡片。
    为什么不写绝对时间戳：模型对「2026-10-11」这种绝对时间不敏感，
    但对「3 天前」很敏感，所以渲染相对时间。
    """
    if not cards:
        return ""
    moment = now if now is not None else time.time()

    def _relevance(card: MemoryCard) -> float:
        base = card_importance(card, now=moment)
        if query:
            # 主体或事实命中当前输入的词，给一个轻量加权（不做向量检索，
            # 那是下层 recall 的职责；概览位只需要「明显相关」的先上）。
            lowered = query.lower()
            if card.subject and card.subject.lower() in lowered:
                base += 0.5
            elif any(tok and tok in lowered for tok in re.split(r"[\s，,。！？!?]+", card.fact)[:3]):
                base += 0.2
        return base

    ordered = sorted(cards, key=_relevance, reverse=True)[:max_cards]
    lines: list[str] = []
    total = 0
    for card in ordered:
        age = max(0.0, (moment - (card.created_at or moment)) / 86400.0)
        if age < 1:
            when = "今天"
        elif age < 2:
            when = "昨天"
        elif age < 30:
            when = f"{int(age)} 天前"
        else:
            when = f"{int(age / 30)} 个月前"
        subject = f"{card.subject}：" if card.subject else ""
        line = f"- [{when}] {subject}{card.fact}"
        if card.qualifiers:
            line += f"（限：{card.qualifiers}）"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    header = "【长期记忆·概览】（结构化事实，跨会话累积，越靠前越相关）"
    return header + "\n" + "\n".join(lines)


_STORE: CardStore | None = None
_STORE_DIR: str = ""


def get_card_store(data_dir: str | Path, *, max_cards: int = 60) -> CardStore:
    """取全局卡片存储（按目录缓存，配置变了就重建）。"""
    global _STORE, _STORE_DIR
    key = f"{Path(data_dir).resolve()}|{max_cards}"
    if _STORE is None or _STORE_DIR != key:
        _STORE = CardStore(data_dir, max_cards=max_cards)
        _STORE_DIR = key
    return _STORE


def reset_card_store() -> None:
    """清掉缓存（测试与热重载用）。"""
    global _STORE, _STORE_DIR
    _STORE = None
    _STORE_DIR = ""