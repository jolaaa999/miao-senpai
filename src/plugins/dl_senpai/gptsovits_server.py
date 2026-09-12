"""随 Bot 启动/关闭 GPT-SoVITS api_v2 子进程。"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from nonebot import logger

from .config import PluginConfig

_process: subprocess.Popen[bytes] | None = None
_log_file = None
_managed_by_bot = False


def parse_gptsovits_url(url: str) -> tuple[str, int]:
    parsed = urlparse(url.strip())
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9880
    return host, port


def resolve_gptsovits_python(home: Path) -> Path | None:
    candidates = [
        home / "runtime" / "python.exe",
        home / "runtime" / "python",
        home / "runtime" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_api_command(cfg: PluginConfig) -> tuple[Path, list[str], Path]:
    home = cfg.gptsovits_home_path()
    api_script = home / "api_v2.py"
    if not api_script.is_file():
        raise FileNotFoundError(f"GPT-SoVITS api_v2.py not found: {api_script}")

    python_exec = resolve_gptsovits_python(home)
    if python_exec is None:
        python_exec = Path(sys.executable)

    host, port = cfg.gptsovits_bind_host_port()
    config_path = cfg.gptsovits_config.strip() or "GPT_SoVITS/configs/tts_infer.yaml"
    cmd = [
        str(python_exec),
        str(api_script),
        "-a",
        host,
        "-p",
        str(port),
        "-c",
        config_path,
    ]
    return home, cmd, python_exec


async def start_gptsovits_server(cfg: PluginConfig) -> bool:
    """启动 GPT-SoVITS API；若端口已有服务则复用。"""
    global _process, _log_file, _managed_by_bot

    if not cfg.should_autostart_gptsovits():
        return False

    host, port = cfg.gptsovits_bind_host_port()
    if is_port_open(host, port):
        logger.info(f"[dl_senpai] gptsovits api already running at {host}:{port}")
        return True

    try:
        home, cmd, python_exec = build_api_command(cfg)
    except FileNotFoundError as e:
        logger.warning(f"[dl_senpai] gptsovits autostart skipped: {e}")
        return False

    log_path = cfg.project_root() / "data" / "dl_senpai" / "gptsovits-api.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_file = open(log_path, "a", encoding="utf-8")
    _log_file.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    _log_file.flush()

    logger.info(
        f"[dl_senpai] starting gptsovits api: cwd={home} python={python_exec} url={cfg.gptsovits_base_url}"
    )

    popen_kwargs: dict = {
        "cwd": home,
        "stdout": _log_file,
        "stderr": subprocess.STDOUT,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    _process = subprocess.Popen(cmd, **popen_kwargs)
    _managed_by_bot = True

    ready = await wait_for_gptsovits_ready(host, port, cfg.gptsovits_startup_timeout)
    if ready:
        logger.info(f"[dl_senpai] gptsovits api ready at {host}:{port} (log={log_path})")
        return True

    await stop_gptsovits_server()
    logger.error(
        f"[dl_senpai] gptsovits api failed to start within {cfg.gptsovits_startup_timeout}s; "
        f"see {log_path}"
    )
    return False


async def wait_for_gptsovits_ready(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process is not None and _process.poll() is not None:
            return False
        if is_port_open(host, port):
            return True
        await asyncio.sleep(1.0)
    return False


async def stop_gptsovits_server() -> None:
    global _process, _log_file, _managed_by_bot

    if not _managed_by_bot or _process is None:
        return

    proc = _process
    _process = None
    _managed_by_bot = False

    if proc.poll() is None:
        logger.info("[dl_senpai] stopping gptsovits api subprocess")
        proc.terminate()
        try:
            await asyncio.to_thread(proc.wait, 15)
        except subprocess.TimeoutExpired:
            proc.kill()
            await asyncio.to_thread(proc.wait)

    if _log_file is not None:
        _log_file.close()
        _log_file = None
