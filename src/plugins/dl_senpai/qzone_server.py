"""随 Bot 启动/关闭 onebot-qzone 空间桥子进程，并用 NapCat Cookie 登录。"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from nonebot import logger

from .config import PluginConfig

_process: subprocess.Popen[bytes] | None = None
_log_file = None
_managed_by_bot = False


def parse_bridge_url(url: str) -> tuple[str, int]:
    parsed = urlparse((url or "").strip() or "http://127.0.0.1:5700")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5700
    return host, port


def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_npm() -> str | None:
    name = "npm.cmd" if sys.platform == "win32" else "npm"
    found = shutil.which(name) or shutil.which("npm")
    return found


def resolve_npx_tsx(home: Path) -> list[str] | None:
    """优先用本地 node_modules/.bin/tsx，避免依赖全局。"""
    if sys.platform == "win32":
        local = home / "node_modules" / ".bin" / "tsx.cmd"
        if local.is_file():
            return [str(local), "src/main.ts"]
    else:
        local = home / "node_modules" / ".bin" / "tsx"
        if local.is_file():
            return [str(local), "src/main.ts"]
    npx = shutil.which("npx.cmd" if sys.platform == "win32" else "npx")
    if npx:
        return [npx, "tsx", "src/main.ts"]
    return None


def build_qzone_command(cfg: PluginConfig) -> tuple[Path, list[str]]:
    home = cfg.qzone_home_path()
    if not home.is_dir():
        raise FileNotFoundError(f"onebot-qzone 目录不存在: {home}")
    main_ts = home / "src" / "main.ts"
    if not main_ts.is_file():
        raise FileNotFoundError(f"onebot-qzone 入口不存在: {main_ts}")
    node_modules = home / "node_modules"
    if not node_modules.is_dir():
        raise FileNotFoundError(
            f"onebot-qzone 未安装依赖，请先在 {home} 执行 npm install"
        )

    cmd = resolve_npx_tsx(home)
    if cmd is None:
        npm = resolve_npm()
        if npm is None:
            raise FileNotFoundError("找不到 npm/tsx，请安装 Node.js")
        cmd = [npm, "run", "dev"]
    return home, cmd


async def wait_for_bridge_ready(host: str, port: int, timeout: float) -> bool:
    deadline = asyncio.get_event_loop().time() + max(1.0, timeout)
    while asyncio.get_event_loop().time() < deadline:
        if is_port_open(host, port):
            return True
        await asyncio.sleep(0.5)
    return False


async def start_qzone_bridge(cfg: PluginConfig) -> bool:
    """启动 onebot-qzone；端口已有服务则复用。"""
    global _process, _log_file, _managed_by_bot

    if not cfg.should_autostart_qzone():
        return False

    host, port = cfg.qzone_bind_host_port()
    if is_port_open(host, port):
        logger.info(f"[dl_senpai] qzone bridge already running at {host}:{port}")
        return True

    try:
        home, cmd = build_qzone_command(cfg)
    except FileNotFoundError as e:
        logger.warning(f"[dl_senpai] qzone bridge autostart skipped: {e}")
        return False

    log_path = cfg.project_root() / "data" / "dl_senpai" / "qzone-bridge.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_file = open(log_path, "ab", buffering=0)  # noqa: SIM115

    env = os.environ.copy()
    # 子进程读自己目录下的 .env；端口与 bot 配置对齐
    env.setdefault("ONEBOT_HOST", host if host not in {"0.0.0.0", "::"} else "127.0.0.1")
    env["ONEBOT_PORT"] = str(port)
    env["QZONE_ENABLE_QR"] = "0"

    logger.info(
        f"[dl_senpai] starting qzone bridge: cwd={home} cmd={' '.join(cmd)} "
        f"url={cfg.qzone_bridge_url}"
    )

    popen_kwargs: dict = {
        "cwd": str(home),
        "stdout": _log_file,
        "stderr": subprocess.STDOUT,
        "env": env,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    _process = subprocess.Popen(cmd, **popen_kwargs)
    _managed_by_bot = True

    ready = await wait_for_bridge_ready(host, port, cfg.qzone_startup_timeout)
    if ready:
        logger.info(
            f"[dl_senpai] qzone bridge ready at {host}:{port} (log={log_path})"
        )
        return True

    await stop_qzone_bridge()
    logger.warning(
        f"[dl_senpai] qzone bridge failed to start within {cfg.qzone_startup_timeout}s; "
        f"see {log_path}"
    )
    return False


async def stop_qzone_bridge() -> None:
    global _process, _log_file, _managed_by_bot
    if not _managed_by_bot or _process is None:
        _process = None
        _managed_by_bot = False
        if _log_file is not None:
            try:
                _log_file.close()
            except Exception:  # noqa: BLE001
                pass
            _log_file = None
        return

    proc = _process
    _process = None
    _managed_by_bot = False
    logger.info("[dl_senpai] stopping qzone bridge subprocess")
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[dl_senpai] qzone bridge stop failed: {exc}")
    finally:
        if _log_file is not None:
            try:
                _log_file.close()
            except Exception:  # noqa: BLE001
                pass
            _log_file = None
