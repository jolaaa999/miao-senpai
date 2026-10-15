"""本地 3B 降级路径的**真实契约**（Phase 5.5 实测，非照抄报告）。

第三十三节要求：不要继续照抄报告里的数字，直接检查代码确认。

实测结论（`src/plugins/dl_senpai/llm.py`）：

| 常量 | 声明值 | 是否真正生效 |
|---|---|---|
| `_LOCAL_MAX_TOKENS` | 96 | ✅ 生效（min(max_tokens, 96)） |
| `_LOCAL_HISTORY_MESSAGES` | 4 | ✅ 生效（`_trim_history_for_local`） |
| `_LOCAL_TIMEOUT` | 120.0 | ✅ 生效 |
| `_LOCAL_SYSTEM_CHARS` | 800 | ❌ **死代码** —— 从未被读取 |

关于 `_LOCAL_SYSTEM_CHARS`：本地推理时 system 消息会被
`_compact_local_system_prompt()` **整体替换**（不是截断），
而该函数产出的文本实测约 100 字符，与 800 这个数字无关。
因此"system ≤ 800"是**错误描述**——真实行为是"system 被替换成短提示"。

历史裁剪的真实语义（重要）：
    `_trim_history_for_local(rest, 4)` 取的是 **messages 条数**，
    且 `rest` 已经过 `_strip_echo_assistants` 去重。
    之后 `_normalize_local_chat_messages` 还会：
      - 跳过开头的孤立 assistant
      - 合并相邻 user
      - 丢弃末尾 assistant（必须以 user 结尾）
    因此最终 messages 长度可能**小于** 4+2。

Phase 4 报告里写的 "history ≤ 6" 是实测上限（system + 4 + current），
不是常量本身。本文件以常量语义为准。
"""

from __future__ import annotations

# 真实生效的常量值（供 regression test 断言）
LOCAL_MAX_TOKENS = 96
LOCAL_HISTORY_MESSAGES = 4
LOCAL_TIMEOUT = 120.0

# 已知的死代码：声明但从未使用。
# 保留它不是鼓励使用，而是**明确标注**避免后人误以为它有约束力。
LOCAL_SYSTEM_CHARS_IS_DEAD = True


def assert_real_contract() -> None:
    """断言 llm.py 中的常量与本文档一致。

    如果将来有人真的启用了 `_LOCAL_SYSTEM_CHARS`，这个断言会失败，
    提醒更新文档——避免"报告数字与代码漂移"再次发生。
    """
    from dl_senpai import llm

    assert llm._LOCAL_MAX_TOKENS == LOCAL_MAX_TOKENS
    assert llm._LOCAL_HISTORY_MESSAGES == LOCAL_HISTORY_MESSAGES
    assert llm._LOCAL_TIMEOUT == LOCAL_TIMEOUT
    # 该常量存在但不应被使用
    assert llm._LOCAL_SYSTEM_CHARS == 800


__all__ = [
    "LOCAL_MAX_TOKENS",
    "LOCAL_HISTORY_MESSAGES",
    "LOCAL_TIMEOUT",
    "LOCAL_SYSTEM_CHARS_IS_DEAD",
    "assert_real_contract",
]
