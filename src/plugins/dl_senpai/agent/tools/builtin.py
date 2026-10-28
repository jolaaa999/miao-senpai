"""8 个生产工具的 ToolDefinition 注册。

**关键约束**：
1. `prompt_text` 必须逐字取自 `persona.py` 的现有 `*_SYSTEM_EXTRA`
   —— 不润色、不压缩、不重写（第十一节）。
2. `parser` 直接委托给各业务模块现有的 `parse_*` 函数
   —— 不重新实现正则，保证容错闭合差异（CARD 只认 `>>>`、
   SEARCH 认 `》》》` 等）逐字保持。
3. `executor` 复用现有业务函数，只做形状适配。

这样"统一定义的收益"与"零行为漂移的保证"可以同时拿到。
"""

from __future__ import annotations

from typing import Any

from ...observability.logging import log_event
from .models import Continuation, ToolCall, ToolCallSource, ToolResult
from .registry import FailurePolicy, ToolDefinition, ToolRegistry

# 工具优先级：决定 marker 解析顺序与 prompt 片段顺序。
# 顺序参考重构前 persona.build_system_prompt 的拼接顺序，保证 prompt 文本等价。
PRIORITY_CARD = 10
PRIORITY_MUTE = 20
PRIORITY_PERSON_MEMORY = 30
PRIORITY_SEARCH = 40
PRIORITY_DRAW = 50
PRIORITY_BROWSE = 60
PRIORITY_VOICE = 70
PRIORITY_STICKER = 80
PRIORITY_GROUP_READ = 90


# ================================================================
# 解析器：直接委托现有实现，零正则重写
# ================================================================


def _parse_card(text: str) -> tuple[str, list[ToolCall]]:
    from ...group_card import parse_card_actions

    parsed = parse_card_actions(text)
    calls = [
        ToolCall(
            tool_name="card",
            arguments={
                "user_id": a.user_id,
                "card": a.card,
                "kind": a.kind,
            },
            raw=f"<<<CARD {a.__dict__}>>>",
            marker="CARD",
        )
        for a in parsed.actions
    ]
    return parsed.text, calls


def _parse_mute(text: str) -> tuple[str, list[ToolCall]]:
    from ...group_mute import parse_mute_actions

    parsed = parse_mute_actions(text)
    calls = [
        ToolCall(
            tool_name="mute",
            arguments={
                "user_id": a.user_id,
                "duration": a.duration,
                "kind": a.kind,
            },
            raw=f"<<<MUTE {a.__dict__}>>>",
            marker="MUTE",
        )
        for a in parsed.actions
    ]
    return parsed.text, calls


def _parse_person_memory(text: str) -> tuple[str, list[ToolCall]]:
    from ...person_memory import parse_impression_updates

    parsed = parse_impression_updates(text)
    calls: list[ToolCall] = []
    for upd in parsed.updates:
        calls.append(
            ToolCall(
                tool_name="impression",
                # vibe/style/relation/moment/pending 必须一起带过去：
                # 它们是「判断这个人性格」的载体，之前只搬运 add/remove/notes，
                # 导致画像里 vibe/style/relation 永远为空——模型在 marker 里
                # 明明写了，落盘时被这一层悄悄丢掉。
                arguments={
                    "add": list(getattr(upd, "add", []) or []),
                    "remove": list(getattr(upd, "remove", []) or []),
                    "notes": getattr(upd, "notes", None),
                    "vibe": getattr(upd, "vibe", None),
                    "style": getattr(upd, "style", None),
                    "relation": getattr(upd, "relation", None),
                    "moment": getattr(upd, "moment", None),
                    "pending": getattr(upd, "pending", None),
                },
                raw="<<<IMPRESSION>>>",
                marker="IMPRESSION",
            )
        )
    return parsed.text, calls


