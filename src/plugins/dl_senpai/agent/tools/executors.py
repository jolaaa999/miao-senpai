"""真实工具执行器：把 Phase 4 的占位 executor 替换为真实副作用。

Phase 5 第十六/十七节。迁移顺序（低风险 → 高风险）：

    1. SEARCH      只读，最好验证
    2. IMPRESSION  写画像，可逆（可删）
    3. STICKER     发图，失败可降级
    4. VOICE       发语音，失败可降级
    5. DRAW        生图，耗时且可能失败
    6. BROWSE      浏览器，超时长
    7. CARD        群管理，需权限
    8. MUTE        群管理，最敏感

每个 executor 的契约：
    输入  (AgentContext, ToolCall)
    输出  ToolResult（**统一形状**，不再是 bool/str/tuple 混杂）

**关键约束**：executor 复用现有业务函数，不重新实现业务逻辑。
例如 mute 调用 `group_mute.apply_mute_actions`，与 legacy 完全同源。
"""

from __future__ import annotations

import time
from typing import Any

from ...observability.logging import log_event
from .models import ToolCall, ToolResult


def _event_of(context: Any) -> Any:
    return getattr(context, "raw_event", None)


def _bot_of(context: Any) -> Any:
    return getattr(context, "bot", None)


def _group_of(context: Any) -> int | str:
    gid = getattr(context, "group_id", "") or "0"
    try:
        return int(gid)
    except (TypeError, ValueError):
        return gid


# ================================================================
# 1. SEARCH（只读）
# ================================================================


