"""用 NapCat get_cookies 登录 onebot-qzone（无需扫码）。

写入：
1. onebot-qzone/test_cache/cookies.json（桥接启动时自动读）
2. onebot-qzone/.env 的 QZONE_COOKIE_STRING
3. 若桥接已启动：POST login_cookie
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

try:
    from nonebot import logger
except ImportError:
    import logging

    logger = logging.getLogger("dl_senpai")  # type: ignore[assignment]

from .friend_add_web import parse_cookie_string
from .qzone_client import QZoneBridgeClient, QZoneBridgeError

if TYPE_CHECKING:
    from .config import PluginConfig

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_QZONE_DIR = _PROJECT_ROOT / "onebot-qzone"
_COOKIE_DOMAINS = ("qzone.qq.com", "user.qzone.qq.com", "qq.com")


def _qzone_dir(config: "PluginConfig | None" = None) -> Path:
    if config is not None and hasattr(config, "qzone_home_path"):
        return config.qzone_home_path()
    return _DEFAULT_QZONE_DIR



def merge_cookie_maps(*maps: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in maps:
        out.update(m)
    return out


def cookie_map_to_string(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v is not None)


def normalize_qzone_cookies(cookies: dict[str, str], self_id: str | int | None = None) -> dict[str, str]:
    """补齐 uin / p_uin，保证桥接能解析 QQ 号。"""
    out = dict(cookies)
    uin = out.get("uin") or out.get("p_uin") or ""
    digits = re.sub(r"\D", "", uin) or re.sub(r"\D", "", str(self_id or ""))
    if digits:
        out.setdefault("uin", f"o{digits}")
        out.setdefault("p_uin", f"o{digits}")
    return out


def cookies_usable(cookies: dict[str, str]) -> bool:
    has_id = bool(cookies.get("uin") or cookies.get("p_uin"))
    has_key = bool(cookies.get("p_skey") or cookies.get("skey"))
    return has_id and has_key


async def fetch_napcat_qzone_cookies(bot) -> dict[str, str]:
    maps: list[dict[str, str]] = []
    last_err: Exception | None = None
    for domain in _COOKIE_DOMAINS:
        try:
            data = await bot.call_api("get_cookies", domain=domain)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
        if not isinstance(data, dict):
            continue
        raw = str(data.get("cookies") or "").strip()
        if raw:
            maps.append(parse_cookie_string(raw))
    merged = merge_cookie_maps(*maps)
    self_id = getattr(bot, "self_id", None)
    merged = normalize_qzone_cookies(merged, self_id)
    if not cookies_usable(merged):
        raise RuntimeError(
            f"NapCat cookies incomplete (need uin + p_skey/skey); last_err={last_err}"
        )
    return merged


def write_qzone_cookie_files(cookies: dict[str, str], qzone_dir: Path) -> Path:
    """写 cookies.json + 更新 .env 中的 QZONE_COOKIE_STRING。返回 cookies.json 路径。"""
    cache = qzone_dir / "test_cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / "cookies.json"
    payload = {"last_used": time.time(), "cookies": cookies}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    env_path = qzone_dir / ".env"
    cookie_str = cookie_map_to_string(cookies)
    lines: list[str]
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    keys = ("QZONE_COOKIE_STRING", "QZONE_COOKIE")
    written = {k: False for k in keys}
    out: list[str] = []
    for line in lines:
        hit = False
        for k in keys:
            if line.startswith(f"{k}="):
                out.append(f"{k}={cookie_str}")
                written[k] = True
                hit = True
                break
        if hit:
            continue
        if line.startswith("QZONE_ENABLE_QR="):
            out.append("QZONE_ENABLE_QR=0")
            continue
        out.append(line)
    if not written["QZONE_COOKIE_STRING"]:
        out.append(f"QZONE_COOKIE_STRING={cookie_str}")
    if not any(l.startswith("QZONE_ENABLE_QR=") for l in out):
        out.append("QZONE_ENABLE_QR=0")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


async def push_cookie_to_bridge(config: "PluginConfig", cookie_str: str) -> bool:
    client = QZoneBridgeClient(
        getattr(config, "qzone_bridge_url", "") or "",
        access_token=getattr(config, "qzone_access_token", "") or "",
        timeout=float(getattr(config, "qzone_timeout", 30) or 30),
    )
    if not client.configured():
        return False
    try:
        await client.login_cookie(cookie_str)
        return True
    except QZoneBridgeError as exc:
        logger.warning(f"dl_senpai qzone: bridge login_cookie failed: {exc}")
        return False


async def sync_napcat_cookies_to_qzone(bot, config: "PluginConfig") -> dict[str, Any]:
    """从 NapCat 拉 Cookie → 落盘 → 尝试热登录桥接。"""
    cookies = await fetch_napcat_qzone_cookies(bot)
    qdir = _qzone_dir(config)
    path = write_qzone_cookie_files(cookies, qdir)
    cookie_str = cookie_map_to_string(cookies)
    hot = await push_cookie_to_bridge(config, cookie_str)
    uin = re.sub(r"\D", "", cookies.get("uin") or cookies.get("p_uin") or "")
    logger.info(
        f"dl_senpai qzone: NapCat cookie synced uin={uin} file={path} bridge_hot={hot}"
    )
    return {
        "ok": True,
        "uin": uin,
        "cookie_file": str(path),
        "bridge_hot": hot,
        "keys": sorted(cookies.keys()),
    }
