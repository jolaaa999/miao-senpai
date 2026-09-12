"""用本机 QQ 客户端 UI / 深链，按名单逐个发起加好友（RPA）。

改进：
- 启动前激活 QQ 窗口
- 校准同时截按钮模板，点击优先「找图」再回退坐标
- 只有校验通过（资料卡上不再出现「加好友」，或出现「等待验证」）才记入 done
- 失败写入 failed，不记完成

依赖：pip install pyautogui pyperclip opencv-python-headless
可选：keyboard（F8/ESC）、pygetwindow
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Shanghai")
_ROOT = Path(__file__).resolve().parents[3]
_CAND_DIR = _ROOT / "data" / "dl_senpai" / "social" / "candidates"
_TPL_DIR = _CAND_DIR / "rpa_templates"
_DEBUG_DIR = _CAND_DIR / "rpa_debug"
_CALIB_PATH = _CAND_DIR / "rpa_calibration.json"
_PROGRESS_PATH = _CAND_DIR / "rpa_progress.json"
_PAUSE_FLAG = _CAND_DIR / "rpa_pause.flag"
_STOP_FLAG = _CAND_DIR / "rpa_stop.flag"
_SCRIPT = _ROOT / "scripts" / "qq_friend_add_rpa.py"

_TPL_ADD = "add_friend_btn.png"
_TPL_SEND = "send_btn.png"
_TPL_VERIFY = "verify_field.png"
_TPL_PENDING = "pending_verify.png"  # 「等待验证」可选
_TPL_CLOSE = "close_btn.png"


@dataclass
class RpaConfig:
    mode: str = "deeplink"
    verify_msg: str = "我是群里的学姐~"
    delay_min: float = 8.0
    delay_max: float = 18.0
    open_wait: float = 3.5
    after_click_wait: float = 1.5
    verify_wait: float = 2.0
    dry_run: bool = False
    max_count: int = 0
    list_path: str = ""
    locate_confidence: float = 0.82
    require_verified: bool = False  # 默认不校验；依次点完即记完成


@dataclass
class Calibration:
    add_friend_btn: tuple[int, int] = (0, 0)
    send_btn: tuple[int, int] = (0, 0)
    verify_field: tuple[int, int] = (0, 0)
    close_btn: tuple[int, int] = (0, 0)
    notes: str = ""

    @classmethod
    def load(cls, path: Path = _CALIB_PATH) -> "Calibration":
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))

        def _xy(key: str) -> tuple[int, int]:
            v = data.get(key) or [0, 0]
            return int(v[0]), int(v[1])

        return cls(
            add_friend_btn=_xy("add_friend_btn"),
            send_btn=_xy("send_btn"),
            verify_field=_xy("verify_field"),
            close_btn=_xy("close_btn"),
            notes=str(data.get("notes") or ""),
        )

    def save(self, path: Path = _CALIB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "add_friend_btn": list(self.add_friend_btn),
            "send_btn": list(self.send_btn),
            "verify_field": list(self.verify_field),
            "close_btn": list(self.close_btn),
            "notes": self.notes,
            "saved_at": datetime.now(_TZ).isoformat(timespec="seconds"),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def ready_for_clicks(self) -> bool:
        has_coord = self.add_friend_btn != (0, 0) and self.send_btn != (0, 0)
        has_tpl = (_TPL_DIR / _TPL_ADD).is_file() and (_TPL_DIR / _TPL_SEND).is_file()
        return has_coord or has_tpl


def _log(msg: str) -> None:
    ts = datetime.now(_TZ).strftime("%H:%M:%S")
    print(f"[friend_rpa {ts}] {msg}", flush=True)


def load_uin_list(list_path: str = "") -> list[str]:
    path = Path(list_path) if list_path else (_CAND_DIR / "non_friends_latest.txt")
    if not path.is_file():
        raise FileNotFoundError(f"名单不存在: {path}（先私聊发送「导出未加好友」）")
    uins: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = "".join(ch for ch in line.strip() if ch.isdigit())
        if s:
            uins.append(s)
    seen: set[str] = set()
    out: list[str] = []
    for u in uins:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def load_progress() -> dict[str, Any]:
    if not _PROGRESS_PATH.is_file():
        return {"done": [], "failed": [], "updated_at": ""}
    try:
        data = json.loads(_PROGRESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"done": [], "failed": [], "updated_at": ""}
    if not isinstance(data, dict):
        return {"done": [], "failed": [], "updated_at": ""}
    data.setdefault("done", [])
    data.setdefault("failed", [])
    return data


def save_progress(
    done: Iterable[str],
    failed: Iterable[str],
    *,
    last_results: list[dict[str, Any]] | None = None,
) -> None:
    _CAND_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "done": list(done),
        "failed": list(failed),
        "updated_at": datetime.now(_TZ).isoformat(timespec="seconds"),
        "note": "done=已按顺序点过加好友；默认不校验对方是否收到",
    }
    if last_results is not None:
        payload["last_results"] = last_results[-50:]
    _PROGRESS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def clear_control_flags() -> None:
    for p in (_PAUSE_FLAG, _STOP_FLAG):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def request_stop() -> None:
    _CAND_DIR.mkdir(parents=True, exist_ok=True)
    _STOP_FLAG.write_text("1", encoding="utf-8")


def request_pause_toggle() -> None:
    _CAND_DIR.mkdir(parents=True, exist_ok=True)
    if _PAUSE_FLAG.exists():
        _PAUSE_FLAG.unlink(missing_ok=True)
    else:
        _PAUSE_FLAG.write_text("1", encoding="utf-8")


def _wait_if_paused() -> None:
    while _PAUSE_FLAG.exists() and not _STOP_FLAG.exists():
        _log("已暂停（删除 rpa_pause.flag 或再按 F8 继续）…")
        time.sleep(1.0)


def _should_stop() -> bool:
    return _STOP_FLAG.exists()


def build_deeplink(uin: str, mode: str) -> str:
    mode = (mode or "deeplink").lower()
    if mode in ("addcontact", "legacy"):
        return (
            "tencent://AddContact/?fromId=30&fromSubId=1&subcmd=all"
            f"&uin={uin}"
        )
    params = json.dumps(
        {"uin": str(uin), "sourceType": "QrCodeShareBuddyLink"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    enc = urllib.parse.quote(params, safe="")
    return (
        "tencent://ntqq-open?subCmd=profile&action=openMiniBuddyProfile"
        f"&actionParams={enc}"
    )


def open_deeplink(url: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(url)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", url])  # noqa: S603
    else:
        subprocess.Popen(["xdg-open", url])  # noqa: S603


def _ensure_pyautogui():
    try:
        import pyautogui  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "缺少 pyautogui，请执行: pip install pyautogui pyperclip opencv-python-headless"
        ) from exc
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.08
    return pyautogui


def activate_qq_window() -> bool:
    """把标题含 QQ 的窗口拉到前台（Windows）。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lp):  # type: ignore[misc]
            if not user32.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            title = buf.value or ""
            # 排除我们自己的控制台
            low = title.lower()
            if "qq" in low and "rpa" not in low and "powershell" not in low:
                if "qq" == low.strip() or "qq" in title or title.startswith("QQ"):
                    found.append(int(hwnd))
            return True

        user32.EnumWindows(_enum, 0)
        if not found:
            # 放宽：任意含 QQ 的可见窗
            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def _enum2(hwnd, _lp):  # type: ignore[misc]
                if not user32.IsWindowVisible(hwnd):
                    return True
                buf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, buf, 512)
                if "QQ" in (buf.value or ""):
                    found.append(int(hwnd))
                return True

            user32.EnumWindows(_enum2, 0)
        if not found:
            _log("未找到 QQ 窗口标题，继续尝试（请确认 QQ 界面可见）")
            return False
        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        _log(f"已激活 QQ 窗口 hwnd={hwnd}")
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"激活 QQ 窗口失败: {exc}")
        return False


