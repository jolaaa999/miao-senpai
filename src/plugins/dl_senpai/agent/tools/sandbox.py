"""高风险工具的沙箱边界与输出裁剪。

参考《深入理解 AI Agent》第 4 章 4.4（执行侧防御四层）与第 5 章（编码/通用 Agent）。

**必须说清楚这个沙箱是什么、不是什么**：

它是**软边界**——把默认工作目录钉在 `data/dl_senpai/workspace`，并在每次
文件/命令调用前做路径归属校验。它能挡住模型的"手滑"（写相对路径写到别处、
`..` 爬出去、`cd /` 之后乱来），但它**不是安全边界**：

    Python 进程能做的事，它就能做。真正的隔离要靠操作系统级手段
    （容器、Windows Job Object + AppContainer、seccomp 之类），
    本项目没有引入。

那为什么还够用：这些工具的**可见性被钉死在管理员 QQ 上**
（见 permissions.HIGH_RISK_TOOLS）。触发者是有完全权限的运维本人，
不是群友。真正要防的是「群里的不可信内容把模型绕进去」——那条路被
「工具只对管理员可见」堵住了，第 4 章讲的致命三要素（私有数据 + 不可信
内容 + 外发能力）里最关键的第二项因此不成立。

在此之上仍然叠了三道纵深防御：
    1. 路径归属校验（本模块）
    2. 危险命令模式拒绝（shell_tool 的 deny list）
    3. 执行前二次授权 + 全量审计日志（permissions / executor）
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

__all__ = [
    "SandboxViolation",
    "DEFAULT_SANDBOX_DIR",
    "resolve_sandbox_root",
    "resolve_in_sandbox",
    "relative_display",
    "truncate_output",
    "looks_binary",
]

# 默认沙箱目录。与 .env 的 DL_SENPAI_AGENT_SANDBOX_DIR 默认值保持一致。
DEFAULT_SANDBOX_DIR = "data/dl_senpai/workspace"


class SandboxViolation(Exception):
    """路径越出沙箱。由工具转成 ToolResult(success=False)，不向上抛。"""


def resolve_sandbox_root(cfg: Any, *, base: str | Path | None = None) -> Path:
    """解析并创建沙箱根目录。

    相对路径以**进程工作目录**为基准（与项目里其它 data/ 目录一致），
    不用 `__file__` —— 否则从不同 cwd 启动会得到不同的沙箱，
    「同一个 bot 数据在哪」会变成玄学。
    """
    raw = ""
    if cfg is not None:
        raw = str(getattr(cfg, "agent_sandbox_dir", "") or "")
    raw = raw.strip() or DEFAULT_SANDBOX_DIR
    root = Path(raw)
    if not root.is_absolute():
        root = Path(base or os.getcwd()) / root
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_in_sandbox(root: Path, user_path: str, *, must_exist: bool = False) -> Path:
    """把模型给的路径解析成沙箱内的**真实**路径，越界则抛 SandboxViolation。

    三条规则：

    1. 相对路径以沙箱根为基准（这样模型写 `notes/a.md` 落在沙箱里，
       而不是落在 bot 的进程目录里）。
    2. 绝对路径也被接受，但**结果必须仍在沙箱内**——管理员偶尔会想读
       项目里的文件，允许他显式写全路径，前提是那条路径确实在沙箱下。
    3. 解析 `..` 与符号链接之后再判定归属。只比对字符串前缀是不够的，
       `workspace/../.env` 和指向外部的软链都能骗过朴素的前缀检查。
    """
    raw = str(user_path or "").strip()
    if not raw:
        raise SandboxViolation("路径为空")
    # 空字节是路径解析里的经典截断攻击面，直接拒绝。
    if "\x00" in raw:
        raise SandboxViolation("路径包含非法字符")

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate

    # resolve(strict=False) 会把 `..` 与软链一并展开成真实路径；
    # 文件还不存在时（写入场景）也能算出它的最终落点。
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:  # RuntimeError: 软链成环
        raise SandboxViolation(f"路径无法解析: {exc}") from exc

    root_resolved = root.resolve()
    if not _is_within(resolved, root_resolved):
        raise SandboxViolation(
            f"路径越出沙箱（只允许 {root_resolved} 之内）: {raw}"
        )
    if must_exist and not resolved.exists():
        raise SandboxViolation(f"文件不存在: {raw}")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    """path 是否等于 root 或在其之下。

    用 commonpath 而不是 `str.startswith`：后者会把 `/tmp/ws-evil`
    误判成 `/tmp/ws` 的子路径（前缀相同但根本不是同一棵子树）。
    """
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        # 不同盘符（Windows 上 C: 与 D:）会抛 ValueError —— 显然不在沙箱内。
        return False


def relative_display(path: Path, root: Path) -> str:
    """给模型看的路径用相对形式：绝对路径又长又泄露宿主目录结构。"""
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:
        return path.as_posix()


def _decode_text(data: bytes) -> str | None:
    """尝试按 UTF-8 / GBK 解码；解不出来返回 None。

    为什么要在末尾裁 0~3 个字节重试：调用方只读了前 4096 字节，
    一个多字节字符很可能正好被切断。不裁的话，「中文文件刚好在第
    4096 字节处断开」会被误判成二进制。
    """
    for encoding in ("utf-8", "gbk", "cp936"):
        for trim in (0, 1, 2, 3):
            chunk = data[: len(data) - trim] if trim else data
            if not chunk:
                continue
            try:
                return chunk.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
    return None


def looks_binary(data: bytes) -> bool:
    """粗判是不是二进制内容。

    判据是「能不能解出文本来 + 控制字符多不多」，
    **不是**「可打印 ASCII 字节占比」——后者会把整篇中文判成二进制：
    UTF-8 里一个汉字是 3 个 >0x7F 的字节，按字节算可打印率永远偏低。
    这是第一版踩过的坑：中文笔记读不出来，还报成「二进制文件」。
    """
    if not data:
        return False
    # 空字节是二进制最可靠的信号（文本编码里不会出现裸 NUL）。
    if b"\x00" in data:
        return True
    text = _decode_text(data)
    if text is None:
        return True
    if not text:
        return False
    control = sum(1 for ch in text if ord(ch) < 0x20 and ch not in "\n\r\t")
    return control / len(text) > 0.05


def truncate_output(
    text: str,
    *,
    max_chars: int = 6000,
    head_lines: int = 50,
    tail_lines: int = 50,
) -> str:
    """保留头尾、裁掉中段（第 4 章 4.4 的长输出策略）。

    为什么是头尾而不是头部截断：命令输出里**结尾往往最关键**
    （报错栈、测试结果、退出摘要），只留头会把这些全丢掉；
    而开头往往有命令回显与上下文。中间那几百行才是最可牺牲的。
    """
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) > head_lines + tail_lines:
        omitted = len(lines) - head_lines - tail_lines
        lines = (
            lines[:head_lines]
            + [f"...（省略中间 {omitted} 行）..."]
            + lines[-tail_lines:]
        )
    out = "\n".join(lines)
    if len(out) <= max_chars:
        return out
    half = max(1, max_chars // 2)
    dropped = len(out) - max_chars
    return (
        out[:half]
        + f"\n...（省略中间 {dropped} 字符）...\n"
        + out[-half:]
    )


def strip_ansi(text: str) -> str:
    """去掉终端颜色码。模型看不懂 ANSI 转义序列，只会浪费 token。"""
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text or "")