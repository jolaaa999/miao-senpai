"""Validation 层：隐私脱敏与内容策略。

Phase 7A 第七/八/九/十节。

**核心约束**：真实运行数据的价值在于"能比较行为"，
不在于"能看到用户说了什么"。因此本模块把隐私保护做在**最底层**——
所有写入 Validation 的数据都必须先过这里。

    真实 id  ──HMAC-SHA256(salt)──→  稳定匿名 id
    用户文本 ──redact──→ 只留 length/hash/结构标志
    Prompt   ──summarize──→ 只留 length/section/tool/hash

为什么用 HMAC 而不是裸 SHA256：
    裸哈希对 6~10 位的 QQ 号可以穷举反查（QQ 号空间很小）。
    加盐后即使拿到 Validation 数据也无法反推真实 QQ 号。
    盐存在独立位置（config / 环境变量），不随数据一起导出。

**为什么匿名 id 必须稳定**：
    否则无法统计"同一用户"的行为分布（成本 per user、
    重复提问率等）。HMAC 保证同一输入永远同一输出。
"""

from __future__ import annotations

from ..config_env import get_env, get_flag

import hashlib
import hmac
import os
import re
from typing import Any

# 默认盐。
#
# 生产应通过 `DL_SENPAI_VALIDATION_SALT` 覆盖。
# 默认值存在的意义：没有配置时仍然脱敏（而不是退化成明文）。
# **注意**：默认值公开，因此只能防"随手看到"，不能防定向反查。
# 真实部署必须设自己的盐。
DEFAULT_SALT = "dl-senpai-validation-v1"

_SALT_ENV = "DL_SENPAI_VALIDATION_SALT"


def _salt() -> bytes:
    return (get_env(_SALT_ENV, "") or DEFAULT_SALT).encode("utf-8")


def anonymize(value: Any, *, kind: str = "id") -> str:
    """把真实 id 转成稳定的匿名 id。

    Args:
        value: 原始 id（QQ 号 / 群号 / session_id）。
        kind: 命名空间前缀，避免不同类型 id 撞名
              （同一个数字既是群号又是用户号时不应混淆）。

    Returns:
        形如 `u_3f2a1b8c9d0e4f5a` 的短标识。
    """
    if value is None or value == "":
        return ""
    prefix = {"user": "u", "group": "g", "session": "s"}.get(kind, "x")
    msg = f"{kind}:{value}".encode("utf-8")
    digest = hmac.new(_salt(), msg, hashlib.sha256).hexdigest()
    return f"{prefix}_{digest[:16]}"


def content_hash(text: str) -> str:
    """文本的短哈希（用于去重与关联，不可逆）。

    同样加盐 —— 短文本（如"在吗"）的裸哈希容易被字典反查。
    """
    if not text:
        return ""
    digest = hmac.new(_salt(), text.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:16]


# -- 密钥泄露防护（第三十九节）--

# 这些模式出现在任何 Validation 字段里都应被视为事故。
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),            # OpenAI 风格
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{10,}", re.I),
    re.compile(r"Authorization\s*[:=]", re.I),
    re.compile(r"api[_-]?key\s*[:=]\s*\S+", re.I),
    re.compile(r"(password|passwd|pwd)\s*[:=]\s*\S+", re.I),
    re.compile(r"(token|secret)\s*[:=]\s*[A-Za-z0-9._\-]{12,}", re.I),
]


def contains_secret(value: Any) -> bool:
    """值里是否含疑似密钥（用于测试与运行期兜底断言）。"""
    text = value if isinstance(value, str) else str(value)
    return any(p.search(text) for p in _SECRET_PATTERNS)


def redact_secrets(value: str) -> str:
    """把疑似密钥替换成占位符（运行期最后一道防线）。"""
    out = value
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


# -- 内容策略（第九/十节）--


def message_profile(text: str, *, message_type: str = "group", **flags: Any) -> dict[str, Any]:
    """用户消息的**结构画像**（不含原文）。

    第九节允许记录的字段：length / message_type / has_image /
    has_reply / is_to_me / command_match / hash。
    """
    return {
        "length": len(text or ""),
        "message_type": message_type,
        "has_image": bool(flags.get("has_image", False)),
        "has_reply": bool(flags.get("has_reply", False)),
        "is_to_me": bool(flags.get("is_to_me", False)),
        "command_match": bool(flags.get("command_match", False)),
        "hash": content_hash(text or ""),
    }


def prompt_profile(
    *,
    system_prompt: str = "",
    messages: list[dict[str, Any]] | None = None,
    tool_count: int = 0,
) -> dict[str, Any]:
    """Prompt 的**结构画像**（不含内容）。

    第十节允许记录：prompt length / section count / tool count / prompt hash。

    "section" 的口径：persona/system 段算 1 段，之后每条消息算 1 段。
    这样能看出"历史是否变长"，又不会泄露任何文本。
    """
    msgs = messages or []
    parts = [system_prompt] + [
        str(m.get("content") or "") for m in msgs
    ]
    joined = "\n".join(parts)
    return {
        "prompt_length": len(joined),
        "section_count": len(parts),
        "tool_count": int(tool_count),
        "prompt_hash": content_hash(joined),
    }


def debug_content_enabled() -> bool:
    """是否显式打开了内容落盘（第九节，默认关闭）。

    即使打开，也只应写进本地 debug 文件，不应进入常规 Validation 数据。
    """
    return get_flag("DL_SENPAI_DEBUG_CONTENT")


__all__ = [
    "anonymize",
    "content_hash",
    "contains_secret",
    "redact_secrets",
    "message_profile",
    "prompt_profile",
    "debug_content_enabled",
    "DEFAULT_SALT",
]