def _parse_search(text: str) -> tuple[str, list[ToolCall]]:
    from ...web_search import parse_search_request

    parsed = parse_search_request(text)
    calls: list[ToolCall] = []
    if parsed.queries:
        calls.append(
            ToolCall(
                tool_name="search",
                arguments={"queries": list(parsed.queries)},
                raw=f"<<<SEARCH {' '.join(parsed.queries)}>>>",
                marker="SEARCH",
            )
        )
    return parsed.text, calls


def _parse_group_read(text: str) -> tuple[str, list[ToolCall]]:
    """解析群资源读取标记。

    标记形态（kind 就是用途，直接写中文，比英文枚举更好让模型写对）：
        <<<GROUP 公告>>>              读群公告
        <<<GROUP 文件>>>              读群文件列表
        <<<GROUP 文件内容 计网复习>>> 读某个群文件的正文
        <<<GROUP 相册>>>              读群相册列表
        <<<GROUP 相册内容 12345>>>    读指定相册里的照片
        <<<GROUP 精华>>>              读群精华消息

    第二个词统一叫 target，含义随 kind 变：
    「相册内容」时是相册 ID，「文件内容」时是文件名关键词。
    用一个通用参数而不是每个 kind 一个专用字段，是因为解析器只做分词，
    不懂业务——让 executor 按 kind 去解释 target 更清楚。

    为什么带参数：一个工具覆盖六种资源，prompt 只多一段说明，而不是
    给六个几乎同构的工具各写一段——省下的 prompt 预算留给回答质量。
    """
    import re

    calls: list[ToolCall] = []
    pattern = re.compile(r"<<<GROUP\s*([^>\n]*?)>>>")
    for match in pattern.finditer(text):
        raw_args = (match.group(1) or "").strip()
        if not raw_args:
            continue
        parts = raw_args.split()
        kind = parts[0].strip()
        # 文件名里可能有空格，所以 target 是"第一个词之后的全部"
        target = " ".join(parts[1:]).strip()
        calls.append(
            ToolCall(
                tool_name="group_read",
                arguments={"kind": kind, "target": target},
                raw=match.group(0),
                marker="GROUP",
            )
        )
    # 无论有没有解析出调用，都要把标记从正文里剥掉，避免泄漏给群友
    cleaned = pattern.sub("", text)
    return cleaned, calls


def _parse_draw(text: str) -> tuple[str, list[ToolCall]]:
    from ...image_gen import parse_draw_request, strip_draw_markers

    parsed = parse_draw_request(text)
    calls: list[ToolCall] = []
    if parsed.force or parsed.prompt:
        calls.append(
            ToolCall(
                tool_name="draw",
                arguments={"prompt": parsed.prompt, "force": parsed.force},
                raw=f"<<<DRAW {parsed.prompt}>>>",
                marker="DRAW",
            )
        )
    # 未启用时也要剥掉 marker，避免泄漏（strip_draw_markers 负责）
    cleaned = parsed.text if parsed.force else strip_draw_markers(parsed.text)
    return cleaned, calls


def _parse_browse(text: str) -> tuple[str, list[ToolCall]]:
    from ...browser_agent import parse_browse_request, strip_browse_markers

    parsed = parse_browse_request(text)
    calls: list[ToolCall] = []
    if parsed.force or parsed.tasks:
        calls.append(
            ToolCall(
                tool_name="browse",
                arguments={"tasks": list(parsed.tasks)},
                raw="<<<BROWSE>>>",
                marker="BROWSE",
            )
        )
    cleaned = parsed.text if parsed.force else strip_browse_markers(parsed.text)
    return cleaned, calls


def _parse_voice(text: str) -> tuple[str, list[ToolCall]]:
    from ...voice import parse_voice_request

    parsed = parse_voice_request(text)
    calls: list[ToolCall] = []
    if parsed.force:
        calls.append(
            ToolCall(
                tool_name="voice",
                arguments={"override": parsed.override},
                raw="<<<VOICE>>>",
                marker="VOICE",
            )
        )
    return parsed.text, calls


