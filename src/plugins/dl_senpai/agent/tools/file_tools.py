"""文件类工具：READ_FILE / WRITE_FILE / EDIT_FILE / GLOB / GREP。

参考《深入理解 AI Agent》第 5 章 5.2 的 7 个核心工具与第 4 章 4.2 的 ACI 原则：

    能用通用工具表达的就不要拆成窄工具（`read_file` 一个入口读所有文本），
    描述必须写清「什么时候用 + 边界 + 反例 + 参数示例 + 返回什么」。

所有路径都过 `sandbox.resolve_in_sandbox`，越界一律失败返回而不是抛异常
（执行器虽会兜住异常，但明确的失败原因对模型更有用）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition
from .sandbox import (
    SandboxViolation,
    looks_binary,
    relative_display,
    resolve_in_sandbox,
    resolve_sandbox_root,
    strip_ansi,
    truncate_output,
)

__all__ = [
    "READ_FILE_MARKER",
    "WRITE_FILE_MARKER",
    "EDIT_FILE_MARKER",
    "GLOB_MARKER",
    "GREP_MARKER",
    "FILE_TOOL_NAMES",
    "build_file_tools",
    "execute_read_file",
    "execute_write_file",
    "execute_edit_file",
    "execute_glob",
    "execute_grep",
]

READ_FILE_MARKER = "READ_FILE"
WRITE_FILE_MARKER = "WRITE_FILE"
EDIT_FILE_MARKER = "EDIT_FILE"
GLOB_MARKER = "GLOB"
GREP_MARKER = "GREP"

FILE_TOOL_NAMES = ("read_file", "write_file", "edit_file", "glob", "grep")

# 单次读取的行数上限。超过就只给头尾并报出总行数，
# 让模型自己用 `路径 | 起-止` 去要具体区间，而不是一口气灌进上下文。
_READ_MAX_LINES = 400
# 正则搜索的命中上限。
_GREP_MAX_HITS = 60
# 匹配时跳过的目录：这些内容对模型无意义，只会淹没真正的结果。
_SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "node_modules",
    ".pytest_cache", ".mypy_cache", ".code",
}


def _arg(call: ToolCall, key: str, default: Any = "") -> Any:
    return (call.arguments or {}).get(key, default)


def _root(context: Any) -> Path:
    return resolve_sandbox_root(getattr(context, "config", None))


def _limit(context: Any, key: str, default: int) -> int:
    cfg = getattr(context, "config", None)
    try:
        return max(1, int(getattr(cfg, key, default) or default))
    except Exception:  # noqa: BLE001
        return default


def _fail(call: ToolCall, name: str, message: str, error_type: str) -> ToolResult:
    return ToolResult(
        tool_name=name, tool_call_id=call.id, success=False,
        error=message, error_type=error_type,
    )


# ---------------------------------------------------------------- READ_FILE


def _parse_read_file(text: str) -> tuple[str, list[ToolCall]]:
    """解析 `<<<READ_FILE 路径>>>` 或 `<<<READ_FILE 路径 | 起-止>>>`。"""
    pattern = re.compile(r"<<<READ_FILE\s+([^>\n]+?)\s*>>>", re.IGNORECASE)
    calls: list[ToolCall] = []
    cleaned = text or ""
    for match in pattern.finditer(text or ""):
        spec = (match.group(1) or "").strip()
        if not spec:
            continue
        path, _, rng = spec.partition("|")
        args: dict[str, Any] = {"path": path.strip()}
        rng = rng.strip()
        if rng:
            span = re.match(r"^(\d+)\s*-\s*(\d+)$", rng)
            if span:
                args["start_line"] = int(span.group(1))
                args["end_line"] = int(span.group(2))
        calls.append(
            ToolCall(tool_name="read_file", arguments=args, marker=READ_FILE_MARKER)
        )
    if calls:
        cleaned = pattern.sub("", cleaned)
    else:
        cleaned = re.sub(r"<<<READ_FILE\s*[^\n]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned, calls


async def execute_read_file(context: Any, call: ToolCall) -> ToolResult:
    """读一个文本文件（可指定行区间）。"""
    root = _root(context)
    raw_path = str(_arg(call, "path"))
    try:
        target = resolve_in_sandbox(root, raw_path, must_exist=True)
    except SandboxViolation as exc:
        return _fail(call, "read_file", str(exc), "sandbox_violation")

    if target.is_dir():
        names = sorted(p.name for p in target.iterdir())[:50]
        return ToolResult(
            tool_name="read_file", tool_call_id=call.id, success=True,
            output=f"{relative_display(target, root)} 是目录，包含：\n" + "\n".join(names),
        )

    max_bytes = _limit(context, "agent_file_read_max_bytes", 200000)
    try:
        with target.open("rb") as handle:
            head = handle.read(4096)
    except OSError as exc:
        return _fail(call, "read_file", f"打开失败: {exc}", "io_error")

    # 二进制文件明确拒绝：给模型一段乱码只会让它瞎猜，还会污染后续上下文。
    if looks_binary(head):
        return _fail(
            call, "read_file",
            f"{relative_display(target, root)} 看起来是二进制文件（大小 "
            f"{target.stat().st_size} 字节），不能当文本读。",
            "binary_file",
        )

    size = target.stat().st_size
    if size > max_bytes:
        return _fail(
            call, "read_file",
            f"文件过大（{size} 字节 > 上限 {max_bytes}）。",
            "file_too_large",
        )

    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _fail(call, "read_file", f"读取失败: {exc}", "io_error")

    lines = strip_ansi(raw).splitlines()
    total = len(lines)
    start = int(_arg(call, "start_line", 0) or 0)
    end = int(_arg(call, "end_line", 0) or 0)
    if start or end:
        lo = max(1, start or 1)
        hi = min(total, end or total)
        shown = lines[lo - 1 : hi]
        body = "\n".join(f"{lo + i}\t{line}" for i, line in enumerate(shown))
        head_note = f"{relative_display(target, root)} 第 {lo}-{hi} 行（共 {total} 行）"
    else:
        shown = lines[:_READ_MAX_LINES]
        body = "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(shown))
        head_note = f"{relative_display(target, root)}（共 {total} 行）"
        if total > _READ_MAX_LINES:
            body += (
                f"\n...（只显示前 {_READ_MAX_LINES} 行；"
                f"要看后面用 <<<READ_FILE {raw_path} | {_READ_MAX_LINES + 1}-{total}>>>）..."
            )

    return ToolResult(
        tool_name="read_file", tool_call_id=call.id, success=True,
        output=f"{head_note}\n{body}",
        data={"lines": total, "path": relative_display(target, root)},
    )


# --------------------------------------------------------------- WRITE_FILE


def _parse_write_file(text: str) -> tuple[str, list[ToolCall]]:
    """解析块式 `<<<WRITE_FILE 路径>>>\n内容\n<<<END>>>`。

    为什么用显式结束符：写入的内容本身可能含空行、含 `>>>`，
    靠「到行尾」或「到下一个 marker」都不安全。
    """
    pattern = re.compile(
        r"<<<WRITE_FILE\s+([^>\n]+?)\s*>>>\r?\n(.*?)\r?\n<<<END>>>",
        re.IGNORECASE | re.DOTALL,
    )
    calls: list[ToolCall] = []
    cleaned = text or ""
    for match in pattern.finditer(text or ""):
        calls.append(
            ToolCall(
                tool_name="write_file",
                arguments={
                    "path": (match.group(1) or "").strip(),
                    "content": match.group(2),
                },
                marker=WRITE_FILE_MARKER,
            )
        )
    if calls:
        cleaned = pattern.sub("", cleaned)
    return cleaned, calls


async def execute_write_file(context: Any, call: ToolCall) -> ToolResult:
    """写文件。父目录不存在时自动创建。"""
    root = _root(context)
    raw_path = str(_arg(call, "path"))
    content = str(_arg(call, "content", ""))
    try:
        target = resolve_in_sandbox(root, raw_path)
    except SandboxViolation as exc:
        return _fail(call, "write_file", str(exc), "sandbox_violation")

    existed = target.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return _fail(call, "write_file", f"写入失败: {exc}", "io_error")

    action = "覆盖" if existed else "新建"
    return ToolResult(
        tool_name="write_file", tool_call_id=call.id, success=True,
        output=(
            f"已{action} {relative_display(target, root)}（{len(content)} 字符，"
            f"{len(content.splitlines())} 行）"
        ),
        data={"path": relative_display(target, root), "created": not existed},
    )


# ---------------------------------------------------------------- EDIT_FILE


def _parse_edit_file(text: str) -> tuple[str, list[ToolCall]]:
    """解析 `<<<EDIT_FILE 路径>>>\n查找\n<<<SEP>>>\n替换\n<<<END>>>`。"""
    pattern = re.compile(
        r"<<<EDIT_FILE\s+([^>\n]+?)\s*>>>\r?\n(.*?)\r?\n<<<SEP>>>\r?\n(.*?)\r?\n<<<END>>>",
        re.IGNORECASE | re.DOTALL,
    )
    calls: list[ToolCall] = []
    cleaned = text or ""
    for match in pattern.finditer(text or ""):
        calls.append(
            ToolCall(
                tool_name="edit_file",
                arguments={
                    "path": (match.group(1) or "").strip(),
                    "old_string": match.group(2),
                    "new_string": match.group(3),
                },
                marker=EDIT_FILE_MARKER,
            )
        )
    if calls:
        cleaned = pattern.sub("", cleaned)
    return cleaned, calls


async def execute_edit_file(context: Any, call: ToolCall) -> ToolResult:
    """按精确匹配替换文件中的一段。

    **要求唯一匹配**：模型给一段太短或太常见的文本时，
    替换第一个命中点往往是错的（改错函数、改错配置项）。
    宁可让它失败重来，也不要静默改错地方。
    """
    root = _root(context)
    raw_path = str(_arg(call, "path"))
    old = str(_arg(call, "old_string", ""))
    new = str(_arg(call, "new_string", ""))
    try:
        target = resolve_in_sandbox(root, raw_path, must_exist=True)
    except SandboxViolation as exc:
        return _fail(call, "edit_file", str(exc), "sandbox_violation")

    if not old:
        return _fail(call, "edit_file", "查找文本为空", "empty_old_string")

    try:
        original = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _fail(call, "edit_file", f"读取失败: {exc}", "io_error")

    count = original.count(old)
    if count == 0:
        return _fail(
            call, "edit_file",
            "在文件中找不到这段文本。注意缩进与换行必须完全一致，"
            "建议先用 READ_FILE 看清原文。",
            "old_string_not_found",
        )
    if count > 1:
        return _fail(
            call, "edit_file",
            f"这段文本在文件中出现了 {count} 次，无法确定改哪一处。"
            "请多带几行上下文，让它唯一。",
            "old_string_not_unique",
        )

    try:
        target.write_text(original.replace(old, new, 1), encoding="utf-8")
    except OSError as exc:
        return _fail(call, "edit_file", f"写入失败: {exc}", "io_error")

    return ToolResult(
        tool_name="edit_file", tool_call_id=call.id, success=True,
        output=f"已修改 {relative_display(target, root)}（替换 {len(old)} → {len(new)} 字符）",
        data={"path": relative_display(target, root)},
    )


# --------------------------------------------------------------------- GLOB


def _parse_glob(text: str) -> tuple[str, list[ToolCall]]:
    """解析 `<<<GLOB 模式>>>`（相对沙箱根的通配模式）。"""
    pattern = re.compile(r"<<<GLOB\s+([^>\n]+?)\s*>>>", re.IGNORECASE)
    calls: list[ToolCall] = []
    cleaned = text or ""
    for match in pattern.finditer(text or ""):
        value = (match.group(1) or "").strip()
        if not value:
            continue
        calls.append(
            ToolCall(
                tool_name="glob", arguments={"pattern": value}, marker=GLOB_MARKER
            )
        )
    if calls:
        cleaned = pattern.sub("", cleaned)
    return cleaned, calls


async def execute_glob(context: Any, call: ToolCall) -> ToolResult:
    """按通配符找文件。返回相对路径，按修改时间倒序（新的更可能是要的那个）。"""
    root = _root(context)
    pattern = str(_arg(call, "pattern")).strip()
    if not pattern:
        return _fail(call, "glob", "模式为空", "empty_pattern")

    # 归一化 Windows 分隔符：模型常写 `src\*.py`。
    pattern = pattern.replace("\\", "/")
    if ".." in pattern.split("/"):
        return _fail(call, "glob", "模式不得包含 ..", "sandbox_violation")

    try:
        matches = list(root.glob(pattern))
    except (OSError, ValueError) as exc:
        return _fail(call, "glob", f"模式非法: {exc}", "bad_pattern")

    files = [p for p in matches if p.is_file() and not _under_skip(p, root)]
    files.sort(key=lambda p: _safe_mtime(p), reverse=True)
    limit = _limit(context, "agent_glob_max", 60)
    shown = files[:limit]
    if not shown:
        return ToolResult(
            tool_name="glob", tool_call_id=call.id, success=True,
            output=f"没有匹配 {pattern} 的文件（沙箱根 {relative_display(root, root)}）",
            data={"count": 0},
        )
    body = "\n".join(relative_display(p, root) for p in shown)
    note = f"共 {len(files)} 个匹配" + (f"，显示前 {limit} 个" if len(files) > limit else "")
    return ToolResult(
        tool_name="glob", tool_call_id=call.id, success=True,
        output=f"{note}：\n{body}",
        data={"count": len(files)},
    )


def _under_skip(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in _SKIP_DIRS for part in parts)


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# --------------------------------------------------------------------- GREP


def _parse_grep(text: str) -> tuple[str, list[ToolCall]]:
    """解析 `<<<GREP 正则>>>` 或 `<<<GREP 正则 | *.py>>>`。"""
    pattern = re.compile(r"<<<GREP\s+([^>\n]+?)\s*>>>", re.IGNORECASE)
    calls: list[ToolCall] = []
    cleaned = text or ""
    for match in pattern.finditer(text or ""):
        spec = (match.group(1) or "").strip()
        if not spec:
            continue
        expr, _, include = spec.partition("|")
        args: dict[str, Any] = {"pattern": expr.strip()}
        if include.strip():
            args["include"] = include.strip()
        calls.append(
            ToolCall(tool_name="grep", arguments=args, marker=GREP_MARKER)
        )
    if calls:
        cleaned = pattern.sub("", cleaned)
    return cleaned, calls


async def execute_grep(context: Any, call: ToolCall) -> ToolResult:
    """在沙箱内的文本文件里做正则搜索。"""
    root = _root(context)
    expr = str(_arg(call, "pattern")).strip()
    include = str(_arg(call, "include") or "").strip() or "*"
    if not expr:
        return _fail(call, "grep", "正则为空", "empty_pattern")

    try:
        regex = re.compile(expr)
    except re.error as exc:
        return _fail(call, "grep", f"正则非法: {exc}", "bad_regex")

    hits: list[str] = []
    scanned = 0
    for path in root.rglob(include):
        if len(hits) >= _GREP_MAX_HITS:
            break
        if not path.is_file() or _under_skip(path, root):
            continue
        scanned += 1
        if scanned > 800:  # 防止在超大目录里扫到超时
            break
        try:
            if path.stat().st_size > 512_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(
                    f"{relative_display(path, root)}:{lineno}: {line.strip()[:160]}"
                )
                if len(hits) >= _GREP_MAX_HITS:
                    break

    if not hits:
        return ToolResult(
            tool_name="grep", tool_call_id=call.id, success=True,
            output=f"没有匹配 /{expr}/ 的文本（扫描 {scanned} 个文件）",
            data={"count": 0},
        )
    return ToolResult(
        tool_name="grep", tool_call_id=call.id, success=True,
        output=truncate_output("\n".join(hits), max_chars=_limit(context, "agent_output_max_chars", 6000)),
        data={"count": len(hits)},
    )


def _visible_file(context: Any) -> bool:
    """可见性只看功能开关；管理员限定由 permissions 层叠加。"""
    cfg = getattr(context, "config", None)
    if cfg is None:
        return True
    return bool(getattr(cfg, "agent_tools_enable", True)) and bool(
        getattr(cfg, "agent_file_enable", True)
    )


# 提示词。按第 4 章 4.2 的 ACI 要求写：何时用 + 边界 + 反例 + 参数示例 + 返回。
FILE_PROMPT_TEXT = """你可以读写工作目录里的文件。

