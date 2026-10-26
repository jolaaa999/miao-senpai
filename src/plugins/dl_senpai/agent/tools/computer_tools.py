"""电脑类工具：看屏幕、切窗口、点、打字、按键、滚动。

跟 file_tools / exec_tools 的关系
---------------------------------
那两层是「文本」，这一层是「桌面」。所以本模块只做三件事：
把 marker 解析成动作参数、把动作交给 computer_control.perform、
把结果翻译成模型看得懂的一句话。

**所有围栏都在 computer_control 里**，这里一条判定都不重复写——
判定分散在两处，早晚会出现「命令行走的是 A 路径、模型走的是 B 路径」
这种只有出事后才发现的漏洞。

默认不注册：computer_enable=false 时 build_computer_tools() 返回空列表，
模型在 prompt 里连这些名字都看不到。
"""

from __future__ import annotations

import re
from typing import Any

from ... import computer_control as cc
from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition

__all__ = [
    "COMPUTER_TOOL_NAMES",
    "build_computer_tools",
    "execute_click",
    "execute_focus",
    "execute_key",
    "execute_screen",
    "execute_scroll",
    "execute_type",
    "execute_wait",
    "execute_windows",
]

SCREEN_MARKER = "SCREEN"
WINDOWS_MARKER = "WINDOWS"
FOCUS_MARKER = "FOCUS"
CLICK_MARKER = "CLICK"
TYPE_MARKER = "TYPE"
KEY_MARKER = "KEY"
SCROLL_MARKER = "SCROLL"
WAIT_MARKER = "WAIT"

COMPUTER_TOOL_NAMES = (
    "computer_screen",
    "computer_windows",
    "computer_focus",
    "computer_click",
    "computer_type",
    "computer_key",
    "computer_scroll",
    "computer_wait",
)


def _visible_computer(context: Any) -> bool:
    """可见性只看功能开关；管理员限定由 permissions 层叠加。"""
    cfg = getattr(context, "config", None)
    if cfg is None:
        return False
    return bool(getattr(cfg, "computer_enable", False)) and cc.IS_WINDOWS


def _arg(call: ToolCall, key: str, default: Any = "") -> Any:
    return (call.arguments or {}).get(key, default)


def _one(pattern: str, text: str, flags: int = re.IGNORECASE) -> tuple[str, list[ToolCall]]:
    """解析「一行一个」的 marker，命中后从正文里删掉。"""
    regex = re.compile(pattern, flags)
    calls: list[ToolCall] = []
    cleaned = text or ""
    for match in regex.finditer(text or ""):
        arguments = match.groupdict()
        arguments = {k: v for k, v in arguments.items() if v is not None}
        arguments["_marker_text"] = match.group(0)
        calls.append(ToolCall(tool_name="", arguments=arguments, marker=""))
    if calls:
        cleaned = regex.sub("", cleaned)
    return cleaned, calls


# ------------------------------------------------------------------ 解析


