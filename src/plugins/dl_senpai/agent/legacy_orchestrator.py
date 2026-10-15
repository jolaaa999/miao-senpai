"""LegacyOrchestrator：旧 Agent 编排方式（Phase 6.5）。

第一节目标架构：

    AgentEntry
      ├── RuntimeOrchestrator → AgentRuntime
      └── LegacyOrchestrator  → SenpaiLLM legacy flow   ← 本模块

第二节（Legacy 的定义）：LEGACY 不再意味着"另一套完整机器人"，
而只是"旧 Agent orchestration"。以下能力全部与 RUNTIME **共享**：

    MemoryFacade / ToolRegistry / MarkerToolAdapter
    ToolExecutor / PermissionService / RecoveryPolicy
    ResponsePipeline / Observability

本模块只保留 legacy 独有的东西：
    - 旧 prompt / context 编排
    - SenpaiLLM.chat 的调用方式（单轮 + 多轮 grounding）
    - legacy 单轮处理流程
    - 冷却与意图相关流程

迁移原则（第一/二节）：**Move first**，函数体一字未改。
Phase 6.6 更新：本模块**不再依赖 handlers**。
    9 个原共享 helper 已抽到明确的归属模块：
        gateway/platform.py  —— plain_text / sender_name / resolve_session
                                / is_to_me / reply
        core/domain.py       —— feature_enabled / affection_scope
                                / collect_stickers / finalize_affection_gain
    依赖方向变为 legacy → gateway|core，而不是 legacy → handlers。
"""

from __future__ import annotations

import re as _re
import random as _random
import time
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)

from ..config import PluginConfig
from ..group_card import (
    action_summary_for_memory,
    bot_can_manage_cards,
    build_card_candidates,
    card_failure_note,
    card_missing_marker_note,
    extract_at_user_ids,
    filter_actions,
    format_candidates_for_prompt,
    looks_like_card_request,
)
from ..group_features import observe_group
from ..group_mute import (
    can_emit_mute,
    filter_mute_actions,
    format_mute_ack,
    looks_like_mute_request,
    mute_block_note,
    mute_summary_for_memory,
    resolve_explicit_mute,
)
from ..image_gen import (
    classify_senpai_draw,
    draw_block_note,
    draw_failure_note,
    resolve_draw_prompt,
    wants_senpai_image,
)
from ..browser_agent import (
    browse_block_note,
    browse_failure_note,
    browser_configured,
    resolve_browse_tasks,
    wants_browser_help,
)
from ..llm import LLMError, get_llm
from ..observability.logging import log_event
from ..person_memory import ImpressionUpdate, extract_traits_from_user_text
from ..persona import get_senpai_name
from ..reply_quote import (
    build_chat_text_with_reply,
    resolve_reply_message,
    user_intent_text,
)
from ..reply_send import _send_image_with_retry
from ..stickers import extract_sticker_segments
from ..style import polish_reply, strip_unapplied_mute_claims
from ..trigger import AttentionWindow, InterruptCooldown, decide_trigger
from ..vision import (
    extract_vision_image_segments,
    format_user_text_for_chat,
    format_user_text_for_memory,
    resolve_vision_images,
)
from ..group_card import CardCooldown
from ..image_gen import DrawCooldown
from ..browser_agent import BrowseCooldown
from ..group_mute import MuteCooldown
from ..inner_state import scope_key as inner_scope_key
from ..voice import (
    extract_voice_quote_from_user,
    looks_like_voice_request,
    text_for_voice,
)
from .memory_facade import MemoryRef
from .tool_dispatch import (
    SharedToolDispatcher,
    tool_calls_from_card_actions,
    tool_calls_from_mute_actions,
)

# 冷却器实例。
#
# 经审计确认：这 6 个冷却器的**全部**使用点都在 legacy 编排内
# （checkin / shop / affection 命令不使用），因此放在本模块，
# 不需要提取到共享模块。
_cooldown = InterruptCooldown()
_attention = AttentionWindow()
_card_cooldown = CardCooldown()
_mute_cooldown = MuteCooldown()
_draw_cooldown = DrawCooldown()
_browse_cooldown = BrowseCooldown()


# Phase 6.6：平台与领域 helper 已从 handlers 抽到明确的归属模块。
# 依赖方向变为 legacy → gateway.platform / core.domain，
# 不再是 legacy → handlers。
from ..core.domain import (
    affection_scope,
    collect_stickers,
    feature_enabled,
    finalize_affection_gain,
)
from ..gateway.platform import (
    is_to_me,
    plain_text,
    reply,
    resolve_session,
    sender_name,
)


# 函数体里用的是裸名 `re`（从 handlers 原样搬来），这里绑定同名别名，
# 保证搬运后**一个字符都不用改**。
re = _re





