"""Phase 6.6 任务（四）：run() 职责地图。

根据 scripts/phase66_audit.py 的真实输出划分阶段。
**不机械切分** —— 边界取在"局部变量不再被后续段落使用"的位置。
"""

from __future__ import annotations

# (起始行, 结束行, 阶段名, 职责一句话, 主要写入的局部变量, 副作用)
STAGES: list[tuple[int, int, str, str, str, str]] = [
    (
        303, 306, "resolve_session",
        "解析会话 id；无法解析则直接返回",
        "resolved, session_id",
        "无",
    ),
    (
        308, 321, "prepare_message_context",
        "群观察、纯文本、贴纸采集与计数",
        "text, sticker_segments, sticker_count",
        "sticker 采集（观察者层）",
    ),
    (
        322, 341, "prepare_media_context",
        "解析被引用消息、提取 vision 图片、组装 chat_text",
        "reply_message, photo_segments, image_urls, has_images, image_count, chat_text",
        "下载图片（只读）",
    ),
    (
        343, 386, "prepare_interaction_context",
        "intent_text、跟进贴纸判定、是否 @ 我、插嘴窗口",
        "intent_text, followup_sticker, is_mentioned",
        "武装 attention 窗口 + cooldown.mark",
    ),
    (
        389, 415, "prepare_memory_context",
        "群风格采样、内心状态读取与插嘴缩放",
        "inner_key, interrupt_scale, inner_state_brief, istate",
        "群风格 observe（写 memory）",
    ),
    (
        417, 445, "decide_trigger",
        "触发决策（回复/插嘴/忽略）",
        "decision, interrupt",
        "cooldown.mark（按 kind）",
    ),
    (
        447, 465, "validate_preconditions",
        "API 未配置、图片未下载成功时的早退",
        "无（早退）",
        "发提示消息",
    ),
    (
        467, 494, "prepare_prompt_context",
        "sender、facade、历史、群风格、工具开关、候选名单",
        "sender, _facade, _mem_ref, history, memory_recall_brief, llm, "
        "group_style_brief, card_enable, mute_enable, draw_enable, "
        "browser_enable, card_prompt, candidates, need_mod, local_infer",
        "读 memory（Facade）",
    ),
    (
        495, 557, "prepare_optional_context",
        "候选头像同步、趋势、好感度、人物画像 brief",
        "trends_brief, affection_brief, aff_scope, person_memory_brief, "
        "person_memory_enable",
        "读 memory + 可能同步群名片",
    ),
    (
        559, 633, "invoke_model",
        "调用 SenpaiLLM.chat（含本地 3B 分支、异常处理）",
        "result, reply, t0, private_chat, was_searched, sticker_count",
        "**LLM 调用**；LLMError 时回一条错误文案",
    ),
    (
        642, 651, "interpret_result",
        "从 ChatResult 提取意图标志",
        "image_request, browse_intent, browse_wanted, card_intent",
        "无",
    ),
    (
        652, 784, "apply_card_and_mute",
        "CARD/MUTE 的意图校验、过滤、共享 ToolExecutor 执行",
        "applied_cards, applied_mutes, block_reason",
        "**Tool 副作用**（card/mute）",
    ),
    (
        787, 868, "prepare_media_response",
        "贴纸与语音准备（经共享 ToolExecutor）",
        "user_stored, sticker, voice, voice_override, voice_wanted",
        "**Tool 副作用**（sticker/voice）",
    ),
    (
        870, 970, "prepare_draw_and_browse",
        "生图与浏览器任务准备（经共享 ToolExecutor）",
        "image, draw_prompt, draw_scene, draw_intent, draw_wanted, browse_images",
        "**Tool 副作用**（draw/browse）",
    ),
    (
        972, 1047, "apply_state_updates",
        "会话记忆写入、人物画像写入、好感度结算",
        "assistant_stored, updates, aff",
        "**Memory 写入**（Facade）",
    ),
    (
        1048, 1072, "finalize_response",
        "组装最终回复（贴纸/语音/图片/额外浏览图）并发送；内心状态收尾",
        "bundled_browse, extra_browse",
        "**发送回复**；内心状态写入",
    ),
]


def main() -> int:
    print("=" * 100)
    print("LegacyOrchestrator.run() 职责地图")
    print("=" * 100)
    total = 0
    for start, end, name, resp, _, _ in STAGES:
        size = end - start + 1
        total += size
        print(f"\n  L{start}-{end}  ({size:3} 行)  {name}")
        print(f"      职责: {resp}")
    print("\n" + "=" * 100)
    print(f"阶段数: {len(STAGES)}   覆盖行数: {total} / 778")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
