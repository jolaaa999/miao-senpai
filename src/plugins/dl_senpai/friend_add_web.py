"""通过 NapCat Cookie + QQ 空间 CGI 主动发好友申请。

NapCat 无主动加好友 OneBot API。本模块：
1. 缓存 get_cookies（避免每人打一次被限流）
2. 拉取 qzonetoken
3. 调 friend_addfriend.cgi（群来源），要求 JSON 响应

风控自负：务必限速与每日配额。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

try:
    from nonebot import logger
except ImportError:
    import logging

    logger = logging.getLogger("dl_senpai")  # type: ignore[assignment]

_ADD_URLS = (
    "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/tfriend/friend_addfriend.cgi",
    "https://h5.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/tfriend/friend_addfriend.cgi",
)
_AUTH_URL = (
    "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com"
    "/cgi-bin/tfriend/friend_authfriend.cgi"
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Cookie / token 缓存，避免刷 get_cookies
_cookie_cache: dict[str, Any] = {"ts": 0.0, "cookie": "", "gtk": "", "token": ""}
_COOKIE_TTL = 300.0


def gtk_from_skey(skey: str) -> str:
    h = 5381
    for ch in skey or "":
        h += (h << 5) + ord(ch)
    return str(h & 0x7FFFFFFF)


def parse_cookie_string(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _uin_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "") or value


def clear_cookie_cache() -> None:
    _cookie_cache.update({"ts": 0.0, "cookie": "", "gtk": "", "token": ""})


async def fetch_napcat_cookies(bot, domain: str = "qzone.qq.com") -> tuple[str, str]:
    """返回 (cookie_header, g_tk)。合并多域名 Cookie，优先 p_skey 算 g_tk。"""
    maps: dict[str, str] = {}
    last_err: Exception | None = None
    for d in (domain, "user.qzone.qq.com", "qq.com"):
        try:
            data = await bot.call_api("get_cookies", domain=d)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
        if not isinstance(data, dict):
            continue
        raw = str(data.get("cookies") or "").strip()
        if raw:
            maps.update(parse_cookie_string(raw))
    if not maps:
        raise RuntimeError(f"get_cookies empty; last_err={last_err}")
    key = maps.get("p_skey") or maps.get("skey") or ""
    if not key:
        raise RuntimeError("cookies missing p_skey/skey")
    cookie = "; ".join(f"{k}={v}" for k, v in maps.items())
    return cookie, gtk_from_skey(key)


async def _fetch_qzonetoken(cookie: str, self_uin: str, timeout: float) -> str:
    """从空间首页 HTML 提取 qzonetoken。"""
    url = f"https://user.qzone.qq.com/{self_uin}"
    headers = {
        "User-Agent": _UA,
        "Cookie": cookie,
        "Referer": "https://user.qzone.qq.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except Exception:  # noqa: BLE001
        return ""
    text = resp.text or ""
    for pat in (
        r'window\.g_qzonetoken\s*=\s*(?:encodeURIComponent\()?["\']([^"\']+)["\']',
        r'g_qzonetoken\s*=\s*(?:encodeURIComponent\()?["\']([^"\']+)["\']',
        r'qzonetoken["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        r'return\s+"([a-zA-Z0-9_-]{8,})"',
    ):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""


async def _ensure_session(bot, self_id: int | str, timeout: float) -> tuple[str, str, str]:
    now = time.monotonic()
    if (
        _cookie_cache["cookie"]
        and _cookie_cache["gtk"]
        and now - float(_cookie_cache["ts"]) < _COOKIE_TTL
    ):
        return (
            str(_cookie_cache["cookie"]),
            str(_cookie_cache["gtk"]),
            str(_cookie_cache["token"] or ""),
        )
    cookie, gtk = await fetch_napcat_cookies(bot)
    self_uin = _uin_digits(str(self_id))
    token = await _fetch_qzonetoken(cookie, self_uin, timeout)
    _cookie_cache.update({"ts": now, "cookie": cookie, "gtk": gtk, "token": token})
    if not token:
        logger.warning("dl_senpai friend_add_web: qzonetoken empty (may still work)")
    return cookie, gtk, token


def _parse_cgi_payload(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    if raw.startswith("<!") or "<html" in raw[:80].lower():
        # iframe 回调页：尝试抠 cb({...}) / frameElement
        for pat in (
            r"cb\s*\(\s*(\{.*?\})\s*\)",
            r"callback\s*\(\s*(\{.*?\})\s*\)",
            r"_Callback\s*\(\s*(\{.*?\})\s*\)",
        ):
            m = re.search(pat, raw, re.S)
            if m:
                try:
                    data = json.loads(m.group(1))
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    pass
        if "ptlogin2" in raw or "登录" in raw:
            return {"_error": "login_required", "_raw": raw[:200]}
        return {"_error": "html_response", "_raw": raw[:200]}
    m = re.search(r"(\{.*\})", raw, re.S)
    if not m:
        return {"_raw": raw[:300]}
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, dict) else {"_raw": raw[:300]}
    except json.JSONDecodeError:
        return {"_raw": raw[:300]}


def _add_ok(payload: dict[str, Any]) -> bool:
    if not payload or payload.get("_error"):
        return False
    code = payload.get("code", payload.get("retcode", payload.get("ret")))
    try:
        if code is not None and int(code) == 0:
            return True
    except (TypeError, ValueError):
        pass
    msg = str(payload.get("message") or payload.get("msg") or "")
    ok_hints = ("成功", "已发送", "等待验证", "申请已发送", "好友请求")
    if any(h in msg for h in ok_hints):
        return True
    already = ("已经是好友", "已是好友", "重复添加", "正在等待验证", "对方已是你的好友")
    return any(h in msg for h in already)


async def send_friend_request_via_web(
    bot,
    *,
    self_id: int | str,
    user_id: int,
    group_id: int | str,
    verify_msg: str,
    timeout: float = 20.0,
) -> tuple[bool, str]:
    """群来源主动加好友。返回 (ok, detail)。"""
    msg = (verify_msg or "").strip() or "我是群里的学姐~"
    try:
        cookie, gtk, token = await _ensure_session(bot, self_id, timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"get_cookies failed: {exc}"

    self_uin = _uin_digits(str(self_id))
    target = str(int(user_id))
    gid = str(int(group_id))
    referer = (
        f"https://user.qzone.qq.com/{self_uin}"
        f"/friend/addfriend?ouin={target}&from=3&groupId={gid}"
    )
    base_form = {
        "sid": "0",
        "ouin": target,
        "fuin": target,
        "uin": self_uin,
        "from_source": "3",
        "sourceId": "3",
        "groupId": gid,
        "fupdate": "1",
        "flag": "0",
        "im": "0",
        "from": "3",
        "msg": msg,
        "struin": target,
        "format": "json",
        "qzreferrer": quote(referer, safe=""),
    }
    headers = {
        "User-Agent": _UA,
        "Cookie": cookie,
        "Referer": referer,
        "Origin": "https://user.qzone.qq.com",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }

    last_detail = ""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            # 可选预热：authfriend（部分环境需要）
            if token:
                auth_q = f"{_AUTH_URL}?g_tk={gtk}&qzonetoken={quote(token)}"
                try:
                    await client.post(
                        auth_q,
                        data={
                            "sid": "0",
                            "ouin": target,
                            "uin": self_uin,
                            "fuin": target,
                            "sourceId": "3",
                            "fupdate": "1",
                            "qzreferrer": quote(
                                f"https://user.qzone.qq.com/{target}", safe=""
                            ),
                        },
                        headers=headers,
                    )
                except Exception:  # noqa: BLE001
                    pass

            for add_url in _ADD_URLS:
                qs = f"g_tk={gtk}"
                if token:
                    qs += f"&qzonetoken={quote(token)}"
                url = f"{add_url}?{qs}"
                resp = await client.post(url, data=base_form, headers=headers)
                payload = _parse_cgi_payload(resp.text)
                last_detail = json.dumps(payload, ensure_ascii=False)[:400]
                if payload.get("_error") == "login_required":
                    clear_cookie_cache()
                    return False, last_detail
                if _add_ok(payload):
                    return True, last_detail
                # HTML 无 JSON：换下一个 URL
                if payload.get("_error") == "html_response":
                    continue
                # 业务错误也返回，便于日志
                if "code" in payload or "ret" in payload or "retcode" in payload:
                    return False, last_detail
    except Exception as exc:  # noqa: BLE001
        return False, f"http error: {exc}"

    return False, last_detail or "empty response"
