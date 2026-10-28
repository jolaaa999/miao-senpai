"""在场状态机：等对方说完、说完先听一会儿、什么时候潜水。

搬自 qq-bridge「二代仿真」里两件**靠机制而不是靠提示词**撑住的事：

1. **等下文**（对应 qq_wait_for_messages(purpose="reply", quietMs=...)）：
   对方话没说完（「…但是」「我跟你讲」「然后」）就先别抢答，等一段安静
   窗口再回；他补完了就当成一整段一起回。
2. **潜水与唤醒**（对应 qq_set_wake_config / qq_mark_read）：说完话先听
   一会儿，没人再说就潜水；潜水期间普通消息不叫醒她，只有 @ / 召唤词 /
   主人能把她叫回来。

为什么必须做成**状态**而不是逐条独立掷骰子：

原来的判定（trigger.decide_trigger）对每条消息独立算概率，等于假设「她每
一刻都在场、每次都重新决定要不要说话」。真人不是这样——他有一份连续的在场
状态：刚说完话会先听，对方没说完会等，散场了才走，被点名立刻回来。缺了这份
状态，骰子掷得再准也只是「随机的客服」。

设计约束（都是为了可测、可长跑）：

- 纯内存、不落盘：在场状态本来就是瞬时语义，重启归零可以接受；
- 时间必须可注入（now=），否则所有规则只能用 sleep 测；
- 状态表容量有界（复用 cache.BoundedTTLMap），不会随群成员数增长。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

from .cache import BoundedTTLMap
from .observability.logging import log_event

#: 话没说完的线索词（句尾）。取自 qq-bridge 里 qq_wait_for_messages 的触发清单：
#: 这些词出现在句尾，说明后面还有下文，谁抢答谁被第二句打脸。
UNFINISHED_TAILS: tuple[str, ...] = (
    "你知道",
    "你知道吧",
    "等下",
    "等一下",
    "等等",
    "等会",
    "等我",
    "我跟你讲",
    "我跟你说",
    "告诉你",
    "但是",
    "可是",
    "所以说",
    "然后",
    "那个",
    "这个",
    "就是",
    "对了",
    "还有",
    "其实",
    "而且",
    "不过",
    "所以",
    "或者",
    "因为",
    "如果",
    "要是",
    "就是说",
)

#: 虽然以虚词/逗号结尾，但本身就是一句完整话。不加这份白名单，
#: 「好的，」这种客套就会被当成没说完，白白等 45 秒。
COMPLETE_SHORT: frozenset[str] = frozenset(
    {
        "好的",
        "好",
        "行",
        "行吧",
        "好的吧",
        "嗯",
        "嗯嗯",
        "哦",
        "草",
        "乐",
        "典",
        "懂了",
        "知道了",
        "明白了",
        "算了",
        "没事",
        "没事了",
        "可以了",
        "就这样",
        "没了",
        "不是",
        "确实",
        "对的",
        "不知道",
        "为什么",
        "啥",
        "你说",
        "然后呢",
        "那你呢",
        "你呢",
    }
)

#: 句尾是这些标点时一律当作「没打完」：真人打一半手滑发出去就是这种形状。
_NON_TERMINAL_TAIL = "，,、；;：:—–-~～"

#: 句尾这些标点表示说完了，不再判线索词。
_TERMINAL_TAIL = "。.！!？?…"

_MAX_SESSIONS = 4096


def _now(now: float | None) -> float:
    """统一时间源：传了就用传的，没传才读墙钟。"""
    return time.time() if now is None else float(now)


def looks_unfinished(text: str) -> bool:
    """这句话是不是**明显没说完**（真人会等对方补完再回）。

    判据按优先级：
    1. 白名单里那些「看起来很虚但其实完整」的短句 → 说完了；
    2. 句尾是逗号/顿号/分号/冒号/破折号 → 没打完；
    3. 句尾是线索词（「…但是」「我跟你讲」「然后」）→ 还有下文。

    刻意不看句号/问号/感叹号：那是明确的收尾信号，哪怕前一句是「但是」
    也一样——「但是？」是真在问，不是在铺垫。
    """
    raw = (text or "").strip()
    if not raw:
        return False
    body = raw.rstrip()
    # 先剥掉「不是线索」的收尾标点，看真正的最后一个字
    core = body.rstrip(_NON_TERMINAL_TAIL + _TERMINAL_TAIL + " \t")
    if core and core in COMPLETE_SHORT:
        return False
    if body and body[-1] in _NON_TERMINAL_TAIL:
        return True
    spoken = body.rstrip(_TERMINAL_TAIL + " \t")
    if not spoken:
        return False
    return spoken.endswith(UNFINISHED_TAILS)


@dataclass
class PresenceState:
    """一个会话的在场状态。

    字段全部是时间戳而不是布尔量：布尔量只能表达「在/不在」，而活人味恰恰
    藏在「安静了多久」「说完多久了」这些连续量里。
    """

    #: 最近一次有人说话（普通消息）。
    last_incoming_at: float = 0.0
    #: 她最近一次真的发出回复。
    last_reply_at: float = 0.0
    #: 潜水到什么时候（0 = 没在潜水）。
    dive_until: float = 0.0
    #: 本话题里她主动开口的次数（没被叫到就说话）。
    topic_pokes: int = 0
    #: 上一次主动开口的时间，用来判断「这是不是同一个话题」。
    last_poke_at: float = 0.0
    #: 她在这个会话里**开过口吗**。
    #:
    #: 为什么不直接用 last_reply_at > 0 判断：时间戳 0.0 是合法输入（单测注入
    #: 合成时间就常用 0 当起点），那样「说过话」会永远判成 False，潜水整条规则
    #: 静默失效。这类 bug 只在测试或时间回拨时出现，最难查，所以单列一个布尔量。
    has_spoken: bool = False


def _cfg() -> Any:
    """取当前配置。放在函数里 import，避免与 config 模块形成循环依赖。"""
    from .config_runtime.runtime import get_config

    return get_config()


class PresenceGate:
    """在场状态机的唯一入口。

    刻意做成「注入时间 + 注入随机源」的对象而不是一堆模块级函数：
    所有规则都要能在单测里用合成时间戳钉住，不能依赖真实 sleep。
    """

    def __init__(self, *, cfg_getter: Any = None, rng: Any = None) -> None:
        self._states: BoundedTTLMap = BoundedTTLMap(
            ttl=0.0, max_size=_MAX_SESSIONS
        )
        self._cfg_getter = cfg_getter or _cfg
        self._rng = rng or random

    # -- 状态读写 --

    def state(self, session_id: str) -> PresenceState:
        """取出该会话的状态，没有就新建一条。"""
        st = self._states.get(session_id)
        if isinstance(st, PresenceState):
            return st
        st = PresenceState()
        self._states.set(session_id, st)
        return st

    def note_incoming(
        self, session_id: str, *, addressed: bool = False, now: float | None = None
    ) -> PresenceState:
        """记一条进来的消息。

        addressed=True 表示这条是在**叫她**（@/召唤词/主人）：那是唤醒，
        潜水立刻结束、话题计数清零。普通消息只推进「最近有人说话」的时间，
        **不会**把她从潜水里捞出来——这正是潜水与「每条都看一眼」的区别。
        """
        ts = _now(now)
        st = self.state(session_id)
        st.last_incoming_at = ts
        if addressed:
            if st.dive_until > ts:
                log_event(
                    "presence.wake",
                    session_id=session_id or None,
                    by="addressed",
                )
            st.dive_until = 0.0
            st.topic_pokes = 0
            st.last_poke_at = 0.0
        return st

    def note_replied(self, session_id: str, *, now: float | None = None) -> None:
        """记一次「她说过话了」。

        为什么要单独记账：潜水是从「说完话之后安静了多久」算起的，
        用「最近有人说话」算会把潜水时机推晚一整轮。
        """
        ts = _now(now)
        st = self.state(session_id)
        st.last_reply_at = ts
        st.has_spoken = True
        st.dive_until = 0.0

    def quiet_ms(self, session_id: str, *, now: float | None = None) -> float:
        """距离「最后一次有人说话或她开口」过去了多久（毫秒）。"""
        st = self.state(session_id)
        last = max(st.last_incoming_at, st.last_reply_at)
        if last <= 0:
            return float("inf")
        return max(0.0, (_now(now) - last) * 1000.0)

    # -- 潜水 --

    def is_diving(self, session_id: str, *, now: float | None = None) -> bool:
        """她现在是不是在潜水（普通消息叫不醒）。

        没用后台定时器，而是**惰性进入**：只有真的来了判定请求，才一次算清
        「安静够久了没」。少一个后台任务就少一处会和真实时间对不上的地方。
        """
        cfg = self._cfg_getter()
        if not bool(getattr(cfg, "presence_enable", True)):
            return False
        if not bool(getattr(cfg, "presence_dive_enable", True)):
            return False
        ts = _now(now)
        st = self.state(session_id)
        if st.dive_until > ts:
            return True
        # 从没开过口的会话不需要「该不该走」——她本来就不在场。
        if not st.has_spoken:
            return False
        observe_ms = max(0, int(getattr(cfg, "presence_observe_ms", 90_000)))
        quiet = ts - max(st.last_reply_at, st.last_incoming_at)
        if quiet * 1000.0 < observe_ms:
            return False
        lo = max(0, int(getattr(cfg, "presence_dive_min_ms", 180_000)))
        hi = max(lo, int(getattr(cfg, "presence_dive_max_ms", 600_000)))
        if hi <= 0:
            return False
        duration = self._rng.randint(lo, hi)
        st.dive_until = ts + duration / 1000.0
        log_event(
            "presence.dive",
            session_id=session_id or None,
            duration_ms=duration,
            quiet_ms=int(quiet * 1000),
        )
        return True

    def dive_remaining_ms(
        self, session_id: str, *, now: float | None = None
    ) -> int:
        """潜水还剩多久（没在潜水返回 0）。"""
        st = self.state(session_id)
        left = st.dive_until - _now(now)
        return max(0, int(left * 1000.0))

    # -- 话题追问上限 --

    def topic_pokes_exhausted(
        self, session_id: str, *, now: float | None = None
    ) -> bool:
        """这个话题里她主动开口的次数是不是已经用完了。

        对应 qq-bridge 的「同一件事主动追问/发起最多 2 次；没人接就放下」。
        超过话题窗口（默认 10 分钟）就算换了话题，重新给两次机会。
        """
        cfg = self._cfg_getter()
        limit = int(getattr(cfg, "presence_topic_poke_max", 2))
        if limit <= 0:
            return False
        ts = _now(now)
        st = self.state(session_id)
        window_ms = max(0, int(getattr(cfg, "presence_topic_window_ms", 600_000)))
        if st.last_poke_at > 0 and (ts - st.last_poke_at) * 1000.0 > window_ms:
            # 话题窗口过去了，算新话题：顺手清零，免得下次还挂在这个计数上
            st.topic_pokes = 0
            st.last_poke_at = 0.0
            return False
        return st.topic_pokes >= limit

    def mark_topic_poke(self, session_id: str, *, now: float | None = None) -> None:
        """记一次「她主动开口了」（只统计没被叫到的那些）。"""
        ts = _now(now)
        st = self.state(session_id)
        window_ms = max(
            0,
            int(getattr(self._cfg_getter(), "presence_topic_window_ms", 600_000)),
        )
        if st.last_poke_at > 0 and (ts - st.last_poke_at) * 1000.0 > window_ms:
            st.topic_pokes = 1
        else:
            st.topic_pokes += 1
        st.last_poke_at = ts

    # -- 等下文 --

    def wait_window_ms(
        self, text: str, base_ms: int, *, now: float | None = None
    ) -> int:
        """这一条消息该等多久再回。

        对方把话说完（句号/问号收尾）就用基础窗口（合并窗口，2~3 秒）；
        明显没说完（「我跟你讲」「…但是」）就拉长到 pending_wait_ms，
        让第二句有时间跟上来——真人这时候本来就不会抢答。
        """
        base = max(0, int(base_ms))
        cfg = self._cfg_getter()
        if not bool(getattr(cfg, "presence_enable", True)):
            return base
        if not bool(getattr(cfg, "pending_wait_enable", True)):
            return base
        if not looks_unfinished(text):
            return base
        want = max(0, int(getattr(cfg, "pending_wait_ms", 45_000)))
        # 上限只兜住「配置写太大」，不能反过来把已经更长的基础窗口砍短，
        # 所以先把 base 算进下限里。
        cap = max(base, int(getattr(cfg, "pending_wait_max_ms", 120_000)))
        return max(base, min(want, cap))

    # -- 沉睡前观察 --

    def last_activity_age_ms(self, *, now: float | None = None) -> float | None:
        """所有会话里「最近一次有人说话」距今多久。一个都没有时返回 None。"""
        ts = _now(now)
        newest = 0.0
        for key in self._states.keys():
            st = self._states.get(key)
            if isinstance(st, PresenceState) and st.last_incoming_at > newest:
                newest = st.last_incoming_at
        if newest <= 0:
            return None
        return max(0.0, (ts - newest) * 1000.0)

    def pre_sleep_remaining_ms(self, *, now: float | None = None) -> int:
        """还要安静多久才算「可以离开 / 可以主动开口」。

        对应 qq-bridge 的沉睡前观察窗口：想收尾、想主动冒泡之前先确认群里
        真的安静下来了，否则就是「别人正聊着，你插一句然后跑了」。
        返回 0 = 现在可以开口。
        """
        cfg = self._cfg_getter()
        if not bool(getattr(cfg, "presence_enable", True)):
            return 0
        if not bool(getattr(cfg, "presence_pre_sleep_enable", True)):
            return 0
        want = max(0, int(getattr(cfg, "presence_pre_sleep_ms", 300_000)))
        age = self.last_activity_age_ms(now=now)
        if age is None:
            return 0
        return max(0, int(want - age))

    def reset(self) -> None:
        """测试用：清空全部状态。"""
        self._states.clear()


_PRESENCE_HINT = """【在场与节奏】

