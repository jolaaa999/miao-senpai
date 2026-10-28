"""ResponsePipeline：AgentResult → 实际发送 的统一出口。

第三十六节要求：**不要重写** reply_send / style（它们积累了大量生产逻辑），
只增加一个统一入口。

    AgentResult
        ↓ ResponsePipeline
    polish（style.polish_reply）          ← 复用，不改语义
        ↓
    出站整形（tone.shape_for_send）        ← 口语化：空格断句/去破折号/清空行
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


def _first(items: list[Any]) -> Any:
    """取第一项，空列表返回 None。"""
    return items[0] if items else None


def _media_from_tools(result: Any, tool_name: str) -> list[Any]:
    """从工具结果里取出媒体段（`ToolResult.data["value"]`）。

    为什么必须从工具结果取：RUNTIME 模式下 executor 只负责**产出**媒体段
    （draw / sticker / voice 都是 `data={"value": segment}`），没有任何代码
    把它写回 `context.extra`。而 `build_plan` 原先只读 extra —— extra 是
    legacy orchestrator 的写入口，而 legacy 路径早已不可运行。结果就是：
    draw 生成的图片、表情包、语音条全部静默丢弃，日志里 success=True，
    用户却只收到一句纯文字。
    """
    found: list[Any] = []
    for item in getattr(result, "tool_results", None) or []:
        if getattr(item, "tool_name", "") != tool_name:
            continue
        if not getattr(item, "success", False):
            continue
        value = (getattr(item, "data", None) or {}).get("value")
        if value is None:
            continue
        # browse 一次可能产出多张截图（list）；draw / sticker / voice 是单个段
        if isinstance(value, (list, tuple)):
            found.extend(v for v in value if v is not None)
        else:
            found.append(value)
    return found


@dataclass
class ResponsePlan:
    """发送计划（由 AgentResult 推导，便于 dry_run 与测试断言）。"""

    text: str = ""
    sticker: Any = None
    voice: Any = None
    image: Any = None
    # 其余图片（browse 的多张截图）。send_text_reply 只接收一张 image，
    # 所以除第一张之外的按独立图片消息补发。
    extra_images: list[Any] = field(default_factory=list)

    @property
    def media(self) -> list[str]:
        out: list[str] = []
        if self.sticker is not None:
            out.append("sticker")
        if self.image is not None or self.extra_images:
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
        # 把说话人昵称一起传下去：style 的「伪造转录」判据要靠它认出台头。
        # 不传的话这一层永远退化成「按短名重复」的兜底，昵称带点（如「失语.」）
        # 时识别不到台头。注入的 polisher 可能只收一个参数，所以退化时重试一次。
        speaker = getattr(context, "actor", "") or ""
        try:
            try:
                plan.text = polisher(plan.text, speaker=speaker)
            except TypeError:
                plan.text = polisher(plan.text)
        except Exception:  # noqa: BLE001 — 润色失败时保留原文
            pass

        # 出站整形：agent 这条链原来只有 polish_reply（删污染），
        # 口语化那几道工序（空格断句、去破折号、清空行）全在 llm.chat 那条路上，
        # 而线上 100% 走 agent（2026-10-28 日志 19/19 条 agent.entry）。
        # 结果就是干跑看着像人、群里发出去的全是逗号句号破折号齐全的完整句。
        # 整形失败不挡发送：它只负责「更像人」，不负责「能不能发」。
        try:
            from ..tone import is_casual_prompt, shape_for_send

            user_text = getattr(context, "user_text", "") or ""
            casual = is_casual_prompt(user_text)
            plan.text, shaped = shape_for_send(
                plan.text,
                casual=casual,
                space_split=bool(
                    getattr(getattr(context, "config", None), "casual_space_split", True)
                ),
            )
            if any(shaped.values()):
                log_event(
                    "llm.shape_for_send",
                    session_id=context.session_id or None,
                    casual=casual,
                    chars=len(plan.text),
                    **shaped,
                )
        except Exception as exc:  # noqa: BLE001 — 整形失败也要把话发出去
            log_event(
                "llm.shape_for_send_failed",
                level="warning",
                error=f"{type(exc).__name__}: {exc}"[:120],
            )

        # extra 优先：显式写入者（legacy 兼容分支 / 测试）语义保持不变；
        # 取不到再回落到工具结果 —— 只有这样 RUNTIME 模式才真带得出媒体。
        plan.sticker = context.extra.get("sticker_segment") or _first(
            # emoji（表情本子）发的也是表情段：跟 sticker 一起取，
            # 谁先谁后无所谓——一轮里本来就只该发一张表情
            _media_from_tools(result, "sticker") + _media_from_tools(result, "emoji")
        )
        plan.voice = context.extra.get("voice_segment") or _first(
            _media_from_tools(result, "voice")
        )
        extra_image = context.extra.get("image_segment")
        if extra_image is not None:
            plan.image = extra_image
        else:
            # 生图优先于浏览截图：用户要的是那张画，截图只是旁证
            images = (
                _media_from_tools(result, "draw")
                + _media_from_tools(result, "browse")
                + _media_from_tools(result, "self_image")
            )
            plan.image = _first(images)
            plan.extra_images = images[1:]
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

        # 其余图片单独补发：send_text_reply 只接受一张 image，
        # 多张只能自己再发。补发失败只记日志，不影响已经发出去的正文。
        if plan.extra_images:
            await self._send_extra_images(bot, event, plan.extra_images)

        metrics.incr("reply.sent")
        outcome = ResponseOutcome(sent=True, text=plan.text, media=plan.media)
        log_event(
            "reply.sent",
            session_id=context.session_id or None,
            **outcome.as_log_fields(),
        )
        return outcome

    async def _send_extra_images(
        self, bot: Any, event: Any, images: list[Any]
    ) -> None:
        """补发额外图片（browse 的多张截图）。

        为什么复用 reply_send._send_image_with_retry：那里已经有发送熔断
        与两次重试，是生产验证过的图片通道；另写一套只会多出一条绕过
        熔断的路径。
        """
        from ..reply_send import _send_image_with_retry

        for image in images:
            try:
                ok = await _send_image_with_retry(bot, event, image)
            except Exception as exc:  # noqa: BLE001 — 补发失败不影响正文
                log_event(
                    "reply.extra_image_failed",
                    level="warning",
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if not ok:
                log_event(
                    "reply.extra_image_failed", level="warning", error="send_failed"
                )

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
