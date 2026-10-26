"""PromptBuilder：把 system prompt 装配从"12 个 if 分支"改为"注册表驱动"。

重构前（persona.build_system_prompt）：

    prompt = SYSTEM_PROMPT + MONSTER_HUNTER + 模式片段
    if has_images:            prompt += VISION_SYSTEM_EXTRA
    if card_enable:           prompt += CARD_SYSTEM_EXTRA
    if interrupt:             prompt += CARD_INTERRUPT_EXTRA
    if interrupt:             prompt += VOICE_INTERRUPT_EXTRA
    if mute_enable:           prompt += MUTE_SYSTEM_EXTRA
    if person_memory_enable:  prompt += PERSON_MEMORY_SYSTEM_EXTRA
    if interrupt:             prompt += PERSON_MEMORY_INTERRUPT_EXTRA
    if search_enable:         prompt += SEARCH_SYSTEM_EXTRA
    if draw_enable:           prompt += DRAW_SYSTEM_EXTRA
    if interrupt:             prompt += DRAW_INTERRUPT_EXTRA
    if browser_enable:        prompt += BROWSE_SYSTEM_EXTRA
    if interrupt:             prompt += BROWSE_INTERRUPT_EXTRA
    ...（few-shot / trends / affection / person / inner / style / recall / overlay / 时间）

Phase 4 目标（第十节、第四十四节）：

    ToolRegistry.visible_tools(context)
        ↓
    MarkerToolAdapter.render_tools()
        ↓
    system prompt 片段

**但文本行为必须等价**（第十节明确要求"以当前真实 prompt 为准"）。

因此本模块采取的策略是：
- **保留** `persona.build_system_prompt()` 作为唯一渲染实现
  （它已经过真实调优，不重写）；
- PromptBuilder 的职责是**把可见工具集合传递给 persona**，
  由 persona 按注册表结果决定拼接哪些片段，而不是自己再判断 enable。

这样既消除了 handlers/llm 层的 if 分支，又保证输出文本逐字不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..tools.registry import ToolDefinition


# 这些工具的说明被**无条件写进** persona.py 的基础段落，与 enable 开关无关。
# 其余工具（card/mute/browse）走 persona 的 enable 标志，关掉就自然消失——
# 所以「关不掉」只发生在下面这几项上。
_UNCONDITIONAL_TOOL_SECTIONS: dict[str, tuple[str, str]] = {
    "sticker": ("【表情包】", "<<<STICKER"),
    "voice": ("【语音能力】", "<<<VOICE"),
}


def _remove_heading_block(text: str, heading: str) -> str:
    """删掉以 heading 开头的整个【…】段落，直到下一个【…】或文本结尾。"""
    start = text.find(heading)
    if start == -1:
        return text
    nxt = text.find("\n【", start + len(heading))
    if nxt == -1:
        return text[:start].rstrip()
    return text[:start] + text[nxt + 1:]


def _drop_sentences_with(text: str, needle: str) -> str:
    """按句号删掉含 needle 的句子，保留同一行内的其他句子。

    为什么精确到句子而不是整行：这些提及混在模式段落的末尾
    （如「…不要助手腔/咨询腔。想发表情可附 <<<STICKER ...>>>。」），
    整行删掉会连带把整段「被点名/插嘴」的行为说明一起删掉。
    """
    out: list[str] = []
    for line in text.split("\n"):
        if needle in line:
            line = "。".join(part for part in line.split("。") if needle not in part)
        out.append(line)
    return "\n".join(out)


def apply_tool_visibility_filter(text: str, visible_names: Any) -> str:
    """把「已关闭功能」的说明从提示词里摘掉。

    背景（2026-10-12 线上）：DL_SENPAI_STICKER_ENABLE=false 之后，模型仍被
    教着写表情包指令；实测它会**照抄示例**里的关键词，把本该生图的一轮浪费
    在空的表情包库上。根因是 persona.py 的基础段落把【表情包】【语音能力】
    写成了无条件文本，所以这层过滤落在装配期，与 persona.py 的文本内容解耦。

    为什么单独导出：test_phase4_prompt_registry 需要拿它对 legacy 原文施加
    **同一层**过滤，再与 PromptBuilder 的输出逐字比对——这样「prompt 是生产
    资产」的守卫依然有效，只是把「这层已声明的可见性过滤」纳入了契约。
    """
    names = {str(name) for name in visible_names}
    for tool_name, (heading, marker) in _UNCONDITIONAL_TOOL_SECTIONS.items():
        # 工具可见 = 功能开着：说明必须原样保留，一个字都不动。
        if tool_name in names:
            continue
        text = _drop_sentences_with(_remove_heading_block(text, heading), marker)
    return text


@dataclass
class PromptRequest:
    """一次 prompt 装配的输入。

    字段与重构前 `llm.chat()` 传给 `build_system_prompt` 的参数一一对应，
    额外增加 `visible_tools`（来自 ToolRegistry）。
    """

    interrupt: bool = False
    has_images: bool = False
    private_chat: bool = False
    fewshot_enable: bool = False
    # -- 动态片段 --
    trends_brief: str = ""
    affection_brief: str = ""
    person_memory_brief: str = ""
    inner_state_brief: str = ""
    group_style_brief: str = ""
    memory_recall_brief: str = ""
    # 长期记忆·概览层（双层记忆的上层）：结构化事实卡片。
    memory_cards_brief: str = ""
    # 当天聊天流水：她自己没参与、但发生在她眼前的对话背景。
    recent_chat_brief: str = ""
    # 跨场合对话流：她在别的群/私聊里接过的轮次（见 cross_chat.py）
    cross_chat_brief: str = ""
    # 她自己最近写过的日记（搬自 ai-companion-kit）。
    # 与记忆账本分开注入：记忆是「关于别人的事实」，日记是「她自己的日子」，
    # 混成一段会让模型把两者都当成档案念，反而不会顺嘴提一句「我昨天也……」。
    diary_brief: str = ""
    # 回复长度纪律。放在提示词最末——人设中段的规则压不住模型的输出长度，
    # 最后读到的那段才管用。
    brevity_hint: str = ""
    # 本轮语气判断（吐槽/真倾诉）。比长度纪律更靠后：它判的是这一句要不要安慰
    tone_hint: str = ""
    # -- 注册表驱动的工具集合 --
    visible_tools: list[ToolDefinition] = field(default_factory=list)


@dataclass
class BuiltPrompt:
    """装配结果。

    Attributes:
        text: 最终 system prompt 文本。
        sections: 各片段名 → 是否包含（用于 shadow 比对与诊断）。
        tool_names: 参与渲染的工具名。
    """

    text: str = ""
    sections: dict[str, bool] = field(default_factory=dict)
    tool_names: list[str] = field(default_factory=list)


class PromptBuilder:
    """Prompt 装配器。

    依赖注入了 `renderer`（默认 `persona.build_system_prompt`），
    便于测试与 shadow 比对替换实现。
    """

    def __init__(self, renderer: Any = None, adapter: Any = None) -> None:
        self._renderer = renderer
        self._adapter = adapter

    def _get_renderer(self) -> Any:
        if self._renderer is not None:
            return self._renderer
        from ...persona import build_system_prompt

        return build_system_prompt

    def _tool_flags(self, tools: list[ToolDefinition]) -> dict[str, bool]:
        """把可见工具集合翻译成 persona 需要的 enable 标志。

        这是"注册表驱动"的关键：**可见性判断只发生在 ToolRegistry**，
        persona 只接收结果，不再自己判断 draw_enable / mute_enable。
        """
        names = {t.name for t in tools}
        return {
            "card_enable": "card" in names,
            "mute_enable": "mute" in names,
            "person_memory_enable": "impression" in names,
            "search_enable": "search" in names,
            "draw_enable": "draw" in names,
            "browser_enable": "browse" in names,
        }

    def build(self, request: PromptRequest) -> BuiltPrompt:
        """装配 system prompt。"""
        flags = self._tool_flags(request.visible_tools)
        renderer = self._get_renderer()

        text = renderer(
            interrupt=request.interrupt,
            has_images=request.has_images,
            private_chat=request.private_chat,
            fewshot_enable=request.fewshot_enable,
            trends_brief=request.trends_brief,
            affection_brief=request.affection_brief,
            person_memory_brief=request.person_memory_brief,
            inner_state_brief=request.inner_state_brief,
            group_style_brief=request.group_style_brief,
            memory_recall_brief=request.memory_recall_brief,
            **flags,
        )

        # 关闭的功能不该继续教模型怎么用（见 apply_tool_visibility_filter）。
        text = apply_tool_visibility_filter(
            text, {tool.name for tool in request.visible_tools}
        )

        # 新注册工具的说明块在这里追加。
        #
        # **这是「注册一个 ToolDefinition 就能被模型看见」真正落地的地方。**
        # 此前 build() 只把工具集合翻译成 persona 的 enable 标志，
        # 而 persona 只认识原来那 8 个工具——于是 knowledge / memory / 高风险
        # 这些后加的工具，prompt_text 写好了却从来没进过提示词。
        # 模型不知道 marker 的存在，自然也就不会调用它们。
        # （build_tool_block 一直存在，但只在 shadow 比对里用过。）
        # interrupt（插嘴）轮不注入工具说明，与 persona 的既有行为一致：
        # 插嘴是没被叫到时自己凑一句，不该拥有完整工具箱——
        # 尤其不该在那样的轮次里握有 SHELL / CODE。
        # 这条判断放在这里而不是 _new_tool_block 里，是因为
        # 「什么时候给工具」属于装配策略，不是渲染细节。
        block = "" if request.interrupt else self._new_tool_block(
            request.visible_tools, text
        )
        if block:
            text = f"{text}\n{block}"

        # 长期记忆·概览层（双层记忆的上层）在这里追加，而不是交给 persona 渲染。
        # 两个理由：
        # 1. persona.py 是生产资产，有守卫测试盯着它「零改动」（prompt 不该漂移）；
        #    概览是**装配**职责，归 PromptBuilder 更合理。
        # 2. 只有 runtime 路径会装配卡片，legacy 路径本来就没有这层数据。
        cards_brief = (request.memory_cards_brief or "").strip()
        if cards_brief:
            text = f"{text}\n{cards_brief}"

        # 当天聊天流水同样在这里追加。它自带标题行与用法说明
        # （【今天群里聊过的】（背景资料，别复读…）），所以不必改 persona.py——
        # 那个文件有守卫测试盯着「提示词不该漂移」。
        recent_chat = (request.recent_chat_brief or "").strip()
        if recent_chat:
            text = f"{text}\n{recent_chat}"

        # 跨场合对话流紧跟在当天流水之后：先看「这个群刚才聊了什么」，
        # 再看「我在别处接过什么话」，粒度从近到远。
        cross_chat = (request.cross_chat_brief or "").strip()
        if cross_chat:
            text = f"{text}\n{cross_chat}"

        # 她自己的日记。放在聊天流水之后：先看「群里发生了什么」，
        # 再看「我自己最近是什么状态」，顺序跟人回忆的顺序一致。
        # 与 persona.py 解耦的理由同上——那个文件有守卫测试盯着不许漂移。
        diary = (request.diary_brief or "").strip()
        if diary:
            text = f"{text}\n{diary}"

        # 长度纪律放在**最末**：模型对最后读到的那段最敏感，塞在几千字人设
        # 中段的规则实际压不住。之前「目标 400~1500 字」就是这么把闲聊撑成
        # 小作文的，改完人设还得在这儿再兜一道。
        if request.brevity_hint.strip():
            text = f"{text}\n{request.brevity_hint.strip()}"

        # 本轮语气判断放在**整个提示词的最后一行之后**：
        # 位置比措辞重要，这一段要的是「读到最后还记得」。
        if (request.tone_hint or "").strip():
            text = f"{text}\n{request.tone_hint.strip()}"

        sections = {
            "vision": request.has_images,
            **flags,
            "trends": bool(request.trends_brief.strip()),
            "affection": bool(request.affection_brief.strip()),
            "person_memory_brief": bool(request.person_memory_brief.strip()),
            "inner_state": bool(request.inner_state_brief.strip()),
            "group_style": bool(request.group_style_brief.strip()),
            "memory_recall": bool(request.memory_recall_brief.strip()),
            "memory_cards": bool((request.memory_cards_brief or "").strip()),
            "recent_chat": bool((request.recent_chat_brief or "").strip()),
            "cross_chat": bool((request.cross_chat_brief or "").strip()),
            "diary": bool((request.diary_brief or "").strip()),
            # 诊断用：长度纪律这次有没有真的拼进提示词（它是最容易漏接的一段）
            "brevity_hint": bool((request.brevity_hint or "").strip()),
            "tone_hint": bool((request.tone_hint or "").strip()),
            # 诊断用：本次追加了哪些新工具的说明块。
            "extra_tool_block": bool(block),
        }

        return BuiltPrompt(
            text=text,
            sections=sections,
            tool_names=[t.name for t in request.visible_tools],
        )

    def _new_tool_block(self, tools: list[ToolDefinition], existing: str) -> str:
        """挑出**尚未出现在提示词里**的工具说明，拼成追加块。

        为什么用「文本是否已存在」而不是按工具名判断：

        - 原来那 8 个工具的说明是 persona.py 渲染的，
          它们 ToolDefinition.prompt_text 里存的是同一段原文。
          这里若直接追加整块，就会把那段话在提示词里写两遍。
        - 用名字列表（`if name in (...)`）去排除，等于把工具清单
          又硬编码到一个新地方——这正是整个注册表设计要消灭的东西。
        - 用「原文是否已出现」判重是自维护的：谁把说明搬去 persona，
          它自动不再被追加；谁新注册一个工具，它自动被追加。

        顺带按文本去重：5 个文件工具共用同一段说明，只该出现一次。
        """
        chunks: list[str] = []
        seen: set[str] = set()
        for tool in tools:
            body = (tool.prompt_text or "").strip()
            if not body or body in seen or body in existing:
                continue
            seen.add(body)
            chunks.append(body)
        return "\n".join(chunks)

    def build_tool_block(self, tools: list[ToolDefinition]) -> str:
        """单独渲染工具说明块（供 shadow 比对与新 Adapter 使用）。

        注意：当前生产路径的工具说明由 persona 渲染（保证文本等价），
        本方法用于验证"注册表确实能独立驱动工具说明"。
        """
        if self._adapter is None:
            from ..tools.marker_adapter import MarkerToolAdapter
            from ..tools.registry import get_tool_registry

            self._adapter = MarkerToolAdapter(get_tool_registry())
        return self._adapter.render_tools(tools)


__all__ = ["PromptBuilder", "PromptRequest", "BuiltPrompt"]
