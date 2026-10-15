"""引用消息可见 + 上下文记忆（runtime 路径）。

背景：runtime 只负责"怎么问模型"，从不装配上下文。切换到 runtime 后
AgentContext.history 一直是空的、被引用消息也传不进去，导致模型每次都对着
一条孤立消息作答，而且新对话从不落盘（记忆目录最新文件停在切换那天）。

本文件覆盖桥接层补上的三件事：引用合成、历史加载、发送成功后落盘。
"""

from __future__ import annotations

from pathlib import Path

from dl_senpai import legacy_bridge as bridge
from dl_senpai.agent import response as response_mod
from dl_senpai.config import PluginConfig
from dl_senpai.memory import ChatMemory


# -- 桩件 ----------------------------------------------------------


class _Seg:
    """OneBot 消息段替身：extract_message_plaintext 只读 type/data。"""

    def __init__(self, type_: str, **data) -> None:
        self.type = type_
        self.data = data


class _Sender:
    def __init__(self, nickname: str) -> None:
        self.nickname = nickname
        self.card = ""
        self.user_id = "7"


class _Reply:
    def __init__(self, text: str, nickname: str = "小明") -> None:
        self.message = [_Seg("text", text=text)]
        self.sender = _Sender(nickname)
        self.message_id = 555


class _Event:
    def __init__(self, reply=None) -> None:
        self.reply = reply


class _Ctx:
    def __init__(self, text: str, *, config=None, reply=None, session_id="group:100") -> None:
        self.plain_text = text
        self.session_id = session_id
        self.is_private = False
        self.trace_id = "trace-1"
        self.raw_event = _Event(reply)
        self.bot = object()
        self.extra = {"config": config} if config is not None else {}


def _cfg(tmp_path: Path, **kw):
    """落盘目录必须全部指向 tmp_path，否则测试会读写生产数据。

    只覆盖 memory_dir 不够：_deliver_runtime_reply 现在还会结算内心状态、
    回复落盘，用生产默认目录就会写进 data/dl_senpai。
    """
    base = {
        "DL_SENPAI_MEMORY_DIR": str(tmp_path / "memory"),
        "DL_SENPAI_INNER_STATE_DIR": str(tmp_path / "inner_state"),
        "DL_SENPAI_AFFECTION_DIR": str(tmp_path / "affection"),
        "DL_SENPAI_GROUP_STYLE_DIR": str(tmp_path / "group_style"),
        "DL_SENPAI_RECENT_CHAT_DIR": str(tmp_path / "recent_chat"),
        "DL_SENPAI_MEMORY_CARDS_DIR": str(tmp_path / "memory_cards"),
    }
    base.update(kw)
    return PluginConfig(**base)


# ================================================================
# 1. 引用消息进入用户输入
# ================================================================


def test_reply_quote_is_prefixed_into_user_text(tmp_path: Path) -> None:
    ctx = _Ctx("那它怎么解决", config=_cfg(tmp_path), reply=_Reply("什么是死锁"))
    agent_context = bridge.build_agent_context(ctx)
    assert "【引用消息】小明：什么是死锁" in agent_context.user_text
    assert "【我现在说的】那它怎么解决" in agent_context.user_text


def test_without_reply_user_text_is_plain(tmp_path: Path) -> None:
    """没有引用时不能凭空多出引用块。"""
    ctx = _Ctx("普通一句话啊啊", config=_cfg(tmp_path))
    agent_context = bridge.build_agent_context(ctx)
    assert agent_context.user_text == "普通一句话啊啊"


def test_plain_text_is_not_polluted(tmp_path: Path) -> None:
    """plain_text 必须保持原始消息——权限/命令匹配都在读它。"""
    ctx = _Ctx("那它怎么解决", config=_cfg(tmp_path), reply=_Reply("什么是死锁"))
    agent_context = bridge.build_agent_context(ctx)
    assert ctx.plain_text == "那它怎么解决"
    assert agent_context.plain_text == "那它怎么解决"


def test_quote_resolution_failure_falls_back(tmp_path: Path) -> None:
    """引用解析炸了也不能拦掉正常回复。"""

    class _Boom:
        @property
        def reply(self):
            raise RuntimeError("boom")

    ctx = _Ctx("还在吗在吗", config=_cfg(tmp_path))
    ctx.raw_event = _Boom()
    agent_context = bridge.build_agent_context(ctx)
    assert agent_context.user_text == "还在吗在吗"


# ================================================================
# 2. 上下文记忆
# ================================================================


