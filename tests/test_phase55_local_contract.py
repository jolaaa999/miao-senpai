"""Phase 5.5：本地 3B 降级路径的**真实契约** regression test（第三十三节）。

用户明确要求：不要继续照抄报告里的数字，直接查代码确认真实行为，
然后加一个明确的 regression test，以后以代码 contract 为准。
"""

from __future__ import annotations

import pytest

from tests.local_contract import (
    LOCAL_HISTORY_MESSAGES,
    LOCAL_MAX_TOKENS,
    LOCAL_TIMEOUT,
    assert_real_contract,
)


def test_constants_match_documented_contract() -> None:
    """llm.py 的常量必须与本文件记录的契约一致。

    如果将来有人改了这些值，本测试失败，提醒同步更新文档——
    避免"报告数字与代码漂移"再次发生。
    """
    assert_real_contract()


def test_local_history_limit_is_four_messages() -> None:
    """历史裁剪真实语义：**4 条 messages**（不是 4 轮，也不是 6）。"""
    from dl_senpai.llm import _trim_history_for_local

    history = [{"role": "user", "content": f"m{i}"} for i in range(20)]
    trimmed = _trim_history_for_local(history, LOCAL_HISTORY_MESSAGES)
    assert len(trimmed) == 4
    assert trimmed[-1]["content"] == "m19"


def test_local_history_limit_zero_means_unlimited() -> None:
    from dl_senpai.llm import _trim_history_for_local

    history = [{"role": "user", "content": f"m{i}"} for i in range(20)]
    assert len(_trim_history_for_local(history, 0)) == 20


def test_local_system_chars_constant_is_dead_code() -> None:
    """`_LOCAL_SYSTEM_CHARS` 是**死代码**，从未被任何逻辑读取。

    实测确认：本地推理时 system 会被 `_compact_local_system_prompt()`
    **整体替换**（不是截断），因此"system ≤ 800"这个说法是错的。

    本测试的作用是：如果将来有人真的启用了这个常量，
    测试会失败并提醒更新文档。
    """
    import inspect

    from dl_senpai import llm

    src = inspect.getsource(llm)
    # 常量定义处出现一次是正常的
    occurrences = [
        line for line in src.splitlines()
        if "_LOCAL_SYSTEM_CHARS" in line
    ]
    assert len(occurrences) == 1, (
        f"_LOCAL_SYSTEM_CHARS 被引用了 {len(occurrences)} 次，"
        "说明它不再是死代码 —— 请更新 local_contract.py 的说明"
    )
    assert llm._LOCAL_SYSTEM_CHARS == 800


def test_compact_local_system_prompt_is_short() -> None:
    """本地 system prompt 的真实长度（实测约 100 字符，远小于 800）。"""
    from dl_senpai.llm import _compact_local_system_prompt

    text = _compact_local_system_prompt()
    assert text
    assert len(text) < 800, "压缩后的 system 应远短于 800"
    # 必须包含风格说明与禁止项
    assert "回复" in text or "语气" in text


@pytest.mark.asyncio
async def test_local_path_trims_history_to_four(tmp_path) -> None:
    """端到端：本地端点时 messages 里的历史被裁到 4 条以内。"""
    from unittest.mock import AsyncMock, MagicMock

    from dl_senpai.config import PluginConfig
    from dl_senpai.llm import SenpaiLLM
    from dl_senpai.observability.retry import NoWaitRetryPolicy

    cfg = PluginConfig(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="http://127.0.0.1:8001/v1",   # 触发本地路径
        OPENAI_MODEL="local",
        DL_SENPAI_MAX_TOKENS=1600,
    )
    llm = SenpaiLLM(cfg, retry=NoWaitRetryPolicy())

    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        msg = MagicMock()
        msg.content = "短回复"
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = None
        return resp

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    llm.use_client(client)

    history = []
    for i in range(20):
        history.append({"role": "user", "content": f"u{i}"})
        history.append({"role": "assistant", "content": f"a{i}"})

    await llm.chat(history, "你好", sender_name="小明")

    messages = captured["messages"]
    # system + 最多 4 条历史 + 当前 user
    assert len(messages) <= 1 + LOCAL_HISTORY_MESSAGES + 1
    # max_tokens 被压到 96
    assert captured["max_tokens"] <= LOCAL_MAX_TOKENS


def test_timeout_constant() -> None:
    assert LOCAL_TIMEOUT == 120.0
