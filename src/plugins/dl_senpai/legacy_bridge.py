"""AgentEntry 的 Router 适配层（原 LegacyAgentBridge）。

Phase 5 变化：这个模块不再直接调 `_handle_chat`，而是把消息交给
`AgentEntry`，由它根据执行模式决定走新路径还是旧路径。

    Router Agent Stage
          ↓
    handle_agent_message(ctx, trigger)      ← 本模块
          ↓
    SessionExecutor（同 session 串行 / 不同并发）
          ↓
    AgentEntry.handle(agent_context)        ← 模式 + fallback 边界
          ↓
    AgentRuntime 或 legacy handler

**为什么保留 SessionExecutor 这一层**（第五十一节）：并发规则不得退化。
AgentRuntime 自己不允许创建绕过 executor 的消息级 task。
"""

from __future__ import annotations

import re
import time
from typing import Any

from .observability.logging import log_event


def _resolve_config(ctx: Any) -> Any:
    """取配置对象：优先上下文携带的，其次全局单例。

    为什么允许两条路：legacy 早期路径不一定把 config 塞进 extra，
    但知识库装配无论如何都需要一份配置才能判断是否开启。
    """
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict) and extra.get("config") is not None:
        return extra["config"]
    try:
        from .config import get_config

        return get_config()
    except Exception:  # noqa: BLE001
        return None


def _strip_summon_words(ctx: Any, text: str) -> str:
    """删掉正文里的召唤词；只叫了一声没别的内容时用占位符顶上。

    为什么在桥接层做而不是 gateway：这一层才知道配置，而且下面
    user_text / 召回 query / _record_turn 全都共用这一份文本——
    在这里删一次，prompt、召回、落盘历史就一起干净了，不会出现
    「prompt 里删了、记忆里还留着喵喵」这种半吊子状态。

    取不到配置或剥词抛异常时原样返回：删词只是锦上添花，
    绝不能因为它出错就让整条回复发不出去。
    """
    if not text:
        return text
    try:
        from .core.agent_trigger import (
            SUMMON_ONLY_PLACEHOLDER,
            has_summon_keyword,
            strip_summon_keywords,
        )

        cfg = _resolve_config(ctx)
        if cfg is None:
            return text
        keywords = cfg.summon_keyword_list()
        if not keywords:
            return text
        # 先判有没有命中再剥：否则「只有空白」的正文会被 strip 成空串，
        # 接着被误当成「纯呼叫」而塞进占位符。
        if not has_summon_keyword(text, keywords):
            return text
        return strip_summon_keywords(text, keywords) or SUMMON_ONLY_PLACEHOLDER
    except Exception:  # noqa: BLE001
        return text


def _compose_user_text(
    ctx: Any,
    plain_text: str,
    reply_message: Any,
    attachment_texts: list[tuple[str, str]] | None = None,
    forward_text: str = "",
) -> str:
    """有引用时把被引用内容拼进用户输入；附件正文也拼在这里。

    为什么在桥接层做：runtime 只拿到 AgentContext，不认识 OneBot 的
    event.reply；这里复用 legacy 同一个 build_chat_text_with_reply，
    保证两条链路对「引用」的呈现完全一致。

    附件正文与转发的聊天记录都拼在**本句之前**：先给材料、再给要求，
    模型的注意力顺序更自然，也避免长正文把「我现在说的」那半句挤到看不见。

    转发的聊天记录单独成块（不走 attachment_texts）：它和文件是两种东西，
    混在一起会让模型以为记录是个文件名。内容已由异步侧取好传进来。
    """
    raw_event = getattr(ctx, "raw_event", None)
    text = plain_text
    if raw_event is not None:
        try:
            # reply 的读取也放进 try：适配层/事件替身里它可能是会抛异常的描述符，
            # 不能只因为读一下就炸掉整条回复。
            if getattr(raw_event, "reply", None) is not None:
                from .reply_quote import build_chat_text_with_reply

                text = build_chat_text_with_reply(
                    raw_event, plain_text, reply_message=reply_message
                )
        except Exception as exc:  # noqa: BLE001 — 引用解析失败不该拦掉正常回复
            log_event(
                "agent.reply_quote_failed",
                level="warning",
                session_id=getattr(ctx, "session_id", "") or None,
                error=type(exc).__name__,
            )
            text = plain_text

    materials: list[str] = []
    if forward_text:
        materials.append("【对方转发的聊天记录】\n" + forward_text)
    if attachment_texts:
        for name, body in attachment_texts:
            materials.append(
                "【对方发来的文件：" + (name or "未命名") + "】\n" + (body or "")
            )
    if materials:
        text = "\n\n".join(materials) + "\n\n【我现在说的】\n" + text
    return text


