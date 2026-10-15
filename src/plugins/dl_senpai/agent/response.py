"""ResponsePipeline：AgentResult → 实际发送 的统一出口。

第三十六节要求：**不要重写** reply_send / style（它们积累了大量生产逻辑），
只增加一个统一入口。

    AgentResult
        ↓ ResponsePipeline
    polish（style.polish_reply）          ← 复用，不改语义
        ↓
    media（sticker / voice / image）      ← 复用现有构建函数
        ↓
    send（reply_send.send_text_reply）    ← 复用
        ↓
    ResponseOutcome

本模块的价值是**把"结果 → 发送"的编排固定下来**，让 AgentRuntime 不必
知道发送细节，也让未来替换发送通道只需要改这一处。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..observability.logging import log_event
from ..observability.metrics import metrics
from .context import AgentContext, AgentResult


@dataclass
class ResponseOutcome:
    """发送结果。"""

    sent: bool = False
    text: str = ""
    # 实际附带的媒体类型
    media: list[str] = field(default_factory=list)
    error: str = ""

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "reply_len": len(self.text or ""),
            "media": ",".join(self.media) if self.media else None,
            "error": self.error or None,
        }


@dataclass
class ResponsePlan:
    """发送计划（由 AgentResult 推导，便于 dry_run 与测试断言）。"""

    text: str = ""
    sticker: Any = None
    voice: Any = None
    image: Any = None

    @property
    def media(self) -> list[str]:
        out: list[str] = []
        if self.sticker is not None:
            out.append("sticker")
        if self.image is not None:
            out.append("image")
        if self.voice is not None:
            out.append("voice")
        return out


class ResponsePipeline:
    """把 AgentResult 落成实际回复。

    Phase 4 定位：**编排层**。真正的文本润色、媒体构建、分段发送
    仍然复用现有实现——这是"Preserve behavior"的直接体现。
    """

    def __init__(self, sender: Any = None, polisher: Any = None) -> None:
        self._sender = sender
        self._polisher = polisher

    # -- 计划 --

    def build_plan(self, result: AgentResult, context: AgentContext) -> ResponsePlan:
        """从 AgentResult 推导发送计划（**不产生副作用**）。

        这里只读取工具结果，不认识具体业务——sticker / voice / image
        的具体构建由调用方注入的构建器完成（保持 legacy 行为）。
        """
        plan = ResponsePlan(text=result.text or "")

        # 文本润色：复用 style.polish_reply（不改其语义）
        polisher = self._polisher
        if polisher is None:
            from ..style import polish_reply

            polisher = polish_reply
        try:
            plan.text = polisher(plan.text)
        except Exception:  # noqa: BLE001 — 润色失败时保留原文
            pass

        plan.sticker = context.extra.get("sticker_segment")
        plan.voice = context.extra.get("voice_segment")
        plan.image = context.extra.get("image_segment")
        return plan

    # -- 发送 --

    async def send(
        self,
        plan: ResponsePlan,
        context: AgentContext,
        *,
        reply_options: dict[str, Any] | None = None,
    ) -> ResponseOutcome:
        """执行发送。复用 `reply_send.send_text_reply` 的全部行为。"""
        bot = context.bot
        event = context.raw_event
        if bot is None or event is None:
            return ResponseOutcome(
                sent=False, text=plan.text, error="missing_bot_or_event"
            )

        sender = self._sender
        if sender is None:
            from ..reply_send import send_text_reply

            sender = send_text_reply

        options = dict(reply_options or {})
        try:
            await sender(
                bot,
                event,
                plan.text,
                sticker=plan.sticker,
                voice=plan.voice,
                image=plan.image,
                **options,
            )
        except Exception as exc:  # noqa: BLE001
            metrics.incr("reply.failed")
            log_event(
                "reply.failed",
                level="warning",
                error=f"{type(exc).__name__}: {exc}",
                session_id=context.session_id or None,
            )
            return ResponseOutcome(
                sent=False, text=plan.text, media=plan.media,
                error=f"{type(exc).__name__}: {exc}",
            )

        metrics.incr("reply.sent")
        outcome = ResponseOutcome(sent=True, text=plan.text, media=plan.media)
        log_event(
            "reply.sent",
            session_id=context.session_id or None,
            **outcome.as_log_fields(),
        )
        return outcome

    async def deliver(
        self,
        result: AgentResult,
        context: AgentContext,
        *,
        dry_run: bool = False,
        reply_options: dict[str, Any] | None = None,
    ) -> ResponseOutcome:
        """一站式：计划 + 发送。

        `dry_run=True` 时只返回计划，**不发送**（用于 shadow / 测试）。
        """
        plan = self.build_plan(result, context)
        if dry_run:
            return ResponseOutcome(
                sent=False, text=plan.text, media=plan.media, error="dry_run"
            )
        return await self.send(plan, context, reply_options=reply_options)


__all__ = ["ResponsePipeline", "ResponsePlan", "ResponseOutcome"]