def _type_text(pyautogui, text: str) -> None:
    try:
        import pyperclip

        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
    except Exception:  # noqa: BLE001
        pyautogui.write(text, interval=0.05)


def _tpl_path(name: str) -> Path:
    return _TPL_DIR / name


def _capture_template(pyautogui, center: tuple[int, int], name: str, w: int = 120, h: int = 48) -> Path:
    _TPL_DIR.mkdir(parents=True, exist_ok=True)
    x, y = center
    left = max(0, x - w // 2)
    top = max(0, y - h // 2)
    shot = pyautogui.screenshot(region=(left, top, w, h))
    path = _tpl_path(name)
    shot.save(path)
    return path


def locate_center(
    pyautogui,
    template_name: str,
    *,
    confidence: float,
    tries: int = 4,
    pause: float = 0.45,
) -> tuple[int, int] | None:
    path = _tpl_path(template_name)
    if not path.is_file():
        return None
    last_err: Exception | None = None
    for _ in range(max(1, tries)):
        try:
            try:
                box = pyautogui.locateOnScreen(str(path), confidence=confidence)
            except TypeError:
                # 无 opencv 时不支持 confidence
                box = pyautogui.locateOnScreen(str(path))
            if box:
                c = pyautogui.center(box)
                return int(c.x), int(c.y)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(pause)
    if last_err:
        _log(f"找图 {template_name} 异常: {last_err}")
    return None


def click_target(
    pyautogui,
    *,
    template_name: str,
    fallback_xy: tuple[int, int],
    confidence: float,
    label: str,
) -> bool:
    """优先模板找图点击；找不到再用校准坐标。返回是否认为点到了。"""
    xy = locate_center(pyautogui, template_name, confidence=confidence)
    source = "template"
    if xy is None:
        if fallback_xy == (0, 0):
            _log(f"未找到「{label}」模板且无校准坐标")
            return False
        xy = fallback_xy
        source = "calib"
        _log(f"「{label}」用校准坐标 {xy}")
    else:
        _log(f"「{label}」找图命中 {xy}")

    # 先移入再点，提高命中（单击；加好友场景双击易连点两次）
    pyautogui.moveTo(xy[0], xy[1], duration=0.18)
    time.sleep(0.1)
    pyautogui.click()
    _log(f"已点击「{label}」via {source}")
    return True


def _save_debug(pyautogui, tag: str) -> None:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = _DEBUG_DIR / f"{datetime.now(_TZ).strftime('%H%M%S')}_{tag}.png"
        pyautogui.screenshot(path)
        _log(f"调试截图: {path}")
    except Exception:  # noqa: BLE001
        pass


def profile_shows_add_friend(pyautogui, calib: Calibration, confidence: float) -> bool:
    if locate_center(pyautogui, _TPL_ADD, confidence=confidence, tries=3):
        return True
    # 无模板时无法可靠判断
    return False


def profile_shows_pending(pyautogui, confidence: float) -> bool:
    return locate_center(pyautogui, _TPL_PENDING, confidence=confidence, tries=3) is not None


def verify_request_sent(
    pyautogui,
    uin: str,
    cfg: RpaConfig,
    calib: Calibration,
) -> tuple[bool, str]:
    """再次打开资料卡，确认不再能「加好友」或已「等待验证」。"""
    time.sleep(max(0.5, cfg.verify_wait))
    open_deeplink(build_deeplink(uin, cfg.mode))
    time.sleep(max(1.0, cfg.open_wait))
    activate_qq_window()
    time.sleep(0.5)

    pending = profile_shows_pending(pyautogui, cfg.locate_confidence)
    if pending:
        return True, "看到「等待验证」模板"

    has_add_tpl = _tpl_path(_TPL_ADD).is_file()
    if has_add_tpl:
        still_add = profile_shows_add_friend(pyautogui, calib, cfg.locate_confidence)
        if still_add:
            return False, "资料卡仍能匹配「加好友」按钮=申请未发出"
        return True, "「加好友」按钮已消失（视为已申请/已是好友）"

    return False, "无「加好友」模板无法校验，拒记完成（请重新 --calibrate）"


def run_one(
    uin: str,
    cfg: RpaConfig,
    calib: Calibration,
    pyautogui,
) -> tuple[bool, str]:
    """返回 (本条已处理完, 原因)。默认不校验是否真正发出。"""
    url = build_deeplink(uin, cfg.mode)
    _log(f"打开 {uin} → {url[:80]}…")
    if cfg.dry_run:
        return True, "dry-run"

    activate_qq_window()
    open_deeplink(url)
    time.sleep(max(0.8, cfg.open_wait))
    activate_qq_window()
    time.sleep(0.3)

    # 1) 必须点到「加好友」
    ok_add = click_target(
        pyautogui,
        template_name=_TPL_ADD,
        fallback_xy=calib.add_friend_btn,
        confidence=cfg.locate_confidence,
        label="加好友",
    )
    if not ok_add:
        _save_debug(pyautogui, f"{uin}_no_add_btn")
        # 仍继续尝试校准坐标硬点一次（用户要求依次加、不强调校验）
        if calib.add_friend_btn != (0, 0):
            pyautogui.moveTo(*calib.add_friend_btn, duration=0.15)
            pyautogui.click()
            time.sleep(max(0.5, cfg.after_click_wait))
        else:
            return False, "未点到加好友（找图失败且无有效坐标）"
    else:
        time.sleep(max(0.5, cfg.after_click_wait))

    # 2) 验证语
    if cfg.verify_msg:
        clicked_field = click_target(
            pyautogui,
            template_name=_TPL_VERIFY,
            fallback_xy=calib.verify_field,
            confidence=cfg.locate_confidence,
            label="验证框",
        )
        if clicked_field or calib.verify_field != (0, 0):
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.05)
            _type_text(pyautogui, cfg.verify_msg)
            time.sleep(0.25)

    # 3) 发送
    ok_send = click_target(
        pyautogui,
        template_name=_TPL_SEND,
        fallback_xy=calib.send_btn,
        confidence=cfg.locate_confidence,
        label="发送",
    )
    if not ok_send:
        pyautogui.press("enter")
        time.sleep(0.4)
        _log("发送按钮未命中，已尝试 Enter")
    time.sleep(max(0.5, cfg.after_click_wait))

    if calib.close_btn != (0, 0) or _tpl_path(_TPL_CLOSE).is_file():
        click_target(
            pyautogui,
            template_name=_TPL_CLOSE,
            fallback_xy=calib.close_btn,
            confidence=cfg.locate_confidence,
            label="关闭",
        )

    if cfg.require_verified:
        ok, reason = verify_request_sent(pyautogui, uin, cfg, calib)
        if not ok:
            _save_debug(pyautogui, f"{uin}_verify_fail")
        return ok, reason

    return True, "已依次点击（未校验）"

def run_batch(cfg: RpaConfig) -> dict[str, Any]:
    uins = load_uin_list(cfg.list_path)
    progress = load_progress()
    done = set(str(x) for x in progress.get("done") or [])
    failed = set(str(x) for x in progress.get("failed") or [])
    # 失败的本轮会重试；完成的跳过
    pending = [u for u in uins if u not in done]
    if cfg.max_count and cfg.max_count > 0:
        pending = pending[: cfg.max_count]

    _log(
        f"名单={len(uins)} 已处理={len(done)} 本轮待跑={len(pending)} "
        f"mode={cfg.mode} require_verified={cfg.require_verified}"
    )
    if not pending:
        return {"ok": True, "ran": 0, "done": len(done), "pending": 0}

    calib = Calibration.load()
    if not cfg.dry_run and not calib.ready_for_clicks():
        _log("请先校准: python scripts/qq_friend_add_rpa.py --calibrate")
        return {"ok": False, "error": "not_calibrated", "ran": 0}

    pyautogui = None if cfg.dry_run else _ensure_pyautogui()
    clear_control_flags()
    ran = 0
    ok_n = 0
    last_results: list[dict[str, Any]] = list(progress.get("last_results") or [])

    _log("开始依次加好友（默认不校验是否发出）。F8 暂停，ESC/rpa_stop.flag 停止")
    try:
        if pyautogui is not None:
            import threading

            def _hotkeys() -> None:
                try:
                    import keyboard

                    keyboard.add_hotkey("f8", request_pause_toggle)
                    keyboard.add_hotkey("esc", request_stop)
                    keyboard.wait()
                except Exception:  # noqa: BLE001
                    return

            threading.Thread(target=_hotkeys, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass

    activate_qq_window()

    for uin in pending:
        if _should_stop():
            _log("收到停止信号，结束")
            break
        _wait_if_paused()
        if _should_stop():
            break
        try:
            assert pyautogui is not None or cfg.dry_run
            ok, reason = run_one(uin, cfg, calib, pyautogui)
            last_results.append(
                {
                    "uin": uin,
                    "ok": ok,
                    "reason": reason,
                    "at": datetime.now(_TZ).isoformat(timespec="seconds"),
                }
            )
            if ok:
                done.add(uin)
                failed.discard(uin)
                ok_n += 1
                _log(f"→ 已处理 {uin}: {reason}")
            else:
                # 即使失败也记 done，避免卡住反复点同一个人（按「依次加即可」）
                done.add(uin)
                failed.add(uin)
                _log(f"→ 跳过并记进度 {uin}: {reason}")
        except Exception as exc:  # noqa: BLE001
            done.add(uin)
            failed.add(uin)
            last_results.append(
                {
                    "uin": uin,
                    "ok": False,
                    "reason": str(exc),
                    "at": datetime.now(_TZ).isoformat(timespec="seconds"),
                }
            )
            _log(f"→ 异常仍记进度 {uin}: {exc}")
            if pyautogui is not None:
                _save_debug(pyautogui, f"{uin}_exc")
        ran += 1
        save_progress(
            sorted(done, key=int),
            sorted(failed, key=int),
            last_results=last_results,
        )
        gap = random.uniform(cfg.delay_min, cfg.delay_max)
        _log(f"本轮 {ran}/{len(pending)} 已点={ok_n} 等待 {gap:.1f}s")
        end = time.time() + gap
        while time.time() < end:
            if _should_stop():
                break
            _wait_if_paused()
            time.sleep(0.2)

    clear_control_flags()
    summary = {
        "ok": True,
        "ran": ran,
        "ok_n": ok_n,
        "done": len(done),
        "failed": len(failed),
        "pending_left": max(0, len(uins) - len(done)),
    }
    _log(f"结束 {summary}")
    return summary


def run_calibrate() -> None:
    pyautogui = _ensure_pyautogui()
    _TPL_DIR.mkdir(parents=True, exist_ok=True)
    activate_qq_window()
    _log(
        "校准：请先手动打开一个「仍显示加好友」的资料卡。\n"
        "每步把鼠标移到目标中心，终端回车；会同时保存坐标+按钮截图模板。"
    )
    steps = [
        (_TPL_ADD, "add_friend_btn", "资料卡上的「加好友」按钮中心", 140, 56),
        (_TPL_VERIFY, "verify_field", "验证信息输入框中心（没有则把鼠标移到屏幕左上角附近再回车跳过）", 160, 40),
        (_TPL_SEND, "send_btn", "「发送」/「确定」按钮中心", 120, 48),
        (_TPL_PENDING, "pending_skip", "可选：「等待验证」文字中心（没有则鼠标移到左上角回车跳过）", 140, 40),
        (_TPL_CLOSE, "close_btn", "关闭资料卡按钮（可跳过：鼠标移到左上角回车）", 40, 40),
    ]
    calib = Calibration.load()
    for tpl_name, attr, desc, w, h in steps:
        input(f"→ 鼠标移到【{desc}】后按 Enter…")
        x, y = pyautogui.position()
        # 左上角近似跳过
        if x < 15 and y < 15:
            _log(f"跳过 {tpl_name}")
            continue
        _log(f"记录 {attr}=({x},{y}) 并截模板 {tpl_name}")
        if attr not in ("pending_skip",) and hasattr(calib, attr):
            setattr(calib, attr, (int(x), int(y)))
        _capture_template(pyautogui, (x, y), tpl_name, w=w, h=h)
    calib.notes = "coords+templates; success needs add gone or pending tpl"
    calib.save()
    _log(f"校准完成: {_CALIB_PATH}")
    _log(f"模板目录: {_TPL_DIR}")
    _log("建议再跑: python scripts/qq_friend_add_rpa.py --reset-progress --max 1")


def spawn_rpa_process(
    *,
    max_count: int = 0,
    dry_run: bool = False,
    mode: str = "deeplink",
    verify_msg: str = "我是群里的学姐~",
    delay_min: float = 8.0,
    delay_max: float = 18.0,
    reset_progress: bool = False,
) -> subprocess.Popen:
    _CAND_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _CAND_DIR / "rpa_run.log"
    cmd = [
        sys.executable,
        str(_SCRIPT),
        "--mode",
        mode,
        "--verify-msg",
        verify_msg,
        "--delay-min",
        str(delay_min),
        "--delay-max",
        str(delay_max),
    ]
    if max_count and max_count > 0:
        cmd.extend(["--max", str(max_count)])
    if dry_run:
        cmd.append("--dry-run")
    if reset_progress:
        cmd.append("--reset-progress")

    log_f = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    log_f.write(f"\n==== spawn {datetime.now(_TZ).isoformat()} ====\n")
    log_f.flush()
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=str(_ROOT),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    meta = {
        "pid": proc.pid,
        "cmd": cmd,
        "log": str(log_path),
        "started_at": datetime.now(_TZ).isoformat(timespec="seconds"),
    }
    (_CAND_DIR / "rpa_process.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return proc
