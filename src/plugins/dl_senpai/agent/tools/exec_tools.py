"""执行类工具：SHELL（命令）与 CODE（代码解释器）。

参考《深入理解 AI Agent》第 5 章 5.2：Bash 与 Code Interpreter 是通用 Agent 的
两个核心能力。

这两个工具是整个项目里权限最高、风险最大的东西，所以：

1. 可见性被钉死在管理员 QQ（见 permissions.HIGH_RISK_TOOLS）。
   群友看不到、也调不到——这是最主要的那道门。
2. 工作目录固定为沙箱根，且每次都显式设置 cwd，不继承 bot 进程的目录。
3. 危险模式 deny list 兜底（第 4 章 4.4 的输入验证层）。
4. 全量审计：命令原文、退出码、耗时、输出摘要都进日志。

诚实地说：这不是沙箱逃逸意义上的安全边界。真正的隔离需要操作系统级手段
（容器 / Job Object + AppContainer / seccomp），本项目没有引入。
能成立的前提是「只有管理员本人能触发」，这一点由 permissions 层保证。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition
from .sandbox import (
    SandboxViolation,
    relative_display,
    resolve_in_sandbox,
    resolve_sandbox_root,
    strip_ansi,
    truncate_output,
)

__all__ = [
    "SHELL_MARKER",
    "CODE_MARKER",
    "EXEC_TOOL_NAMES",
    "DANGEROUS_PATTERNS",
    "build_exec_tools",
    "execute_shell",
    "execute_code",
    "check_dangerous",
]

SHELL_MARKER = "SHELL"
CODE_MARKER = "CODE"

EXEC_TOOL_NAMES = ("shell", "code")

# 危险命令模式（正则，忽略大小写）。
#
# 设计立场：这是一张刻意保持短小的网，只拦「几乎不可能是正常需求」的动作。
# 长 deny list 会不断误伤（拦 Remove-Item 就会连正常的临时文件清理一起拦掉），
# 最后没人再信任它。真正的边界是「只有管理员能看见这个工具」。
DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+-[a-z]*[rf][a-z]*\s+/(\s|$)", "删除根目录"),
    (r"\brm\s+-[a-z]*[rf][a-z]*\s+~(\s|/|$)", "删除用户主目录"),
    (r"\bformat\s+[a-z]:", "格式化磁盘"),
    (r"\bdiskpart\b", "磁盘分区操作"),
    (r"\bbcdedit\b", "修改启动配置"),
    (r"\bshutdown\b|\bRestart-Computer\b|\bStop-Computer\b", "关机或重启"),
    (r"\bnet\s+(user|localgroup)\b", "修改系统账户"),
    (r"\breg\s+(add|delete)\s+.*HKLM", "修改系统注册表"),
    (r"\bSet-ExecutionPolicy\b", "修改脚本执行策略"),
    (r"\bRemove-Item\b[^|;]*Recurse[^|;]*\s[a-z]:\\?\s*($|;)", "递归删除盘符根"),
    (r"\bdel\s+/[a-z]\s+[a-z]:\\?\s*($|;)", "删除盘符根"),
    (r"\btakeown\b|\bicacls\s+[a-z]:\\", "夺取系统文件权限"),
    (r"\bschtasks\b.*/create", "创建计划任务"),
    (r"\bcipher\s+/w", "覆写磁盘空闲空间"),
    (r"\biex\s*\(|\|\s*iex\b|Invoke-Expression", "执行远程下载的内容"),
    (r"curl[^|;]*\|\s*(ba)?sh", "管道执行远程脚本"),
    (r"\bmkfs\b|\bdd\s+if=.*of=/dev/", "覆写块设备"),
)


def check_dangerous(command: str) -> str:
    """命中危险模式时返回原因，否则返回空串。

    在**执行前**跑。被拦下的尝试要单独记一条审计日志——
    它本身就是最有价值的信号。
    """
    text = command or ""
    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    return ""


def _cfg(context: Any, key: str, default: Any) -> Any:
    cfg = getattr(context, "config", None)
    return getattr(cfg, key, default) if cfg is not None else default


def _fail(call: ToolCall, name: str, message: str, error_type: str) -> ToolResult:
    return ToolResult(
        tool_name=name, tool_call_id=call.id, success=False,
        error=message, error_type=error_type,
    )


def _root(context: Any) -> Path:
    return resolve_sandbox_root(getattr(context, "config", None))


def _decode(raw: bytes | None) -> str:
    """解码子进程输出。

    先试 UTF-8，失败退回系统 ANSI 代码页（Windows 中文环境是 GBK）——
    否则一句中文报错就会变成一串 U+FFFD，模型完全读不懂。
    """
    data = raw or b""
    if not data:
        return ""
    for encoding in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _child_env() -> dict[str, str]:
    """给子进程的环境变量。

    刻意摘掉凭据类变量：沙箱里的命令不该能读到 bot 的 API key。
    第 4 章讲的致命三要素里「私有数据」这一项，能在这一层削掉一点是一点。
    """
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if any(t in upper for t in ("API_KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD")):
            env.pop(key, None)
    return env


def _kill_tree(proc: Any) -> None:
    """杀掉子进程。"""
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


async def _run_process(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str, bool]:
    """跑一个子进程，返回 (退出码, stdout, stderr, 是否超时)。

    用 create_subprocess_exec 传 argv 列表。
    """
    from ...winproc import no_window_kwargs

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        # 这条链路一天能起几十次 powershell，忘了它就一直闪黑窗
        **no_window_kwargs(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, _decode(out), _decode(err), False
    except asyncio.TimeoutError:
        _kill_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        return -1, "", f"命令超过 {timeout} 秒未结束，已强制结束", True


def _shell_argv(command: str) -> list[str]:
    """把命令交给当前平台的 shell。

    为什么还是经过 shell：模型写的是 shell 命令（管道、重定向、&&），
    不经过 shell 就只能跑单条可执行文件，工具会变得几乎没用。
    模式检查在 check_dangerous 里已经做过一遍，这是权衡后的选择。
    """
    if sys.platform == "win32":
        # -NoProfile 让结果可复现（不加载用户 profile 里的别名与函数）。
        return [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", command,
        ]
    return ["/bin/sh", "-c", command]


def _parse_shell(text: str) -> tuple[str, list[ToolCall]]:
    """解析单行 marker：<<<SHELL 命令>>>。"""
    pattern = re.compile(r"<<<SHELL\s+([^>\n]+?)\s*>>>", re.IGNORECASE)
    calls: list[ToolCall] = []
    cleaned = text or ""
    for match in pattern.finditer(text or ""):
        command = (match.group(1) or "").strip()
        if not command:
            continue
        calls.append(
            ToolCall(
                tool_name="shell", arguments={"command": command}, marker=SHELL_MARKER
            )
        )
    if calls:
        cleaned = pattern.sub("", cleaned)
    else:
        cleaned = re.sub(r"<<<SHELL\s*[^\n]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned, calls


async def execute_shell(context: Any, call: ToolCall) -> ToolResult:
    """执行一条 shell 命令（Windows 上走 PowerShell）。"""
    root = _root(context)
    command = str((call.arguments or {}).get("command") or "").strip()
    if not command:
        return _fail(call, "shell", "命令为空", "empty_command")

    reason = check_dangerous(command)
    if reason:
        return _fail(
            call, "shell",
            "命令被安全策略拒绝（" + reason + "）。如确需执行，请运维直接在机器上操作。",
            "blocked_by_policy",
        )

    timeout = float(_cfg(context, "agent_shell_timeout", 30) or 30)
    started = time.perf_counter()
    try:
        code, out, err, timed_out = await _run_process(
            _shell_argv(command), cwd=root, timeout=timeout, env=_child_env()
        )
    except (OSError, ValueError) as exc:
        return _fail(call, "shell", f"无法启动命令: {exc}", "spawn_failed")

    elapsed = time.perf_counter() - started
    return _exec_result(
        call, "shell", code, out, err, elapsed, timed_out, context
    )


_CODE_BLOCK_RE = re.compile(
    r"<<<CODE[ \t]*(?:([A-Za-z0-9+#._-]+)[ \t]*)?>{0,3}[ \t]*\r?\n?"
    r"(.*?)"
    r"(?:\r?\n?[ \t]*<<<END[ \t]*>{0,3}|\Z)",
    re.IGNORECASE | re.DOTALL,
)

def _strip_code_fence(code: str) -> str:
    """去掉模型套在代码外面的 ``` 围栏，只留纯代码。"""
    lines = (code or "").splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip("\n")


def _parse_code(text: str) -> tuple[str, list[ToolCall]]:
    """解析块式 marker：<<<CODE>>> 代码 <<<END>>>（语言可选，闭括号可缺）。

    为什么这么宽松：模型实测会写 <<<CODE（少 >>>）、<<<CODE python>>> 不带换行、
    <<<END 少括号。原来的严格正则一条都不认，于是整段 Python 原样发到群里
    （第六问真实翻车）。这里只坚持「开头像 CODE」，收尾尽量宽容；连 <<<END 都
    没有就一直吃到结尾 —— 让代码漏给用户，比让这段文本被吞掉更糟。
    """
    calls: list[ToolCall] = []
    cleaned = text or ""
    for match in _CODE_BLOCK_RE.finditer(text or ""):
        language = (match.group(1) or "python").strip().lower()
        code = _strip_code_fence(match.group(2))
        if not code:
            continue

        calls.append(
            ToolCall(
                tool_name="code",
                arguments={"code": code, "language": language},
                marker=CODE_MARKER,
            )
        )
    if calls:
        cleaned = _CODE_BLOCK_RE.sub("", cleaned)
    return cleaned, calls

async def execute_code(context: Any, call: ToolCall) -> ToolResult:
    """跑一段代码，把 stdout/stderr 交回模型。

    为什么落盘到文件再执行，而不是 python -c：

    1. -c 的代码跨多层引号转义极易出错，尤其是含中文与引号时；
    2. 落盘后文件留在沙箱里，出问题时管理员能直接打开看；
    3. 长代码在多轮里可以增量修改（配合 EDIT_FILE）。
    """
    root = _root(context)
    code = str((call.arguments or {}).get("code") or "")
    language = str((call.arguments or {}).get("language") or "python").lower()
    if not code.strip():
        return _fail(call, "code", "代码为空", "empty_code")

    if language not in ("python", "python3", "py"):
        return _fail(
            call, "code",
            f"目前只支持 python，收到的是 {language}。",
            "unsupported_language",
        )

    reason = check_dangerous(code)
    if reason:
        return _fail(
            call, "code", f"代码被安全策略拒绝（{reason}）。", "blocked_by_policy"
        )

    # 放在 .code/ 子目录里：既留痕（管理员能直接打开看），
    # 又不污染工作区——GLOB/GREP 会跳过 .code，
    # 否则每跑一次代码就多一个文件，很快把搜索结果淹掉。
    # 文件名带时间戳与随机后缀：并发调用不会互相覆盖。
    name = f".code/_code_{int(time.time())}_{uuid.uuid4().hex[:6]}.py"
    try:
        script = resolve_in_sandbox(root, name)
    except SandboxViolation as exc:
        return _fail(call, "code", str(exc), "sandbox_violation")

    try:
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(code, encoding="utf-8")
    except OSError as exc:
        return _fail(call, "code", f"写入脚本失败: {exc}", "io_error")

    timeout = float(_cfg(context, "agent_code_timeout", 30) or 30)
    started = time.perf_counter()
    try:
        exit_code, out, err, timed_out = await _run_process(
            [sys.executable, "-X", "utf8", str(script)],
            cwd=root,
            timeout=timeout,
            env=_child_env(),
        )
    except (OSError, ValueError) as exc:
        return _fail(call, "code", f"无法启动解释器: {exc}", "spawn_failed")

    elapsed = time.perf_counter() - started
    result = _exec_result(call, "code", exit_code, out, err, elapsed, timed_out, context)
    result.data["script"] = relative_display(script, root)
    return result


def _exec_result(
    call: ToolCall,
    name: str,
    exit_code: int,
    out: str,
    err: str,
    elapsed: float,
    timed_out: bool,
    context: Any,
) -> ToolResult:
    """把进程结果整理成给模型看的 ToolResult。

    非 0 退出码**不算工具失败**：命令跑起来了、也有输出，只是返回码非零
    （grep 没匹配到就是 1，python 脚本 raise 也是 1）。
    交给模型让它自己判断——这正是 RETURN_TO_MODEL 的语义。
    真正的执行失败（无法启动、超时）才走 success=False。
    """
    max_chars = int(_cfg(context, "agent_output_max_chars", 6000) or 6000)
    parts: list[str] = []
    stdout = truncate_output(strip_ansi(out).strip(), max_chars=max_chars)
    stderr = truncate_output(strip_ansi(err).strip(), max_chars=max_chars)
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append("[stderr]\n" + stderr)
    if not parts:
        parts.append("（没有输出）")
    if exit_code != 0:
        parts.append(f"[退出码 {exit_code}]")
    parts.append(f"[耗时 {elapsed:.2f}s]")
    body = "\n".join(parts)

    if timed_out:
        return ToolResult(
            tool_name=name, tool_call_id=call.id, success=False,
            output=body, error=body, error_type="timeout",
        )
    return ToolResult(
        tool_name=name, tool_call_id=call.id, success=True,
        output=body,
        data={"exit_code": exit_code, "duration_s": round(elapsed, 3)},
    )


def _visible_shell(context: Any) -> bool:
    """可见性只看功能开关；管理员限定由 permissions 层叠加。

    为什么分开：可见性有两层语义——「功能开没开」与「这个人能不能用」。
    分开写更清楚，也避免两处各判一次管理员、改配置时漏改一处。
    """
    return bool(_cfg(context, "agent_tools_enable", True)) and bool(
        _cfg(context, "agent_shell_enable", True)
    )


def _visible_code(context: Any) -> bool:
    return bool(_cfg(context, "agent_tools_enable", True)) and bool(
        _cfg(context, "agent_code_enable", True)
    )


# 提示词。按第 4 章 4.2 的 ACI 要求写：何时用 + 边界 + 反例 + 参数示例 + 返回。
SHELL_PROMPT_TEXT = """你可以在这台机器上执行命令（在受限工作目录内）。

用 <<<SHELL 一条命令>>>，例如：
    <<<SHELL ls -la>>>
    <<<SHELL git status>>>
    <<<SHELL python -c "print(1+1)">>>

什么时候用：看目录、跑测试、查进程、用 git、装依赖、做系统层面的操作。
什么时候不要用：只是想算个数（用 CODE）、只是读一个文件（用 READ_FILE）、
只是找文件（用 GLOB 或 GREP）——那几种更省 token，也不会有副作用。

规则：
- 一条命令写一行，一次只做一件事；需要串联就用 && 或分号
- 命令是**在你自己的工作目录**里跑的，用相对路径就行
- 破坏性命令（删系统目录、改注册表、关机）会被拒绝，别试
- 输出会保留头尾，中间太长会被截断；要看特定部分就换更精确的命令
- 非 0 退出码不算失败（比如 grep 没匹配到返回 1），你自己看输出判断
- 超时会强制结束（默认 30 秒），长任务请自己加超时或放后台
- 不要把 marker 原文展示给用户，也不要写「我来执行一下命令」这种机械话"""

CODE_PROMPT_TEXT = """你可以运行 Python 代码来算东西、验证想法、处理数据。

用块式写法：

<<<CODE
import math
print(math.factorial(20))
<<<END>>>

什么时候用：需要精确计算、需要试一段逻辑对不对、要处理文件里的数据、
要验证一个假设。
什么时候不要用：一步 shell 就能搞定的（用 SHELL）；纯聊天不需要算的。

规则：
- 只支持 Python
- 代码会落成沙箱里的一个 .py 文件再运行，所以**必须有显式的 print**
  才会看到结果——只写个表达式是不会输出的
- 结果走 stdout，报错会作为 stderr 回到你手里
- 不要写死循环；默认 30 秒超时后会被强制结束
- 不要把 marker 原文展示给用户"""


def build_exec_tools() -> list[ToolDefinition]:
    """构造 SHELL 与 CODE 两个工具定义。

    continuation=RETURN_TO_MODEL：结果必须回给模型，否则跑了等于没跑。
    failure_policy=RETURN_TO_MODEL：失败也要让模型看到，好让它换个办法。
    timeout=0：**不在这里设置超时**——进程超时由 _run_process 自己按配置管，
    并要给出「已强制结束」这种有信息量的错误。若这里再包一层，
    执行器的 wait_for 会先触发，模型只会收到一句干巴巴的「工具超时」。
    side_effect=True：shell 与代码都可能改到磁盘。对外部世界而言这是不可逆的，
    因此一旦执行过，本轮就不该再允许 legacy 重跑（fallback_allowed 变 False）。
    """
    return [
        ToolDefinition(
            name="shell",
            description="在沙箱工作目录内执行 shell 命令",
            marker=SHELL_MARKER,
            prompt_text=SHELL_PROMPT_TEXT,
            parser=_parse_shell,
            executor=execute_shell,
            parameter_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "单行 shell 命令"},
                },
                "required": ["command"],
            },
            permission="exec.shell",
            timeout=0.0,
            rate_limit=1.0,
            continuation=Continuation.RETURN_TO_MODEL,
            failure_policy=FailurePolicy.RETURN_TO_MODEL,
            visibility=_visible_shell,
            owner="high_risk_layer",
            priority=20,
            side_effect=True,
        ),
        ToolDefinition(
            name="code",
            description="运行 Python 代码并把输出交回模型",
            marker=CODE_MARKER,
            prompt_text=CODE_PROMPT_TEXT,
            parser=_parse_code,
            executor=execute_code,
            parameter_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的代码"},
                    "language": {"type": "string", "enum": ["python"], "description": "目前仅支持 python"},
                },
                "required": ["code"],
            },
            permission="exec.code",
            timeout=0.0,
            rate_limit=1.0,
            continuation=Continuation.RETURN_TO_MODEL,
            failure_policy=FailurePolicy.RETURN_TO_MODEL,
            visibility=_visible_code,
            owner="high_risk_layer",
            priority=21,
            side_effect=True,
        ),
    ]