class LegacyOrchestrator:
    """旧 Agent 编排（第一节）。

    与 RuntimeOrchestrator 的差异**仅限 orchestration 层**：
    它自己组装 prompt、自己调 SenpaiLLM、自己走单轮流程；
    但 marker 解析、工具执行、权限、记忆、恢复全部复用共享实现。

    为什么不用 `AgentContext` 而是收 (bot, event, ...)：
    legacy 编排深度依赖 OneBot 的 Bot / MessageEvent 对象（发消息、
    取群成员、读 at 列表），而 AgentContext 是平台无关的。
    Phase 6A 已建立 `legacy_context.build_legacy_context()` 在需要时
    把 (bot, event) 转成 AgentContext 供共享 ToolExecutor 使用 ——
    这就是两者之间的桥。
    """


    async def run(
        self,
        bot: Bot,
        event: MessageEvent,
        *,
        force_mention: bool,
        config: PluginConfig,
    ) -> None:
        """Legacy 编排入口（Phase 6.6 拆分后）。

        本方法只做 orchestration：构造 state 并顺序调用 16 个语义阶段。
        具体业务在各自阶段方法里，便于独立测试与演进。

        阶段返回 False 表示中止（会话无法解析 / 无需回复 / 前置条件不满足）。

        **当前不可用（2026-10 审计确认）**：Phase 6.6 的 state 重构没有做完。
        具体是两处硬伤：

        1. `LegacyRunState` 从未定义（全文件 16 处引用它，但既没有定义也没有
           导入），所以构造 state 那一步必然抛 NameError。
        2. 各阶段方法读的是**未定义的局部变量**，不是 `state.X`——例如
           `invoke_model` 里的 `session_id` / `chat_text` / `sender` / `llm`
           / `history` 都没有来源。这是机械改写时把 `state.` 前缀删掉留下的。

        为什么不就地修：它是**已被 runtime 取代**的旧实现，重写 16 个阶段
        的取值路径属于复活死代码，收益为负。

        为什么还要抛一条明确的异常而不是放着 NameError：NameError 会让人
        以为只是少了一行定义，排查方向完全错了。这里一次说清。

        需要回滚时怎么办：用 SHADOW 模式（新旧都跑，旧的只读不写副作用），
        或直接把 `DL_SENPAI_AGENT_MODE` 设回 `runtime`。
        另外 `AgentEntry._run_legacy` 已经能在本方法抛异常时降级到 runtime，
        所以线上不会因为这条异常而静默不回话。
        """
        raise RuntimeError(
            "legacy_orchestrator_unavailable: DL_SENPAI_AGENT_MODE=legacy 不可用。"
            "该模块在 Phase 6.6 的 state 重构中未完成（缺 LegacyRunState 定义，"
            "且各阶段读取未定义的局部变量）。请改用 runtime 或 shadow 模式。"
        )

        # Phase 6.6 第四十七节：明确标记编排器身份，便于与 runtime 区分。
        # 第四十八节：只记 session 标识，**不记完整 prompt / 用户原文**。
        log_event(
            "agent.orchestrator",
            orchestrator="legacy",
            session_id=str(getattr(event, "group_id", "") or getattr(event, "user_id", "")),
        )

        for stage in (
            self.resolve_session_stage,
            self.prepare_message_context,
            self.prepare_media_context,
            self.prepare_interaction_context,
            self.prepare_memory_context,
            self.decide_trigger_stage,
            self.validate_preconditions,
            self.prepare_prompt_context,
            self.prepare_optional_context,
            self.invoke_model,
            self.interpret_result,
            self.apply_card_and_mute,
            self.prepare_media_response,
            self.prepare_draw_and_browse,
            self.apply_state_updates,
            self.finalize_response,
        ):
            # 第三十七节：每个关键阶段记耗时（结构化 duration log，
            # 不引入 Prometheus）。trace_id 由 log_event 自动携带。
            started = time.perf_counter()
            cont = await stage(state, bot, event, config=config)
            state.stage_timings[stage.__name__] = round(
                (time.perf_counter() - started) * 1000, 2
            )
            if not cont:
                break

        log_event(
            "legacy.stage_timings",
            total_ms=round(sum(state.stage_timings.values()), 2),
            stage_count=len(state.stage_timings),
            stages=state.stage_timings,
        )

    async def resolve_session_stage(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        resolved = resolve_session(event, config)
        if resolved is None:
            return False
        state.session_id, _ = resolved
        return True
    async def prepare_message_context(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        if isinstance(event, GroupMessageEvent):
            observe_group(event.group_id)
        state.text = plain_text(event)
        await collect_stickers(
            event,
            session_id=session_id,
            context_text=text,
            source_user=sender_name(event),
            config=config,
            bot=bot,
        )
        state.sticker_segments = extract_sticker_segments(event.message)
        state.sticker_count = len(state.sticker_segments)
        return True

    async def prepare_media_context(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        state.reply_message = await resolve_reply_message(bot, event)
        state.photo_segments: list[dict] = []
        if feature_enabled(config, event, "vision"):
            photo_segments.extend(extract_vision_image_segments(event.message))
            if reply_message is not None:
                photo_segments.extend(extract_vision_image_segments(reply_message))
        state.image_urls: list[str] = []
        if state.photo_segments:
            image_urls = await resolve_vision_images(
                bot,
                photo_segments,
                max_images=config.vision_max_images,
            )
        state.has_images = bool(state.photo_segments) or bool(state.image_urls)
        state.image_count = len(state.photo_segments)
        state.chat_text = format_user_text_for_chat(
            text,
            image_count=image_count,
            sticker_count=sticker_count,
        )
        state.chat_text = build_chat_text_with_reply(event, state.chat_text, reply_message=state.reply_message)
        # 引用的那条消息也过一遍过滤器：有人把设定贴出来、别人再引用它时，
        # 上面那次过滤只作用于本条消息，引用部分会从这里漏进去。
        # 对已替换过的文本再跑一次是幂等的：过滤器见到自己的占位文案会直接放行。
        from ..persona_filter import neutralize

        state.chat_text, _quoted_hit = neutralize(state.chat_text)
        return True

    async def prepare_interaction_context(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        state.intent_text = user_intent_text(state.text, state.chat_text)
        # intent_text 优先取**原始**文本（plain 非空就返回 plain），
        # 所以它会绕过 chat_text 上那道过滤。而它还要流向两个下游：
        # 性格特征兜底提取（可能把"我是XX"记成对方的长时印象）
        # 和联网搜索关键词（拿别的角色的设定去搜，结果又会带回来）。
        # 这里补一道，一处覆盖两条路。
        from ..persona_filter import neutralize

        state.intent_text, _intent_hit = neutralize(state.intent_text)
        followup_sticker = False
        if (
            not force_mention
            and not is_to_me(event)
            and sticker_count > 0
            and isinstance(event, GroupMessageEvent)
            and _attention.active(
                session_id,
                event.user_id,
                config.sticker_followup_sec,
            )
        ):
            force_mention = True
            followup_sticker = True
            if chat_text.startswith("（发了个表情"):
                chat_text = chat_text.replace("（发了个表情", "（接着找学姐，发了个表情", 1)
            elif chat_text:
                chat_text = f"（接着找学姐）{chat_text}"
            else:
                chat_text = "（接着找学姐，发了个表情）"
        if force_mention and not state.chat_text and not state.has_images and state.sticker_count == 0:
            chat_text = f"{get_senpai_name()}～"
        if not state.chat_text and not force_mention and not state.has_images and state.sticker_count == 0:
            return
        if (
            sticker_count > 0
            and not text.strip()
            and not has_images
            and not force_mention
        ):
            return
        state.is_mentioned = (
            force_mention
            or is_to_me(event)
            or isinstance(event, PrivateMessageEvent)
        )
        return True

    async def prepare_memory_context(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        if (
            feature_enabled(config, event, "group_style")
            and isinstance(event, GroupMessageEvent)
            and (text or "").strip()
        ):
            try:
                # 采样文本过一道过滤器：别人贴的角色设定不该被当成"本群说话风格"
                # 学进去 —— 那些词（猫娘、傲娇、凛）会进群风格词频，
                # 之后又通过风格提示词回来影响她。
                from ..persona_filter import neutralize as _pf_neutralize

                _style_text, _style_hit = _pf_neutralize(text or intent_text)
                if not _style_hit:
                    _facade.observe_group_text(
                        event.group_id, _style_text, config
                    )
            except Exception:  # noqa: BLE001
                logger.warning("dl_senpai group_style observe failed")
        state.inner_key = "global"
        state.interrupt_scale = 1.0
        state.inner_state_brief = ""
        if feature_enabled(config, event, "inner_state"):
            try:
                inner_key = inner_scope_key(
                    per_group=bool(config.inner_state_per_group),
                    group_id=event.group_id if isinstance(event, GroupMessageEvent) else None,
                    private=isinstance(event, PrivateMessageEvent),
                )
                istate = _facade.load_inner_state(inner_key, config)
                interrupt_scale = _facade.inner_state_interrupt_multiplier(istate)
                inner_state_brief = _facade.build_inner_state_brief(istate)
            except Exception:  # noqa: BLE001
                logger.warning("dl_senpai inner_state load failed")
        return True

    async def decide_trigger_stage(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        state.decision = decide_trigger(
            is_mentioned=is_mentioned,
            text=text or chat_text,
            session_id=session_id,
            interrupt_prob=config.interrupt_prob,
            cooldown_sec=config.interrupt_cooldown,
            min_msg_len=config.min_msg_len,
            keyword_boost=config.keyword_boost,
            cooldown=_cooldown,
            has_images=has_images,
            image_boost=config.image_interrupt_boost,
            interrupt_prob_scale=interrupt_scale,
        )
        if state.decision.kind == "none":
            return False
        if state.decision.kind == "mention" and isinstance(event, GroupMessageEvent):
            _attention.arm(session_id, event.user_id)
            if followup_sticker:
                logger.info(
                    f"dl_senpai sticker follow-up as mention "
                    f"session={session_id} user={event.user_id}"
                )
        state.interrupt = state.decision.kind == "interrupt"
        if state.interrupt:
            _cooldown.mark(session_id)
        return True
    async def validate_preconditions(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        if not config.api_configured():
            if is_mentioned:
                await reply(
                    bot,
                    event,
                    polish_reply("稍等一会啦，学姐这边还没收拾好，晚点再来找我哦～"),
                )
            return False
        if state.has_images and not state.image_urls:
            if is_mentioned:
                await reply(
                    bot,
                    event,
                    polish_reply(
                        "图学姐有点看不清呢……你再发一次，或者把文字也打出来好不好？"
                    ),
                )
            return False
        return True
    async def prepare_prompt_context(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        state.sender = sender_name(event)
        _facade = _get_memory_facade()
        _mem_ref = MemoryRef.for_session(state.session_id)
        history, memory_recall_brief = _facade.load_history_for_prompt(
            _mem_ref,
            intent_text or chat_text,
            config,
            enable_recall=feature_enabled(config, event, "memory_recall"),
        )
        llm = get_llm(config)
        state.group_style_brief = ""
        if feature_enabled(config, event, "group_style") and isinstance(event, GroupMessageEvent):
            try:
                group_style_brief = _facade.get_group_style(
                    event.group_id, config, short=bool(interrupt)
                )
            except Exception:  # noqa: BLE001
                logger.warning("dl_senpai group_style brief failed")
        state.card_enable = False
        state.mute_enable = False
        state.draw_enable = False
        state.browser_enable = False
        state.card_prompt = ""
        state.candidates = []
        state.need_mod = False
        state.local_infer = config.is_local_infer()
        if state.local_infer:
            # 本地微调模型吃不消工具指令，开着就会把候选名单/系统提示原样发出去
            logger.info("dl_senpai local infer: disable card/mute/draw/browser/voice extras")
        if isinstance(event, GroupMessageEvent) and not state.local_infer:
            if feature_enabled(config, event, "card") and (not interrupt or config.card_allow_on_interrupt):
                need_mod = True
            if feature_enabled(config, event, "mute") and (not interrupt or config.mute_allow_on_interrupt):
                need_mod = True
        if state.need_mod:
            if await bot_can_manage_cards(bot, event.group_id):
                candidates = await build_card_candidates(bot, event)
                if candidates:
                    card_prompt = format_candidates_for_prompt(candidates)
                    if feature_enabled(config, event, "card") and (
                        not interrupt or config.card_allow_on_interrupt
                    ):
                        card_enable = True
                    if feature_enabled(config, event, "mute") and (
                        not interrupt or config.mute_allow_on_interrupt
                    ):
                        mute_enable = True
        if (
            not local_infer
            and feature_enabled(config, event, "draw")
            and config.draw_configured()
            and (not interrupt or config.draw_allow_on_interrupt)
        ):
            draw_enable = True
        if (
            not local_infer
            and feature_enabled(config, event, "browser")
            and browser_configured(config)
            and (not interrupt or config.browser_allow_on_interrupt)
        ):
            browser_enable = True
        return True

    async def prepare_optional_context(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        state.trends_brief = ""
        state.trend_titles: list[str] = []
        if feature_enabled(config, event, "trends"):
            try:
                trends_brief, trend_titles = await _facade.get_trends(
                    config, interrupt=interrupt
                )
            except Exception:  # noqa: BLE001
                logger.warning("dl_senpai trends brief failed")
        state.affection_brief = ""
        state.aff_scope = affection_scope(config, event)
        if state.aff_scope and state.is_mentioned and not state.interrupt:
            affection_brief, _ = _facade.get_affection(
                aff_scope, str(event.user_id), config
            )
        state.person_memory_brief = ""
        state.person_memory_enable = feature_enabled(config, event, "person_memory")
        if state.person_memory_enable:
            # 每次互动都记下当前昵称，便于跨群认出同一个人
            person_memory_brief, _profile = _facade.get_person_profile(
                MemoryRef.for_user(str(event.user_id), group_id=str(event.group_id)),
                sender,
                intent_text or chat_text,
                config,
            )
        return True

    async def invoke_model(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        try:
            import time as _time

            t0 = _time.perf_counter()
            logger.info(
                f"dl_senpai LLM call session={session_id} "
                f"interrupt={interrupt} text_len={len(chat_text)}"
            )
            private_chat = isinstance(event, PrivateMessageEvent)
            result = await llm.chat(
                history,
                chat_text,
                interrupt=interrupt,
                sender_name=sender,
                image_urls=image_urls or None,
                card_enable=card_enable,
                card_candidates_prompt=card_prompt,
                mute_enable=mute_enable,
                trends_brief=trends_brief,
                trend_titles=trend_titles,
                affection_brief=affection_brief,
                person_memory_brief=person_memory_brief,
                person_memory_enable=person_memory_enable,
                draw_enable=draw_enable,
                browser_enable=browser_enable,
                private_chat=private_chat,
                plain_user_text=intent_text,
                search_enable=feature_enabled(config, event, "search"),
                fewshot_enable=bool(config.fewshot_enable),
                inner_state_brief=inner_state_brief if config.inner_state_enable else "",
                group_style_brief=group_style_brief,
                memory_recall_brief=memory_recall_brief,
            )
            was_searched = result.searched
            # [Phase 5.5 已删除] 4 段 recovery 内联实现（CARD/MUTE/DRAW/BROWSE）
            #
            # 原来这里是约 110 行、结构高度重复的「模型漏写 marker 就补救一轮」逻辑。
            # Phase 5.5 已把它收敛到 agent.RecoveryPolicy，由 AgentRuntime 驱动：
            #
            #     ToolDefinition.recovery_policy
            #         -> AgentRuntime._try_recovery()
            #         -> RecoveryPolicy.should_retry / build_retry_prompt / build_history
            #
            # 等价性由 tests/test_phase55_recovery_matrix.py 的决策矩阵断言：
            #     4 工具 x 2 intent x 2 enabled x 2 marker x 2 interrupt x 2 history
            #     （should_retry / retry prompt / history 构造 / max_retries 全等价）
            #
            # 注意：本函数只在 DL_SENPAI_AGENT_MODE=legacy 时被调用。
            # RUNTIME 模式下 recovery 由 AgentRuntime 负责，且计入 Guard
            # （steps / timeout / token budget），不再游离在护栏之外。
            was_searched = result.searched
            elapsed = _time.perf_counter() - t0
            logger.info(
                f"dl_senpai LLM ok session={session_id} "
                f"reply_len={len(result.text)} "
                f"card_actions={len(result.card_actions)} "
                f"mute_actions={len(result.mute_actions)} "
                f"searched={was_searched or result.searched} elapsed={elapsed:.1f}s"
            )
        except LLMError as e:
            logger.warning(f"dl_senpai LLM error: {e}")
            if is_mentioned:
                await reply(bot, event, str(e))
            return
        except Exception as e:  # noqa: BLE001
            logger.exception(f"dl_senpai unexpected error: {e}")
            if is_mentioned:
                await reply(
                    bot,
                    event,
                    polish_reply("稍等一会啦，学姐现在有些事情要做噢～晚点再叫我吧"),
                )
            return
        state.reply = state.result.text
        return True

    async def interpret_result(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        state.image_request = bool(
            draw_enable and (result.draw_force or wants_senpai_image(intent_text))
        )
        state.browse_intent = state.browser_enable and wants_browser_help(state.intent_text)
        state.browse_wanted = state.browser_enable and (
            result.browse_force or browse_intent
        )
        state.card_intent = state.card_enable and looks_like_card_request(state.intent_text)
        return True

    async def apply_card_and_mute(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        state.applied_cards = []
        state.applied_mutes = []
        if state.card_enable and state.result.card_actions and isinstance(event, GroupMessageEvent):
            # 硬门槛：闲聊（晚安/QWQ）里模型乱写 CARD 一律不执行
            if not card_intent:
                logger.warning(
                    f"dl_senpai drop hallucinated card actions "
                    f"session={session_id} n={len(result.card_actions)} "
                    f"text={chat_text!r}"
                )
                # 顺带清掉「已经改成xxx」的嘴炮，避免晚安被说成改名片
                reply = polish_reply(
                    re.sub(
                        r"[^。！？\n]{0,40}群名片已经改成[^。！？\n]{0,30}",
                        "",
                        reply,
                    )
                )
            else:
                playful_ready = _card_cooldown.ready(
                    session_id, config.card_playful_cooldown
                )
                actions = filter_actions(
                    result.card_actions,
                    candidates=candidates,
                    max_len=config.card_max_len,
                    allow_playful=True,
                    playful_ready=playful_ready,
                )
                if actions:
                    # Phase 6A：改走共享 ToolExecutor（与 RUNTIME 同一实现）。
                    # 权限、幂等、journal、观测全部自动共享，
                    # 不再直接调用 group_card.apply_card_actions。
                    applied_cards = await _dispatch_legacy_tool_calls(
                        tool_calls_from_card_actions(actions),
                        bot=bot,
                        event=event,
                        config=config,
                        session_id=session_id,
                        sender=sender,
                        intent_text=intent_text,
                        chat_text=chat_text,
                        interrupt=interrupt,
                    )
                    applied_cards = _first_values(applied_cards, "card")
                    if any(getattr(a, "kind", "") == "playful" for a in applied_cards):
                        _card_cooldown.mark(session_id)
                if result.card_actions and not applied_cards:
                    logger.warning(
                        f"dl_senpai card actions dropped/failed "
                        f"session={session_id} raw={len(result.card_actions)} "
                        f"filtered={len(actions)} applied=0"
                    )
                    reply = polish_reply(f"{reply}\n{card_failure_note()}")
        elif state.card_intent and not state.result.card_actions:
            logger.warning(
                f"dl_senpai card intent but still no marker session={session_id}"
            )
            reply = polish_reply(f"{reply}\n{card_missing_marker_note()}")
        if state.mute_enable and isinstance(event, GroupMessageEvent) and can_emit_mute(state.intent_text):
            mute_ready = _mute_cooldown.ready(session_id, config.mute_cooldown)
            mute_actions = list(result.mute_actions)
            block_reason = ""

            # 模型仍漏指令时：本地解析「禁言我 / @禁言」
            if not mute_actions:
                at_ids = extract_at_user_ids(event, self_id=int(event.self_id))
                explicit, block_reason = resolve_explicit_mute(
                    chat_text,
                    sender_id=int(event.user_id),
                    at_ids=at_ids,
                    candidates=candidates,
                    duration=max(config.mute_min_sec, min(60, config.mute_max_sec)),
                )
                if explicit is not None:
                    mute_actions = [explicit]
                    logger.info(
                        f"dl_senpai mute explicit fallback "
                        f"user={explicit.user_id} duration={explicit.duration}"
                    )

            if mute_actions:
                mutes, filter_reason = filter_mute_actions(
                    mute_actions,
                    candidates=candidates,
                    max_duration=config.mute_max_sec,
                    min_duration=config.mute_min_sec,
                    ready=mute_ready,
                )
                if not mute_ready:
                    block_reason = "cooldown"
                elif filter_reason:
                    block_reason = filter_reason
                if mutes:
                    # Phase 6A：走共享 ToolExecutor（与 RUNTIME 同一实现）。
                    _mute_results = await _dispatch_legacy_tool_calls(
                        tool_calls_from_mute_actions(mutes),
                        bot=bot,
                        event=event,
                        config=config,
                        session_id=session_id,
                        sender=sender,
                        intent_text=intent_text,
                        chat_text=chat_text,
                        interrupt=interrupt,
                    )
                    applied_mutes = _first_values(_mute_results, "mute")
                    if applied_mutes:
                        _mute_cooldown.mark(session_id)
                        ack = format_mute_ack(applied_mutes)
                        if ack:
                            reply = polish_reply(f"{reply}\n{ack}")
                        block_reason = ""
                    else:
                        block_reason = "failed"

            if not applied_mutes:
                reply = strip_unapplied_mute_claims(reply)
                # 仅明确禁言请求失败时提示；辱骂未禁言不必贴系统说明
                if looks_like_mute_request(intent_text):
                    note = mute_block_note(block_reason or "no_marker")
                    if note:
                        reply = polish_reply(f"{reply}\n{note}")
                logger.warning(
                    f"dl_senpai mute not applied session={session_id} "
                    f"reason={block_reason or 'no_marker'} "
                    f"raw={len(result.mute_actions)}"
                )
        elif state.mute_enable and state.result.mute_actions:
            logger.warning(
                f"dl_senpai drop hallucinated mute actions "
                f"session={session_id} n={len(result.mute_actions)}"
            )
            reply = strip_unapplied_mute_claims(reply)
        if not state.applied_mutes:
            reply = polish_reply(strip_unapplied_mute_claims(reply))
        return True

    async def prepare_media_response(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        state.user_stored = format_user_text_for_memory(
            sender,
            text,
            image_count=image_count,
            sticker_count=sticker_count,
        )
        if getattr(event, "reply", None) is not None and state.chat_text:
            user_stored = f"{sender}：{chat_text}"
        state.sticker = None
        if not state.local_infer and not state.image_request and not state.browse_wanted:
            from .agent.tool_dispatch import tool_call_for_sticker

            _sticker_call = tool_call_for_sticker(
                result.sticker_query or "", force=bool(result.sticker_force)
            )
            if _sticker_call is not None:
                _sticker_results = await _dispatch_legacy_tool_calls(
                    [_sticker_call],
                    bot=bot,
                    event=event,
                    config=config,
                    session_id=session_id,
                    sender=sender,
                    intent_text=intent_text,
                    chat_text=chat_text,
                    reply_text=reply,
                    interrupt=interrupt,
                )
                sticker = _first_values(_sticker_results, "sticker")
                sticker = sticker[0] if sticker else None
        if state.sticker is None and state.result.sticker_force and not state.local_infer:
            logger.info(
                f"dl_senpai wanted sticker ({result.sticker_query!r}) "
                f"but collection empty for {session_id}"
            )
        state.voice = None
        state.voice_override = state.result.voice_override
        voice_wanted = (
            not local_infer
            and feature_enabled(config, event, "voice")
            and not interrupt
            and (
                result.voice_force
                or looks_like_voice_request(intent_text)
                or (
                    len(reply) <= config.voice_max_chars
                    and _random.random()
                    < max(0.0, min(1.0, config.voice_reply_prob))
                )
            )
        )
        if voice_wanted:
            if not voice_override and looks_like_voice_request(intent_text):
                voice_override = extract_voice_quote_from_user(intent_text)
            # Phase 6A：TTS 构建走共享 ToolExecutor。
            from .agent.tool_dispatch import tool_call_for_voice

            _voice_call = tool_call_for_voice(force=True, override=voice_override or "")
            if _voice_call is not None:
                _voice_results = await _dispatch_legacy_tool_calls(
                    [_voice_call],
                    bot=bot,
                    event=event,
                    config=config,
                    session_id=session_id,
                    sender=sender,
                    intent_text=intent_text,
                    chat_text=chat_text,
                    reply_text=reply,
                    interrupt=interrupt,
                )
                _voice_values = _first_values(_voice_results, "voice")
                voice = _voice_values[0] if _voice_values else None
        return True

    async def prepare_draw_and_browse(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        state.image = None
        state.draw_prompt = ""
        state.draw_scene = classify_senpai_draw(state.intent_text)
        draw_intent = state.draw_enable and wants_senpai_image(state.intent_text)
        draw_wanted = (
            draw_enable
            and (
                result.draw_force
                or draw_intent
            )
        )
        if draw_wanted:
            draw_ready = _draw_cooldown.ready(session_id, config.draw_cooldown)
            draw_prompt = resolve_draw_prompt(
                marker_prompt=result.draw_prompt,
                user_request=intent_text,
                reply_text=reply,
                max_chars=config.draw_max_prompt_chars,
                group_id=event.group_id if isinstance(event, GroupMessageEvent) else None,
            )
            if not draw_ready:
                note = draw_block_note("cooldown", scene=draw_scene)
                if note:
                    reply = polish_reply(f"{reply}\n{note}")
            elif not draw_prompt:
                note = draw_block_note("empty_prompt", scene=draw_scene)
                if note and draw_intent:
                    reply = polish_reply(f"{reply}\n{note}")
            else:
                # Phase 6A：生图走共享 ToolExecutor。
                from .agent.tool_dispatch import tool_call_for_draw

                _draw_call = tool_call_for_draw(draw_prompt, force=True)
                if _draw_call is not None:
                    try:
                        _draw_results = await _dispatch_legacy_tool_calls(
                            [_draw_call],
                            bot=bot,
                            event=event,
                            config=config,
                            session_id=session_id,
                            sender=sender,
                            intent_text=intent_text,
                            chat_text=chat_text,
                            reply_text=reply,
                            draw_scene=draw_scene,
                            interrupt=interrupt,
                        )
                        _draw_values = _first_values(_draw_results, "draw")
                        image = _draw_values[0] if _draw_values else None
                        if image is not None:
                            _draw_cooldown.mark(session_id)
                        elif draw_intent or result.draw_force:
                            reply = polish_reply(f"{reply}\n{draw_failure_note()}")
                    except Exception:  # noqa: BLE001
                        logger.warning("dl_senpai draw build failed", exc_info=True)
                        if draw_intent or result.draw_force:
                            reply = polish_reply(f"{reply}\n{draw_failure_note()}")
        state.browse_images: list[MessageSegment] = []
        if state.browse_wanted:
            browse_ready = _browse_cooldown.ready(session_id, config.browser_cooldown)
            browse_tasks = resolve_browse_tasks(
                marker_tasks=list(result.browse_tasks),
                user_request=intent_text,
            )
            if not browse_ready:
                note = browse_block_note("cooldown")
                if note:
                    reply = polish_reply(f"{reply}\n{note}")
            elif not browse_tasks:
                if browse_intent or result.browse_force:
                    reply = polish_reply(f"{reply}\n{browse_failure_note()}")
            else:
                # Phase 6A：浏览器任务走共享 ToolExecutor。
                from .agent.tool_dispatch import tool_call_for_browse

                _browse_call = tool_call_for_browse(browse_tasks, force=True)
                if _browse_call is not None:
                    try:
                        _browse_results = await _dispatch_legacy_tool_calls(
                            [_browse_call],
                            bot=bot,
                            event=event,
                            config=config,
                            session_id=session_id,
                            sender=sender,
                            intent_text=intent_text,
                            chat_text=chat_text,
                            interrupt=interrupt,
                        )
                        _browse_values = _first_values(_browse_results, "browse")
                        browse_images = _browse_values[0] if _browse_values else []
                        if browse_images:
                            _browse_cooldown.mark(session_id)
                        elif browse_intent or result.browse_force:
                            reply = polish_reply(f"{reply}\n{browse_failure_note()}")
                    except Exception:  # noqa: BLE001
                        logger.warning("dl_senpai browse build failed", exc_info=True)
                        if browse_intent or result.browse_force:
                            reply = polish_reply(f"{reply}\n{browse_failure_note()}")
        return True

    async def apply_state_updates(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        state.assistant_stored = state.reply if state.sticker is None else f"{state.reply} [学姐发了一个表情]"
        if state.voice is not None:
            spoken = text_for_voice(
                reply,
                max_chars=config.voice_max_chars,
                override=voice_override,
            )
            if spoken:
                assistant_stored += f" [学姐发了一条语音：{spoken}]"
            else:
                assistant_stored += " [学姐发了一条语音]"
        if state.image is not None:
            preview = (draw_prompt or "").strip()
            if len(preview) > 80:
                preview = preview[:79] + "…"
            if preview:
                assistant_stored += f" [学姐发了一张图：{preview}]"
            else:
                assistant_stored += " [学姐发了一张图]"
        if state.browse_images:
            assistant_stored += f" [学姐发了{len(browse_images)}张网页截图]"
        state.assistant_stored += action_summary_for_memory(state.applied_cards)
        state.assistant_stored += mute_summary_for_memory(state.applied_mutes)
        _facade.record_turn(_mem_ref, state.user_stored, state.assistant_stored, config)
        if (
            person_memory_enable
            and (not interrupt or config.person_memory_allow_on_interrupt)
        ):
            updates = list(result.impression_updates or [])
            # 模型漏写指令时：用户明确自我介绍仍记一笔
            if not updates:
                auto_traits = extract_traits_from_user_text(intent_text)
                if auto_traits:
                    updates = [ImpressionUpdate(add=auto_traits, remove=[], notes=None)]
                    logger.info(
                        f"dl_senpai person_memory heuristic traits={auto_traits!r} "
                        f"user={event.user_id}"
                    )
            if updates:
                try:
                    _facade.record_person_updates(
                        MemoryRef.for_user(
                            str(event.user_id), group_id=str(event.group_id)
                        ),
                        updates,
                        sender,
                        config,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        f"dl_senpai person_memory apply failed user={event.user_id}"
                    )
        state.aff_scope = affection_scope(config, event)
        if state.aff_scope and state.is_mentioned and not state.interrupt:
            aff = _facade.record_interaction(
                aff_scope,
                str(event.user_id),
                config.affection_gain_chat,
                sender,
                config,
            )
            group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
            reply = polish_reply(
                await finalize_affection_gain(
                    bot,
                    scope=aff_scope,
                    user_id=event.user_id,
                    text=reply,
                    result=aff,
                    config=config,
                    always_show=False,
                    group_id=group_id,
                )
            )
        return True

    async def finalize_response(
        self,
        state: LegacyRunState,
        bot: Bot,
        event: MessageEvent,
        *,
        config: PluginConfig,
    ) -> bool:
        state.bundled_browse = state.browse_images[0] if state.browse_images and state.image is None else None
        state.extra_browse = state.browse_images[1:] if state.bundled_browse else state.browse_images
        await state.reply(
            bot,
            event,
            reply,
            sticker=sticker,
            voice=voice,
            image=image or bundled_browse,
        )
        for seg in state.extra_browse:
            await _send_image_with_retry(bot, event, seg)
        if config.inner_state_enable:
            try:
                _facade.update_inner_state(
                    inner_key,
                    reply_len=len(reply or ""),
                    mentioned=bool(is_mentioned),
                    interrupt=bool(interrupt),
                    private_chat=isinstance(event, PrivateMessageEvent),
                    config=config,
                )
            except Exception:  # noqa: BLE001
                logger.warning("dl_senpai inner_state update failed")
        return True

