"""Agent 触发策略：把 private / mention / interrupt 三个 matcher 收敛成一次判定。

Phase 3 任务二十二。三个旧 matcher 本质都是"是否进入 AI"，区别只是触发条件：

    private_matcher   (priority 8)  → 私聊，必进
    mention_matcher   (priority 10) → @ 了机器人，必进
    interrupt_matcher (priority 50) → 未 @，按概率插嘴

Phase 2 已建好的 `core.router` 用 `matched/consumed` 表达"是否短路"，
但"要不要进 AI"是一个**业务判定**，不该由 matcher 数量表达。

因此收敛为一个策略函数：

    AgentTriggerPolicy.decide(ctx) → TriggerDecision(kind=...)

**概率算法完全复用 `trigger.py`**（`decide_trigger` / `should_ignore_text` /
`InterruptCooldown` / `AttentionWindow`），不重写、不调整参数。

一个重要细节：旧的三条路径对「是否忽略短消息」的处理不同——
- mention / private 走 `force_mention=True`，**绕过** `should_ignore_text`
- interrupt 走 `force_mention=False`，**经过** 完整过滤

这一点必须保持，否则"@学姐 在吗"（3 字，短于 min_msg_len=8）会从"必回"
变成"被忽略"。策略里用 `forced` 分支显式表达。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..observability.logging import log_event


class AgentTrigger(str, Enum):
    """进入 Agent 的原因。"""

    NONE = "none"
    PRIVATE = "private"
    MENTION = "mention"
    INTERRUPT = "interrupt"
    # 消息里出现召唤词（默认「喵喵」「喵学姐」）。语义等同 @，但配置可改，
    # 且不依赖 QQ 的 at 段——所以群里、私聊、临时会话都能用。
    SUMMON = "summon"


@dataclass
class AgentDecision:
    """一次 Agent 触发判定结果。

    Attributes:
        kind: 触发类型。
        reason: 判定依据（用于日志与 shadow 比对）。
        forced: 是否"强制回复"语义（@/私聊）。
                与 `kind` 分开是因为下游 `_handle_chat(force_mention=...)`
                用的就是这个布尔量。
        roll: 概率判定时的随机值（便于复现与调试）。
        probability: 本次使用的实际概率。
    """

    kind: AgentTrigger = AgentTrigger.NONE
    reason: str = ""
    forced: bool = False
    roll: float | None = None
    probability: float | None = None

    @property
    def should_run(self) -> bool:
        return self.kind is not AgentTrigger.NONE

    def as_log_fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {"trigger": self.kind.value, "reason": self.reason}
        if self.roll is not None:
            out["roll"] = round(self.roll, 4)
        if self.probability is not None:
            out["probability"] = round(self.probability, 4)
        return out


def has_summon_keyword(text: str, keywords: list[str]) -> bool:
    """文本是否**包含**任一召唤词（大小写不敏感）。

    用「包含」而不是「整条等于」：用户会写「喵喵在吗」「喵学姐这道题怎么做」，
    要求整条匹配的话这些全都召唤不出来。
    """
    lower = (text or "").lower()
    return any(k.lower() in lower for k in keywords if k)


#: 召唤词被剥干净后用来占位的文本。
#:
#: 纯呼叫（「喵喵喵」，正好是打卡任务 meow_three 要求的动作）剥完什么都不剩。
#: 既不能发空消息让模型面对一条空输入，也不能把原词塞回去——那正是要修的 bug。
SUMMON_ONLY_PLACEHOLDER = "（只是叫了我一声，没别的内容）"

#: 剥掉召唤词后需要一并清掉的首尾残留标点。
#: 「喵喵，在吗」删掉「喵喵」会剩下「，在吗」，标点顶在句首很难看。
_EDGE_PUNCT = " \t\r\n，,。.、；;：:！!？?~～…·-—"


def _keyword_pattern(kw: str) -> str:
    """把召唤词编成「一次或多次连续呼叫」的正则。

    为什么不能简单地删掉每个「喵喵」：打卡任务要求连发「喵喵喵」，删掉一个
    「喵喵」还剩一个孤零零的「喵」，正文里就多出一个无意义的字。
    这里用 (?:词)+(?:词的前缀)? 匹配整段连续呼叫，前缀那段专门收拾
    「喵喵喵」这种「整数次呼叫 + 半个词」的尾巴。

    例：kw=「喵喵」→ (?:喵喵)+(?:喵)?，可吞掉「喵喵」「喵喵喵」「喵喵喵喵喵」。
    """
    esc = re.escape(kw)
    # 前缀按长度降序，保证「喵学」先于「喵」被尝试，不会只吃掉半个
    prefixes = sorted({kw[:i] for i in range(1, len(kw))}, key=len, reverse=True)
    tail = ""
    if prefixes:
        tail = "(?:" + "|".join(re.escape(p) for p in prefixes) + ")?"
    return "(?:" + esc + ")+" + tail


def strip_summon_keywords(text: str, keywords: list[str]) -> str:
    """把召唤词从正文里删干净；没命中任何召唤词时**原样返回**。

    为什么必须删：召唤词是「叫她的方式」，不是「要说的话」。留着会让模型把
    「喵喵」当成对话内容的一部分去回应。@ 反倒没这个问题——OneBot 的 at 是
    独立段，取纯文本时整个被丢弃，所以只有召唤词需要特殊处理。

    删**所有**出现位置而不是只删开头：用户会把召唤词放句尾（「锐评下我喵喵」）
    或夹在中间。代价是真的在讨论该词本身时（「喵喵这词是谁起的」）也会被删，
    这是有意的取舍——前一种才是常态。

    没命中时原样返回（而不是返回空串）：这样调用方可以直接无脑调用，
    不必先自己判断一遍，也不会把「只有空白」的正文误当成纯呼叫。

    只删词、不删中途的标点：删完再把重复标点合并即可，
    「你好，喵喵，我是小明」→「你好，我是小明」，比硬生生拼成
    「你好我是小明」更自然。
    """
    cleaned = text or ""
    valid = [k for k in keywords if k]
    # 先看有没有命中：没命中就一点都不要动，后面的空白/标点合并也一并跳过，
    # 否则普通消息会被莫名其妙地改写。
    if not cleaned or not any(k.lower() in cleaned.lower() for k in valid):
        return text or ""
    # 长的先删：配置里同时有「喵喵」和「喵学姐」时，先删短的会在「喵学姐」
    # 里留下一个孤零零的「学姐」。按长度降序就不会有半截残留。
    for kw in sorted(valid, key=len, reverse=True):
        if kw.lower() in cleaned.lower():
            cleaned = re.sub(
                _keyword_pattern(kw), "", cleaned, flags=re.IGNORECASE
            )
    # 删掉词之后常出现连续的空白或重复标点（「你好，，我是小明」），合并掉
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"([，,、。；;：:！!？?])\1+", r"\1", cleaned)
    return cleaned.strip(_EDGE_PUNCT)


class AgentTriggerPolicy:
    """决定"这条消息要不要进 AI"。

    构造时注入冷却与关注窗口对象，使策略**无全局状态**、可独立测试。
    """

    def __init__(self, *, cooldown: Any = None, attention: Any = None) -> None:
        # 复用 trigger.py 的成熟实现（不重写概率算法）
        from ..trigger import AttentionWindow, InterruptCooldown

        self._cooldown = cooldown if cooldown is not None else InterruptCooldown()
        self._attention = attention if attention is not None else AttentionWindow()

    @property
    def cooldown(self) -> Any:
        return self._cooldown

    @property
    def attention(self) -> Any:
        return self._attention

    def decide(
        self,
        ctx: Any,
        *,
        config: Any = None,
        text: str = "",
        has_images: bool = False,
        interrupt_scale: float = 1.0,
        rng: random.Random | None = None,
        now: float | None = None,
    ) -> AgentDecision:
        """判定是否进入 Agent。

        Args:
            ctx: `MessageContext`。
            config: 配置；不传则取当前配置。
            text: 用于关键词加成/长度过滤的文本（默认取 ctx.plain_text）。
            has_images: 是否含图片（影响插嘴概率）。
            interrupt_scale: 内心状态对插嘴概率的缩放（由 handler 计算）。
            rng: 随机源（测试注入）。
            now: 时间源（测试注入）。
        """
        from ..trigger import decide_trigger, should_ignore_text

        if config is None:
            from ..config_runtime.runtime import get_config

            config = get_config()

        text = text or getattr(ctx, "plain_text", "") or ""
        session_id = getattr(ctx, "session_id", "") or ""
        is_private = bool(getattr(ctx, "is_private", False))
        is_tome = bool(getattr(ctx, "is_to_me", False))

        # 0) 召唤词：命中即强制回复，等价于 @。
        #
        # 位置很关键，必须排在下面两处**之前**：
        #   - 排在 should_ignore_text 之前：「喵喵」只有 2 字，短于 min_msg_len，
        #     否则会被当噪音丢掉（@ 的既有处理同样刻意绕过长度过滤）；
        #   - 排在 is_private 之前：这样私聊即使被 enable_private 关掉，召唤词
        #     仍然有效，且日志能区分是「私聊必回」还是「召唤词」触发的。
        if has_summon_keyword(text, config.summon_keyword_list()):
            return AgentDecision(
                AgentTrigger.SUMMON,
                reason="summon_keyword",
                forced=True,
            )

        # 1) 私聊：每条都当直接找学姐（对应 handle_private force_mention=True）
        if is_private:
            if not getattr(config, "enable_private", True):
                return AgentDecision(AgentTrigger.NONE, reason="private_disabled")
            return AgentDecision(
                AgentTrigger.PRIVATE, reason="private", forced=True
            )

        # 2) 群聊未 @：走概率插嘴，必须先过完整过滤
        if not is_tome:
            # 插嘴总开关关闭时直接判「不参与」，连骰子都不掷。
            # 放在 should_ignore_text 之前：关掉插嘴后不应再消耗冷却，
            # 日志里也能一眼区分是「关掉了」还是「掷骰子没中」。
            if not getattr(config, "interrupt_enable", True):
                return AgentDecision(AgentTrigger.NONE, reason="interrupt_disabled")
            if should_ignore_text(
                text, config.min_msg_len, has_images=has_images
            ):
                return AgentDecision(AgentTrigger.NONE, reason="ignored")
            decision = decide_trigger(
                is_mentioned=False,
                text=text,
                session_id=session_id,
                interrupt_prob=config.interrupt_prob,
                cooldown_sec=config.interrupt_cooldown,
                min_msg_len=config.min_msg_len,
                keyword_boost=config.keyword_boost,
                cooldown=self._cooldown,
                has_images=has_images,
                image_boost=config.image_interrupt_boost,
                interrupt_prob_scale=interrupt_scale,
                # 主人说话跳过冷却和骰子。listening 不给 forced：
                # 他没 @ 她，只是「叫她一定听得见」，不是「必须回答」。
                is_owner=config.is_owner(getattr(ctx, "user_id", "")),
                rng=rng,
                now=now,
            )
            # 非 @ 的情况下 decide_trigger 只会因 owner 返回 mention，
            # 落到这里说明是主人：按 mention 处理（走完整工具、加好感）。
            if decision.kind == "mention":
                return AgentDecision(
                    AgentTrigger.MENTION, reason=decision.reason, forced=False
                )
            if decision.kind == "interrupt":
                return AgentDecision(
                    AgentTrigger.INTERRUPT,
                    reason=decision.reason,
                    forced=False,
                )
            return AgentDecision(AgentTrigger.NONE, reason=decision.reason)

        # 3) 群聊 @ 了机器人：必回（对应 handle_mention force_mention=True）
        #    注意这里**刻意不过 should_ignore_text**：@ 就是明确指令，
        #    短消息（"@学姐 在吗"）也必须回，与旧 matcher 行为一致。
        return AgentDecision(AgentTrigger.MENTION, reason="at_bot", forced=True)

    def decide_via_legacy(
        self,
        *,
        is_mentioned: bool,
        text: str,
        session_id: str,
        interrupt_prob: float,
        cooldown_sec: int,
        min_msg_len: int,
        keyword_boost: float,
        has_images: bool = False,
        image_boost: float = 0.0,
        interrupt_prob_scale: float = 1.0,
        is_owner: bool = False,
        rng: random.Random | None = None,
        now: float | None = None,
    ) -> Any:
        """直接调用旧 `decide_trigger`，供 shadow 比对使用。

        保留这个薄封装是为了让 shadow 验证能拿到**未经包装**的旧决策，
        从而真正比较"新旧是否一致"，而不是比较新实现对它自己的调用。
        """
        from ..trigger import decide_trigger

        return decide_trigger(
            is_mentioned=is_mentioned,
            text=text,
            session_id=session_id,
            interrupt_prob=interrupt_prob,
            cooldown_sec=cooldown_sec,
            min_msg_len=min_msg_len,
            keyword_boost=keyword_boost,
            cooldown=self._cooldown,
            has_images=has_images,
            image_boost=image_boost,
            interrupt_prob_scale=interrupt_prob_scale,
            # 必须一起透传：不给的话 shadow 比对里「主人」永远按普通人算，
            # 新旧结果会一直对不上，反而把真问题淹掉。
            is_owner=is_owner,
            rng=rng,
            now=now,
        )

    def log_decision(self, decision: AgentDecision, ctx: Any) -> None:
        log_event(
            "agent.trigger",
            session_id=getattr(ctx, "session_id", "") or None,
            **decision.as_log_fields(),
        )


__all__ = [
    "AgentTrigger",
    "AgentDecision",
    "AgentTriggerPolicy",
    "has_summon_keyword",
]
