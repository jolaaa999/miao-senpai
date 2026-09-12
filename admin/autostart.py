"""随 bot.py 一并拉起/关闭学姐 Web 控制台（Go 后端 + Vite 前端）。"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PROCS: list[subprocess.Popen] = []
_LOG_HANDLES: list[object] = []


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _port_pids(port: int) -> list[int]:
    """找到监听该端口的进程 PID（netstat -ano）。"""
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return pids
    for line in out.splitlines():
        parts = line.split()
        # TCP  127.0.0.1:28473  0.0.0.0:0  LISTENING  1234
        if len(parts) >= 5 and parts[3].upper() == "LISTENING":
            local = parts[1].rsplit(":", 1)
            if len(local) == 2 and local[1].isdigit() and int(local[1]) == port:
                try:
                    pid = int(parts[4])
                except ValueError:
                    continue
                if pid > 0:
                    pids.append(pid)
    return sorted(set(pids))


def _kill_port(port: int, *, label: str) -> bool:
    """强杀占用端口的进程；返回是否已成功清空。"""
    pids = _port_pids(port)
    if not pids:
        return True
    for pid in pids:
        _log(f"{label}端口 {port} 被 pid={pid} 占用，结束旧进程…")
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
    time.sleep(1.0)
    remaining = _port_pids(port)
    if remaining:
        _log(f"{label}端口 {port} 仍被 {remaining} 占用，放弃杀进程")
        return False
    return True


def _ensure_port_free(port: int, *, label: str) -> bool:
    """端口空闲（或 kill 后空闲）则返回 True。"""
    if not _port_open("127.0.0.1", port):
        return True
    if not _env_flag("DL_SENPAI_CONSOLE_KILL_PORT", True):
        _log(f"{label}端口 {port} 已占用，跳过（DL_SENPAI_CONSOLE_KILL_PORT=false）")
        return False
    return _kill_port(port, label=label)


def _which(cmd: str) -> str | None:
    path = shutil.which(cmd)
    if path:
        return path
    if sys.platform == "win32" and not cmd.lower().endswith((".exe", ".cmd", ".bat")):
        return shutil.which(f"{cmd}.cmd") or shutil.which(f"{cmd}.exe")
    return None


def _log(msg: str) -> None:
    # bot.py 里 nonebot logger 可能尚未就绪；用 print 保证启动阶段可见
    print(f"[senpai-console] {msg}", flush=True)


def _open_log(name: str):
    log_dir = _ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    handle = path.open("a", encoding="utf-8")
    _LOG_HANDLES.append(handle)
    return handle


def _spawn(name: str, cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.Popen | None:
    log_handle = _open_log(f"console-{name}.log")
    merged = os.environ.copy()
    if env:
        merged.update(env)
    kwargs: dict = {
        "cwd": str(cwd),
        "env": merged,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        _log(f"{name} 启动失败: {exc}")
        return None
    _PROCS.append(proc)
    _log(f"{name} 已启动 pid={proc.pid} log=data/logs/console-{name}.log")
    return proc


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def start_console(*, backend_port: int = 28473, frontend_port: int = 41788) -> None:
    """启动管理端；端口被占用则先结束占用者再启动。可用 DL_SENPAI_CONSOLE_AUTOSTART=false 关闭。"""
    if not _env_flag("DL_SENPAI_CONSOLE_AUTOSTART", True):
        _log("已跳过（DL_SENPAI_CONSOLE_AUTOSTART=false）")
        return

    backend_dir = _ROOT / "admin" / "backend"
    frontend_dir = _ROOT / "admin" / "frontend"
    if not (backend_dir / "go.mod").is_file():
        _log(f"未找到后端: {backend_dir}")
        return
    if not (frontend_dir / "package.json").is_file():
        _log(f"未找到前端: {frontend_dir}")
        return

    # ---- 后端 ----
    if not _ensure_port_free(backend_port, label="后端"):
        _log(f"后端端口 {backend_port} 无法释放，跳过后端启动")
    else:
        go = _which("go")
        if not go:
            _log("未找到 go，跳过后端启动")
        else:
            _spawn(
                "backend",
                [go, "run", "./cmd/server/"],
                cwd=backend_dir,
                env={
                    "QQBOT_ROOT": str(_ROOT),
                    "CGO_ENABLED": "0",
                },
            )
            for _ in range(30):
                if _port_open("127.0.0.1", backend_port):
                    break
                time.sleep(0.4)
            if _port_open("127.0.0.1", backend_port):
                _log(f"后端就绪 http://127.0.0.1:{backend_port}/x7k9-dl-senpai-console/v1")
            else:
                _log("后端启动较慢或失败，详见 data/logs/console-backend.log")

    # ---- 前端 ----
    if not _ensure_port_free(frontend_port, label="前端"):
        _log(f"前端端口 {frontend_port} 无法释放，跳过前端启动")
    else:
        npm = _which("npm")
        if not npm:
            _log("未找到 npm，跳过前端启动")
        else:
            if not (frontend_dir / "node_modules").is_dir():
                _log("首次运行，安装前端依赖…")
                install = subprocess.run(
                    [npm, "install"],
                    cwd=str(frontend_dir),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if install.returncode != 0:
                    _log(f"npm install 失败: {(install.stderr or install.stdout)[:400]}")
                    return
            _spawn(
                "frontend",
                [npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(frontend_port)],
                cwd=frontend_dir,
            )
            for _ in range(40):
                if _port_open("127.0.0.1", frontend_port):
                    break
                time.sleep(0.4)
            if _port_open("127.0.0.1", frontend_port):
                _log(f"前端就绪 http://127.0.0.1:{frontend_port}")
            else:
                _log("前端启动较慢或失败，详见 data/logs/console-frontend.log")


def stop_console() -> None:
    """结束由本模块拉起的子进程。"""
    while _PROCS:
        proc = _PROCS.pop()
        _kill(proc)
    while _LOG_HANDLES:
        handle = _LOG_HANDLES.pop()
        try:
            handle.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    _log("管理端子进程已结束")
