"""Phase 5.5：把 handlers._handle_chat_inner 的 memory 调用改为走 MemoryFacade。

第十一/十二节要求：Agent orchestration 只能通过 MemoryFacade 访问记忆。
本脚本做精确的文本替换，并打印每处改动以便复核。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "src" / "plugins" / "dl_senpai" / "handlers.py"

# (旧片段, 新片段, 说明)
REPLACEMENTS: list[tuple[str, str, str]] = [
    (
        "            get_group_style_store(config.group_style_dir).observe(\n"
        "                event.group_id, text or intent_text\n"
        "            )",
        "            _facade.observe_group_text(\n"
        "                event.group_id, text or intent_text, config\n"
        "            )",
        "group_style 写入 -> Facade",
    ),
    (
        "            istore = get_inner_state_store(config.inner_state_dir)\n"
        "            istate = istore.load(inner_key)\n"
        "            interrupt_scale = interrupt_prob_multiplier(istate)\n"
        "            inner_state_brief = build_inner_state_brief(istate)",
        "            istate = _facade.load_inner_state(inner_key, config)\n"
        "            interrupt_scale = _facade.inner_state_interrupt_multiplier(istate)\n"
        "            inner_state_brief = _facade.build_inner_state_brief(istate)",
        "inner_state 读取 -> Facade",
    ),
    (
        "    memory = _get_memory(config)\n"
        "    raw_history = memory.load_all(session_id)\n"
        "    history, memory_recall_brief = messages_for_llm_with_recall(\n"
        "        raw_history,\n"
        "        intent_text or chat_text,\n"
        "        recent_turns=config.memory_recent_turns,\n"
        "        recall_top=config.memory_recall_top,\n"
        "        enable=_fe(config, event, \"memory_recall\"),\n"
        "    )",
        "    _facade = _get_memory_facade()\n"
        "    _mem_ref = MemoryRef.for_session(session_id)\n"
        "    history, memory_recall_brief = _facade.load_history_for_prompt(\n"
        "        _mem_ref,\n"
        "        intent_text or chat_text,\n"
        "        config,\n"
        "        enable_recall=_fe(config, event, \"memory_recall\"),\n"
        "    )",
        "会话历史读取 -> Facade",
    ),
    (
        "            group_style_brief = build_group_style_brief(\n"
        "                get_group_style_store(config.group_style_dir),\n"
        "                event.group_id,\n"
        "                top_n=config.group_style_top_n,\n"
        "                min_count=config.group_style_min_count,\n"
        "                short=bool(interrupt),\n"
        "            )",
        "            group_style_brief = _facade.get_group_style(\n"
        "                event.group_id, config, short=bool(interrupt)\n"
        "            )",
        "group_style 读取 -> Facade",
    ),
    (
        "            hot_items = await get_hot_items(limit=config.trends_max_items)\n"
        "            trend_titles = [i.title for i in hot_items if i.title]\n"
        "            trends_brief = await get_trends_brief(\n"
        "                max_items=config.trends_max_items,\n"
        "                max_chars=config.trends_prompt_max_chars,\n"
        "                interrupt=interrupt,\n"
        "            )",
        "            trends_brief, trend_titles = await _facade.get_trends(\n"
        "                config, interrupt=interrupt\n"
        "            )",
        "trends 读取 -> Facade",
    ),
    (
        "        aff_user = get_affection_store(config.affection_dir).get_user(\n"
        "            aff_scope, event.user_id\n"
        "        )\n"
        "        affection_brief = build_affection_brief(name=sender, value=aff_user.value)",
        "        affection_brief, _ = _facade.get_affection(\n"
        "            aff_scope, str(event.user_id), config\n"
        "        )",
        "affection 读取 -> Facade",
    ),
    (
        "        pm_store = get_person_memory_store(\n"
        "            config.person_memory_dir,\n"
        "            max_traits=config.person_memory_max_traits,\n"
        "            max_note_chars=config.person_memory_max_note_chars,\n"
        "        )\n"
        "        # 每次互动都记下当前昵称，便于跨群认出同一个人\n"
        "        profile = pm_store.touch_name(event.user_id, sender)\n"
        "        person_memory_brief = build_person_memory_brief(\n"
        "            profile,\n"
        "            name=sender,\n"
        "            query=intent_text or chat_text,\n"
        "        )",
        "        # 每次互动都记下当前昵称，便于跨群认出同一个人\n"
        "        person_memory_brief, _profile = _facade.get_person_profile(\n"
        "            MemoryRef.for_user(str(event.user_id), group_id=str(event.group_id)),\n"
        "            sender,\n"
        "            intent_text or chat_text,\n"
        "            config,\n"
        "        )",
        "person_memory 读取 -> Facade",
    ),
    (
        "    memory.append_turn(session_id, user_stored, assistant_stored)",
        "    _facade.record_turn(_mem_ref, user_stored, assistant_stored, config)",
        "会话历史写入 -> Facade",
    ),
    (
        "                get_person_memory_store(\n"
        "                    config.person_memory_dir,\n"
        "                    max_traits=config.person_memory_max_traits,\n"
        "                    max_note_chars=config.person_memory_max_note_chars,\n"
        "                ).apply_updates(\n"
        "                    event.user_id,\n"
        "                    updates,\n"
        "                    display_name=sender,\n"
        "                )",
        "                _facade.record_person_updates(\n"
        "                    MemoryRef.for_user(\n"
        "                        str(event.user_id), group_id=str(event.group_id)\n"
        "                    ),\n"
        "                    updates,\n"
        "                    sender,\n"
        "                    config,\n"
        "                )",
        "person_memory 写入 -> Facade",
    ),
    (
        "        aff = get_affection_store(config.affection_dir).add_chat(\n"
        "            aff_scope,\n"
        "            event.user_id,\n"
        "            config.affection_gain_chat,\n"
        "            display_name=sender,\n"
        "        )",
        "        aff = _facade.record_interaction(\n"
        "            aff_scope,\n"
        "            str(event.user_id),\n"
        "            config.affection_gain_chat,\n"
        "            sender,\n"
        "            config,\n"
        "        )",
        "affection 写入 -> Facade",
    ),
    (
        "            get_inner_state_store(config.inner_state_dir).apply_after_reply(\n"
        "                inner_key,\n"
        "                reply_len=len(reply or \"\"),\n"
        "                mentioned=bool(is_mentioned),\n"
        "                interrupt=bool(interrupt),\n"
        "                private_chat=isinstance(event, PrivateMessageEvent),\n"
        "            )",
        "            _facade.update_inner_state(\n"
        "                inner_key,\n"
        "                reply_len=len(reply or \"\"),\n"
        "                mentioned=bool(is_mentioned),\n"
        "                interrupt=bool(interrupt),\n"
        "                private_chat=isinstance(event, PrivateMessageEvent),\n"
        "                config=config,\n"
        "            )",
        "inner_state 写入 -> Facade",
    ),
]


def main() -> int:
    text = HANDLERS.read_text(encoding="utf-8")
    applied = 0
    missing: list[str] = []

    for old, new, label in REPLACEMENTS:
        if old not in text:
            missing.append(label)
            continue
        if text.count(old) != 1:
            missing.append(f"{label} (出现 {text.count(old)} 次，不唯一)")
            continue
        text = text.replace(old, new)
        applied += 1
        print(f"  OK  {label}")

    for label in missing:
        print(f"  MISS {label}")

    HANDLERS.write_text(text, encoding="utf-8")
    print(f"\n应用 {applied}/{len(REPLACEMENTS)} 处替换")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