def _parse_sticker(text: str) -> tuple[str, list[ToolCall]]:
    from ...stickers import parse_sticker_request

    parsed = parse_sticker_request(text)
    calls: list[ToolCall] = []
    if parsed.force:
        calls.append(
            ToolCall(
                tool_name="sticker",
                arguments={"query": parsed.query},
                raw=f"<<<STICKER {parsed.query}>>>",
                marker="STICKER",
            )
        )
    return parsed.text, calls


# ================================================================
# 可见性谓词：替代 persona 里的 if 分支
# ================================================================


def _flag(ctx: Any, key: str) -> bool:
    """读功能开关（走 AgentContext.feature → 群覆盖）。"""
    try:
        return bool(ctx.feature(key))
    except Exception:  # noqa: BLE001
        return False


def _visible_card(ctx: Any) -> bool:
    # CARD 需要候选人名单才可用（与重构前 card_enable 的判定一致）
    return _flag(ctx, "card")


def _visible_mute(ctx: Any) -> bool:
    return _flag(ctx, "mute")


def _visible_person_memory(ctx: Any) -> bool:
    return _flag(ctx, "person_memory")


def _visible_search(ctx: Any) -> bool:
    if not _flag(ctx, "search"):
        return False
    # 插嘴时是否允许搜索由配置决定（与重构前一致）
    if getattr(ctx, "interrupt", False):
        cfg = getattr(ctx, "config", None)
        return bool(getattr(cfg, "search_allow_on_interrupt", False))
    return True


def _visible_group_read(ctx: Any) -> bool:
    """群资源读取只在群里可见。

    私聊里没有「当前群」这个概念，放出去只会让模型瞎调一个查不到的接口。
    另外它只读不写，没有权限门槛——任何群友都能问群里有什么。
    """
    if not _flag(ctx, "group_read"):
        return False
    return not bool(getattr(ctx, "private_chat", False))


def _visible_draw(ctx: Any) -> bool:
    if not _flag(ctx, "draw"):
        return False
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return False
    if not cfg.draw_configured():
        return False
    if getattr(ctx, "interrupt", False):
        return bool(getattr(cfg, "draw_allow_on_interrupt", False))
    return True


def _visible_browse(ctx: Any) -> bool:
    if not _flag(ctx, "browser"):
        return False
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return False
    from ...browser_agent import browser_configured

    if not browser_configured(cfg):
        return False
    if getattr(ctx, "interrupt", False):
        return bool(getattr(cfg, "browser_allow_on_interrupt", False))
    return True


def _visible_voice(ctx: Any) -> bool:
    if not _flag(ctx, "voice"):
        return False
    if getattr(ctx, "interrupt", False):
        cfg = getattr(ctx, "config", None)
        return bool(getattr(cfg, "voice_allow_on_interrupt", False))
    return True


def _visible_sticker(ctx: Any) -> bool:
    return _flag(ctx, "sticker")


# ================================================================
# 执行器：Phase 4 初期只做形状适配，业务仍由 handlers 执行
# ================================================================
#
# 说明：这 8 个 executor 在 Phase 4 里**不擅自执行业务**。
# 原因：现有业务执行依赖 handlers 里的大量上下文（candidates、cooldown、
# 会话状态），一次性搬过来会引入巨大行为风险。
#
# 因此 Phase 4 采取的是：
#   - 注册表 / 解析 / 可见性 / prompt 生成  → 真正接管
#   - 实际副作用执行                        → 仍走 handlers（executor 只记录）


def _side_effect_marker(tool_name: str):
    """生成一个只记录"模型要求执行该工具"的 executor。

    **Phase 5 已弃用**：8 个工具全部迁移到真实 executor
    （见 `executors.REAL_EXECUTORS`）。此函数保留仅为向后兼容
    与测试（需要"不产生副作用"的占位时使用）。
    """

    def _executor(context: Any, call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_name=tool_name,
            tool_call_id=call.id,
            success=True,
            data={"value": call.arguments, "deferred": True},
            metadata={"executed_by": "handlers"},
        )

    return _executor