async def execute_search(context: Any, call: ToolCall) -> ToolResult:
    """联网检索。

    这是唯一一个"结果要回填给模型"的工具（continuation=RETURN_TO_MODEL），
    因此 `output` 必须是模型可读的检索摘要。
    """
    from ...web_search import prepare_tool_queries, searcher_from_config

    cfg = context.config
    user_text = str(getattr(context, "plain_text", "") or "").strip()
    # 模型常把问题压成一两个词（「签到」「弓」），单搜必然跑偏 —— 实测
    # 「你也可以找 签到」被搜成歌名。过短的检索词在这里并回用户原话，
    # 写全的检索词原样放行，不擅自改写。
    queries = prepare_tool_queries(list(call.arguments.get("queries") or []), user_text)
    if not queries:
        return ToolResult(
            tool_name="search", tool_call_id=call.id, success=False,
            error="没有检索词", error_type="empty_queries",
        )

    started = time.perf_counter()
    # 时效补搜要用**用户原话**判定（"最新/补丁/赛季"这些词在用户消息里，
    # 不在模型写出的检索词里）。拿不到就退回模型自己的检索词。
    freshness_intent = user_text or queries[0]
    try:
        searcher = searcher_from_config(cfg)
        brief = await searcher.search_many(
            queries[:2], intent_query=queries[0], freshness_intent=freshness_intent
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool_name="search", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="search_failed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    # 截断到配置上限，避免撑爆上下文
    max_chars = max(400, int(getattr(cfg, "search_prompt_max_chars", 2000)))
    if brief and len(brief) > max_chars:
        brief = brief[: max_chars - 1].rstrip() + "…"

    if not brief or not brief.strip():
        return ToolResult(
            tool_name="search", tool_call_id=call.id, success=False,
            error="检索无结果", error_type="no_result",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    return ToolResult(
        tool_name="search",
        tool_call_id=call.id,
        success=True,
        output=(
            f"{brief}\n\n"
            "上面是实时联网检索结果。开口之前按顺序做三件事：\n"
            "1) 核对它是不是**回答了对方问的那件事**（只是话题沾边不算）；\n"
            "2) 看**日期和版本号**：涉及最新/版本/赛季/补丁/更新 的问题，"
            "若这批结果一条日期都没有、或写的是旧赛季旧版本，说明它可能已经过期，"
            "**必须换词再搜一次**再下结论，不要默认搜到的就是最新的；\n"
            "3) 确认对得上、时效也没问题，再用学姐口吻回答。\n"
            "换词要真的换：加年份/月份/版本号，或换成「补丁说明」「更新公告」「官网」这类角度，"
            "别把同一句原样再问一遍。一次最多写 2 条 <<<SEARCH>>>，搜够两轮再收。\n"
            "确实没有就直说没查到、别编。"
        ),
        data={"value": brief, "queries": queries},
        duration_ms=(time.perf_counter() - started) * 1000,
    )


# ================================================================
# 2. IMPRESSION（写画像）
# ================================================================


async def execute_impression(context: Any, call: ToolCall) -> ToolResult:
    """写入人物画像。

    **不改 JSON schema**（第十九节），且**通过 MemoryFacade 写入**
    （第十五节：读走 Facade，写也必须走 Facade，不能读走 A 写走 B）。
    """
    cfg = context.config
    args = call.arguments or {}
    add = list(args.get("add") or [])
    remove = list(args.get("remove") or [])
    notes = args.get("notes")
    # 这 5 个是「判断性格」的载体（vibe 气质/性格、style 说话习惯、
    # relation 关系感、moment 难忘瞬间、pending 未说出口的心事）。
    # 之前只读 add/remove/notes，模型即使在 tool_calls 里填了也直接丢弃，
    # 于是人物画像里性格字段恒为空。缺失/空串统一归一成 None，
    # 让下游 apply_updates 保持「None=不动、''=清空」的既有语义。
    vibe = args.get("vibe")
    style = args.get("style")
    relation = args.get("relation")
    moment = args.get("moment")
    pending = args.get("pending")

    if not any(
        (add, remove, notes, vibe, style, relation, moment, pending)
    ):
        return ToolResult(
            tool_name="impression", tool_call_id=call.id, success=False,
            error="没有可写入的内容", error_type="empty_update",
        )

    started = time.perf_counter()
    try:
        from ..memory_facade import MemoryFacade, MemoryRef

        # 仅用于构造 update 值对象；真正的写盘走 Facade
        from ...person_memory import ImpressionUpdate

        facade = MemoryFacade(cfg)
        facade.record_person_updates(
            MemoryRef.for_user(context.user_id, group_id=context.group_id),
            [
                ImpressionUpdate(
                    add=add,
                    remove=remove,
                    notes=notes,
                    vibe=vibe,
                    style=style,
                    relation=relation,
                    moment=moment,
                    pending=pending,
                )
            ],
            context.actor,
            cfg,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool_name="impression", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="write_failed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    return ToolResult(
        tool_name="impression", tool_call_id=call.id, success=True,
        output="",
        data={
            "value": {
                "add": add,
                "remove": remove,
                "notes": notes,
                "vibe": vibe,
                "style": style,
                "relation": relation,
                "moment": moment,
                "pending": pending,
            }
        },
        duration_ms=(time.perf_counter() - started) * 1000,
    )


# ================================================================
# 3. STICKER（发图，可降级）
# ================================================================


async def execute_sticker(context: Any, call: ToolCall) -> ToolResult:
    """挑选并"决定发送"一个表情包。

    **重要区分（第二十节）**：
    - Observer sticker collection：收到用户表情 → **收藏**（在 Observation 层）
    - Agent sticker tool：Agent 决定**发送**表情（在这里）

    两者绝不混用。本 executor 只做"发送决策"，不采集。
    """
    from ...stickers import maybe_pick_sticker_reply

    cfg = context.config
    query = str(call.arguments.get("query") or "")
    session_id = context.session_id

    try:
        segment = maybe_pick_sticker_reply(
            session_id,
            user_text=context.extra.get("chat_text", "") or context.plain_text,
            reply_text=context.extra.get("reply_text", "") or "",
            interrupt=context.interrupt,
            config=cfg,
            force=True,
            extra_query=query,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool_name="sticker", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="pick_failed",
        )

    if segment is None:
        # 库里没有匹配的表情：不是错误，只是没图可发（可降级为纯文字）
        return ToolResult(
            tool_name="sticker", tool_call_id=call.id, success=False,
            error="表情库没有可用的表情", error_type="empty_library",
            data={"value": None},
        )

    return ToolResult(
        tool_name="sticker", tool_call_id=call.id, success=True,
        data={"value": segment, "query": query},
    )


# ================================================================
# 4. VOICE（TTS，失败降级）
# ================================================================


async def execute_voice(context: Any, call: ToolCall) -> ToolResult:
    """构建语音条。

    保持原有行为（第二十一节）：**失败 → 文字降级**，
    即返回 failed 但让上层继续用文字回复，而不是整轮失败。
    """
    from ...voice import build_voice_segment

    cfg = context.config
    override = str(call.arguments.get("override") or "")
    reply_text = context.extra.get("reply_text", "") or ""

    try:
        segment = await build_voice_segment(
            reply_text,
            config=cfg,
            force=True,
            override=override or None,
            user_request=context.plain_text,
            interrupt=context.interrupt,
        )
    except Exception as exc:  # noqa: BLE001
        log_event("voice.build_failed", level="warning", error=str(exc)[:200])
        return ToolResult(
            tool_name="voice", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="tts_failed",
        )

    if segment is None:
        return ToolResult(
            tool_name="voice", tool_call_id=call.id, success=False,
            error="TTS 未产出音频", error_type="no_audio",
        )

    return ToolResult(
        tool_name="voice", tool_call_id=call.id, success=True,
        data={"value": segment, "override": override},
    )


# ================================================================
# 5. DRAW（生图，耗时长）
# ================================================================


# 每会话的上一次派单时刻（session_id → time.monotonic()）。
# 放内存是有意的：draw_cooldown 只为挡「同一会话连刷」——作业队列吃不下
# 第二张图时，第二张注定超时。重启后清零没关系。
_last_draw_at: dict[str, float] = {}


def _draw_cooldown_key(context: Any) -> str:
    """冷却按会话算：群里两个人先后要图不该互相挡，同一会话要挡。"""
    sid = str(getattr(context, "session_id", "") or "")
    if sid:
        return sid
    return f"{getattr(context, 'group_id', '')}:{getattr(context, 'user_id', '')}"


def _draw_cooldown_left(context: Any) -> int:
    """还要等几秒才能再画（0 = 可以画）。

    `draw_cooldown` 之前是个只存在于配置里的死项（从没被读过），
    这里把它接上：>0 才生效，默认 0 表示不限制。
    """
    cfg = getattr(context, "config", None)
    cooldown = int(getattr(cfg, "draw_cooldown", 0) or 0)
    if cooldown <= 0:
        return 0
    last = _last_draw_at.get(_draw_cooldown_key(context))
    if last is None:
        return 0
    return max(0, int(cooldown - (time.monotonic() - last)))


def reset_draw_cooldown() -> None:
    """清空冷却表（测试与热重载用）。"""
    _last_draw_at.clear()


async def execute_draw(context: Any, call: ToolCall) -> ToolResult:
    """生图。

    保持原有行为（第二十二节）：超时/失败/图片发送失败
    → 正文 fallback，不因为统一 ToolResult 而丢失媒体降级。
    """
    from ...image_gen import build_image_segment

    cfg = context.config
    prompt = str(call.arguments.get("prompt") or "")
    if not prompt:
        return ToolResult(
            tool_name="draw", tool_call_id=call.id, success=False,
            error="没有绘图提示", error_type="empty_prompt",
        )

    # 冷却：刚画过就直说还要等几秒，别让作业队列排队到超时。
    # 返回 success=False + 明确的 error，模型才知道「不是不能画，是刚画过」。
    cooldown_left = _draw_cooldown_left(context)
    if cooldown_left > 0:
        log_event(
            "draw.cooldown",
            level="info",
            left_sec=cooldown_left,
            session=str(getattr(context, "session_id", ""))[:60],
        )
        return ToolResult(
            tool_name="draw", tool_call_id=call.id, success=False,
            error=f"刚画过一张，还要等 {cooldown_left} 秒才能再画",
            error_type="draw_cooldown",
        )

    # 默认派给后台作业（子 agent）：出图要 30~120 秒，同步跑会把整轮回复卡住，
    # 连她「先说的那句话」都发不出去。作业自己发图，这里只返回 job_id。
    if bool(getattr(cfg, "draw_async", True)):
        from ...draw_worker import submit_draw_job

        try:
            job_id = submit_draw_job(context, prompt)
        except Exception as exc:  # noqa: BLE001 — 派单失败退回同步，别让这轮完全没图
            log_event(
                "draw.submit_failed",
                level="warning",
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            _last_draw_at[_draw_cooldown_key(context)] = time.monotonic()
            return ToolResult(
                tool_name="draw", tool_call_id=call.id, success=True,
                data={"job_id": job_id, "prompt": prompt},
            )

    try:
        segment = await build_image_segment(
            resolved_prompt=prompt,
            user_request=context.plain_text,
            reply_text=context.extra.get("reply_text", "") or "",
            config=cfg,
            interrupt=context.interrupt,
            archive_session_id=context.session_id,
            archive_source_user=context.actor,
            archive_source_user_id=context.user_id,
            archive_scene=context.extra.get("draw_scene"),
            # 本轮用户发的图透传给生图：他说「按这张画」时，那张图会真的
            # 交给图生图接口，而不是只变成一句文字描述
            reference_images=context.extra.get("image_urls") or [],
        )
    except Exception as exc:  # noqa: BLE001
        log_event("draw.build_failed", level="warning", error=str(exc)[:200])
        return ToolResult(
            tool_name="draw", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="draw_failed",
        )

    if segment is None:
        return ToolResult(
            tool_name="draw", tool_call_id=call.id, success=False,
            error="生图未产出图片", error_type="no_image",
        )

    return ToolResult(
        tool_name="draw", tool_call_id=call.id, success=True,
        data={"value": segment, "prompt": prompt},
    )


# ================================================================
# 6. BROWSE（浏览器，超时长）
# ================================================================


async def execute_browse(context: Any, call: ToolCall) -> ToolResult:
    """浏览器逛站截图。

    第二十三节：必须 async，并受 Agent 总 timeout 约束
    （timeout 由 ToolExecutor 的 asyncio.wait_for 强制）。
    """
    from ...browser_agent import paths_to_image_segments, run_browse_tasks

    cfg = context.config
    tasks = list(call.arguments.get("tasks") or [])
    if not tasks:
        return ToolResult(
            tool_name="browse", tool_call_id=call.id, success=False,
            error="没有浏览任务", error_type="empty_tasks",
        )

    started = time.perf_counter()
    try:
        result = await run_browse_tasks(tasks, config=cfg)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool_name="browse", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="browse_failed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    if not getattr(result, "ok", False):
        return ToolResult(
            tool_name="browse", tool_call_id=call.id, success=False,
            error="浏览器任务失败", error_type="browse_failed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    segments = paths_to_image_segments(getattr(result, "image_paths", []) or [])
    page_url = getattr(result, "page_url", "") or ""
    output = ""
    if page_url:
        output = f"已浏览页面：{page_url}"

    return ToolResult(
        tool_name="browse", tool_call_id=call.id, success=True,
        output=output,
        data={"value": segments, "page_url": page_url},
        duration_ms=(time.perf_counter() - started) * 1000,
    )


# ================================================================
# 7. CARD（群管理）
# ================================================================


async def execute_card(context: Any, call: ToolCall) -> ToolResult:
    """修改群名片。走现有 `group_card.apply_card_actions`。"""
    from ...group_card import CardAction, apply_card_actions

    bot = _bot_of(context)
    if bot is None:
        return ToolResult(
            tool_name="card", tool_call_id=call.id, success=False,
            error="缺少 bot 上下文", error_type="no_bot",
        )

    args = call.arguments or {}
    try:
        action = CardAction(
            user_id=int(args.get("user_id")),
            card=str(args.get("card") or ""),
            kind=str(args.get("kind") or "request"),
        )
    except (TypeError, ValueError) as exc:
        return ToolResult(
            tool_name="card", tool_call_id=call.id, success=False,
            error=f"参数非法: {exc}", error_type="bad_arguments",
        )

    started = time.perf_counter()
    try:
        applied = await apply_card_actions(
            bot, group_id=_group_of(context), actions=[action]
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool_name="card", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="apply_failed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    if not applied:
        return ToolResult(
            tool_name="card", tool_call_id=call.id, success=False,
            error="名片未改动（被过滤或接口失败）", error_type="not_applied",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    return ToolResult(
        tool_name="card", tool_call_id=call.id, success=True,
        data={"value": applied, "action": action},
        duration_ms=(time.perf_counter() - started) * 1000,
    )


# ================================================================
# 8. MUTE（群管理，最敏感）
# ================================================================


async def execute_mute(context: Any, call: ToolCall) -> ToolResult:
    """禁言。走现有 `group_mute.apply_mute_actions`。

    这是**最敏感**的副作用：多执行一次意味着用户被禁言两遍。
    因此它：
    1. 在 ExecutionJournal 里标记为不可逆（阻断 fallback）；
    2. 由 ToolExecutor 按 tool_call_id 幂等去重。
    """
    from ...group_mute import MuteAction, apply_mute_actions

    bot = _bot_of(context)
    if bot is None:
        return ToolResult(
            tool_name="mute", tool_call_id=call.id, success=False,
            error="缺少 bot 上下文", error_type="no_bot",
        )

    args = call.arguments or {}
    try:
        action = MuteAction(
            user_id=int(args.get("user_id")),
            duration=int(args.get("duration") or 60),
            kind=str(args.get("kind") or "request"),
        )
    except (TypeError, ValueError) as exc:
        return ToolResult(
            tool_name="mute", tool_call_id=call.id, success=False,
            error=f"参数非法: {exc}", error_type="bad_arguments",
        )

    started = time.perf_counter()
    try:
        applied = await apply_mute_actions(
            bot, group_id=_group_of(context), actions=[action]
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool_name="mute", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="apply_failed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    if not applied:
        return ToolResult(
            tool_name="mute", tool_call_id=call.id, success=False,
            error="禁言未生效（被过滤或接口失败）", error_type="not_applied",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    return ToolResult(
        tool_name="mute", tool_call_id=call.id, success=True,
        data={"value": applied, "action": action},
        duration_ms=(time.perf_counter() - started) * 1000,
    )


# ================================================================
# 9. GROUP_READ（只读）
# ================================================================


async def execute_group_read(context: Any, call: ToolCall) -> ToolResult:
    """读取群公告 / 群文件 / 群相册。

    与 SEARCH 同类：结果要回填给模型（continuation=RETURN_TO_MODEL），
    所以 output 必须是模型能直接读的摘要文本。

    这里是只读工具，不产生任何群内副作用；拿不到内容时给一句
    「没读到」的真实反馈，而不是让模型自己猜（猜就是编）。
    """
    args = call.arguments or {}
    kind = str(args.get("kind") or "").strip()
    # 第二个参数统一叫 target：对「相册内容」是相册 ID，
    # 对「文件内容」是文件名关键词。也接受旧的 album_id，避免旧调用失效。
    target = str(args.get("target") or args.get("album_id") or "").strip()

    from ...group_info import SUPPORTED_KINDS, read_group_resource

    if kind not in SUPPORTED_KINDS:
        return ToolResult(
            tool_name="group_read", tool_call_id=call.id, success=False,
            error="不认识这种资源：" + (kind or "(空)"),
            error_type="bad_kind",
        )

    started = time.perf_counter()
    try:
        found, text = await read_group_resource(
            _bot_of(context), _group_of(context), kind, target=target
        )
    except Exception as exc:  # noqa: BLE001 — 读群资源失败不能拦掉回复
        return ToolResult(
            tool_name="group_read", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}",
            error_type="group_read_failed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    if not found:
        return ToolResult(
            tool_name="group_read", tool_call_id=call.id, success=False,
            error="没读到内容（群里可能确实没有，或者学姐没有权限）",
            error_type="empty_group_resource",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    return ToolResult(
        tool_name="group_read",
        tool_call_id=call.id,
        success=True,
        output=f"{text}\n\n上面是学姐自己从群里读到的内容，照着实说就行，"
        "别补记录里没有的东西；读不到就说读不到。",
        data={"value": text, "kind": kind},
        duration_ms=(time.perf_counter() - started) * 1000,
    )


# -- 注册表：工具名 → executor --

REAL_EXECUTORS: dict[str, Any] = {
    "search": execute_search,
    "impression": execute_impression,
    "sticker": execute_sticker,
    "voice": execute_voice,
    "draw": execute_draw,
    "browse": execute_browse,
    "card": execute_card,
    "mute": execute_mute,
    "group_read": execute_group_read,
}


__all__ = [
    "REAL_EXECUTORS",
    "execute_group_read",
    "execute_search",
    "execute_impression",
    "execute_sticker",
    "execute_voice",
    "execute_draw",
    "execute_browse",
    "execute_card",
    "execute_mute",
]
