"""生图作业：把出图甩到后台，主对话立刻能继续说。

为什么要异步：一张图 30~120 秒（自检不合格还要重画，翻倍）。同步出图时，
她「先说的那句话」也得等图出来才发得出去，群里看着就是卡住不动 —— 而这句话
本来可以立刻发。

作业完成后**直接发图，不配文字**：图本身就是回复，再说「画好了」是机器人腔。
失败/超时则发一句降级文案，总比让人干等强。
"""

from __future__ import annotations

from typing import Any

from nonebot import logger

from .image_gen import build_image_segment, draw_failure_note
from .observability.logging import log_event
from .subagent import get_subagent_runner


def _cfg_of(context: Any) -> Any:
    return getattr(context, "config", None)


def _extra_of(context: Any) -> dict[str, Any]:
    extra = getattr(context, "extra", None)
    return extra if isinstance(extra, dict) else {}


def submit_draw_job(context: Any, prompt: str) -> str:
    """派一个后台生图作业，立刻返回 job_id。

    调用方（execute_draw）拿到 id 就返回，主 agent 继续把话说完。
    """
    cfg = _cfg_of(context)
    runner = get_subagent_runner(
        max_concurrent=int(getattr(cfg, "draw_job_max", 2) or 2)
    )
    timeout = float(getattr(cfg, "draw_job_timeout", 300.0) or 300.0)

    async def _factory() -> Any:
        extra = _extra_of(context)
        return await build_image_segment(
            resolved_prompt=prompt,
            user_request=str(getattr(context, "plain_text", "") or ""),
            reply_text=str(extra.get("reply_text") or ""),
            config=cfg,
            interrupt=bool(getattr(context, "interrupt", False)),
            archive_session_id=str(getattr(context, "session_id", "") or ""),
            archive_source_user=str(getattr(context, "actor", "") or ""),
            archive_source_user_id=str(getattr(context, "user_id", "") or ""),
            archive_scene=extra.get("draw_scene"),
            reference_images=extra.get("image_urls") or [],
            # 让对方等图的前提是画得对：没参考图时模型会凭记忆编设定
            auto_reference=True,
        )

    job_id = runner.submit(
        "draw", _factory, timeout=timeout, on_done=lambda r: deliver_draw_result(context, r)
    )
    log_event("draw.job_submitted", job_id=job_id, timeout=timeout,
              prompt_len=len(prompt or ""), pending=runner.pending())
    return job_id


async def deliver_draw_result(context: Any, result: Any) -> None:
    """作业收尾：成功就发图，失败就发一句降级话术。"""
    bot = getattr(context, "bot", None)
    event = getattr(context, "raw_event", None)
    if bot is None or event is None:
        # 没有 bot/event 就没法主动发（比如干跑）：日志留痕，别静默吞掉
        log_event(
            "draw.no_channel",
            level="warning",
            status=getattr(result, "status", "?"),
            job_id=getattr(result, "job_id", ""),
        )
        return

    segment = getattr(result, "value", None) if getattr(result, "ok", False) else None
    if segment is None:
        log_event(
            "draw.job_failed",
            level="warning",
            status=getattr(result, "status", "?"),
            error=str(getattr(result, "error", ""))[:200],
        )
        await _say(bot, event, draw_failure_note())
        return

    from .reply_send import _send_image_with_retry

    ok = await _send_image_with_retry(bot, event, segment)
    log_event(
        "draw.job_sent" if ok else "draw.job_send_failed",
        level="info" if ok else "warning",
        job_id=getattr(result, "job_id", ""),
        elapsed_ms=getattr(result, "elapsed_ms", 0),
    )
    if not ok:
        await _say(bot, event, draw_failure_note())


async def _say(bot: Any, event: Any, text: str) -> None:
    """发一句纯文本。作业里的兜底话术，失败也不该把作业再炸一次。"""
    try:
        await bot.send(event, text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"dl_senpai draw 兜底文案发送失败: {type(exc).__name__}: {exc}"
        )