def _real(tool_name: str, fallback: Any = None):
    """取真实 executor；不可用时回退到占位（保证启动不被阻断）。"""
    from .executors import REAL_EXECUTORS

    executor = REAL_EXECUTORS.get(tool_name)
    if executor is None:
        return fallback or _side_effect_marker(tool_name)
    return executor


# ================================================================
# 注册
# ================================================================


def build_production_tools() -> list[ToolDefinition]:
    """构造 9 个生产工具定义（不注册）。

    `prompt_text` 全部通过函数延迟读取 persona 常量，
    保证与 persona.py 的**单一事实来源**一致——
    如果 persona 文案改了，这里自动跟随，不会产生第二份副本。
    """
    from ...group_info import SUPPORTED_KINDS as SUPPORTED_GROUP_KINDS
    from ...persona import (
        BROWSE_SYSTEM_EXTRA,
        CARD_SYSTEM_EXTRA,
        DRAW_SYSTEM_EXTRA,
        GROUP_READ_SYSTEM_EXTRA,
        MUTE_SYSTEM_EXTRA,
        PERSON_MEMORY_SYSTEM_EXTRA,
        SEARCH_SYSTEM_EXTRA,
    )
    from ..recovery import build_recovery_policies

    # 补救策略与 legacy 同源（Phase 5.5 决策矩阵已验证等价）
    recovery = build_recovery_policies()

    return [
        ToolDefinition(
            recovery_policy=recovery["card"],
            name="card",
            description="群名片修改（改群名片 / 改群昵称）",
            marker="CARD",
            prompt_text=CARD_SYSTEM_EXTRA.strip(),
            parser=_parse_card,
            executor=_real("card"),
            failure_policy=FailurePolicy.STOP,
            permission="group.manage_card",
            continuation=Continuation.SIDE_EFFECT_ONLY,
            visibility=_visible_card,
            owner="builtin_tools",
            priority=PRIORITY_CARD,
            parameter_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer"},
                    "card": {"type": "string"},
                    "kind": {"type": "string", "enum": ["request", "playful"]},
                },
                "required": ["user_id", "card"],
            },
        ),
        ToolDefinition(
            recovery_policy=recovery["mute"],
            name="mute",
            description="群禁言（生气禁言 / 要求禁言某人）",
            marker="MUTE",
            prompt_text=MUTE_SYSTEM_EXTRA.strip(),
            parser=_parse_mute,
            executor=_real("mute"),
            failure_policy=FailurePolicy.STOP,
            permission="group.mute",
            continuation=Continuation.SIDE_EFFECT_ONLY,
            visibility=_visible_mute,
            owner="builtin_tools",
            priority=PRIORITY_MUTE,
            parameter_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer"},
                    "duration": {"type": "integer"},
                    "kind": {"type": "string"},
                },
                "required": ["user_id", "duration"],
            },
        ),
        ToolDefinition(
            name="impression",
            description="长期人物印象更新",
            marker="IMPRESSION",
            prompt_text=PERSON_MEMORY_SYSTEM_EXTRA.strip(),
            parser=_parse_person_memory,
            executor=_real("impression"),
            failure_policy=FailurePolicy.FALLBACK_TEXT,
            continuation=Continuation.SIDE_EFFECT_ONLY,
            visibility=_visible_person_memory,
            owner="builtin_tools",
            priority=PRIORITY_PERSON_MEMORY,
            # 参数形状必须与 _parse_person_memory 填的 arguments 一致，
            # 否则 native 调用喂进来的 dict 对不上 executor 的取值键。
            # schema 必须把 vibe/style/relation/moment/note 都列出来：
            # 原生 function calling 模式下，模型只能按 schema 填参数，
            # 没声明的字段它根本不会发。之前这里只有 add/remove/notes，
            # 于是 persona 里教了半天的「判断性格」字段全部落空，
            # 31 个用户的 vibe/style/relation 全是空串。
            parameter_schema={
                "type": "object",
                "properties": {
                    "add": {"type": "array", "items": {"type": "string"}},
                    "remove": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": ["string", "null"]},
                    "vibe": {
                        "type": ["string", "null"],
                        "description": "对方是什么样的人（气质/性格），一两句话，可覆盖旧值",
                    },
                    "style": {
                        "type": ["string", "null"],
                        "description": "说话习惯：短句多、爱阴阳、爱撒娇、爱发表情等",
                    },
                    "relation": {
                        "type": ["string", "null"],
                        "description": "跟你处成什么关系感：损友、常来问问题的学弟、爱找茬的等",
                    },
                    "moment": {
                        "type": ["string", "null"],
                        "description": "一条难忘小瞬间，一句话即可",
                    },
                    "pending": {
                        "type": ["string", "null"],
                        "description": "还没说出口的牵挂/心事，一句话；传空串表示清空",
                    },
                },
            },
        ),
        ToolDefinition(
            name="search",
            description="联网检索（时事/天气/价格等时效问题）",
            marker="SEARCH",
            prompt_text=SEARCH_SYSTEM_EXTRA.strip(),
            parser=_parse_search,
            executor=_real("search"),
            failure_policy=FailurePolicy.RETURN_TO_MODEL,
            # SEARCH 是唯一需要"结果回填 → 再问模型"的工具
            continuation=Continuation.RETURN_TO_MODEL,
            visibility=_visible_search,
            owner="builtin_tools",
            priority=PRIORITY_SEARCH,
            timeout=30.0,
            parameter_schema={
                "type": "object",
                "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
                "required": ["queries"],
            },
        ),
        ToolDefinition(
            recovery_policy=recovery["draw"],
            name="draw",
            description="文生图（给学姐画图 / 穿某套衣服）",
            marker="DRAW",
            prompt_text=DRAW_SYSTEM_EXTRA.strip(),
            parser=_parse_draw,
            executor=_real("draw"),
            failure_policy=FailurePolicy.FALLBACK_TEXT,
            continuation=Continuation.SIDE_EFFECT_ONLY,
            visibility=_visible_draw,
            owner="builtin_tools",
            priority=PRIORITY_DRAW,
            timeout=120.0,
            parameter_schema={
                "type": "object",
                "properties": {
                    # 对已知角色来说「越具体越好」是错的：模型凭记忆写的外貌经常是错的，
                    # 写进去反而会覆盖角色档案里的正确设定（实测把艾雅法拉写成深紫褐色短发）。
                    "prompt": {
                        "type": "string",
                        "description": (
                            "画面描述。写场景/动作/构图/光影/画风即可。"
                            "如果用户点名了已知角色，必须原样保留角色名，不要自己描述该角色的"
                            "发色/瞳色/服装/配饰——系统会用角色档案补全身份，编造的外貌描述会覆盖正确设定。"
                        ),
                    },
                    "force": {"type": "boolean", "description": "是否强制出图"},
                },
                "required": ["prompt"],
            },
        ),
        ToolDefinition(
            recovery_policy=recovery["browse"],
            name="browse",
            description="浏览器逛网站截图（淘宝/pixiv 等）",
            marker="BROWSE",
            prompt_text=BROWSE_SYSTEM_EXTRA.strip(),
            parser=_parse_browse,
            executor=_real("browse"),
            failure_policy=FailurePolicy.RETURN_TO_MODEL,
            continuation=Continuation.RETURN_TO_MODEL,
            visibility=_visible_browse,
            owner="builtin_tools",
            priority=PRIORITY_BROWSE,
            timeout=180.0,
            parameter_schema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要浏览的任务，如「淘宝 卫衣」「pixiv 猫」或网址",
                    },
                },
                "required": ["tasks"],
            },
        ),
        ToolDefinition(
            name="sticker",
            description="发表情包",
            marker="STICKER",
            # 重要：表情包说明**内嵌在 SYSTEM_PROMPT 里**（persona.py 的
            # 【表情包】段落），重构前并没有独立的 STICKER_SYSTEM_EXTRA，
            # 也没有 if sticker_enable 门控——它始终在 system prompt 中。
            # 因此这里 prompt_text 留空，由 persona 的基础段落负责说明，
            # 避免出现第二份副本导致 prompt 文本重复。
            prompt_text="",
            parser=_parse_sticker,
            executor=_real("sticker"),
            failure_policy=FailurePolicy.FALLBACK_TEXT,
            continuation=Continuation.SIDE_EFFECT_ONLY,
            visibility=_visible_sticker,
            owner="builtin_tools",
            priority=PRIORITY_STICKER,
            parameter_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "表情包关键词，可留空"},
                },
            },
        ),
        ToolDefinition(
            name="voice",
            description="发语音条（TTS）",
            marker="VOICE",
            # 同上：语音说明也在 SYSTEM_PROMPT 的【语音能力】段落里，
            # 没有独立的 VOICE_SYSTEM_EXTRA。仅「插嘴模式」有额外约束。
            prompt_text="",
            parser=_parse_voice,
            executor=_real("voice"),
            failure_policy=FailurePolicy.FALLBACK_TEXT,
            continuation=Continuation.SIDE_EFFECT_ONLY,
            visibility=_visible_voice,
            owner="builtin_tools",
            priority=PRIORITY_VOICE,
            timeout=120.0,
            parameter_schema={
                "type": "object",
                "properties": {
                    "override": {"type": "string", "description": "要朗读的文本，可留空"},
                },
            },
        ),
        ToolDefinition(
            name="group_read",
            description="读群公告 / 群文件列表 / 群相册",
            marker="GROUP",
            prompt_text=GROUP_READ_SYSTEM_EXTRA.strip(),
            parser=_parse_group_read,
            executor=_real("group_read"),
            failure_policy=FailurePolicy.RETURN_TO_MODEL,
            continuation=Continuation.RETURN_TO_MODEL,
            visibility=_visible_group_read,
            owner="builtin_tools",
            priority=PRIORITY_GROUP_READ,
            # 列公告/文件/相册都是秒级，但「读文件正文」要下载，
            # 20MB 的边界最大可能要几十秒。取 60s 与 httpx 的超时对齐，
            # 否则大文件会在下载途中被工具超时掐掉，用户只看到"读不到"。
            timeout=60.0,
            parameter_schema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": list(SUPPORTED_GROUP_KINDS),
                    },
                    "target": {
                        "type": "string",
                        "description": "相册 ID 或文件名关键词，仅部分 kind 需要",
                    },
                },
                "required": ["kind"],
            },
        ),
    ]


def register_production_tools(registry: ToolRegistry) -> list[ToolDefinition]:
    """把 9 个生产工具注册进注册表。"""
    tools = build_production_tools()
    for tool in tools:
        registry.register(tool)
    log_event(
        "tool.registry_ready",
        count=len(tools),
        names=[t.name for t in tools],
        markers=[t.marker for t in tools if t.marker],
    )
    return tools


__all__ = [
    "build_production_tools",
    "register_production_tools",
    "PRIORITY_CARD",
    "PRIORITY_MUTE",
    "PRIORITY_SEARCH",
    "PRIORITY_DRAW",
    "PRIORITY_BROWSE",
    "PRIORITY_VOICE",
    "PRIORITY_STICKER",
    "PRIORITY_GROUP_READ",
    "PRIORITY_PERSON_MEMORY",
]
