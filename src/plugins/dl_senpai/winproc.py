"""Windows 下启动子进程统一「别弹黑窗口」。

为什么要单独一个模块：一天里 ffmpeg 要起几百次（每轮抽 8 帧）、powershell 要起
几十次（她自己的 shell 工具）、TTS 服务每次启动一次。只要有一处忘了加
CREATE_NO_WINDOW，用户就会看到黑窗口一闪——而且极难定位是哪个功能引起的。

所以规矩是：**所有 spawn 子进程的地方都从这里拿参数**，不许各写一套。
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

__all__ = ["no_window_kwargs"]


def no_window_kwargs(extra_flags: int = 0) -> dict[str, Any]:
    """返回让子进程不开窗的 Popen/create_subprocess_exec 参数。

    Windows 之外直接返回空 dict（POSIX 没有这个概念）。
    extra_flags 用来叠加调用方自己的需求，比如 CREATE_NEW_PROCESS_GROUP：
    叠加而不是覆盖，免得调用方为了加一个 flag 就把 NO_WINDOW 弄丢了。
    """
    if os.name != "nt":
        return {}
    kwargs: dict[str, Any] = {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        | int(extra_flags or 0),
    }
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE
    kwargs["startupinfo"] = startup
    return kwargs