def test_history_is_loaded_into_agent_context(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    ChatMemory(cfg.memory_dir).append_turn("group:100", "旧问题", "旧回答")
    agent_context = bridge.build_agent_context(_Ctx("新问题", config=cfg))
    contents = [m["content"] for m in agent_context.history]
    assert "旧问题" in contents
    assert "旧回答" in contents


def test_history_is_empty_for_new_session(tmp_path: Path) -> None:
    agent_context = bridge.build_agent_context(_Ctx("第一次说话", config=_cfg(tmp_path)))
    assert agent_context.history == []


def test_memory_load_failure_is_silent(tmp_path: Path) -> None:
    """记忆读不出来最多是没上下文，绝不能让用户收不到回复。"""

    class _BadCfg:
        memory_dir = "Z:/definitely/not/here"
        max_history = 0
        memory_recent_turns = 24
        memory_recall_top = 4
        memory_recall_enable = True

    ctx = _Ctx("还能说话吗", config=_BadCfg())
    agent_context = bridge.build_agent_context(ctx)
    assert agent_context.history == []
    assert agent_context.user_text == "还能说话吗"


# ================================================================
# 3. 发送成功后落盘
# ================================================================


class _Delivered:
    def __init__(self, sent: bool, error: str = "", text: str = "") -> None:
        self.sent = sent
        self.error = error
        # 生产里 ResponseOutcome.text 是**润色后**的发送文本；缺省空串表示
        # 这个替身不关心润色，此时桥接层回退到 outcome.result.text。
        self.text = text


def _pipe(sent: bool, text: str = ""):
    class _P:
        async def deliver(self, result, context, *, dry_run=False, reply_options=None):
            return _Delivered(sent, text=text)

    return _P


class _Result:
    def __init__(self, text: str) -> None:
        self.text = text


class _Outcome:
    def __init__(self, text: str) -> None:
        self.result = _Result(text)


class _AgentCtx:
    def __init__(self, cfg=None) -> None:
        self.session_id = "group:100"
        self.user_text = "用户问题"
        self.plain_text = "用户问题"
        self.config = cfg


async def test_record_turn_after_successful_send(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(bridge, "_record_turn", lambda c, u, a: calls.append((u, a)))
    monkeypatch.setattr(response_mod, "ResponsePipeline", _pipe(True, "回复正文"))
    # 必须给 tmp 配置：_deliver_runtime_reply 现在还会结算内心状态，传 None
    # 会回退到真实 get_config()，把状态写进生产目录。
    await bridge._deliver_runtime_reply(_AgentCtx(_cfg(tmp_path)), _Outcome("回复正文"))
    assert calls == [("用户问题", "回复正文")]


async def test_no_record_when_send_failed(monkeypatch, tmp_path: Path) -> None:
    """没发出去的回复若写进历史，下一轮模型会把它当成已发生的事实。"""
    calls: list[tuple] = []
    monkeypatch.setattr(bridge, "_record_turn", lambda c, u, a: calls.append((u, a)))
    monkeypatch.setattr(response_mod, "ResponsePipeline", _pipe(False))
    await bridge._deliver_runtime_reply(_AgentCtx(_cfg(tmp_path)), _Outcome("回复正文"))
    assert calls == []


def test_record_turn_writes_to_disk(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    ctx = _AgentCtx()
    ctx.config = cfg
    bridge._record_turn(ctx, "问了一句", "答了一句")
    saved = ChatMemory(cfg.memory_dir).load("group:100")
    assert [m["content"] for m in saved] == ["问了一句", "答了一句"]


def test_record_turn_skips_empty_reply(tmp_path: Path) -> None:
    """空回复不该污染历史。"""
    cfg = _cfg(tmp_path)
    ctx = _AgentCtx()
    ctx.config = cfg
    bridge._record_turn(ctx, "问了一句", "")
    assert ChatMemory(cfg.memory_dir).load("group:100") == []


async def test_history_stores_the_polished_text_that_was_sent(
    monkeypatch, tmp_path: Path
) -> None:
    """历史里存的必须是真正发出去的那条，不是润色前的原文。

    为什么：response.py 发送前先 `plan.text = polisher(plan.text)`。存润色前的
    原文，等于把 markdown 排版通过历史召回又喂回 prompt，和去 AI 味直接对冲。
    """
    calls: list[tuple] = []
    monkeypatch.setattr(bridge, "_record_turn", lambda c, u, a: calls.append((u, a)))
    monkeypatch.setattr(response_mod, "ResponsePipeline", _pipe(True, "润色后的正文"))
    await bridge._deliver_runtime_reply(
        _AgentCtx(_cfg(tmp_path)), _Outcome("润色**前**的正文")
    )
    assert calls == [("用户问题", "润色后的正文")]


async def test_history_falls_back_to_raw_when_outcome_has_no_text(
    monkeypatch, tmp_path: Path
) -> None:
    """发送结果没带正文时回退到原文，不能把这一轮记成空。"""
    calls: list[tuple] = []
    monkeypatch.setattr(bridge, "_record_turn", lambda c, u, a: calls.append((u, a)))
    monkeypatch.setattr(response_mod, "ResponsePipeline", _pipe(True))
    await bridge._deliver_runtime_reply(_AgentCtx(_cfg(tmp_path)), _Outcome("原文正文"))
    assert calls == [("用户问题", "原文正文")]