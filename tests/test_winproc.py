"""子进程不许开窗的测试。

Windows 上一天要起几百次 ffmpeg、几十次 powershell。任何一处漏了
CREATE_NO_WINDOW，用户就会看到黑窗口闪——很难定位是哪个功能。
所以这里钉住：工具函数真的带了 flag，且四个调用点都接上了。
"""

from __future__ import annotations

import importlib
import inspect
import os

from dl_senpai import winproc


def test_no_window_kwargs_on_windows() -> None:
    kwargs = winproc.no_window_kwargs()
    if os.name != "nt":
        assert kwargs == {}
        return
    assert kwargs["creationflags"] & 0x08000000        # CREATE_NO_WINDOW
    assert kwargs["startupinfo"].wShowWindow == 0      # SW_HIDE
    assert kwargs["startupinfo"].dwFlags & 0x1         # STARTF_USESHOWWINDOW


def test_extra_flags_are_added_not_replacing() -> None:
    kwargs = winproc.no_window_kwargs(0x00000200)      # CREATE_NEW_PROCESS_GROUP
    if os.name != "nt":
        return
    # 叠加：既要进程组，也要不开窗
    assert kwargs["creationflags"] & 0x00000200
    assert kwargs["creationflags"] & 0x08000000


def test_call_sites_use_helper() -> None:
    """四个会频繁起子进程的地方都必须用这个工具，不许各写一套。"""
    for mod_name in (
        "dl_senpai.video_vision",
        "dl_senpai.gptsovits_server",
        "dl_senpai.doubao_ppt",
        "dl_senpai.agent.tools.exec_tools",
    ):
        mod = importlib.import_module(mod_name)
        src = inspect.getsource(mod)
        assert "no_window_kwargs" in src, mod_name