def _parse_screen(text: str) -> tuple[str, list[ToolCall]]:
    cleaned, calls = _one(r"<<<SCREEN\s*>>>", text)
    for call in calls:
        call.tool_name = "computer_screen"
        call.marker = SCREEN_MARKER
    # 兜底：只写了半截 marker 时也清掉，免得残骸漏进聊天
    if not calls:
        cleaned = re.sub(r"<<<SCREEN[^\n]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned, calls


def _parse_windows(text: str) -> tuple[str, list[ToolCall]]:
    cleaned, calls = _one(r"<<<WINDOWS(?:\s+(?P<limit>\d+))?\s*>>>", text)
    for call in calls:
        call.tool_name = "computer_windows"
        call.marker = WINDOWS_MARKER
        if "limit" in call.arguments:
            call.arguments["limit"] = int(call.arguments["limit"])
    if not calls:
        cleaned = re.sub(r"<<<WINDOWS[^\n]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned, calls


def _parse_focus(text: str) -> tuple[str, list[ToolCall]]:
    cleaned, calls = _one(r"<<<FOCUS\s+(?P<target>[^>\n]+?)\s*>>>", text)
    for call in calls:
        call.tool_name = "computer_focus"
        call.marker = FOCUS_MARKER
    return cleaned, calls


def _parse_click(text: str) -> tuple[str, list[ToolCall]]:
    """写法：CLICK 100,200  /  CLICK 100,200 right  /  CLICK 100,200 left 2"""
    pattern = (
        r"<<<CLICK\s+(?P<x>-?\d+)\s*[,，]\s*(?P<y>-?\d+)"
        r"(?:\s+(?P<button>left|right|middle))?"
        r"(?:\s+(?P<clicks>[123]))?\s*>>>"
    )
    cleaned, calls = _one(pattern, text)
    for call in calls:
        call.tool_name = "computer_click"
        call.marker = CLICK_MARKER
        call.arguments["x"] = int(call.arguments["x"])
        call.arguments["y"] = int(call.arguments["y"])
        if "clicks" in call.arguments:
            call.arguments["clicks"] = int(call.arguments["clicks"])
    return cleaned, calls


def _parse_type(text: str) -> tuple[str, list[ToolCall]]:
    cleaned, calls = _one(r"<<<TYPE\s+(?P<text>[^>\n]+?)\s*>>>", text)
    for call in calls:
        call.tool_name = "computer_type"
        call.marker = TYPE_MARKER
    return cleaned, calls


def _parse_key(text: str) -> tuple[str, list[ToolCall]]:
    cleaned, calls = _one(r"<<<KEY\s+(?P<combo>[^>\n]+?)\s*>>>", text)
    for call in calls:
        call.tool_name = "computer_key"
        call.marker = KEY_MARKER
    return cleaned, calls


def _parse_scroll(text: str) -> tuple[str, list[ToolCall]]:
    cleaned, calls = _one(r"<<<SCROLL\s+(?P<amount>-?\d+)\s*>>>", text)
    for call in calls:
        call.tool_name = "computer_scroll"
        call.marker = SCROLL_MARKER
        call.arguments["amount"] = int(call.arguments["amount"])
    return cleaned, calls


def _parse_wait(text: str) -> tuple[str, list[ToolCall]]:
    cleaned, calls = _one(r"<<<WAIT\s+(?P<ms>\d+)\s*>>>", text)
    for call in calls:
        call.tool_name = "computer_wait"
        call.marker = WAIT_MARKER
        call.arguments["ms"] = int(call.arguments["ms"])
    return cleaned, calls


# ------------------------------------------------------------------ 执行


def _cfg(context: Any) -> Any:
    return getattr(context, "config", None)


def _actor(context: Any) -> str:
    return str(getattr(context, "actor", "") or "")


def _to_result(call: ToolCall, name: str, result: cc.ActionResult) -> ToolResult:
    if result.ok:
        return ToolResult(
            tool_name=name,
            tool_call_id=call.id,
            success=True,
            output=result.message,
            data=result.data or {},
        )
    return ToolResult(
        tool_name=name,
        tool_call_id=call.id,
        success=False,
        error=result.message,
        error_type="computer_denied" if "白名单" in result.message or "租约" in result.message
        or "模式" in result.message or "敏感" in result.message else "computer_error",
    )


async def _run(context: Any, call: ToolCall, name: str, action: str, params: dict[str, Any]) -> ToolResult:
    result = await cc.perform(_cfg(context), action, actor=_actor(context), params=params)
    return _to_result(call, name, result)


async def execute_screen(context: Any, call: ToolCall) -> ToolResult:
    return await _run(context, call, "computer_screen", "screenshot", {})


async def execute_windows(context: Any, call: ToolCall) -> ToolResult:
    limit = int(_arg(call, "limit", 15) or 15)
    return await _run(context, call, "computer_windows", "windows", {"limit": limit})


async def execute_focus(context: Any, call: ToolCall) -> ToolResult:
    return await _run(
        context, call, "computer_focus", "focus", {"target": str(_arg(call, "target"))}
    )


async def execute_click(context: Any, call: ToolCall) -> ToolResult:
    params = {
        "x": int(_arg(call, "x", 0) or 0),
        "y": int(_arg(call, "y", 0) or 0),
        "button": str(_arg(call, "button", "left") or "left"),
        "clicks": int(_arg(call, "clicks", 1) or 1),
    }
    return await _run(context, call, "computer_click", "click", params)


async def execute_type(context: Any, call: ToolCall) -> ToolResult:
    return await _run(
        context, call, "computer_type", "type", {"text": str(_arg(call, "text"))}
    )


async def execute_key(context: Any, call: ToolCall) -> ToolResult:
    return await _run(
        context, call, "computer_key", "key", {"combo": str(_arg(call, "combo"))}
    )


async def execute_scroll(context: Any, call: ToolCall) -> ToolResult:
    return await _run(
        context, call, "computer_scroll", "scroll", {"amount": int(_arg(call, "amount", -120) or -120)}
    )


async def execute_wait(context: Any, call: ToolCall) -> ToolResult:
    return await _run(
        context, call, "computer_wait", "wait", {"ms": int(_arg(call, "ms", 500) or 500)}
    )


# ------------------------------------------------------------------ 提示词与注册


COMPUTER_PROMPT_TEXT = """你可以看这台电脑的屏幕、切换窗口，并在被允许的窗口里动鼠标键盘。

先看清楚：默认你**只能看**（截图/列窗口/看前台）。要动手需要管理员
显式授权（改配置 + 开一张限时租约），而且前台窗口必须在白名单里。
被拒绝时不要反复重试，把拒绝原因原样说出来就行——那道门是管理员设的，
不是你运气不好。

看现在屏幕上有什么：
    <<<SCREEN>>>
    <<<WINDOWS>>>
    <<<WINDOWS 30>>>
    <<<FOCUS 记事本>>>

在允许的窗口里操作：
    <<<CLICK 640,360>>>
    <<<CLICK 640,360 right>>>
    <<<CLICK 640,360 left 2>>>
    <<<TYPE 你好呀>>>
    <<<KEY ctrl+s>>>
    <<<KEY enter>>>
    <<<SCROLL -480>>>
    <<<WAIT 800>>>

用这些工具的前提：
- 一次只做一件事，做完看结果再决定下一步（点完不知道点了什么就继续点，
  等于闭着眼睛操作别人的电脑）；
- 坐标是**屏幕绝对坐标**，先 SCREEN 或 WINDOWS 确认，别猜；
- TYPE 打的是当前光标处，先点输入框再打；
- 文字一次别超过 2000 字。

反例（会被直接拒绝或做错）：
- 想「先看看支付页面」——敏感窗口连截图都会被拒；
- 一口气连点七八下不看结果；
- 把 marker 原文念给用户听。"""


def _enabled(cfg: Any) -> bool:
    """开关没开就当这层不存在。"""
    if cfg is None:
        try:
            from ...config import get_config

            cfg = get_config()
        except Exception:  # noqa: BLE001 — 拿不到配置一律按关闭处理
            return False
    return bool(getattr(cfg, "computer_enable", False))


def build_computer_tools(cfg: Any = None) -> list[ToolDefinition]:
    """构造 8 个电脑工具。**未开启时返回空列表**（不是注册后隐藏）。

    为什么在 build 这一层就返回空，而不是靠 visibility 谓词挡：
    注册表里的名字本身也是信息——它会被写进 prompt 的工具清单、
    出现在日志、也能被「凭空构造 tool_call」的方式探测到。
    默认关的意思就是**连名字都不存在**。

    side_effect 的划分直接决定 ExecutionJournal 会不会禁止本轮 legacy
    回退：截图/列窗口是只读，点、打、按、滚都会改变桌面状态，必须打标。
    """
    if not cc.IS_WINDOWS:
        return []
    if not _enabled(cfg):
        return []
    view_like = dict(
        continuation=Continuation.RETURN_TO_MODEL,
        failure_policy=FailurePolicy.RETURN_TO_MODEL,
        visibility=_visible_computer,
        owner="computer_layer",
        side_effect=False,
        prompt_text=COMPUTER_PROMPT_TEXT,
    )
    control_like = dict(view_like, side_effect=True)
    return [
        ToolDefinition(
            name="computer_screen",
            description="截一张主屏截图（只落盘，模型看不到画面内容）",
            marker=SCREEN_MARKER,
            parser=_parse_screen,
            executor=execute_screen,
            parameter_schema={"type": "object", "properties": {}},
            permission="computer.view",
            timeout=20.0,
            rate_limit=0.2,
            priority=40,
            **view_like,
        ),
        ToolDefinition(
            name="computer_windows",
            description="列出当前可见窗口（前台排最前）",
            marker=WINDOWS_MARKER,
            parser=_parse_windows,
            executor=execute_windows,
            parameter_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "最多列几个（默认 15）"},
                },
            },
            permission="computer.view",
            timeout=15.0,
            rate_limit=0.2,
            priority=41,
            **view_like,
        ),
        ToolDefinition(
            name="computer_focus",
            description="把某个窗口切到前台（必须在白名单内）",
            marker=FOCUS_MARKER,
            parser=_parse_focus,
            executor=execute_focus,
            parameter_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "窗口标题或进程名关键字"},
                },
                "required": ["target"],
            },
            permission="computer.control",
            timeout=15.0,
            rate_limit=0.2,
            priority=42,
            **control_like,
        ),
        ToolDefinition(
            name="computer_click",
            description="在指定屏幕坐标点鼠标",
            marker=CLICK_MARKER,
            parser=_parse_click,
            executor=execute_click,
            parameter_schema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "屏幕绝对 X"},
                    "y": {"type": "integer", "description": "屏幕绝对 Y"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "clicks": {"type": "integer", "description": "1~3 连击"},
                },
                "required": ["x", "y"],
            },
            permission="computer.control",
            timeout=15.0,
            rate_limit=0.2,
            priority=43,
            **control_like,
        ),
        ToolDefinition(
            name="computer_type",
            description="在当前光标处输入文本（不走剪贴板）",
            marker=TYPE_MARKER,
            parser=_parse_type,
            executor=execute_type,
            parameter_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要输入的文字，最多 2000 字"},
                },
                "required": ["text"],
            },
            permission="computer.control",
            timeout=30.0,
            rate_limit=0.2,
            priority=44,
            **control_like,
        ),
        ToolDefinition(
            name="computer_key",
            description="按组合键，如 ctrl+s / alt+tab / enter",
            marker=KEY_MARKER,
            parser=_parse_key,
            executor=execute_key,
            parameter_schema={
                "type": "object",
                "properties": {
                    "combo": {"type": "string", "description": "用 + 连接，如 ctrl+shift+s"},
                },
                "required": ["combo"],
            },
            permission="computer.control",
            timeout=15.0,
            rate_limit=0.2,
            priority=45,
            **control_like,
        ),
        ToolDefinition(
            name="computer_scroll",
            description="滚动鼠标滚轮（负数向下）",
            marker=SCROLL_MARKER,
            parser=_parse_scroll,
            executor=execute_scroll,
            parameter_schema={
                "type": "object",
                "properties": {
                    "amount": {"type": "integer", "description": "一格 120，负数向下"},
                },
                "required": ["amount"],
            },
            permission="computer.control",
            timeout=15.0,
            rate_limit=0.2,
            priority=46,
            **control_like,
        ),
        ToolDefinition(
            name="computer_wait",
            description="等一会儿再继续（连招中间用）",
            marker=WAIT_MARKER,
            parser=_parse_wait,
            executor=execute_wait,
            parameter_schema={
                "type": "object",
                "properties": {
                    "ms": {"type": "integer", "description": "毫秒，最多 30000"},
                },
                "required": ["ms"],
            },
            permission="computer.control",
            timeout=35.0,
            rate_limit=0.1,
            priority=47,
            **control_like,
        ),
    ]
