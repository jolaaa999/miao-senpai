"""结构化日志：与现有 loguru 文本日志共存，不破坏 Go 控制台的读取。

关键约束（来自 Phase 1 审计）：`data/logs/bot-YYYYMMDD.log` 是纯文本，
`admin/backend` 的控制台「运行日志」页直接按行读取并展示。因此**不能**
把日志改成纯 JSON——那会让控制台页面变成一坨不可读的字符串。

折中方案：保持「一行一条人类可读消息」的格式，把结构化字段以
`key=value` 形式追加在末尾，并总是带上 `trace_id=`。这样：

- 控制台正则解析仍然有效，人眼阅读体验不变；
- 日志可以用 grep 按 `trace_id=abc123` 捞出整条链路；
- 需要机器消费时，`key=value` 也可被简单解析。

后续如果要做真正的 JSON 日志，可以再加一个独立的 `.jsonl` 文件，
而不动这个面向控制台的文本文件。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .context import current_trace

try:  # 插件在无 nonebot driver 的单测环境下也能 import
    from nonebot import logger as _nb_logger
except Exception:  # noqa: BLE001
    _nb_logger = None

_LOG = _nb_logger or logging.getLogger("dl_senpai")

# 需要脱敏的字段名关键字（小写比较）
_SENSITIVE_MARKERS = ("key", "token", "secret", "password", "passwd", "authorization")


def _is_sensitive(name: str) -> bool:
    low = name.lower()
    return any(m in low for m in _SENSITIVE_MARKERS)


def _format_value(value: Any) -> str:
    """把值渲染成单行、无空格的 `key=value` 片段。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.4g}"
    text = str(value)
    # 换行会把一条日志拆成多行，破坏控制台按行读取
    text = text.replace("\r", " ").replace("\n", " ")
    if not text:
        return '""'
    if any(ch.isspace() for ch in text) or "=" in text:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def format_fields(fields: dict[str, Any]) -> str:
    """渲染结构化字段；自动脱敏、自动跳过空值。"""
    parts: list[str] = []
    for name, value in fields.items():
        if value is None or value == "":
            continue
        if _is_sensitive(name):
            parts.append(f"{name}=<redacted>")
            continue
        parts.append(f"{name}={_format_value(value)}")
    return " ".join(parts)


def log_event(
    event: str,
    *,
    level: str = "info",
    session_id: str | None = None,
    stage: str | None = None,
    **fields: Any,
) -> None:
    """记录一条结构化业务日志。

    用法::

        log_event("llm.request", model="gpt-6-astra", duration_ms=1234)

    自动带上当前 trace 的 trace_id / session_id / stage，因此调用方不需要
    显式传递这些上下文。
    """
    ctx = current_trace()
    merged: dict[str, Any] = {}
    if ctx is not None:
        merged.update(ctx.as_log_fields())
    if session_id is not None:
        merged["session_id"] = session_id
    if stage is not None:
        merged["stage"] = stage
    merged.update(fields)

    suffix = format_fields(merged)
    message = f"{event} {suffix}".rstrip() if suffix else event

    emit = getattr(_LOG, level, None) or getattr(_LOG, "info")
    try:
        emit(message)
    except Exception:  # noqa: BLE001 — 日志失败绝不能影响业务
        pass


def log_llm_usage(
    *,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    duration_ms: float | None = None,
    session_id: str | None = None,
    status: str = "ok",
) -> None:
    """记录一次 LLM 调用的 token 用量。

    Phase 1 审计发现：`resp.usage` 从未被读取，成本完全不可控。这里开始
    记录。字段可能为 None（部分中转站不返回 usage），`format_fields`
    会自动跳过，**不会**因为缺字段而报错。
    """
    log_event(
        "llm.usage",
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        duration_ms=round(duration_ms, 1) if duration_ms is not None else None,
        session_id=session_id,
        status=status,
    )


__all__ = ["log_event", "log_llm_usage", "format_fields"]


def rotate_log_if_large(path: Path, *, max_bytes: int = 8 * 1024 * 1024) -> Path | None:
    """子进程日志超过 max_bytes 就滚成 `<名字>.1`，返回备份路径（没滚则 None）。

    为什么需要它：`qzone-bridge.log` / `gptsovits-api.log` 是外部子进程 stdout 的
    重定向，写入方不是我们的 logger，所以 bot.py 里那套 `rotation="00:00"` +
    `retention="30 days"` 碰不到它们——它们只会一路 append（实测涨到 24 MB）。

    为什么在启动时检查就够了：这两个文件只在拉起子进程时打开一次，那是唯一可靠
    的钩子。轮转必须在 open **之前**做完，否则已经打开的 fd 还指着被改名的旧文件，
    后续输出会写进备份、当前文件反而空着。

    只保留一个备份，所以最坏占用约 2×max_bytes。
    """
    try:
        if not path.is_file() or path.stat().st_size < max_bytes:
            return None
        backup = path.with_name(path.name + ".1")
        os.replace(path, backup)
    except OSError:
        # 轮转失败不能拖垮子进程启动：最多是日志继续变大
        return None
    return backup