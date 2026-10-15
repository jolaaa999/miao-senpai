"""Phase 6.6：LegacyOrchestrator.run() 语义阶段拆分（正式执行）。

依据 scripts/phase66_stmts.py 输出的 108 个顶层语句行号，
把 run() 拆成 16 个语义阶段。

改写规则（AST 节点级，不用正则）：
    - 裸的 state 字段名 → `state.<field>`（Name 节点）
    - 其余一律不动

阶段方法签名统一：
    async def <stage>(self, state: LegacyRunState, bot, event, *, config) -> bool
返回 False 表示中止整个 run。
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "src" / "plugins" / "dl_senpai" / "agent" / "legacy_orchestrator.py"

# 阶段 → 属于它的顶层语句起始行
STAGE_STMTS: list[tuple[str, list[int], bool]] = [
    ("resolve_session_stage", [181, 182, 184], True),    ("prepare_message_context", [186, 189, 190, 198, 199], False),
    ("prepare_media_context", [200, 201, 202, 206, 207, 213, 214, 215, 221], False),
    ("prepare_interaction_context", [222, 225, 226, 246, 248, 252, 260], False),
    ("prepare_memory_context", [267, 279, 280, 281, 282], False),
    ("decide_trigger_stage", [295, 309, 313, 321, 322], True),
    ("validate_preconditions", [325, 334], True),
    (
        "prepare_prompt_context",
        [345, 346, 347, 348, 354, 356, 357, 365, 366, 367, 368, 369, 370,
         371, 372, 373, 376, 381, 394, 401],
        False,
    ),
    ("prepare_optional_context", [409, 410, 411, 419, 420, 421, 426, 427, 428], False),
    ("invoke_model", [437, 511], False),
    ("interpret_result", [520, 523, 524, 529], False),
    ("apply_card_and_mute", [527, 528, 530, 588, 665], False),
    ("prepare_media_response", [668, 675, 681, 682, 703, 709, 710, 711, 725], False),
    ("prepare_draw_and_browse", [748, 749, 750, 751, 752, 759, 807, 808], False),
    ("apply_state_updates", [850, 851, 861, 869, 871, 872, 873, 875, 904, 905], False),
    ("finalize_response", [926, 927, 928, 936, 939], False),
]

# LegacyRunState 的字段名（需要加 state. 前缀）
STATE_FIELDS = {
    "session_id", "text", "sticker_segments", "sticker_count", "reply_message",
    "photo_segments", "image_urls", "has_images", "image_count", "chat_text",
    "intent_text", "is_mentioned", "private_chat",
    "inner_key", "interrupt_scale", "inner_state_brief", "decision", "interrupt",
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
}


def indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + ln if ln.strip() else ln for ln in text.splitlines())


def main() -> int:
    src = LEGACY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()

    run = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LegacyOrchestrator":
            for m in node.body:
                if isinstance(m, ast.AsyncFunctionDef) and m.name == "run":
                    run = m
    if run is None:
        raise SystemExit("找不到 run")

    by_line = {s.lineno: s for s in run.body}

    # 校验：所有声明的行号都对应真实语句，且无遗漏
    declared = [n for _name, nums, _a in STAGE_STMTS for n in nums]
    missing = [n for n in declared if n not in by_line]
    if missing:
        raise SystemExit(f"行号不对应顶层语句: {missing}")
    unassigned = [s.lineno for s in run.body if s.lineno not in set(declared)]
    if unassigned:
        raise SystemExit(f"以下顶层语句未被分配: {unassigned}")

    # 生成阶段方法
    stage_methods: list[str] = []
    for name, stmts, can_abort in STAGE_STMTS:
        chunks: list[str] = []
        for ln in stmts:
            s = by_line[ln]
            seg = "\n".join(lines[s.lineno - 1 : (s.end_lineno or s.lineno)])
            chunks.append(seg)
        body = "\n".join(chunks)

        # -- Name 节点级改名：裸字段 → state.field --
        #
        # 用 ast.unparse 重新生成代码是不可接受的（会丢注释、改格式）。
        # 因此只在**原始文本**上按 AST 给出的 (行, 列) 精确替换。
        #
        # 注意：body 片段带 8 空格缩进，直接 ast.parse 会报
        # "unexpected indent"，因此先 dedent 解析，再按列偏移换算。
        dedented = textwrap.dedent(body)
        sub = ast.parse(dedented)

        # 哪些 Name 是"元组解包目标"（如 `session_id, _ = resolved`）——
        # 这些不能改成 state.session_id（语法非法）。先标记出来。
        tuple_targets: set[int] = set()
        for node in ast.walk(sub):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, (ast.Tuple, ast.List)):
                        for elt in ast.walk(t):
                            if isinstance(elt, ast.Name):
                                tuple_targets.add(id(elt))

        replacements: list[tuple[int, int, str, str]] = []
        for n in ast.walk(sub):
            if not isinstance(n, ast.Name) or n.id not in STATE_FIELDS:
                continue
            if id(n) in tuple_targets:
                # 元组解包目标默认保持局部；但 `session_id, _ = resolved`
                # 这个特例必须写入 state —— 后续所有阶段都读 state.session_id。
                if n.id == "session_id" and n.ctx is not None:
                    replacements.append(
                        (n.lineno, n.col_offset, n.id, "state.session_id")
                    )
                continue
            replacements.append((n.lineno, n.col_offset, n.id, f"state.{n.id}"))

        body_lines = body.splitlines()
        for ln, col, old, new in sorted(replacements, reverse=True):
            idx = ln - 1
            if not (0 <= idx < len(body_lines)):
                continue
            line = body_lines[idx]
            # dedent 后列号需加回原始缩进量
            pad = len(line) - len(line.lstrip())
            col_full = col + pad
            if line[col_full : col_full + len(old)] != old:
                continue
            body_lines[idx] = line[:col_full] + new + line[col_full + len(old) :]
        body = "\n".join(body_lines)

        ret = "return True"
        # run() 的语句体原本缩进 8（方法体内）。阶段方法体同样需要 8，
        # 因此**不需要再缩进** —— 直接拼即可。之前多加了一次 indent 导致
        # IndentationError。
        method = (
            f"    async def {name}(\n"
            f"        self,\n"
            f"        state: LegacyRunState,\n"
            f"        bot: Bot,\n"
            f"        event: MessageEvent,\n"
            f"        *,\n"
            f"        config: PluginConfig,\n"
            f"    ) -> bool:\n"
            f"{body}\n"
            f"        {ret}\n"
        )
        # 可中止阶段：把裸 `return`（value is None）改成 `return False`。
        #
        # 不限定 col_offset：这些 return 天然嵌在 `if resolved is None:`
        # 之类的条件里，缩进是 12 而不是 0。
        if can_abort:
            abort_lines: set[int] = set()
            for node in sub.body:  # sub 是 dedent 后的模块（阶段体）
                for stmt in ast.walk(node):
                    if isinstance(stmt, ast.Return) and stmt.value is None:
                        abort_lines.add(stmt.lineno)

            header_len = 8  # 方法签名占 8 行（def + self + state + bot + event + * + config + ") -> bool:"）
            out_lines = method.splitlines()
            for ln in abort_lines:
                idx = header_len + ln - 1
                if 0 <= idx < len(out_lines) and out_lines[idx].strip() == "return":
                    # 保持原缩进层级，只把 return 换成 return False
                    pad = len(out_lines[idx]) - len(out_lines[idx].lstrip())
                    out_lines[idx] = " " * pad + "return False"
            method = "\n".join(out_lines)
        stage_methods.append(method)
        print(f"  {name:32} {len(stmts):3} 语句")

    # 生成新 run()
    calls = "\n".join(
        f"        if not await self.{name}(state, bot, event, config=config):\n"
        f"            return"
        for name, _s, _a in STAGE_STMTS
    )
    new_run = (
        "    async def run(\n"
        "        self,\n"
        "        bot: Bot,\n"
        "        event: MessageEvent,\n"
        "        *,\n"
        "        force_mention: bool,\n"
        "        config: PluginConfig,\n"
        "    ) -> None:\n"
        '        """Legacy 编排入口（Phase 6.6 拆分后）。\n'
        "\n"
        "        本方法只做 orchestration：构造 state 并顺序调用 16 个语义阶段。\n"
        "        具体业务在各自阶段方法里，便于独立测试与演进。\n"
        "\n"
        "        阶段返回 False 表示中止（会话无法解析 / 无需回复 / 前置条件不满足）。\n"
        '        """\n'
        "        state = LegacyRunState()\n"
        "        state.force_mention = force_mention\n"
        f"{calls}\n"
    )

    # 组装：把 run() 的整段替换掉
    head = "\n".join(lines[: run.lineno - 1])
    tail = "\n".join(lines[run.end_lineno :])
    out = head + "\n" + new_run + "\n" + "\n".join(stage_methods) + "\n" + tail

    target = LEGACY.with_suffix(".py.new")
    target.write_text(out, encoding="utf-8")
    print(f"\n已写出 {target.name}（{len(out.splitlines())} 行）")
    print("请人工核对后替换。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
