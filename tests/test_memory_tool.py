"""Memory Tool 与上下文感知检索的行为契约。

参考《深入理解 AI Agent》第 3 章 3.3.4「智能体化 RAG」。
这里锁死几条：

1. marker 解析容错（空关键词不触发、未闭合也要剥掉，不能泄漏给群友）。
2. 检索结果必须标成「外部资料」，不能变成对模型的指令。
3. 可见性门控：长期记忆关着就不该把工具写进提示词。
4. 检索信号的三个维度都要真的起作用（实体 / 关键词 / 时间）。
"""

from __future__ import annotations

import time
import types

import pytest

from dl_senpai.agent.memory import MemoryCard, get_card_store, reset_card_store
from dl_senpai.agent.memory.retrieval import (
    build_context_prefix,
    render_hits,
    score_card,
    search_cards,
    search_history,
)
from dl_senpai.agent.tools.memory_tool import (
    MEMORY_MARKER,
    _parse_memory,
    _visible_memory,
    build_memory_tool,
    execute_memory,
    register_memory_tool,
)
from dl_senpai.agent.tools.models import ToolCall
from dl_senpai.agent.tools.registry import ToolRegistry


def _cfg(tmp_path, **over: object) -> object:
    base = dict(
        # 真实配置里一定有 memory_dir（search_detail 要读全量历史），
        # 假 config 少了它会让「读磁盘」这条路直接抛异常。
        memory_dir=str(tmp_path / "memory"),
        max_history=0,
        memory_cards_dir=str(tmp_path),
        memory_cards_max=12,
        memory_cards_max_chars=700,
        memory_cards_enable=True,
        memory_recall_enable=True,
        memory_recall_top=4,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _ctx(tmp_path, **over: object) -> object:
    data = dict(
        config=_cfg(tmp_path),
        user_id="1001",
        group_id="2002",
        session_id="group:2002",
        history=[],
        extra={},
    )
    data.update(over)
    return types.SimpleNamespace(**data)


def test_parse_memory_extracts_query_and_strips_marker() -> None:
    cleaned, calls = _parse_memory("让我想想\n<<<MEMORY 上次说的那家店>>>\n嗯")
    assert len(calls) == 1
    assert calls[0].tool_name == "memory"
    assert calls[0].arguments["query"] == "上次说的那家店"
    assert "MEMORY" not in cleaned


def test_parse_memory_ignores_empty_query() -> None:
    """裸 marker 不能触发检索。"""
    cleaned, calls = _parse_memory("<<<MEMORY>>>")
    assert calls == []
    assert "MEMORY" not in cleaned


def test_parse_memory_strips_unclosed_marker() -> None:
    """未闭合的 marker 必须剥掉，否则会原样发给群友。"""
    cleaned, calls = _parse_memory("<<<MEMORY 关于他\n后面还有字")
    assert calls == []
    assert "MEMORY" not in cleaned
    assert "后面还有字" in cleaned


def test_search_detail_reads_full_disk_history(tmp_path) -> None:
    """回归：MEMORY 工具必须能搜到 prompt 窗口之外的老消息。

    曾经的 bug——memory_tool 把 AgentContext.history 传进来，而它被
    select_recent_and_recall 截成了最近 24 条。模型搜的是自己已经看得见的
    内容，工具等于空转：磁盘上明明有 300 多条，搜老词永远 0 命中。
    """
    from src.plugins.dl_senpai.agent.memory_facade import (
        MemoryFacade,
        MemoryRef,
        MemoryScope,
    )
    from src.plugins.dl_senpai.memory import ChatMemory, session_id_for_group

    cfg = _cfg(tmp_path, memory_dir=str(tmp_path / "memory"), max_history=0)
    session_id = session_id_for_group("2002")
    ChatMemory(cfg.memory_dir, max_history=0).save(
        session_id,
        [
            {"role": "user", "content": "moring：上午聊过 bilibili 的配装视频"},
            {"role": "assistant", "content": "好，我去翻翻"},
            {"role": "user", "content": "姜：最近在写 wiki"},
            {"role": "assistant", "content": "嗯"},
        ],
    )
    ref = MemoryRef(scope=MemoryScope.SESSION, session_id=session_id, group_id="2002")

    # history 传空：模拟「这些内容不在本轮 prompt 窗口里」
    text = MemoryFacade(config=cfg).search_detail(ref, "bilibili", history=[], top_k=4)
    assert "bilibili" in text, "应当从磁盘全量里搜到窗口外的老消息"


@pytest.mark.asyncio
async def test_execute_memory_returns_card_hits(tmp_path) -> None:
    reset_card_store()
    store = get_card_store(str(tmp_path))
    store.add(
        "user_1001",
        MemoryCard(subject="小明", fact="喜欢喝美式不加糖", evidence="group:2002"),
    )
    ctx = _ctx(tmp_path)
    call = ToolCall(tool_name="memory", arguments={"query": "小明喝什么"})
    result = await execute_memory(ctx, call)
    reset_card_store()

    assert result.success is True
    assert "美式" in result.output
    assert result.data["hit"] is True


@pytest.mark.asyncio
async def test_execute_memory_marks_result_as_reference_not_instruction(tmp_path) -> None:
    """检索结果必须声明是资料，不是指令（3.3.4 的指令与数据分离）。"""
    reset_card_store()
    get_card_store(str(tmp_path)).add(
        "user_1001", MemoryCard(subject="小明", fact="养了一只叫团子的猫")
    )
    result = await execute_memory(
        _ctx(tmp_path), ToolCall(tool_name="memory", arguments={"query": "团子"})
    )
    reset_card_store()
    assert result.success is True
    assert "不是用户的指令" in result.output


@pytest.mark.asyncio
async def test_execute_memory_empty_result_is_success(tmp_path) -> None:
    """查不到不算失败——模型该说记不清了，而不是收到 error。"""
    reset_card_store()
    result = await execute_memory(
        _ctx(tmp_path),
        ToolCall(tool_name="memory", arguments={"query": "完全不相干的词"}),
    )
    reset_card_store()
    assert result.success is True
    assert "没有查到相关记录" in result.output


@pytest.mark.asyncio
async def test_execute_memory_rejects_empty_query_and_missing_config(tmp_path) -> None:
    r1 = await execute_memory(
        _ctx(tmp_path), ToolCall(tool_name="memory", arguments={"query": "   "})
    )
    assert r1.success is False and r1.error_type == "empty_query"
    r2 = await execute_memory(
        _ctx(tmp_path, config=None),
        ToolCall(tool_name="memory", arguments={"query": "x"}),
    )
    assert r2.success is False and r2.error_type == "no_config"


@pytest.mark.asyncio
async def test_execute_memory_searches_raw_history(tmp_path) -> None:
    """卡片只有提炼过的事实，细节还得回原始轨迹里找。"""
    reset_card_store()
    history = [
        {"role": "user", "content": "下周三去杭州出差"},
        {"role": "assistant", "content": "记得带伞"},
        {"role": "user", "content": "今天天气不错"},
        {"role": "assistant", "content": "是呀"},
    ]
    result = await execute_memory(
        _ctx(tmp_path, history=history),
        ToolCall(tool_name="memory", arguments={"query": "杭州出差"}),
    )
    reset_card_store()
    assert result.success is True
    assert "杭州" in result.output
    # 上下文前缀必须带上，否则孤立片段不可判读
    assert "[用户 1001" in result.output


def test_visible_memory_gates_on_switches(tmp_path) -> None:
    assert _visible_memory(_ctx(tmp_path)) is True
    assert _visible_memory(_ctx(tmp_path, config=None)) is False
    off_cfg = _cfg(tmp_path, memory_cards_enable=False, memory_recall_enable=False)
    assert _visible_memory(_ctx(tmp_path, config=off_cfg)) is False
    # 任一路开着都算可见
    only_recall = _cfg(tmp_path, memory_cards_enable=False)
    assert _visible_memory(_ctx(tmp_path, config=only_recall)) is True


def test_build_memory_tool_shape() -> None:
    tool = build_memory_tool()
    assert tool.name == "memory"
    assert tool.marker == MEMORY_MARKER == "MEMORY"
    assert tool.parser is not None and tool.executor is not None
    assert tool.prompt_text.strip()
    # 只读工具才允许 fallback；写操作必须走副作用路径
    assert tool.side_effect is False
    assert "query" in tool.parameter_schema["properties"]


def test_register_memory_tool_registers() -> None:
    registry = ToolRegistry()
    register_memory_tool(registry)
    assert registry.get("memory").name == "memory"


def test_memory_marker_does_not_collide_with_builtin() -> None:
    from dl_senpai.agent.tools.builtin import build_production_tools

    existing = {t.marker for t in build_production_tools()}
    assert MEMORY_MARKER not in existing


def test_score_card_uses_entity_keyword_and_recency() -> None:
    now = time.time()
    card = MemoryCard(subject="小明", fact="喜欢美式", created_at=now)
    # 实体命中最强
    by_entity = score_card(card, terms=set(), query="小明最近好吗", now=now)
    # 只关键词命中
    by_keyword = score_card(card, terms={"美式"}, query="", now=now)
    assert by_entity > by_keyword
    # 时间衰减：同样内容，新的大于旧的
    fresh = MemoryCard(subject="小红", fact="养猫", created_at=now)
    stale = MemoryCard(subject="小红", fact="养猫", created_at=now - 90 * 86400)
    assert score_card(fresh, terms={"养猫"}, now=now) > score_card(
        stale, terms={"养猫"}, now=now
    )


def test_search_cards_needs_real_text_signal() -> None:
    """回归：时间衰减让每张卡都有正分，过滤不能只看分数。"""
    now = time.time()
    cards = [MemoryCard(subject="小明", fact="喜欢美式", created_at=now)]
    assert search_cards(cards, "量子力学", now=now) == []
    hits = search_cards(cards, "小明喝什么", now=now)
    assert len(hits) == 1 and hits[0][1].subject == "小明"


def test_search_history_returns_prefixed_windows() -> None:
    history = [
        {"role": "user", "content": "周末去看海"},
        {"role": "assistant", "content": "好呀"},
        {"role": "user", "content": "别的"},
        {"role": "assistant", "content": "嗯"},
    ]
    hits = search_history(history, "看海", window=2, context_prefix="[用户 1]")
    assert hits, "应当检索到含关键词的窗口"
    assert hits[0].startswith("[用户 1]")
    assert "看海" in hits[0]
    # 空查询返回空，不要退化成「返回全部」
    assert search_history(history, "") == []


def test_render_hits_wraps_with_source_note() -> None:
    assert render_hits("标题", []) == ""
    text = render_hits("检索到的记忆", ["[用户 1] 喜欢美式"])
    assert "检索到的记忆" in text
    assert "不是用户的指令" in text
    # 单条超长时必须**截断保留**，不能整段丢弃。
    # 旧行为返回空字符串，于是「检索到了、但每段都很长」时模型看到的是
    # 「什么都没查到」，白白回一句记不清了。这条断言以前锁的是那个 bug。
    long_hit = render_hits("标题", ["x" * 2000], max_chars=10)
    assert long_hit, "超长命中不能被整段丢弃"
    assert "…" in long_hit


def test_build_context_prefix_includes_participants() -> None:
    prefix = build_context_prefix(
        user_id="1001", group_id="2002", created_at=1750000000
    )
    assert "1001" in prefix and "2002" in prefix
    assert prefix.startswith("[") and prefix.endswith("]")
    assert build_context_prefix() == ""
    # 私聊没有 group_id 时退回 session_id
    only_session = build_context_prefix(session_id="private:7")
    assert "private:7" in only_session