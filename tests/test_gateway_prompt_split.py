"""锁死网关的消息拆分契约（本次修复的真实缺陷）。

**修复的缺陷**：`LegacyModelGateway.chat` 把 `request.messages`（含 runtime 拼好的
system prompt）整包当成 `history` 交给 `llm.chat`，而 `_looks_like_history` 恒为真。
于是：

1. runtime 的提示词被降级成一条普通历史消息——真正生效的 system 是
   `llm.build_system_prompt()` 重新拼的那份，而网关调它时所有 brief 都是空串；
2. 末尾那条 user 消息既进了 history、又被当作 user_text 再发一次，
   人称还叠了一层（群友：小明：你好）；
3. `ModelRequest` 没有图片字段，视觉开关打开也传不进模型。

这些测试保证三条都不会回来。
"""

from __future__ import annotations

from typing import Any

import pytest

from dl_senpai.agent.context import AgentContext
from dl_senpai.agent.model_gateway import (
    LegacyModelGateway,
    ModelRequest,
    StubModelGateway,
    _extract_user_text,
    _split_history,
    _split_history as split_history,
)
from dl_senpai.agent.runtime import AgentRuntime
from dl_senpai.gateway import MessageContext
from dl_senpai.legacy_bridge import _build_status_bar


class _FakeLLM:
    """记录 llm.chat 收到的参数，不碰网络。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, history: Any, user_text: str, **kw: Any) -> Any:
        self.calls.append({"history": history, "user_text": user_text, **kw})

        class _R:
            text = "好"

        return _R()


class _Ctx:
    actor = "群友"
    config = None


def test_split_history_removes_system_and_trailing_user() -> None:
    """兜底拆分必须剥掉 system 与末尾的当前输入，否则会重复发送。"""
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "上一轮"},
        {"role": "assistant", "content": "上一轮回复"},
        {"role": "user", "content": "本轮"},
    ]
    assert split_history(msgs) == [
        {"role": "user", "content": "上一轮"},
        {"role": "assistant", "content": "上一轮回复"},
    ]
    # 只有 system 时，history 为空
    assert split_history([{"role": "system", "content": "SYS"}]) == []
    assert split_history([]) == []


@pytest.mark.asyncio
async def test_gateway_uses_runtime_system_prompt() -> None:
    """runtime 的 system prompt 必须原样成为 system，而不是混进 history。"""
    llm = _FakeLLM()
    gw = LegacyModelGateway(llm=llm)
    req = ModelRequest(
        messages=[
            {"role": "system", "content": "RUNTIME-SYS"},
            {"role": "assistant", "content": "在的"},
            {"role": "user", "content": "群友：你好"},
        ],
        system_prompt="RUNTIME-SYS",
        history=[{"role": "assistant", "content": "在的"}],
        user_text="你好",
    )
    await gw.chat(req, _Ctx())

    call = llm.calls[0]
    assert call["system_prompt_override"] == "RUNTIME-SYS"
    # 历史里绝不能出现 system 那条
    assert all(h.get("role") != "system" for h in call["history"])
    assert call["history"] == [{"role": "assistant", "content": "在的"}]
    # 用户文本传原文，前缀由 llm.chat 按 sender_name 拼一次
    assert call["user_text"] == "你好"


@pytest.mark.asyncio
async def test_gateway_falls_back_when_fields_missing() -> None:
    """老调用方只填 messages 时，网关要自己拆，行为与结构化字段一致。"""
    llm = _FakeLLM()
    gw = LegacyModelGateway(llm=llm)
    req = ModelRequest(
        messages=[
            {"role": "system", "content": "SYS"},
            {"role": "assistant", "content": "在的"},
            {"role": "user", "content": "群友：你好"},
        ],
    )
    await gw.chat(req, _Ctx())

    call = llm.calls[0]
    assert call["history"] == [{"role": "assistant", "content": "在的"}]
    assert call["user_text"] == "群友：你好"
    # 没给 system_prompt 时保持旧行为（交给 llm 自己拼），不能凭空造一个
    assert call["system_prompt_override"] == ""


@pytest.mark.asyncio
async def test_gateway_forwards_images() -> None:
    """图片必须透传，否则视觉开关打开也等于没开。"""
    llm = _FakeLLM()
    gw = LegacyModelGateway(llm=llm)
    urls = ["data:image/png;base64,AAA", "https://example.com/b.png"]
    await gw.chat(ModelRequest(messages=[], image_urls=urls), _Ctx())
    assert llm.calls[0]["image_urls"] == urls
    # 没有图片时传 None，保持 llm.chat 的旧默认
    await gw.chat(ModelRequest(messages=[]), _Ctx())
    assert llm.calls[1]["image_urls"] is None


@pytest.mark.asyncio
async def test_runtime_call_model_fills_structured_fields() -> None:
    """runtime 必须把 system / history / user_text / 图片显式填进请求。"""
    gw = StubModelGateway(["好"])
    rt = AgentRuntime(gateway=gw)
    msg = MessageContext(
        message_type="group", group_id="1", user_id="2",
        session_id="group:1", plain_text="你好", actor="小明",
    )
    history = [{"role": "assistant", "content": "在的"}]
    ctx = AgentContext(
        session_id="group:1", message_context=msg, history=list(history),
        extra={"image_urls": ["data:image/png;base64,AAA"], "agent_status": "<agent_status>\n- x\n</agent_status>"},
    )
    await rt._call_model(ctx, "SYS", list(history), "你好")

    req = gw.requests[0]
    assert req.system_prompt == "SYS"
    assert req.history == history
    assert req.user_text == "你好"
    assert req.image_urls == ["data:image/png;base64,AAA"]
    # messages 仍然完整（system 在首位、用户消息只放原话、状态栏更靠后）
    assert req.messages[0] == {"role": "system", "content": "SYS"}
    assert req.messages[1] == {"role": "assistant", "content": "在的"}
    # 正文不带「昵称：」前缀：昵称进正文会被模型当成发言内容（见 runtime 注释）
    assert req.messages[2]["content"] == "你好"
    assert req.messages[-1]["content"].startswith("<agent_status>")


def test_status_bar_reports_time_and_scene() -> None:
    """状态栏内容必须是代码算出的确定事实。"""
    msg = MessageContext(message_type="group", group_id="1", user_id="2")
    bar = _build_status_bar(msg, {})
    assert bar.startswith("<agent_status>")
    assert bar.rstrip().endswith("</agent_status>")
    assert "场景：群聊" in bar
    assert "当前时间：" in bar


def test_status_bar_flags_images_and_private() -> None:
    """带图与私聊都要体现出来。"""
    msg = MessageContext(message_type="private", user_id="2", is_private=True)
    bar = _build_status_bar(msg, {"image_urls": ["data:image/png;base64,AAA"]})
    assert "场景：私聊" in bar
    assert "本轮带图：是" in bar


def test_extract_user_text_still_works() -> None:
    """老的兜底取值不能坏（它还被别处依赖）。"""
    assert _extract_user_text([{"role": "user", "content": "hi"}]) == "hi"
    assert _extract_user_text([]) == ""

def test_status_bar_declares_current_speaker() -> None:
    """说话人必须在状态栏里声明。

    昵称是用户可控文本（「失语.」这种自带句点的），写进正文会被模型
    当成发言内容来解释。状态栏是代码算出的元数据，模型不会把它当对话。
    """
    msg = MessageContext(
        message_type="private", user_id="2", is_private=True, actor="失语.",
    )
    bar = _build_status_bar(msg, {})
    assert "正在跟你说话的人：失语." in bar
    # 昵称不能出现在正文位置：正文由 runtime 单独拼，状态栏只做声明
    assert bar.count("失语.") == 1
