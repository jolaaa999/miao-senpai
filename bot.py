"""NoneBot2 入口：先启动本进程，再让 NapCat 反向 WebSocket 连入。

默认一并拉起：
- 学姐 Web 控制台（DL_SENPAI_CONSOLE_AUTOSTART）
- GPT-SoVITS（DL_SENPAI_GPTSOVITS_AUTOSTART）
- QQ 空间桥 onebot-qzone（DL_SENPAI_QZONE_ENABLE + DL_SENPAI_QZONE_AUTOSTART）
  连上 NapCat 后自动用 Cookie 登录空间，无需扫码。
"""

from pathlib import Path
import sys

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.log import logger

nonebot.init()

# 同步写入 data/logs，供 Web 控制台「运行日志」读取（与终端输出一致）
_log_dir = Path(__file__).resolve().parent / "data" / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
# 文件用纯文本格式，避免 ANSI 色码污染后台展示
_file_log_format = "{time:MM-DD HH:mm:ss} [{level}] {name} | {message}"
logger.add(
    str(_log_dir / "bot-{time:YYYYMMDD}.log"),
    format=_file_log_format,
    level="INFO",
    encoding="utf-8",
    rotation="00:00",
    retention="30 days",
    enqueue=True,
    colorize=False,
)

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_from_toml("pyproject.toml")


def _kill_port_occupants() -> None:
    """启动前清掉占用机器人端口的旧进程（否则 bind 报 10048 退出）。

    可用 DL_SENPAI_CONSOLE_KILL_PORT=false 关闭。
    """
    port = int(driver.config.port or 0)
    if not port:
        return

    def _log(msg: str) -> None:
        logger.info(f"[port-cleanup] {msg}")

    # netstat 找 LISTENING 的 PID
    import subprocess

    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError as exc:
        _log(f"netstat 不可用: {exc}")
        return
    pids: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING":
            local = parts[1].rsplit(":", 1)
            if len(local) == 2 and local[1].isdigit() and int(local[1]) == port:
                try:
                    pids.add(int(parts[4]))
                except ValueError:
                    continue
    pids.discard(0)
    if not pids:
        return
    for pid in pids:
        _log(f"端口 {port} 被 pid={pid} 占用，结束旧进程…")
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            import os
            import signal

            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    import time

    time.sleep(1.0)


def _bootstrap_admin_console() -> None:
    try:
        from admin.autostart import start_console, stop_console
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"管理端 autostart 导入失败: {exc}")
        return

    @driver.on_shutdown
    async def _stop_admin_console() -> None:
        stop_console()

    try:
        start_console()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"管理端启动失败（bot 仍继续）: {exc}")


if __name__ == "__main__":
    _kill_port_occupants()
    _bootstrap_admin_console()
    nonebot.run()