- 对方话没说完（「…但是」「我跟你讲」「然后」「等一下」）就先别抢答，等他补完；等很久还是没补，就催一句「啥」「你说啊」。
- 刚说完话先听一会儿，别马上再补一条；除非对方接了你，或者你真有新东西要说。
- 同一个话题你主动开口/追问最多两次；没人接就放下，别硬拉回来。
- 潜水不是失败：没人叫你、你也接不上，就看着别接。但被 @ 或被问就必须回。
- 别当群管家：不总结话题、不劝架、不挨个点评、不硬把话题拉回来。
"""


def build_presence_hint(config: Any = None) -> str:
    """返回「在场与节奏」这段提示词；关掉时返回空串。

    为什么机制已经落地了还要写进提示词：机制决定**她什么时候被叫醒**，
    提示词决定**她被叫醒之后怎么说话**。两边不一致时，模型会写出和节奏
    相反的回复（比如刚潜水上岸就来一句「我一直在的」）。
    """
    cfg = config if config is not None else _cfg()
    if not bool(getattr(cfg, "presence_hint_enable", True)):
        return ""
    return _PRESENCE_HINT


_gate: PresenceGate | None = None


def get_presence_gate() -> PresenceGate:
    """进程级单例。"""
    global _gate
    if _gate is None:
        _gate = PresenceGate()
    return _gate


def reset_presence_gate() -> None:
    """测试用：丢掉单例（含全部在场状态）。"""
    global _gate
    _gate = None


__all__ = [
    "COMPLETE_SHORT",
    "UNFINISHED_TAILS",
    "PresenceGate",
    "PresenceState",
    "build_presence_hint",
    "get_presence_gate",
    "looks_unfinished",
    "reset_presence_gate",
]