def _assemble_memory(
    ctx: Any, session_id: str, query: str, name: str, extra: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """一次性装配全部记忆片段，返回 (history, 召回简报)。

    为什么统一走 MemoryFacade.build_snapshot：好感度 / 内心状态 / 群风格 /
    人物印象这四段此前**只有 legacy 编排在写**，而 legacy 编排本身已经跑不
    起来（未定义名 156 处）。结果是 runtime 路径下它们恒为空串——开关开着、
    存储里有数据、模型却什么都看不到。这里让快照聚合一次把它们全填上。

    失败一律降级成「没有记忆」：记忆读不出来最多是聊得没上下文，
    绝不能因此让用户收不到回复。
    """
    if not session_id:
        return [], ""
    cfg = extra.get("config") or _resolve_config(ctx)
    if cfg is None:
        return [], ""

    try:
        from .agent.memory_facade import MemoryFacade, MemoryRef, MemoryScope
    except Exception as exc:  # noqa: BLE001
        log_event(
            "agent.memory_load_failed",
            level="warning",
            session_id=session_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return [], ""

    group_id = str(getattr(ctx, "group_id", "") or "")
    user_id = str(getattr(ctx, "user_id", "") or "")
    # group/user 必须一起带上：群风格靠 group_id 定位，人物画像靠 user_id
    # 定位且允许跨群。
    ref = MemoryRef(
        scope=MemoryScope.SESSION,
        session_id=session_id,
        group_id=group_id,
        user_id=user_id,
    )

    # 好感度与内心状态的作用域规则属于 core.domain，在这里算好传进去，
    # 免得 memory_facade 反过来依赖上层。
    affection_scope_value = ""
    try:
        from .core.domain import affection_scope as _affection_scope

        raw_event = getattr(ctx, "raw_event", None)
        if raw_event is not None:
            affection_scope_value = _affection_scope(cfg, raw_event) or ""
    except Exception:  # noqa: BLE001
        affection_scope_value = ""

    inner_key = ""
    try:
        from .inner_state import scope_key as _inner_scope_key

        inner_key = _inner_scope_key(
            per_group=bool(getattr(cfg, "inner_state_per_group", False)),
            group_id=group_id or None,
            private=bool(getattr(ctx, "is_private", False)),
        )
    except Exception:  # noqa: BLE001
        inner_key = ""

    want = {
        "memory_recall": bool(getattr(cfg, "memory_recall_enable", True)),
        "person_memory": bool(getattr(cfg, "person_memory_enable", False)),
        "affection": bool(getattr(cfg, "affection_enable", False)),
        # 内心状态没有独立开关：它是性格的一部分，读了没内容就是空串。
        "inner_state": True,
        "group_style": bool(getattr(cfg, "group_style_enable", True)),
        # 概览层：双层记忆的上层，默认开。
        "memory_cards": bool(getattr(cfg, "memory_cards_enable", True)),
    }

    try:
        snapshot = MemoryFacade(config=cfg).build_snapshot(
            ref,
            query=query,
            name=name,
            config=cfg,
            want=want,
            affection_scope=affection_scope_value,
            inner_key=inner_key,
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "agent.memory_load_failed",
            level="warning",
            session_id=session_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return [], ""

    # 空串不写进 extra：PromptBuilder 就是按「非空即注入」判断的，
    # 写空串只会让 sections 诊断失真。
    if snapshot.recalled:
        extra["memory_recall_brief"] = snapshot.recalled
    if snapshot.affection:
        extra["affection_brief"] = snapshot.affection
    if snapshot.inner_state:
        extra["inner_state_brief"] = snapshot.inner_state
    if snapshot.group_style:
        extra["group_style_brief"] = snapshot.group_style
    if snapshot.person_profile:
        extra["person_memory_brief"] = snapshot.person_profile
    if snapshot.cards:
        extra["memory_cards_brief"] = snapshot.cards

    # 当天聊天流水：她没参与、但发生在她眼前的那部分。
    # 单独一段而不是塞进 history——那些消息不是对话轮次，混进去会让
    # user/assistant 交替结构错乱。
    try:
        _fill_recent_chat_brief(
            cfg, ctx, session_id, query, extra, history=list(snapshot.history)
        )
    except Exception:  # noqa: BLE001 — 背景读不出来最多是聊得没上下文
        pass

    # 跨场合对话流：她在别处接过的话。
    # 为什么和上面那段并存：RecentChat 记的是**本会话发生过的消息**，
    # 这一段记的是**她在别的场合实际接过的轮次**。同一个人换个地方，
    # 前者完全帮不上忙——这正是「换个群就不认识你」的根因。
    try:
        if bool(getattr(cfg, "cross_chat_enable", True)):
            from .cross_chat import build_cross_brief

            brief = build_cross_brief(
                current_where=session_id,
                n=int(getattr(cfg, "cross_chat_recent_n", 8) or 8),
                max_chars=int(getattr(cfg, "cross_chat_max_chars", 800) or 800),
                directory=getattr(cfg, "cross_chat_dir", None),
            )
            if brief:
                extra["cross_chat_brief"] = brief
                log_event("agent.cross_chat", chars=len(brief))
    except Exception:  # noqa: BLE001 — 这段是锦上添花，失败不挡对话
        pass

    # 她自己的日记：跟「今天群里聊过什么」并列的另一段背景，
    # 让她记得「我昨天写过什么」，不至于天天从同一个角度开口。
    try:
        _fill_diary_brief(cfg, extra, inner_key)
    except Exception:  # noqa: BLE001 — 日记读不出来最多是少一段回味
        pass

    # 人设卡：决定「她是谁」。必须在任何简报之前解析——正文会顶替
    # SYSTEM_PROMPT，后面那些段是叠在它之上的补充，顺序错了就成了
    # 「补充说明一份不存在的人设」。
    try:
        _fill_persona_card(cfg, session_id, extra)
    except Exception:  # noqa: BLE001 — 卡库坏了就回落到 persona.py
        pass

    # 在场与节奏：拼在长度纪律之前（顺序说明见 PromptBuilder.build）。
    # 先定「这一轮要不要开口、说几句」，再压「一条多短」。
    try:
        _fill_presence_hint(cfg, extra)
    except Exception:  # noqa: BLE001 — 少这段最多是节奏差一点，不影响回复
        pass

    # 群黑话词表：跟在场那段一样贴着本轮消息放（见 PromptBuilder.build）。
    try:
        _fill_slang_brief(cfg, extra)
    except Exception:  # noqa: BLE001 — 少这段最多是听错一个梗，不影响回复
        pass

    # 长度纪律：必须最后注入（PromptBuilder 也把它拼在末尾）。
    # 人设中段的规则压不住输出长度，这条得贴着本轮消息放。
    try:
        _fill_brevity_hint(cfg, extra, query)
    except Exception:  # noqa: BLE001 — 少了这段最多是话变长，不影响对话
        pass

    # 本轮语气判断：排在长度纪律**之后**，也就是整个提示词的最后一段。
    # 它比长度纪律更需要贴着本轮消息——判的是「这一句要不要安慰」。
    try:
        extra["_ctx"] = ctx
        _fill_tone_hint(cfg, query, extra, history=list(snapshot.history))
    except Exception:  # noqa: BLE001 — 判不出来按普通处理，不能挡住回复
        pass
    return list(snapshot.history), snapshot.recalled


def _norm_dedup(text: Any) -> str:
    """去重用的归一化：只留字符，抹掉空白与换行差异。"""
    return re.sub(r"\s+", "", str(text or ""))


def _fill_recent_chat_brief(
    cfg: Any,
    ctx: Any,
    session_id: str,
    query: str,
    extra: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
) -> None:
    """把当天流水渲染进 extra["recent_chat_brief"]。

    排除当前这条触发消息：它马上就要以本轮 user 输入的身份出现，
    留在背景里会让模型看到两遍同一句话。

    还要排除**已经在 history 里的消息**。两者时间范围大面积重叠
    （history 24 条 ≈ 最近十几分钟，流水也是），同一段对话在提示词里
    出现两遍的直接后果就是复述——线上被人指出「一本正经地重复废话」。
    """
    if not bool(getattr(cfg, "recent_chat_enable", True)):
        return
    from .recent_chat import build_recent_chat_brief, get_recent_chat_store

    store = get_recent_chat_store(getattr(cfg, "recent_chat_dir", None))
    entries = store.entries(session_id)
    if not entries:
        return

    # 与 history 去重：命中的说明模型已经以 messages 形式看到了
    if history:
        seen = [
            _norm_dedup(m.get("content"))
            for m in history
            if isinstance(m, dict) and _norm_dedup(m.get("content"))
        ]
        kept = []
        for entry in entries:
            norm = _norm_dedup(getattr(entry, "text", ""))
            if norm and len(norm) >= 4 and any(
                norm == h or (len(h) > 6 and (norm in h or h in norm)) for h in seen
            ):
                continue
            kept.append(entry)
        entries = kept
        if not entries:
            return

    raw_event = getattr(ctx, "raw_event", None)
    current_id = str(getattr(raw_event, "message_id", "") or "")
    brief = build_recent_chat_brief(
        entries,
        query,
        recent_n=int(getattr(cfg, "recent_chat_recent_n", 20)),
        recall_n=int(getattr(cfg, "recent_chat_recall_n", 10)),
        max_chars=int(getattr(cfg, "recent_chat_max_chars", 2000)),
        exclude_message_id=current_id,
        is_private=bool(getattr(ctx, "is_private", False)),
    )
    if brief:
        extra["recent_chat_brief"] = brief


def _fill_brevity_hint(cfg: Any, extra: dict[str, Any], query: str = "") -> None:
    """把长度纪律写进 extra["brevity_hint"]。

    默认开：她原来会把「群里问一句」答成三段落，这是最常见的抱怨。
    想关掉就设 DL_SENPAI_REPLY_BREVITY_ENABLE=false。

    query 是本轮用户原话：用它判定「闲聊档 / 解释档」。
    为什么由代码判而不是让模型自判——见 brevity.is_explain_request 的注释。
    """
    if not bool(getattr(cfg, "reply_brevity_enable", True)):
        return
    from .brevity import build_brevity_hint, is_explain_request

    hint = build_brevity_hint(True, explain=is_explain_request(query))
    if hint:
        extra["brevity_hint"] = hint


def _fill_persona_card(cfg: Any, session_id: str, extra: dict[str, Any]) -> None:
    """把当前会话的人设卡正文写进 extra["persona_body"]。

    取不到卡（没绑、目录空、功能关着）就什么都不写：PromptBuilder 按
    「非空即注入」判断，空串写进去只会让 sections 诊断失真，也会让
    persona.py 的回落路径多绕一圈。
    """
    from .personas import resolve_persona_body

    body, card_id = resolve_persona_body(session_id, cfg)
    if not body:
        return
    extra["persona_body"] = body
    log_event(
        "agent.persona_card",
        session_id=session_id or None,
        card=card_id,
        chars=len(body),
    )


def _fill_presence_hint(cfg: Any, extra: dict[str, Any]) -> None:
    """把「在场与节奏」写进 extra["presence_hint"]。

    文案由 presence.py 提供：机制改了措辞就得跟着改，所以两边同源，
    不在这里再抄一份——抄一份的下场是机制已经允许潜水了，提示词还在
    教她「每条都要接」。
    """
    from .presence import build_presence_hint

    hint = build_presence_hint(cfg)
    if hint:
        extra["presence_hint"] = hint


def _fill_slang_brief(cfg: Any, extra: dict[str, Any]) -> None:
    """把已确认的黑话词表写进 extra["slang_brief"]。

    只拼 confirmed 的词：待确认的是「她记下但还没人确认」的候选，
    把半懂的释义喂回人设，比不认识这个词更容易说错还理直气壮。
    """
    from .slang import build_slang_brief

    brief = build_slang_brief(cfg)
    if brief:
        extra["slang_brief"] = brief


def _fill_tone_hint(
    cfg: Any, query: str, extra: dict[str, Any], *, history: list[Any] | None = None
) -> None:
    """判断本轮是「吐槽」还是「真倾诉」，把结论写进 extra["tone_hint"]。

    为什么要判定而不是写死规则：人设里那条「日常吐槽不是情绪求助」连
    「今天好累」都点名了，实测照样进安慰模式——通用规则对这一轮不够具体。
    判定结论是**这一轮**的指令，位置又贴在提示词最末，压得住。

    判定失败一律当作 normal：语气提示缺失最多是回复平淡一句，
    绝不能因为它读崩了让整轮回复出不来。
    """
    if not bool(getattr(cfg, "tone_classify_enable", True)):
        return
    from .tone import (
        build_casual_hint,
        build_tone_hint,
        classify_tone,
        is_casual_prompt,
        tone_label,
    )

    recent: list[str] = []
    for item in list(history or [])[-6:]:
        text = ""
        if isinstance(item, dict):
            text = str(item.get("content") or "")
        else:
            text = str(item)
        if text:
            recent.append(text)

    verdict = classify_tone(query, recent=recent)
    # 打日志是为了让「还犯不犯」能数出来，而不是靠截图去碰
    log_event(
        "llm.tone",
        session_id=getattr(extra.get("_ctx"), "session_id", None) or None,
        verdict=verdict,
        label=tone_label(verdict),
    )
    parts: list[str] = []
    hint = build_tone_hint(verdict)
    if hint:
        parts.append(hint)
    # 梗密度：每轮一贴的分档提示。
    # 为什么不靠人设里那句「可以轻度接梗」——那种写法等于没有约束，
    # 实测要么一轮堆几个梗，要么正经答疑里也硬塞一个。
    try:
        from .meme import build_meme_hint, resolve_level

        level = resolve_level(
            getattr(cfg, "meme_level", "auto"),
            verdict=verdict,
            casual=is_casual_prompt(query),
            user_text=query,
        )
        meme = build_meme_hint(level)
        if meme:
            parts.append(meme)
        log_event("llm.meme", level=level)
    except Exception:  # noqa: BLE001 — 梗密度算不出来就别贴，绝不挡回复
        pass
    # 随口一句的，再给一条本轮字数。放在语气判断之后 = 整个提示词的最后
    try:
        if is_casual_prompt(query):
            casual = build_casual_hint(
                int(getattr(cfg, "casual_reply_hint_chars", 30) or 0)
            )
            if casual:
                parts.append(casual)
    except Exception:  # noqa: BLE001
        pass
    if parts:
        extra["tone_hint"] = "\n".join(parts)


def _fill_diary_brief(cfg: Any, extra: dict[str, Any], inner_key: str = "") -> None:
    """把最近几篇日记渲染进 extra["diary_brief"]。

    只喂最近 N 条（默认 3）：日记是用来「接得上自己」的，不是第二本记忆。
    读不出来就什么都不写——空串会让 PromptBuilder 的 sections 诊断失真，
    所以这里跟 recent_chat 一样，非空才写。
    """
    if not bool(getattr(cfg, "diary_enable", False)):
        return
    from .diary import build_diary_brief, get_diary_store

    store = get_diary_store(getattr(cfg, "diary_dir", None))
    # 心情决定先想起哪条日记（mood_filter）；读不到心情就按时间顺序
    mood_word = ""
    if inner_key:
        try:
            from .inner_state import current_word, get_inner_state_store

            mood_word = current_word(
                get_inner_state_store(getattr(cfg, "inner_state_dir", "")).load(inner_key)
            )
        except Exception:  # noqa: BLE001
            mood_word = ""
    brief = build_diary_brief(
        store, n=int(getattr(cfg, "diary_context_n", 3)), mood_word=mood_word
    )
    if brief:
        extra["diary_brief"] = brief


async def _resolve_image_urls(ctx: Any, reply_message: Any = None) -> list[str]:
    """把本轮消息里的图片解析成 data URL 列表（没有则返回空表）。

    为什么必须显式做这一步：runtime 不认识 OneBot 事件，也不该做网络 IO；
    AgentRuntime._call_model 只从 extra["image_urls"] 取图。此前**没人写这
    个键**，所以 DL_SENPAI_VISION_ENABLE=true 也等于没开——模型永远看不到图，
    连 system prompt 里的视觉段都不会注入。

    失败静默：图下载不到就当没图，不能因此拦掉文字回复。

    引用图（2026-10-15）：本轮没有图时回退到被引用的那条消息。
    为什么必须回退：QQ 把被引用的图折成 [图片]，本轮消息段里根本没有 image，
    于是「引用一张图问学姐」永远拿不到视觉输入，模型只能看见「[图片]」三个字。
    三道闸在 _resolve_quoted_image_urls 里。
    """
    cfg = _resolve_config(ctx)
    if cfg is None or not bool(getattr(cfg, "vision_enable", True)):
        return []
    if ctx.has_images():
        from .vision import extract_vision_image_segments, resolve_vision_images

        raw_event = getattr(ctx, "raw_event", None)
        segments = extract_vision_image_segments(getattr(raw_event, "message", None))
        if segments:
            return await resolve_vision_images(
                getattr(ctx, "bot", None),
                segments,
                max_images=int(getattr(cfg, "vision_max_images", 3)),
            )
    return await _resolve_quoted_image_urls(ctx, cfg, reply_message)


async def _resolve_quoted_image_urls(
    ctx: Any, cfg: Any, reply_message: Any
) -> list[str]:
    """引用消息里的图：本轮无图时回退，用三道闸控制信任范围。

    闸一 开关：这是新行为，线上出问题要能一键关掉而不改代码。
    闸二 发送者：默认只认「本人引用的图」和「引用学姐自己发过的图」。
        反过来（谁都认）意味着任何人引用群友的图都能让模型跑一次下载 + 视觉
        调用，既费额度，也可能把不相干的图带进对话。
    闸三 时效：默认 120 秒，引用几天前的图不该反复触发下载。

    另外默认只认 image，不认 mface（动画表情）——引用个表情包不算带图提问。
    """
    if reply_message is None or not bool(getattr(cfg, "vision_quoted_enable", True)):
        return []
    reply = getattr(getattr(ctx, "raw_event", None), "reply", None)
    if reply is None:
        return []

    if bool(getattr(cfg, "vision_quoted_same_sender_only", True)):
        quoted_uid = str(
            getattr(getattr(reply, "sender", None), "user_id", "") or ""
        ).strip()
        allowed = {
            str(getattr(ctx, "user_id", "") or "").strip(),
            str(getattr(getattr(ctx, "bot", None), "self_id", "") or "").strip(),
        }
        # quoted_uid 为空 = 拿不到发送者（异常事件），按「不认识」处理，不放行。
        if not quoted_uid or quoted_uid not in allowed:
            return []

    ttl = int(getattr(cfg, "vision_quoted_ttl_seconds", 120) or 0)
    if ttl > 0:
        try:
            age = time.time() - float(getattr(reply, "time", None))
        except (TypeError, ValueError):
            age = None
        if age is not None and age > ttl:
            return []

    if bool(getattr(cfg, "vision_quoted_allow_mface", False)):
        from .vision import extract_vision_image_segments

        segments = extract_vision_image_segments(reply_message)
    else:
        # 只认真图片：mface 是动画表情，认它容易把「引用个表情包」当成带图提问。
        segments = [
            dict(getattr(seg, "data", {}) or {})
            for seg in reply_message
            if getattr(seg, "type", None) == "image"
        ]
    if not segments:
        return []
    from .vision import resolve_vision_images

    return await resolve_vision_images(
        getattr(ctx, "bot", None),
        segments,
        max_images=int(getattr(cfg, "vision_max_images", 3)),
    )


async def _resolve_attachment_texts(ctx: Any) -> list[tuple[str, str]]:
    """把本轮消息里的文件**直接读出来**，返回 [(文件名, 文本)]。

    为什么不等模型自己调 read_doc：用户的原话是「发了文档又唤醒了学姐，
    那学姐就该去下载并读取」。如果只在 tool 里按需读，模型很可能直接
    凭文件名瞎答一轮，或者压根不调工具。自动读 = 她一定看得到内容。

    仍然保留 read_doc 工具：那份是给「读第 2 个文件」「文件被截断了想再看」
    这类二次调用用的。两者不冲突，共用同一套下载与解析。

    失败静默：读不出来就当没有附件，绝不能因此拦掉文字回复。
    """
    cfg = _resolve_config(ctx)
    raw_event = getattr(ctx, "raw_event", None)
    raw_message = getattr(raw_event, "message", None)
    if raw_message is None:
        return []

    from .attachments import build_attachments, clip, download_attachment, extract_text

    found = build_attachments(
        raw_message,
        group_id=str(getattr(ctx, "group_id", "") or getattr(raw_event, "group_id", "") or ""),
        user_id=str(getattr(ctx, "user_id", "") or getattr(raw_event, "user_id", "") or ""),
    )
    bot = getattr(ctx, "bot", None)
    if not found or bot is None:
        return []

    # 一次唤醒最多自动读这么多个：防止有人一次丢 10 个 PDF 把上下文吃爆
    limit = int(getattr(cfg, "attachment_auto_read_max", 2) or 2)
    out: list[tuple[str, str]] = []
    for att in found[: max(1, limit)]:
        if not att.readable:
            continue
        try:
            data = await download_attachment(bot, att)
            if not data:
                continue
            text = clip(extract_text(att.name, data))
            if text:
                out.append((att.name, text))
        except Exception as exc:  # noqa: BLE001 — 单个文件失败不影响其它
            log_event(
                "attachment.auto_read_failed",
                level="warning",
                name=att.name,
                error=type(exc).__name__,
            )
    return out


def _build_status_bar(ctx: Any, extra: dict[str, Any]) -> str:
    """构造状态栏（第二章 2.6）：确定性算出的事实，贴在生成位置前。

    为什么不放 system prompt：system 是 KV Cache 的静态前缀，每轮改它会让
    整段前缀失效；放在轨迹末尾只失效最后一小段。

    为什么必须由代码算：模型会**无条件相信**状态栏里的内容，所以这里只写
    确定的事实（时间、场景、有没有图、有没有引用），不做任何模型推断。
    """
    import datetime as _dt

    parts: list[str] = []
    try:
        now = _dt.datetime.now()
        weekday = "一二三四五六日"[now.weekday()]
        parts.append(f"当前时间：{now:%Y-%m-%d %H:%M}（周{weekday}）")
    except Exception:  # noqa: BLE001
        pass
    parts.append("场景：私聊" if getattr(ctx, "is_private", False) else "场景：群聊")

    # 当前说话人必须由代码显式声明：正文里已经不再拼「昵称：」前缀，
    # 模型只能靠这里知道对面是谁。昵称是用户可控的任意文本（带标点、
    # 带冒号都很常见），写进正文会被当成发言内容——「失语.」那次事故
    # 就是这么把昵称解释成了词条。放进状态栏后它只是元数据。
    actor = str(getattr(ctx, "actor", "") or "").strip()
    if actor:
        parts.append(f"正在跟你说话的人：{actor}（这是对方的昵称）")
    if extra.get("image_urls"):
        parts.append("本轮带图：是")
    # 文件同理：这是确定性事实，写进状态栏后模型不必再猜有没有附件
    for item in extra.get("attachments") or []:
        name = str((item or {}).get("name") or "").strip()
        if name:
            parts.append("本轮带文件：" + name)
    try:
        raw_event = getattr(ctx, "raw_event", None)
        if raw_event is not None and getattr(raw_event, "reply", None) is not None:
            parts.append("本条是对某条消息的回复：是")
    except Exception:  # noqa: BLE001
        pass
    # 最后一条 user 消息比 system 中段有效得多（同一轮里位置越靠后越管用，
    # 之前挪语气提示时量过：安慰率 22% → 55%）。
    # 这条针对的是最顽固的一个毛病：她把自己当成写手，回「可以这样回：…」
    # 「共情型：…」——models 把「昵称：这句话」当成一段待处理的聊天记录，
    # 加上 system 里满篇「怎么写」的规范，自然就滑进了交稿模式。
    # 人设里也写了，但人设是几千字里的中段，压不住。
    if not getattr(ctx, "is_private", False):
        parts.append("你就在这个群里，直接说自己的反应（不要写「可以这样回」「共情型」这类提示）")
    body = "\n".join(f"- {p}" for p in parts)
    return f"<agent_status>\n{body}\n</agent_status>"


def _history_user_text(agent_context: Any) -> str:
    """落盘进历史的那半句用户原话。

    优先取 plain_text（干净原话）。为什么：user_text 里带着【引用消息】/
    【我现在说的】这套脚手架，还有附件正文；存进历史后下一轮又被喂回 prompt，
    模型会把「上一轮引用了谁」当成「现在我正在引用谁」。实测群记忆里这样脏了
    114 条。引用块的作用只在本轮，历史里留它有害无益。
    兜底仍用渲染态：只引用、自己没打字时 plain_text 是空的，那条是唯一信息来源。
    """
    clean = str(getattr(agent_context, "plain_text", "") or "").strip()
    if clean:
        return clean
    return str(getattr(agent_context, "user_text", "") or "")


def _record_turn(agent_context: Any, user_text: str, assistant_text: str) -> None:
    """把这一轮对话写进会话历史。

    只在确认发送成功之后调用：没发出去的回复若写进历史，
    下一轮模型会把用户从未看到的内容当成「已经发生过」，越聊越偏。
    """
    session_id = getattr(agent_context, "session_id", "") or ""
    if not session_id or not assistant_text:
        return
    try:
        from .agent.memory_facade import MemoryFacade, MemoryRef

        MemoryFacade(config=getattr(agent_context, "config", None)).record_turn(
            MemoryRef.for_session(session_id), user_text, assistant_text
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "agent.memory_write_failed",
            level="warning",
            session_id=session_id,
            error=f"{type(exc).__name__}: {exc}",
        )

    # 同一轮再记一份到跨场合流水。
    # 为什么单独记：ChatMemory 按会话存，换个地方就不认得这个人；
    # 这一层按时间存，让她在私聊里知道群里刚发生过什么。
    # 必须等发送成功后才写，理由与上面一样。
    try:
        cfg = getattr(agent_context, "config", None)
        if bool(getattr(cfg, "cross_chat_enable", True)):
            from .cross_chat import get_cross_store

            ctx_obj = getattr(agent_context, "message_context", None)
            who = str(getattr(ctx_obj, "actor", "") or "") if ctx_obj else ""
            get_cross_store(getattr(cfg, "cross_chat_dir", None)).record(
                where=session_id,
                who=who,
                text=user_text,
                reply=assistant_text,
                max_entries=int(getattr(cfg, "cross_chat_max_entries", 600) or 600),
            )
    except Exception:  # noqa: BLE001 — 记流水失败绝不能影响回复
        pass

async def _extract_memory_cards(agent_context: Any, history: list[dict[str, str]]) -> None:
    """批量跑一次长期记忆提取，把候选事实写进卡片库。

    为什么要批量：每次提取都是一次额外 LLM 调用。第三章引 Memobase 的做法是
    「对话先在缓冲区累积，达到一定规模或时限后再统一触发一次提取，以摊薄
    LLM 调用成本」。这里按轮数取模触发。

    为什么用 _complete_once 而不是 llm.chat：chat() 会做联网预取、few-shot、
    回复润色（polish_reply 会动标点与引号），把 JSON 洗坏就无法解析了。
    这里只需要「发消息、收文本」这一步，_complete_once 正是那条传输边界。

    全程失败静默：抽不出记忆最多是学姐记性差一点，绝不能影响已经发出去的回复。
    """
    # 导入放在最前面：`should_extract` 是「要不要跑」的判据，必须在它之前就到位。
    # （这里曾经把导入写在判据之后，导致每一轮都必然 NameError——虽然被外层的
    # 静默兜底吞掉了，但提取其实一次都没跑成，长期记忆永远是空的。）
    from .agent.memory import (
        build_extract_user_text,
        get_card_store,
        parse_cards,
        should_extract,
    )
    from .agent.memory.extractor import (
        EXTRACT_RETRY_PROMPT,
        EXTRACT_SYSTEM_PROMPT,
        has_json_array,
    )
    from .llm import get_llm

    session_id = getattr(agent_context, "session_id", "") or ""

    def _skip(reason: str, **fields: Any) -> None:
        """记一条「这轮为什么没提取」。

        为什么要给「不做事」专门打日志：本函数全程静默，只要某个前提长期
        不成立，功能就会安静地什么都不做。这个函数已经因此藏过一次
        NameError（见函数开头注释），不能再让它无声无息。
        """
        log_event(
            "agent.memory_extract_skipped",
            reason=reason,
            session_id=session_id or None,
            **fields,
        )

    # 为什么要回退到 _resolve_config：AgentContext.config 来自 build_agent_context
    # 的 extra.get("config")，而往 ctx.extra 里塞 config 的只有 core/shadow.py 的
    # 比对路径（gateway 根本不引用它）。正常回复路径上它恒为 None——之前这里
    # 读不到就直接 return，提取因此从来没有真正发生过。
    cfg = getattr(agent_context, "config", None) or _resolve_config(agent_context)
    if cfg is None:
        _skip("no_config")
        return
    if not bool(getattr(cfg, "memory_extract_enable", True)):
        _skip("disabled")
        return
    if not history:
        _skip("no_history")
        return

    # 一轮 = user + assistant 两条。用整除得到「已经聊了几轮」。
    turn_count = len(history) // 2
    every = int(getattr(cfg, "memory_extract_every", 6))
    if not should_extract(turn_count, every=every):
        _skip("not_due", turns=turn_count, every=every)
        return
    # 本函数**自己**兜底，不依赖调用方。它承诺的是「全程失败静默」，
    # 那就得是真的全程：模型超时、JSON 解析炸、磁盘写不进去，
    # 都只记一条 warning。否则一旦有人换个调用点，这里就会把异常
    # 抛回发送链路——而那时回复早已发出去了。
    try:
        llm = get_llm(cfg)
        messages = [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": build_extract_user_text(history[-12:])},
        ]
        # max_tokens 给到 1000：500 会把「先推理再给 JSON」的输出拦腰截断，
        # 实测抓到过尾句停在半句话的回复，那种一定解析不出卡片。
        try:
            completion = await llm._complete_once(
                messages, max_tokens=1000, timeout=45.0
            )
        except Exception as exc:  # noqa: BLE001
            # 上游偶发「吐空回复」（_complete_once 会把它包成 LLMError）：
            # 这跟配置错不是一回事，再问一次通常就正常。不重试的代价是这一轮
            # 记忆永久丢失——下一次要再等 memory_extract_every 轮才触发。
            from .llm import LLMError as _LLMError

            if not isinstance(exc, _LLMError):
                raise
            log_event(
                "agent.memory_extract_retry",
                session_id=session_id or None,
                chars=0,
                reason="empty_completion",
            )
            completion = await llm._complete_once(
                messages, max_tokens=1000, timeout=45.0
            )
        # _complete_once 的真实返回是 _Completion（正文在 .text），但测试替身
        # 可能直接返回字符串。两种都要认，否则解析会静默拿到空串。
        raw = getattr(completion, "text", None)
        if not isinstance(raw, str):
            raw = completion if isinstance(completion, str) else ""
        cards = parse_cards(raw)
        # 「解析不出卡片」有两种原因，必须分开：模型回 [] 是合格输出（就是没什么
        # 值得记），回一段英文推理才是真失败。只有后者值得重试——否则每个无需
        # 记忆的轮次都会白烧一次调用，而那才是绝大多数情况。
        retried = False
        if not cards and not has_json_array(raw):
            # 重试一次：抽取是后台旁路，多花一次调用好过这一轮记忆永久丢失——
            # 轮数计数已经走到该抽的位置，下一次要再等 every 轮才会重新触发。
            retried = True
            retry_messages = list(messages)
            if raw.strip():
                # 把违规输出放回上下文，它才有机会「看到自己错在哪」。截到 2000
                # 字符：异常输出可能很长，不能让它把重试的上下文挤爆。
                retry_messages.append({"role": "assistant", "content": raw[:2000]})
            retry_messages.append({"role": "user", "content": EXTRACT_RETRY_PROMPT})
            log_event(
                "agent.memory_extract_retry",
                session_id=session_id or None,
                chars=len(raw),
            )
            completion = await llm._complete_once(
                retry_messages, max_tokens=1000, timeout=45.0
            )
            raw = getattr(completion, "text", None)
            if not isinstance(raw, str):
                raw = completion if isinstance(completion, str) else ""
            cards = parse_cards(raw)
        if not cards:
            _skip("no_cards", retried=retried)
            return

        store = get_card_store(
            cfg.memory_cards_dir, max_cards=max(8, int(cfg.memory_cards_max) * 4)
        )
        group_id = str(getattr(agent_context, "group_id", "") or "")
        user_id = str(getattr(agent_context, "user_id", "") or "")
        written = 0
        for card in cards:
            # 个人事实进 user 作用域（跨群共享），群内共同事实进 group 作用域。
            # 这里不做判断，两边都写：检索时按 card_id 去重，重复不浪费概览位。
            card.evidence = card.evidence or session_id
            if user_id:
                store.add(f"user_{user_id}", card)
                written += 1
            if group_id:
                store.add(f"group_{group_id}", card)
                written += 1
        log_event(
            "agent.memory_cards_written",
            count=len(cards),
            written=written,
            turns=turn_count,
            session_id=session_id or None,
        )
    except Exception as exc:  # noqa: BLE001 — 记忆抽取失败绝不能影响已发出的回复
        log_event(
            "agent.memory_extract_failed",
            level="warning",
            session_id=session_id or None,
            error=f"{type(exc).__name__}: {exc}",
        )


def build_agent_context(
    ctx: Any,
    trigger: Any = None,
    *,
    reply_message: Any = None,
    image_urls: list[str] | None = None,
    attachment_texts: list[tuple[str, str]] | None = None,
    forward_text: str = "",
) -> Any:
    """把 gateway 的 MessageContext 转成 AgentContext。

    Phase 5 起这是**唯一的转换点**：Agent 层只认识 AgentContext。

    记忆与引用为什么在这里装配：AgentRuntime 只负责「怎么问模型」，
    既不碰存储、也不认识 OneBot 事件；而这两件事都需要 config、session
    和原始 event，桥接层恰好三样都有。用的也是重构时定下的那套
    MemoryFacade 接口，不另起一套逻辑。

    Args:
        reply_message: 已由 resolve_reply_message 解析好的被引用消息。
            传 None 时退回 event.reply 自带的 message 段。
    """
    from .agent.context import AgentContext

    trace_id = getattr(ctx, "trace_id", "") or ""
    raw_extra = getattr(ctx, "extra", None)
    extra = dict(raw_extra) if isinstance(raw_extra, dict) else {}
    session_id = getattr(ctx, "session_id", "") or ""
    plain_text = getattr(ctx, "plain_text", "") or ""
    # 召唤词是「叫她的方式」，不是「要说的话」：交给模型前先删掉，
    # 否则她会把「喵喵」当成对话内容的一部分来回应。
    # 注意 @ 不需要处理——OneBot 的 at 是独立段，get_plaintext() 会整个丢弃。
    plain_text = _strip_summon_words(ctx, plain_text)

    # 知识库：只有真正配置好时才把 service 放进 extra。
    # knowledge 工具的可见性就挂在这个键上 —— 不放 = 模型看不到该工具，
    # 也就不会浪费一轮去调一个必然查不到的接口。
    # 整段包在 try 里：知识库装配失败绝不能阻断正常回复（第二十六节精神）。
    try:
        from .knowledge_setup import get_knowledge_service, knowledge_extra

        cfg = _resolve_config(ctx)
        if cfg is not None:
            extra.update(knowledge_extra(cfg, get_knowledge_service(cfg)))
    except Exception as exc:  # noqa: BLE001
        log_event("knowledge.inject_failed", level="warning", error=type(exc).__name__)

    # 附件：这里只采集「有哪些文件」的元数据，**不下载**。
    # 真正下载发生在模型决定调用 read_doc 的那一刻 —— 否则每条带附件的
    # 消息都会先白下载一遍（大 PDF 尤其亏），而且模型可能压根不需要读它。
    # 与图片不同：图片必须在这里解析好，因为模型看图和看文字是同一次调用。
    try:
        from .attachments import build_attachments

        raw_event = getattr(ctx, "raw_event", None)
        raw_message = getattr(raw_event, "message", None)
        if raw_message is not None:
            found = build_attachments(
                raw_message,
                group_id=str(
                    getattr(ctx, "group_id", "")
                    or getattr(raw_event, "group_id", "")
                    or ""
                ),
                user_id=str(
                    getattr(ctx, "user_id", "")
                    or getattr(raw_event, "user_id", "")
                    or ""
                ),
            )
            if found:
                extra["attachments"] = [a.to_brief() for a in found]
    except Exception as exc:  # noqa: BLE001 — 附件识别失败不能拦掉回复
        log_event(
            "attachment.collect_failed", level="warning", error=type(exc).__name__
        )

    # 引用消息：合成「【引用消息】谁：内容 + 【我现在说的】本句」。
    user_text = _compose_user_text(
        ctx,
        plain_text,
        reply_message,
        attachment_texts=attachment_texts,
        forward_text=forward_text,
    )

    # 图片由调用方（异步的 handle_agent_message）解析好传进来：本函数是同步的，
    # 而下载图片是网络 IO。状态栏要用到「本轮带图」这个事实，所以必须在
    # _build_status_bar 之前就塞进 extra。
    if image_urls:
        extra["image_urls"] = list(image_urls)

    # 意图文本：recovery（漏写 marker 时的兜底重试）靠这个键判断「用户是否
    # 明确要求了某能力」。runtime 路径此前从未写入，_try_recovery 读到空串就
    # 直接返回 None，等于 CARD/MUTE/DRAW/BROWSE 四套补救全部失效。legacy 路径
    # 由 legacy_context.py 塞的是同一个键，这里补齐以保持两条链行为一致。
    try:
        from .reply_quote import user_intent_text

        extra["intent_text"] = user_intent_text(plain_text, user_text)
    except Exception:  # noqa: BLE001 — 补救键缺失绝不能阻断正常回复
        pass

    # 会话历史 + 召回 + 好感度 / 内心状态 / 群风格 / 人物印象，一次装配。
    # runtime 此前完全没接这块，导致模型每次对着一条孤立消息作答、
    # 新对话从不落盘、四个 brief 恒为空串。
    history, _ = _assemble_memory(
        ctx,
        session_id,
        user_text,
        name=getattr(ctx, "actor", "") or "",
        extra=extra,
    )

    # 状态栏最后算：它要反映上面几步的结果（比如有没有图）。
    status = _build_status_bar(ctx, extra)
    if status:
        extra["agent_status"] = status

    return AgentContext(
        session_id=session_id,
        message_context=ctx,
        trigger_reason=getattr(trigger, "kind", None)
        and getattr(trigger.kind, "value", "")
        or getattr(trigger, "reason", "")
        or "",
        # 只取 extra["config"] 是不够的：生产路径里没有任何地方往 extra 塞 config
        # （只有 core/shadow.py 的比对路径会塞），于是 AgentContext.config 恒为
        # None → _visible_draw / _visible_browse 在 cfg is None 时直接判不可见，
        # 模型提示词里根本没有【生图能力】这一段，表现就是「学姐不知道自己能生
        # 图」；而商店缩略图走 shop.py 直连绘图 API，不经过模型，所以商店照样
        # 有图。这里回退到全局配置，把这条链路接回来。
        config=extra.get("config") or _resolve_config(ctx),
        interrupt=not bool(getattr(trigger, "forced", False)),
        private_chat=bool(getattr(ctx, "is_private", False)),
        trace_id=trace_id,
        history=history,
        user_text=user_text,
        extra=extra,
    )


def _legacy_handler_factory():
    """构造 legacy 执行器：调用现有 `_handle_chat`。

    这是 legacy 在 Phase 5 的唯一入口。所有 legacy 细节
    （raw_event、bot、force_mention）都封装在这里，
    AgentEntry 与 Router 都不需要知道。
    """

    async def _legacy(context: Any) -> Any:
        from .handlers import _handle_chat

        bot = getattr(context, "bot", None)
        raw_event = getattr(context, "raw_event", None)
        if bot is None or raw_event is None:
            log_event(
                "agent.legacy_skipped",
                level="warning",
                reason="missing_bot_or_event",
                session_id=getattr(context, "session_id", "") or None,
            )
            return None
        # forced 语义：非插嘴即"必回"
        await _handle_chat(bot, raw_event, force_mention=not context.interrupt)
        return None

    return _legacy


def _get_agent_entry():
    """取全局 AgentEntry（首次调用时构建）。"""
    from .core.runtime import get_runtime

    runtime = get_runtime()
    entry = getattr(runtime, "agent_entry", None)
    if entry is None:
        from .agent.entry import AgentEntry

        entry = AgentEntry(
            runtime=getattr(runtime, "agent", None),
            legacy_handler=_legacy_handler_factory(),
        )
        runtime.agent_entry = entry
    return entry


def _reply_send_options() -> dict[str, Any]:
    """构造发送选项（与 legacy `handlers._reply` 保持同一套）。

    为什么不直接复用 `handlers._reply`：那是 legacy 的私有函数，runtime
    路径依赖它会把两条链路重新耦回一起，而 Phase 5 的目标正是让 runtime
    不依赖 legacy。两边真正共用的是底层的 `send_text_reply`；选项集合各自
    显式列出，改动时能被测试发现。
    """
    from .config_runtime.runtime import get_config
    from .persona import get_senpai_name

    cfg = get_config()
    return {
        "reply_max_chars": cfg.reply_max_chars,
        "reply_chunk_chars": cfg.reply_chunk_chars,
        "nickname": get_senpai_name(),
        "humanize_send": bool(cfg.humanize_send),
        "typing_delay_ms_min": cfg.typing_delay_ms_min,
        "typing_delay_ms_max": cfg.typing_delay_ms_max,
        "bubble_max": cfg.bubble_max,
        "bubble_min_chars": cfg.bubble_min_chars,
        "bubble_prefer_chars": cfg.bubble_prefer_chars,
        "bubble_total_max_chars": cfg.bubble_total_max_chars,
    }


async def _apply_chat_affection(agent_context: Any, result: Any) -> None:
    """给「被点名的一次正常对话」加好感，并把升级脚注拼进**待发**正文。

    逐条对齐旧编排 finalize_response 的语义（旧编排已于 2026-10 删除，
    这里保留它的行为）：只在被点名（mention）、
    没被打断时加；复用同一个 finalize_affection_gain 做脚注与群头衔同步，
    always_show=False 也和它一致（只有真升级才提示）。

    为什么必须在发送前做：脚注要跟着这条回复一起发出去，发送后再改 text 就没意义。
    为什么作用域不在这里定：affection_scope 决定群聊**只有签到群**才加好感，而
    checkin_enable 关着时 is_checkin_group 恒为 False——群里不加好感是配置使然；
    私聊则跟随 enable_private。
    """
    from .agent.memory_facade import MemoryFacade
    from .config import get_config
    from .core.domain import affection_scope, finalize_affection_gain

    cfg = getattr(agent_context, "config", None) or get_config()
    if not bool(getattr(cfg, "affection_enable", False)):
        return
    if getattr(agent_context, "trigger_reason", "") != "mention":
        return
    if bool(getattr(agent_context, "interrupt", False)):
        return
    raw_event = getattr(agent_context, "raw_event", None)
    user_id = str(getattr(agent_context, "user_id", "") or "")
    if raw_event is None or not user_id:
        return

    scope = affection_scope(cfg, raw_event)
    if not scope:
        return

    delta = int(getattr(cfg, "affection_gain_chat", 0) or 0)
    if delta <= 0:
        return
    aff = MemoryFacade(config=cfg).record_interaction(
        scope,
        user_id,
        delta,
        str(getattr(agent_context, "actor", "") or user_id),
        cfg,
    )

    bot = getattr(agent_context, "bot", None)
    text = getattr(result, "text", "") or ""
    if bot is None or not text:
        return
    group_id = getattr(agent_context, "group_id", None)
    result.text = await finalize_affection_gain(
        bot,
        scope=scope,
        user_id=int(user_id) if user_id.isdigit() else user_id,
        text=text,
        result=aff,
        config=cfg,
        always_show=False,
        group_id=int(group_id) if str(group_id or "").isdigit() else None,
    )

async def _deliver_runtime_reply(agent_context: Any, outcome: Any) -> None:
    """把 RUNTIME 产出的 AgentResult 真正发出去。

    这里曾经是整条链路唯一的缺口：`AgentRuntime` 只负责**生成**回复，
    发送被设计成 `ResponsePipeline` 的职责，但桥接层当初只接了
    「基础设施失败 → 发安全文案」那一支，**成功分支没有接线**。
    结果是模型正常产出（日志里 `reply_len=164` 好端端的），用户却什么都
    收不到，而且没有任何报错——因为代码根本没走到发送。
    """
    from .agent.response import ResponsePipeline

    # 好感度：这条写入在 runtime 模式下从来没人调用（唯一调用者在跑不起来的
    # 旧编排里，该文件已于 2026-10 删除），affection/ 曾从 9 月起就冻住，而每轮还在
    # 读它。必须自己 try：写入失败若冒到下面那个 except，会被当成「发送失败」
    # 把这一轮回复整条吞掉。
    try:
        await _apply_chat_affection(agent_context, outcome.result)
    except Exception as exc:  # noqa: BLE001 — 好感度失败不能影响这一轮回复
        log_event(
            "agent.affection_gain_failed",
            level="warning",
            session_id=getattr(agent_context, "session_id", "") or None,
            error=f"{type(exc).__name__}: {exc}",
        )

    try:
        delivered = await ResponsePipeline().deliver(
            outcome.result,
            agent_context,
            reply_options=_reply_send_options(),
        )
    except Exception as exc:  # noqa: BLE001 — 发送异常不能静默吞掉
        log_event(
            "agent.reply_deliver_failed",
            level="error",
            session_id=getattr(agent_context, "session_id", "") or None,
            error=f"{type(exc).__name__}: {exc}",
        )
        return

    if not delivered.sent:
        # ResponsePipeline 内部已记 reply.failed；这里补一条带 session 的记录，
        # 便于把「没收到回复」的投诉直接对上具体请求。
        log_event(
            "agent.reply_not_sent",
            level="warning",
            session_id=getattr(agent_context, "session_id", "") or None,
            error=delivered.error or None,
        )
        return

    # 落盘要用**真正发出去**的那条。response.py 发送前先 `plan.text =
    # polisher(plan.text)`，ResponseOutcome.text 才是用户看到的文本；存润色前
    # 的原文，等于把 markdown 排版通过历史召回又喂回 prompt，和去 AI 味对冲。
    sent_text = (
        getattr(delivered, "text", "") or getattr(outcome.result, "text", "") or ""
    )

    # 确认发出去之后才落盘，下一轮就能带上这轮上下文。
    _record_turn(
        agent_context,
        _history_user_text(agent_context),
        sent_text,
    )

    # 内心状态结算。和 _record_turn 一样，必须等确认发出去了才做，否则
    # 「没送出去的回复」也会扣她精力。
    # 为什么写在这里：这个写入者原先唯一的调用者在跑不起来的旧编排里
    # （已于 2026-10 删除），导致状态曾从 9 月起就冻住，而每一轮还在把它
    # 读进 prompt——开关开着，喂的却是一个月前的情绪。
    try:
        from .config import get_config
        from .agent.memory_facade import MemoryFacade
        from .inner_state import scope_key as _inner_scope_key

        cfg = getattr(agent_context, "config", None) or get_config()
        if bool(getattr(cfg, "inner_state_enable", True)):
            key = _inner_scope_key(
                per_group=bool(getattr(cfg, "inner_state_per_group", False)),
                group_id=getattr(agent_context, "group_id", "") or None,
                private=bool(getattr(agent_context, "private_chat", False)),
            )
            MemoryFacade(config=cfg).update_inner_state(
                key,
                reply_len=len(sent_text),
                mentioned=getattr(agent_context, "trigger_reason", "") == "mention",
                interrupt=bool(getattr(agent_context, "interrupt", False)),
                private_chat=bool(getattr(agent_context, "private_chat", False)),
                config=cfg,
            )
    except Exception as exc:  # noqa: BLE001 — 状态更新失败不能影响已发出的回复
        log_event(
            "agent.inner_state_update_failed",
            level="warning",
            session_id=getattr(agent_context, "session_id", "") or None,
            error=f"{type(exc).__name__}: {exc}",
        )

    # 长期记忆提取（第三章的「后台提取候选」）。只有当轮数到点时才真的调用
    # 模型。_extract_memory_cards 自己兜底，这里的 try 是读取历史那段的
    # backstop（历史读不出来也不该影响已发出的回复）。
    try:
        session_id = getattr(agent_context, "session_id", "") or ""
        if session_id:
            from .memory import ChatMemory
            from .config import get_config

            cfg = getattr(agent_context, "config", None) or get_config()
            history = ChatMemory(cfg.memory_dir).load_all(session_id)
            await _extract_memory_cards(agent_context, history)
    except Exception as exc:  # noqa: BLE001
        log_event(
            "agent.memory_extract_failed",
            level="warning",
            session_id=getattr(agent_context, "session_id", "") or None,
            error=type(exc).__name__,
        )


async def _resolve_forward_text(ctx: Any, reply_message: Any = None) -> str:
    """把「合并转发」的聊天记录取出来，渲染成模型能读的文本。

    为什么不等模型调工具：用户转一段聊天记录过来，记录内容才是重点。
    只把 CQ 码丢给模型，她只能瞎猜——和「发了文档就该去读」是同一个道理。

    为什么在异步侧做：build_agent_context 是同步函数，而 get_forward_msg
    是网络 IO。与 _resolve_image_urls 的分工完全一致。

    两个来源都要看。卡片可以直接发过来（forward 段在本轮消息里），也可以
    被引用（用户「回复卡片 + 打字」，此时 forward 段在被引用的那条消息里）。
    只认本轮消息的话，引用场景下学姐拿到的只是一个 [forward] 占位符，
    等于卡片内容从来没进过上下文。

    失败静默：取不到就当没有转发，绝不能因此拦掉文字回复。
    """
    raw_event = getattr(ctx, "raw_event", None)
    from .forwards import extract_forward_segments, resolve_forward_text

    segments = extract_forward_segments(getattr(raw_event, "message", None))
    # 本轮消息里没有才看引用：两者都有时以本轮为准，避免同一张卡片算两遍
    if not segments and reply_message is not None:
        segments = extract_forward_segments(reply_message)
    if not segments:
        return ""
    return await resolve_forward_text(
        getattr(ctx, "bot", None),
        segments,
        sender_hint=str(getattr(ctx, "actor", "") or ""),
    )


async def handle_agent_message(ctx: Any, *, trigger: Any = None) -> bool:
    """把一条已判定"该进 AI"的消息交给 AgentEntry。

    Returns:
        是否被接受执行（背压拒绝时返回 False）。
    """
    raw_event = getattr(ctx, "raw_event", None)
    bot = getattr(ctx, "bot", None)
    if raw_event is None or bot is None:
        log_event(
            "agent.bridge_skipped",
            level="warning",
            reason="missing_raw_event_or_bot",
            session_id=getattr(ctx, "session_id", "") or None,
        )
        return False

    from .gateway.context import agent_session_key

    key = agent_session_key(ctx)
    entry = _get_agent_entry()

    # 引用消息有时只有 id、没有正文（图片/长消息会被 QQ 折叠），
    # 先用 get_msg 补齐再装配上下文，否则学姐看到的是空引用。
    try:
        from .reply_quote import resolve_reply_message

        reply_message = await resolve_reply_message(bot, raw_event)
    except Exception as exc:  # noqa: BLE001 — 拉引用失败不影响正常问答
        log_event(
            "agent.reply_resolve_failed",
            level="warning",
            session_id=getattr(ctx, "session_id", "") or None,
            error=type(exc).__name__,
        )
        reply_message = None

    # 图片也要在这里解析好：build_agent_context 是同步函数，而下载图片是 IO。
    # 取不到就当没图，绝不因为一张图挂了而让文字回复也发不出去。
    try:
        image_urls = await _resolve_image_urls(ctx, reply_message=reply_message)
    except Exception as exc:  # noqa: BLE001
        log_event(
            "agent.vision_resolve_failed",
            level="warning",
            session_id=getattr(ctx, "session_id", "") or None,
            error=type(exc).__name__,
        )
        image_urls = []

    # 附件正文也在这里读好：build_agent_context 是同步的，读文件要网络 IO。
    # 用户要求「发了文档就该去读」，所以这里是**主动读**，不等模型调工具。
    try:
        attachment_texts = await _resolve_attachment_texts(ctx)
    except Exception as exc:  # noqa: BLE001
        log_event(
            "attachment.auto_read_error",
            level="warning",
            session_id=getattr(ctx, "session_id", "") or None,
            error=type(exc).__name__,
        )
        attachment_texts = []

    # 转发的聊天记录也在这里取：同样因为 build_agent_context 是同步的，
    # 而调 get_forward_msg 要网络 IO。
    try:
        forward_text = await _resolve_forward_text(ctx, reply_message)
    except Exception as exc:  # noqa: BLE001
        log_event(
            "forward.resolve_failed",
            level="warning",
            session_id=getattr(ctx, "session_id", "") or None,
            error=type(exc).__name__,
        )
        forward_text = ""

    agent_context = build_agent_context(
        ctx,
        trigger,
        reply_message=reply_message,
        image_urls=image_urls,
        attachment_texts=attachment_texts,
        forward_text=forward_text,
        # 已经自动读过正文了，附件的元数据只用于状态栏与 read_doc 的二次调用
    )

    def _on_reject(session_key: str, reason: str) -> None:
        log_event(
            "agent.session_rejected",
            level="warning",
            session_key=session_key,
            reason=reason,
        )

    try:
        from .core.session import get_executor

        executor = get_executor()
        if getattr(executor, "_on_reject", None) is None:
            executor._on_reject = _on_reject
    except Exception:  # noqa: BLE001
        executor = None

    async def _run() -> None:
        outcome = await entry.handle(agent_context)

        # RUNTIME 只产出 AgentResult、**不发送**，发送统一由 ResponsePipeline
        # 负责。legacy 分支自己已经发过（used_runtime=False），必须区分，
        # 否则会导致同一条回复被发两遍。
        if outcome.used_runtime and outcome.result is not None:
            await _deliver_runtime_reply(agent_context, outcome)
            return

        # Phase 5.5（第十八节）：RUNTIME 基础设施失败时，不再跑完整 legacy，
        # 而是发一句安全文案（复用现有错误话术，不新写人格文案）。
        if entry.need_safe_fallback(outcome):
            if agent_context.bot is None or agent_context.raw_event is None:
                return
            from .reply_send import send_text_reply
            from .config_runtime.runtime import get_config

            cfg = get_config()
            try:
                await send_text_reply(
                    agent_context.bot,
                    agent_context.raw_event,
                    entry.safe_error_text(agent_context),
                    reply_max_chars=cfg.reply_max_chars,
                    reply_chunk_chars=cfg.reply_chunk_chars,
                    forward_min_chars=cfg.forward_min_chars,
                    forward_node_chars=cfg.forward_node_chars,
                )
            except Exception:  # noqa: BLE001 — 兜底发送失败也不再重试
                log_event(
                    "agent.safe_fallback_send_failed",
                    level="warning",
                    session_id=agent_context.session_id or None,
                )

    if executor is None:
        # 无执行器时直接跑（保持行为可用，不因为基础设施缺失而丢消息）
        await _run()
        return True

    result = await executor.submit(key, _run)
    if not result.accepted:
        # 背压拒绝：不重试、不排队，避免同一用户刷屏拖垮机器人
        log_event(
            "agent.message_dropped",
            level="warning",
            session_key=key,
            queued=result.queued,
            reason=result.reject_reason,
        )
        return False
    return True


__all__ = ["handle_agent_message", "build_agent_context"]
