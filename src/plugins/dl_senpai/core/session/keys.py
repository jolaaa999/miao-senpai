"""Session 并发键：区分「记忆作用域」与「并发作用域」。

这是 Phase 3 一个容易做错的地方，值得单独成文件说明。

| 概念 | 取值 | 用途 |
|---|---|---|
| `session_id` | `group:{gid}` / `private:{uid}` | **记忆**作用域（ChatMemory 的 scope） |
| `agent_key` | `group:{gid}:user:{uid}` / `private:{uid}` | **并发**作用域（Session Executor 的 key） |

为什么不能让两者相等：如果把整个群作为串行单位，群里任何一个人触发一次
5 秒的 LLM 调用，其余所有人都要排队。100 人的群会直接不可用。

为什么也不能反过来让记忆按人隔离：学姐的人设是"群里的学姐"，她需要记得
群聊上下文（谁刚说了什么），所以记忆必须按群共享。

结论：**记忆按群共享，执行按人隔离**。
"""

from __future__ import annotations

from ...gateway.context import agent_session_key, session_id_for_group, session_id_for_private


def memory_scope_for_group(group_id: int | str) -> str:
    """记忆作用域（群）。"""
    return session_id_for_group(group_id)


def memory_scope_for_private(user_id: int | str) -> str:
    """记忆作用域（私聊）。"""
    return session_id_for_private(user_id)


def agent_key_for(group_id: int | str | None, user_id: int | str) -> str:
    """Agent 并发键。

    有群 → `group:{gid}:user:{uid}`；无群（私聊）→ `private:{uid}`。
    """
    if group_id:
        return f"group:{group_id}:user:{user_id}"
    return f"private:{user_id}"


__all__ = [
    "agent_session_key",
    "memory_scope_for_group",
    "memory_scope_for_private",
    "agent_key_for",
]
