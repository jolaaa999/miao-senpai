"""Phase 6.6：把 LegacyOrchestrator.run() 拆成语义阶段（MOE 版）。

关键设计：**用 `self.<stage>` 调用 + `state` 传参，但段内代码尽量原样。**

为了在 778 行规模下保证"行为不变"，采用最保守的策略：

    1. 读入 run() 的函数体源码
    2. 按语句边界切成 16 段
    3. 每段包成一个 async 方法，参数统一 (self, state, bot, event, config)
    4. 段内裸局部名 → state.<name>（只改数据访问，不改逻辑）
    5. run() 依次 await 各阶段

本脚本先做**干跑（dry-run）**，输出每段改写后的前几行供核对，
确认无误后再落盘。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "src" / "plugins" / "dl_senpai" / "agent" / "legacy_orchestrator.py"

# 需要替换为 state.<x> 的局部变量（来自 LegacyRunState 字段）
STATE_FIELDS = [
    "session_id", "text", "sticker_segments", "sticker_count", "reply_message",
    "photo_segments", "image_urls", "has_images", "image_count", "chat_text",
    "intent_text", "is_mentioned", "private_chat",
    "inner_key", "interrupt_scale", "inner_state_brief",
    "decision", "interrupt",
    "sender", "history", "memory_recall_brief", "group_style_brief",
    "card_enable", "mute_enable", "draw_enable", "browser_enable",
    "card_prompt", "candidates", "need_mod", "local_infer",
    "trends_brief", "trend_titles", "affection_brief", "aff_scope",
    "person_memory_brief", "person_memory_enable",
    "result", "reply", "was_searched",
    "image_request", "browse_intent", "browse_wanted", "card_intent",
    "applied_cards", "applied_mutes", "mute_block_reason",
    "sticker", "voice", "voice_override", "image", "draw_prompt",
    "draw_scene", "browse_images",
    "user_stored", "assistant_stored",
    "bundled_browse", "extra_browse",
]

# 局部临时变量（不放进 state，保持为方法局部变量）
LOCAL_ONLY = {
    "resolved", "istore", "istate", "t0", "elapsed", "foot", "applied",
    "seg", "photo", "preview", "spoken", "leaked", "leaked_browse",
    "at_ids", "explicit", "mutes", "filter_reason", "mute_ready",
    "applied_cards_", "playful_ready", "actions", "ack", "note",
    "draw_ready", "draw_intent_local", "browse_ready", "browse_tasks",
    "sticker_", "voice_", "updates", "aff", "_mute_results",
    "_mute_values", "_sticker_results", "_sticker_call",
    "_voice_results", "_voice_call", "_voice_values",
    "_draw_call", "_draw_results", "_draw_values",
    "_browse_call", "_browse_results", "_browse_values",
    "group_style_brief_", "istore_", "card_prompt_", "candidates_",
}


def get_run() -> tuple[ast.AsyncFunctionDef, list[str]]:
    text = LEGACY.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LegacyOrchestrator":
            for m in node.body:
                if isinstance(m, ast.AsyncFunctionDef) and m.name == "run":
                    return m, text.splitlines()
    raise SystemExit("找不到 run")


def rewrite_segment(lines: list[str]) -> str:
    """把段内的裸 state 字段改为 state.<field>。"""
    out: list[str] = []
    for line in lines:
        new = line
        for field in STATE_FIELDS:
            # 只替换"作为独立标识符"的出现，避免误伤 obj.field / "field"
            # 1) 左侧赋值：`field = ` / `field: T = ` / `field += `
            new = re.sub(
                rf"^(\s*)({field})(\s*(?::[^=]+)?(?:=|\+=|-=|\|=|&=)\s)",
                rf"\1state.{field}\3",
                new,
            )
            # 2) 右侧引用：前面不是 . 或 单词字符，后面不是 单词字符或 =
            new = re.sub(
                rf"(?<![\w.]){field}(?![\w])(?!\s*(?::[^=]+)?=(?!=))",
                f"state.{field}",
                new,
            )
        out.append(new)
    return "\n".join(out)


def main() -> int:
    run, lines = get_run()
    body_start = run.body[0].lineno
    body_end = run.end_lineno
    print(f"run(): L{run.lineno}-{body_end}, body L{body_start}-{body_end}")
    print(f"顶层语句数: {len(run.body)}")
    print("\n注意：本脚本只做干跑分析。实际拆分由后续脚本执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