读一个文件（可只看某段行区间）：
    <<<READ_FILE notes.md>>>
    <<<READ_FILE notes.md | 100-160>>>

写一个文件（覆盖；父目录会自动创建）：
    <<<WRITE_FILE notes/a.md>>>
    这里是要写的内容
    <<<END>>>

改一个文件（把「查找」那段替换成「替换」那段）：
    <<<EDIT_FILE notes/a.md>>>
    原来的那段文字
    <<<SEP>>>
    换成的新文字
    <<<END>>>

找文件、搜内容：
    <<<GLOB **/*.md>>>
    <<<GREP TODO | *.py>>>

什么时候用：需要落盘、需要看具体文件内容、需要精确定位某个字符串。
什么时候不要用：只是想算个数（用 CODE）、想跑命令（用 SHELL）。

规则：
- 路径只能是**工作目录之内**的相对路径，越界会被拒绝
- 读之前最好先 GLOB 确认文件在不在，别猜路径
- EDIT_FILE 要求你给的「查找」段在文件里**只出现一次**，否则会被拒绝
  （多带几行上下文就能唯一），这是为了不改错地方
- EDIT_FILE 的缩进与换行必须和原文完全一致，先 READ_FILE 看清再改
- 文件是二进制或过大时读不了，会明确告诉你
- 不要把 marker 原文展示给用户"""


def build_file_tools() -> list[ToolDefinition]:
    """构造 5 个文件工具定义。

    全部 RETURN_TO_MODEL：读写结果必须回给模型，否则它无从继续。
    side_effect：读类为 False（重跑无害），写类为 True（重跑会改坏文件）。
    这个区分直接决定 journal 会不会禁止本轮 legacy fallback。
    """
    read_only = dict(
        continuation=Continuation.RETURN_TO_MODEL,
        failure_policy=FailurePolicy.RETURN_TO_MODEL,
        visibility=_visible_file,
        owner="high_risk_layer",
        side_effect=False,
    )
    write_like = dict(read_only, side_effect=True)
    return [
        ToolDefinition(
            name="read_file",
            description="读取沙箱内的文本文件（可指定行区间）",
            marker=READ_FILE_MARKER,
            prompt_text=FILE_PROMPT_TEXT,
            parser=_parse_read_file,
            executor=execute_read_file,
            # 注意：这里必须是**标准 JSON Schema**，不能写成 {"path": "str"} 这种
            # 人类可读简写——native function calling 会把 parameter_schema 原样
            # 发给 OpenAI 兼容接口，简写会被判 400。
            parameter_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "沙箱内相对路径"},
                    "start_line": {"type": "integer", "description": "起始行（1-based，可省略）"},
                    "end_line": {"type": "integer", "description": "结束行（含，可省略）"},
                },
                "required": ["path"],
            },
            permission="file.read",
            timeout=10.0,
            rate_limit=0.2,
            priority=22,
            **read_only,
        ),
        ToolDefinition(
            name="glob",
            description="按通配符查找沙箱内的文件",
            marker=GLOB_MARKER,
            prompt_text=FILE_PROMPT_TEXT,
            parser=_parse_glob,
            executor=execute_glob,
            parameter_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "通配符，如 **/*.py"},
                },
                "required": ["pattern"],
            },
            permission="file.read",
            timeout=15.0,
            rate_limit=0.2,
            priority=23,
            **read_only,
        ),
        ToolDefinition(
            name="grep",
            description="在沙箱内的文本文件里做正则搜索",
            marker=GREP_MARKER,
            prompt_text=FILE_PROMPT_TEXT,
            parser=_parse_grep,
            executor=execute_grep,
            parameter_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "include": {"type": "string", "description": "文件名过滤，如 *.py（可省略）"},
                },
                "required": ["pattern"],
            },
            permission="file.read",
            timeout=20.0,
            rate_limit=0.2,
            priority=24,
            **read_only,
        ),
        ToolDefinition(
            name="write_file",
            description="写入（覆盖）沙箱内的文件",
            marker=WRITE_FILE_MARKER,
            prompt_text=FILE_PROMPT_TEXT,
            parser=_parse_write_file,
            executor=execute_write_file,
            parameter_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "沙箱内相对路径"},
                    "content": {"type": "string", "description": "要写入的完整内容（覆盖）"},
                },
                "required": ["path", "content"],
            },
            permission="file.write",
            timeout=10.0,
            rate_limit=0.2,
            priority=25,
            **write_like,
        ),
        ToolDefinition(
            name="edit_file",
            description="按唯一匹配替换沙箱内文件的一段内容",
            marker=EDIT_FILE_MARKER,
            prompt_text="",
            parser=_parse_edit_file,
            executor=execute_edit_file,
            parameter_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "沙箱内相对路径"},
                    "old_string": {"type": "string", "description": "要被替换的原文（必须唯一匹配）"},
                    "new_string": {"type": "string", "description": "替换后的内容"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            permission="file.write",
            timeout=10.0,
            rate_limit=0.2,
            priority=26,
            **write_like,
        ),
    ]