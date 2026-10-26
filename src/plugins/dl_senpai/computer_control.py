"""给她一台电脑：GUI 操作后端（默认关闭，围栏优先）。

为什么这一层比 file_tools / exec_tools 还要紧一档
------------------------------------------------
文本类高风险工具（读文件、跑命令）的副作用至少是「可回滚、可审计、
落在沙箱根里」。鼠标键盘不是——点错一下可能就把钱转出去了，而且撤不回。

所以这一层额外叠五道门，每一道都独立生效：

1. **默认关**：computer_enable=False 时工具根本不注册，
   模型连 <<<COMPUTER_...>>> 这些名字都看不到（不是「看到了调不动」）。
2. **两档模式**：view（只能看：截图 / 窗口列表 / 前台信息）
   → control（能点、能打字）。升档要显式改配置，光有开关不算。
3. **租约**：control 类动作要求「有人在 N 秒内明确授权过」，
   授权写在 computer/lease.json，过期立刻退回 view。
   没有租约时她可以看，但不能碰。授权入口是 scripts 目录下的
   computer-lease.py，不走聊天消息——聊天里一句「你随便动吧」
   不该等于交出桌面。
4. **白名单 + 黑名单**：前台窗口必须命中 computer_allow_apps
   （**留空 = 谁都不许碰**，这跟「留空 = 全都允许」相反，是故意的），
   且标题/进程名不能含支付、密码这类敏感词。
5. **限流 + 审计**：每分钟动作数、点击最小间隔；
   每个动作都把「谁、什么时候、动了哪、当时前台是什么窗口」写进结构化日志。

必须说清楚它**不是**什么
------------------------
这不是操作系统级隔离。她拿到的是**当前登录用户的完整桌面权限**——
同一个进程用鼠标键盘能做的事，她都能做出来。真隔离要靠独立桌面会话 /
容器 / 备用机 / 虚拟机，本项目没有引入。

因此唯一被推荐的用法是：**接模拟器或备用机**，别接装着支付和密码管理器
的主力机。这条写在这里，也写进 .env.example。
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from nonebot import logger

from .observability.logging import log_event
from .storage import JsonStore

IS_WINDOWS = sys.platform == "win32"

# 只读动作：不改变桌面状态，模式为 view 时也允许。
VIEW_ACTIONS = frozenset({"screenshot", "windows", "foreground"})

# 默认敏感词。这些窗口不光禁止输入，连截图也拒绝——
# 「把支付页截下来发给别人」和「在支付页点错按钮」是同一级别的损失。
DEFAULT_DENY_KEYWORDS = (
    "支付,付款,转账,银行,网银,信用卡,借贷,还款,钱包,保险,"
    "密码,passwords,1password,bitwarden,keepass,lastpass,keychain,"
    "支付宝,微信支付,收款码,验证码"
)

# 单条文本输入的长度上限。不是技术限制，是防「模型把一整段人设贴进输入框」。
MAX_TYPE_CHARS = 2000


def _user32() -> Any:
    if not IS_WINDOWS:
        raise RuntimeError("GUI 操作只支持 Windows")
    return ctypes.windll.user32  # type: ignore[attr-defined]


def _kernel32() -> Any:
    if not IS_WINDOWS:
        raise RuntimeError("GUI 操作只支持 Windows")
    return ctypes.windll.kernel32  # type: ignore[attr-defined]


# ------------------------------------------------------------------ 窗口信息


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    process: str
    pid: int
    is_foreground: bool = False

    def label(self) -> str:
        fg = "（前台）" if self.is_foreground else ""
        return f"[{self.process or '?'}] {self.title}{fg}"


def _process_name(pid: int) -> str:
    """取进程可执行文件名。拿不到就返回空串，不抛异常。

    用 QueryFullProcessImageNameW 而不是 psutil：少一个依赖，而且这个
    项目对「多装一个包」很敏感（见 pyproject 的可选依赖分组）。
    """
    if not pid:
        return ""
    kernel32 = _kernel32()
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _window_from_hwnd(hwnd: int, foreground: int) -> WindowInfo | None:
    user32 = _user32()
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return None
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = (buf.value or "").strip()
    if not title:
        return None
    pid_buf = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
    pid = int(pid_buf.value)
    return WindowInfo(
        hwnd=int(hwnd),
        title=title,
        process=_process_name(pid),
        pid=pid,
        is_foreground=(int(hwnd) == int(foreground)),
    )


def list_windows(limit: int = 40) -> list[WindowInfo]:
    """列出当前可见、有标题的顶层窗口（前台排最前）。"""
    user32 = _user32()
    foreground = int(user32.GetForegroundWindow() or 0)
    items: list[WindowInfo] = []

    try:
        prototype = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    except AttributeError:  # pragma: no cover — 非 Windows 平台根本到不了这里
        raise RuntimeError("GUI 操作只支持 Windows") from None

    def _callback(hwnd: Any, _lparam: Any) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            info = _window_from_hwnd(int(hwnd), foreground)
        except Exception:  # noqa: BLE001 — 单个窗口取信息失败不该中断枚举
            return True
        if info is not None:
            items.append(info)
        return len(items) < max(1, limit) * 4  # 多取一些，后面排序截断

    user32.EnumWindows(prototype(_callback), 0)
    items.sort(key=lambda w: (not w.is_foreground, w.process.lower(), w.title))
    return items[: max(1, limit)]


def foreground_window() -> WindowInfo | None:
    user32 = _user32()
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return None
    return _window_from_hwnd(hwnd, hwnd)


# ------------------------------------------------------------------ 租约


def _computer_dir(cfg: Any) -> Path:
    raw = getattr(cfg, "computer_dir", None) or "data/dl_senpai/computer"
    return Path(str(raw))


def lease_path(cfg: Any) -> Path:
    return _computer_dir(cfg) / "lease.json"


def read_lease(cfg: Any) -> dict[str, Any]:
    path = lease_path(cfg)
    try:
        data = JsonStore(path, create_parent=True).load(default={})
    except Exception as exc:  # noqa: BLE001 — 租约坏了等于没有租约，安全侧
        logger.warning(f"dl_senpai computer lease load failed: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def lease_remaining(cfg: Any) -> float:
    """租约还剩多少秒。<=0 表示没有有效授权。"""
    data = read_lease(cfg)
    try:
        until = float(data.get("until") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, until - time.time())


def issue_lease(cfg: Any, seconds: float, *, actor: str = "") -> dict[str, Any]:
    """开一张限时授权。由 scripts 目录下的 computer-lease.py 调用。"""
    seconds = max(0.0, float(seconds))
    payload = {
        "until": time.time() + seconds,
        "issued_at": time.time(),
        "seconds": seconds,
        "actor": str(actor or ""),
    }
    JsonStore(lease_path(cfg), create_parent=True).save(payload)
    log_event(
        "computer.lease.issue",
        level="warning",
        seconds=seconds,
        actor=str(actor or "-"),
    )
    return payload


def revoke_lease(cfg: Any, *, actor: str = "") -> None:
    try:
        JsonStore(lease_path(cfg), create_parent=True).save(
            {"until": 0.0, "revoked_by": str(actor)}
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai computer lease revoke failed: {exc}")
    log_event("computer.lease.revoke", level="warning", actor=str(actor or "-"))


# ------------------------------------------------------------------ 限流


_ACTION_TIMES: deque[float] = deque(maxlen=1000)
_LAST_ACTION_AT = 0.0
_RATE_LOCK = Lock()


def _rate_check(cfg: Any) -> tuple[bool, str]:
    """滑动窗口限流。返回 (是否允许, 拒绝原因)。"""
    global _LAST_ACTION_AT
    per_min = int(getattr(cfg, "computer_max_actions_per_min", 20) or 20)
    min_gap_ms = int(getattr(cfg, "computer_click_min_interval_ms", 300) or 0)
    now = time.time()
    with _RATE_LOCK:
        cutoff = now - 60.0
        while _ACTION_TIMES and _ACTION_TIMES[0] < cutoff:
            _ACTION_TIMES.popleft()
        if per_min > 0 and len(_ACTION_TIMES) >= per_min:
            return False, f"每分钟动作数已达上限（{per_min}）"
        if min_gap_ms > 0 and _LAST_ACTION_AT:
            gap = (now - _LAST_ACTION_AT) * 1000
            if gap < min_gap_ms:
                return False, f"两次动作间隔太短（{int(gap)}ms < {min_gap_ms}ms）"
        _ACTION_TIMES.append(now)
        _LAST_ACTION_AT = now
    return True, ""


def reset_rate_limiter() -> None:
    """测试用：把限流窗口清空。"""
    global _LAST_ACTION_AT
    with _RATE_LOCK:
        _ACTION_TIMES.clear()
        _LAST_ACTION_AT = 0.0


# ------------------------------------------------------------------ 围栏判定


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str
    window: WindowInfo | None = None

    @property
    def ok(self) -> bool:
        return self.allowed


def _split_list(raw: Any) -> list[str]:
    """逗号分隔配置 → 列表。中英文逗号、分号都认，方便手写 .env。"""
    text = str(raw or "").strip()
    if not text:
        return []
    items: list[str] = []
    for chunk in text.replace("，", ",").replace(";", ",").split(","):
        item = chunk.strip()
        if item:
            items.append(item)
    return items


def deny_keywords(cfg: Any) -> list[str]:
    raw = getattr(cfg, "computer_deny_keywords", None)
    items = _split_list(DEFAULT_DENY_KEYWORDS if raw is None else raw)
    return [i.lower() for i in items]


def allow_apps(cfg: Any) -> list[str]:
    return [i.lower() for i in _split_list(getattr(cfg, "computer_allow_apps", ""))]


def _hits_deny(window: WindowInfo | None, words: list[str]) -> str:
    if window is None:
        return ""
    haystack = f"{window.title} {window.process}".lower()
    for word in words:
        if word and word in haystack:
            return word
    return ""


def _hits_allow(window: WindowInfo | None, allowed: list[str]) -> bool:
    if window is None:
        return False
    haystack = f"{window.title} {window.process}".lower()
    return any(word and word in haystack for word in allowed)


def check_action(cfg: Any, action: str, *, actor: str = "") -> GuardDecision:
    """所有动作的唯一入口。任何一道门不过就直接拒绝，不做「部分允许」。

    判定顺序是有讲究的：先看开关（便宜），再看模式，再看租约，
    最后才碰桌面（枚举窗口有开销，也是唯一会产生副作用的地方）。
    """
    if not bool(getattr(cfg, "computer_enable", False)):
        return GuardDecision(False, "电脑控制未开启（DL_SENPAI_COMPUTER_ENABLE=false）")
    if not IS_WINDOWS:
        return GuardDecision(False, f"当前系统不支持 GUI 操作（{sys.platform}）")

    mode = str(getattr(cfg, "computer_mode", "view") or "view").strip().lower()
    is_view = action in VIEW_ACTIONS

    try:
        window = foreground_window()
    except Exception as exc:  # noqa: BLE001 — 枚举失败就当拿不到窗口，按拒绝处理
        return GuardDecision(False, f"拿不到前台窗口：{type(exc).__name__}: {exc}")

    hit = _hits_deny(window, deny_keywords(cfg))
    if hit:
        # 注意：连截图也拒绝。这里是「宁可什么都不做」的一侧。
        return GuardDecision(
            False, f"前台是敏感窗口（命中「{hit}」），拒绝对它做任何操作", window
        )

    if is_view:
        return GuardDecision(True, "view", window)

    if mode != "control":
        return GuardDecision(
            False,
            f"当前是 {mode} 模式，只能看不能动；要操作请把 "
            f"DL_SENPAI_COMPUTER_MODE 改成 control",
            window,
        )

    allowed = allow_apps(cfg)
    if not allowed:
        return GuardDecision(
            False,
            "没有配置白名单（DL_SENPAI_COMPUTER_ALLOW_APPS 为空）——留空等于谁都不许碰",
            window,
        )
    if not _hits_allow(window, allowed):
        name = f"[{window.process}] {window.title}" if window else "（拿不到前台窗口）"
        return GuardDecision(False, f"前台窗口不在白名单里：{name}", window)

    if bool(getattr(cfg, "computer_require_lease", True)):
        if lease_remaining(cfg) <= 0:
            return GuardDecision(
                False,
                "没有有效授权租约；用 computer-lease.py allow 10 开一张，用完自动过期",
                window,
            )

    ok, why = _rate_check(cfg)
    if not ok:
        return GuardDecision(False, why, window)

    log_event(
        "computer.action.allowed",
        action=action,
        actor=str(actor or "-"),
        window_title=(window.title if window else ""),
        window_process=(window.process if window else ""),
    )
    return GuardDecision(True, "control", window)


# ------------------------------------------------------------------ 底层动作


def capture(save_dir: Path, *, max_side: int = 1280) -> Path:
    """截主屏，落盘成 PNG。超过 max_side 会等比缩小（省磁盘、也省以后发图的流量）。"""
    from PIL import ImageGrab  # 延迟导入：没有 pillow 时只有截图不可用，别的照常

    image = ImageGrab.grab()
    if max_side and max(image.size) > max_side:
        ratio = max_side / float(max(image.size))
        image = image.resize((int(image.width * ratio), int(image.height * ratio)))
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / f"shot-{int(time.time() * 1000)}.png"
    image.save(path)
    return path


def move(x: int, y: int) -> None:
    _user32().SetCursorPos(int(x), int(y))


_BUTTON_FLAGS = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}


def click(x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
    user32 = _user32()
    down, up = _BUTTON_FLAGS.get(button.lower(), _BUTTON_FLAGS["left"])
    user32.SetCursorPos(int(x), int(y))
    for _ in range(max(1, min(int(clicks or 1), 3))):
        user32.mouse_event(down, 0, 0, 0, 0)
        user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.04)


def scroll(amount: int) -> None:
    """滚轮：正数向上，负数向下。一格 = 120。"""
    _user32().mouse_event(0x0800, 0, 0, int(amount), 0)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

VK_CODES = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "printscreen": 0x2C,
}
VK_CODES.update({str(i): 0x30 + i for i in range(10)})
VK_CODES.update({chr(c): 0x41 + (c - ord("a")) for c in range(ord("a"), ord("z") + 1)})
VK_CODES.update({f"f{i}": 0x70 + (i - 1) for i in range(1, 13)})


def _send_inputs(inputs: list[Any]) -> None:
    user32 = _user32()
    count = len(inputs)
    if not count:
        return
    array = (_INPUT * count)(*inputs)
    user32.SendInput(count, array, ctypes.sizeof(_INPUT))


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> Any:
    item = _INPUT()
    item.type = _INPUT_KEYBOARD
    item.u.ki = _KEYBDINPUT(vk, scan, flags, 0, None)
    return item


def press_keys(combo: str) -> None:
    """按组合键，写法 ctrl+s / alt+tab / enter。按下按顺序、松开按倒序。"""
    parts = [
        p.strip().lower()
        for p in str(combo or "").replace(" ", "+").split("+")
        if p.strip()
    ]
    codes: list[int] = []
    for part in parts:
        vk = VK_CODES.get(part)
        if vk is None and len(part) == 1:
            vk = ord(part.upper())
        if vk is None:
            raise ValueError(f"不认识的按键：{part}")
        codes.append(vk)
    if not codes:
        raise ValueError("组合键为空")
    _send_inputs([_key(vk) for vk in codes])
    time.sleep(0.05)
    _send_inputs([_key(vk, 0, _KEYEVENTF_KEYUP) for vk in reversed(codes)])


def type_text(text: str) -> None:
    """用 SendInput 逐字符打 Unicode，不依赖输入法、不经过剪贴板。

    为什么不用「写剪贴板 + ctrl+v」：剪贴板是全局共享的，把她要打的字
    塞进去等于把内容留在了别的程序的粘贴板上（还可能被剪贴板历史同步到
    别的设备）。Unicode 输入没有这个副作用。
    """
    payload = str(text or "")
    if not payload:
        return
    inputs: list[Any] = []
    for ch in payload:
        code = ord(ch)
        # 非 BMP 字符要拆成 UTF-16 代理项，Windows 只认这个。
        if code > 0xFFFF:
            code -= 0x10000
            units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
        else:
            units = [code]
        for unit in units:
            inputs.append(_key(0, unit, _KEYEVENTF_UNICODE))
            inputs.append(_key(0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))
    _send_inputs(inputs)


# ------------------------------------------------------------------ 统一出口


@dataclass
class ActionResult:
    ok: bool
    message: str
    data: dict[str, Any] | None = None


def _guard(cfg: Any, action: str, actor: str) -> GuardDecision:
    decision = check_action(cfg, action, actor=actor)
    if not decision.allowed:
        log_event(
            "computer.action.denied",
            level="warning",
            action=action,
            actor=str(actor or "-"),
            reason=decision.reason,
            window_title=(decision.window.title if decision.window else ""),
        )
    return decision


def _shot_dir(cfg: Any) -> Path:
    return _computer_dir(cfg) / "shots"


def screenshot_sync(cfg: Any) -> ActionResult:
    max_side = int(getattr(cfg, "computer_screenshot_max_side", 1280) or 1280)
    path = capture(_shot_dir(cfg), max_side=max_side)
    window = foreground_window()
    label = window.label() if window else "（无）"
    return ActionResult(
        True,
        f"已截图：{path}\n前台窗口：{label}\n"
        f"（注意：截图只落盘，模型本身看不到画面内容）",
        {"path": str(path), "window": label},
    )


async def perform(
    cfg: Any,
    action: str,
    *,
    actor: str = "",
    params: dict[str, Any] | None = None,
) -> ActionResult:
    """所有 GUI 动作的唯一执行口：先过围栏，再动手，最后留痕。

    action 取值：screenshot / windows / foreground / click / move /
    type / key / scroll / wait / focus
    """
    params = dict(params or {})
    decision = _guard(cfg, action, actor)
    if not decision.allowed:
        return ActionResult(False, decision.reason)

    try:
        if action == "screenshot":
            return await asyncio.to_thread(screenshot_sync, cfg)
        if action == "windows":
            limit = int(params.get("limit") or 15)
            infos = await asyncio.to_thread(list_windows, limit)
            if not infos:
                return ActionResult(True, "当前没有可见窗口。")
            lines = [f"{i + 1}. {w.label()}" for i, w in enumerate(infos)]
            return ActionResult(True, "可见窗口：\n" + "\n".join(lines), {"count": len(infos)})
        if action == "foreground":
            window = await asyncio.to_thread(foreground_window)
            if window is None:
                return ActionResult(True, "拿不到前台窗口。")
            return ActionResult(
                True,
                f"前台窗口：{window.label()}\n进程：{window.process}（pid {window.pid}）",
            )
        if action == "click":
            x, y = int(params.get("x") or 0), int(params.get("y") or 0)
            button = str(params.get("button") or "left")
            clicks = int(params.get("clicks") or 1)
            await asyncio.to_thread(click, x, y, button=button, clicks=clicks)
            return ActionResult(True, f"已点击 ({x}, {y}) {button}×{clicks}")
        if action == "move":
            x, y = int(params.get("x") or 0), int(params.get("y") or 0)
            await asyncio.to_thread(move, x, y)
            return ActionResult(True, f"鼠标已移到 ({x}, {y})")
        if action == "scroll":
            amount = int(params.get("amount") or -120)
            await asyncio.to_thread(scroll, amount)
            return ActionResult(True, f"滚轮 {amount}")
        if action == "type":
            text = str(params.get("text") or "")
            if len(text) > MAX_TYPE_CHARS:
                return ActionResult(
                    False, f"一次输入太长（{len(text)} 字 > {MAX_TYPE_CHARS}），分批打"
                )
            await asyncio.to_thread(type_text, text)
            return ActionResult(True, f"已输入 {len(text)} 个字符")
        if action == "key":
            combo = str(params.get("combo") or "")
            await asyncio.to_thread(press_keys, combo)
            return ActionResult(True, f"已按下 {combo}")
        if action == "wait":
            ms = max(0, min(int(params.get("ms") or 500), 30000))
            await asyncio.sleep(ms / 1000)
            return ActionResult(True, f"等了 {ms}ms")
        if action == "focus":
            keyword = str(params.get("target") or "").strip()
            return await _focus(cfg, keyword)
        return ActionResult(False, f"不认识的动作：{action}")
    except Exception as exc:  # noqa: BLE001 — 桌面动作失败要原样告诉模型
        logger.warning(
            f"dl_senpai computer action {action} failed: {type(exc).__name__}: {exc}"
        )
        log_event(
            "computer.action.error",
            level="warning",
            action=action,
            error=type(exc).__name__,
        )
        return ActionResult(False, f"{action} 失败：{type(exc).__name__}: {exc}")


async def _focus(cfg: Any, keyword: str) -> ActionResult:
    """把某个窗口切到前台。目标也必须过白名单，否则等于绕过围栏。"""
    if not keyword:
        return ActionResult(False, "要给一个窗口标题或进程名关键字")
    infos = await asyncio.to_thread(list_windows, 60)
    allowed = allow_apps(cfg)
    matches = [w for w in infos if keyword.lower() in f"{w.title} {w.process}".lower()]
    if not matches:
        return ActionResult(False, f"没找到含「{keyword}」的窗口")
    target = matches[0]
    if not _hits_allow(target, allowed):
        return ActionResult(
            False, f"目标窗口不在白名单里：[{target.process}] {target.title}"
        )
    hit = _hits_deny(target, deny_keywords(cfg))
    if hit:
        return ActionResult(False, f"目标窗口命中敏感词「{hit}」，不切")

    def _bring() -> None:
        user32 = _user32()
        # SW_RESTORE=9：最小化的窗口也能拉起来，否则切过去是空的
        user32.ShowWindow(target.hwnd, 9)
        user32.SetForegroundWindow(target.hwnd)

    await asyncio.to_thread(_bring)
    return ActionResult(True, f"已切到 [{target.process}] {target.title}")


def status(cfg: Any) -> dict[str, Any]:
    """给健康检查 / 诊断脚本用的一行状态。"""
    enabled = bool(getattr(cfg, "computer_enable", False))
    info: dict[str, Any] = {
        "enabled": enabled,
        "platform_ok": IS_WINDOWS,
        "mode": str(getattr(cfg, "computer_mode", "view") or "view"),
        "allow_apps": allow_apps(cfg),
        "lease_remaining_sec": int(lease_remaining(cfg)) if enabled else 0,
    }
    if enabled and IS_WINDOWS:
        try:
            window = foreground_window()
            info["foreground"] = window.label() if window else ""
        except Exception as exc:  # noqa: BLE001
            info["foreground_error"] = f"{type(exc).__name__}: {exc}"
    return info
