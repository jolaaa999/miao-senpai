"""Legacy 编排的显式运行状态（Phase 6.6 第五/六/七/八节）。

第五节：不要拆函数以后变成几十个参数来回传 —— 建立明确的 RunState。
第六节：只保存**当前这一轮** Legacy orchestration 的临时状态。
第七节：**禁止**变成垃圾桶（不许 services / everything / app / globals）。
第八节：Context 与 State 分开 ——
        AgentContext  负责稳定执行上下文（trace / session / message / config / actor）
        LegacyRunState 负责本次运行产生的中间状态

因此本模块**只放数据字段**，不放任何服务引用。
需要 bot / config / event 的地方直接从 `self`（构造依赖）或
函数参数取，不塞进 state。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LegacyRunState:
    """一次 legacy run 的中间状态。

    字段按**真实代码写入顺序**分组，方便对照 16 个阶段。
    所有字段都有默认值，便于分阶段测试时只构造需要的部分。
    """

    # -- 会话（resolve_session 阶段）--
    session_id: str = ""

    # -- 消息上下文（prepare_message_context / prepare_media_context）--
    text: str = ""
    sticker_segments: list[Any] = field(default_factory=list)
    sticker_count: int = 0
    reply_message: Any = None
    photo_segments: list[Any] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    has_images: bool = False
    image_count: int = 0
    chat_text: str = ""

    # -- 交互上下文（prepare_interaction_context）--
    intent_text: str = ""
    is_mentioned: bool = False
    private_chat: bool = False

    # -- 记忆上下文（prepare_memory_context）--
    inner_key: str = ""
    interrupt_scale: float = 1.0
    inner_state_brief: str = ""

    # -- 触发决策（decide_trigger）--
    decision: Any = None
    interrupt: bool = False

    # -- 提示词上下文（prepare_prompt_context）--
    sender: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    memory_recall_brief: str = ""
    group_style_brief: str = ""
    card_enable: bool = False
    mute_enable: bool = False
    draw_enable: bool = False
    browser_enable: bool = False
    card_prompt: str = ""
    candidates: list[Any] = field(default_factory=list)
    need_mod: bool = False
    local_infer: bool = False

    # -- 可选上下文（prepare_optional_context）--
    trends_brief: str = ""
    trend_titles: list[str] = field(default_factory=list)
    affection_brief: str = ""
    aff_scope: str | None = None
    person_memory_brief: str = ""
    person_memory_enable: bool = False

    # -- 模型结果（invoke_model / interpret_result）--
    result: Any = None
    reply: str = ""
    was_searched: bool = False

    # -- 结果解释（interpret_result）--
    image_request: bool = False
    browse_intent: bool = False
    browse_wanted: bool = False
    card_intent: bool = False

    # -- 工具执行（apply_card_and_mute / prepare_media_response /
    #              prepare_draw_and_browse）--
    applied_cards: list[Any] = field(default_factory=list)
    applied_mutes: list[Any] = field(default_factory=list)
    mute_block_reason: str = ""
    sticker: Any = None
    voice: Any = None
    voice_override: str = ""
    image: Any = None
    draw_prompt: str = ""
    draw_scene: Any = None
    browse_images: list[Any] = field(default_factory=list)

    # -- 状态更新（apply_state_updates）--
    user_stored: str = ""
    assistant_stored: str = ""

    # -- 收尾（finalize_response）--
    bundled_browse: Any = None
    extra_browse: list[Any] = field(default_factory=list)

    # -- 阶段耗时（Observability，第三十七节）--
    stage_timings: dict[str, float] = field(default_factory=dict)


__all__ = ["LegacyRunState"]
