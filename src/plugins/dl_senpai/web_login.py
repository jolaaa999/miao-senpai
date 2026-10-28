"""各平台的持久登录态（cookie 落盘，不碰密码）。

为什么需要这一层
----------------
browser_agent._with_page 每次都是干净的临时 context，逛完就销毁。
凡是"要登录才能做"的动作——发说说、给帖子评论——都做不了。
这一层只解决一件事：把"主人手动登录一次"的结果存下来，之后复用。

三条底线
--------
1. **不接收、不存储账号密码**。只保存站点自己发的 cookie + localStorage。
2. **只接小号**。登录完成后解析出 uin，若等于主人自己的 QQ，直接拒收并删掉
   这份 state——用主号发东西，出事是不可逆的。
3. **权限 600 意义的自觉**：state 文件里就是登录凭证，等同于密码。
   落在 data/dl_senpai/web_login/ 下，不要提交、不要外传（.gitignore 已覆盖 data/）。

现状
----
qzone：登录后能发说说（走文档里的 emotion_cgi_publish_v6 接口）。
heybox：登录后能评论（走 Playwright 点 UI，因为接口要 hkey 签名）。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "read_nickname",
    "SITES",
    "WebLoginError",
    "optional_state_path",
    "assert_not_owner_account",
    "has_state",
    "read_logged_uin",
    "save_login",
    "state_path",
    "with_context",
]

# site -> (中文名, 登录入口)。登录入口只用于"打开一个像样的页面让人扫码/输密码"。
SITES: dict[str, tuple[str, str]] = {
    "qzone": ("QQ空间", "https://user.qzone.qq.com/"),
    "heybox": ("小黑盒", "https://www.xiaoheihe.cn/"),
    "bili": ("B站", "https://www.bilibili.com/"),
    # 抖音必须登录才有推荐流（匿名打开首页会弹登录框），所以只能走有头登录
    "douyin": ("抖音", "https://www.douyin.com/jingxuan"),
}

# 从 cookie 里认账号的候选字段（不同站点不一样，取到第一个非空的就够）
# DedeUserID 是 B 站的账号 id（SESSDATA 是登录票据，bili_jct 是 csrf，别搞混）
_UIN_COOKIES = (
    "uin",
    "p_uin",
    "heybox_id",
    "userid",
    "user_id",
    "account_id",
    "DedeUserID",
    # 抖音登录后的身份 cookie
    "uid_tt",
    "uid_tt_ss",
)

_DEFAULT_DIR = "data/dl_senpai/web_login"


class WebLoginError(Exception):
    """登录态缺失或不可用。message 是给主人看的人话。"""


def _cfg_dir(cfg: Any) -> Path:
    raw = str(getattr(cfg, "web_login_dir", "") or "")
    return Path(raw or _DEFAULT_DIR)


def state_path(site: str, cfg: Any = None) -> Path:
    """某个站点的登录态文件。"""
    return _cfg_dir(cfg) / f"{site}.json"


def has_state(site: str, cfg: Any = None) -> bool:
    return state_path(site, cfg).exists()


def optional_state_path(site: str, cfg: Any = None) -> str | None:
    """有登录态就用，没有就返回 None（只读场景可以匿名跑）。

    只读层用它：有 cookie 罐（哪怕没登录，只是一个过了验证的匿名罐）
    就带上，能显著降低被要人机验证的概率。
    """
    path = state_path(site, cfg)
    return str(path) if path.exists() else None


def _normalize_uin(value: str) -> str:
    """qzone 的 uin 带前缀 o（o0123456789），去掉才是 QQ 号。"""
    raw = (value or "").strip()
    if raw[:1] in ("o", "O") and raw[1:].isdigit():
        return raw[1:]
    return raw


def read_nickname(site: str, cfg: Any = None) -> str:
    """从 state 里取登录账号的昵称（小黑盒存在 nickname cookie 里，URL 编码过）。

    为什么要昵称：评论区里要认出"哪条是我说的、哪条是别人回我的"，
    数字 id 在页面 DOM 里看不到，昵称才看得到。
    """
    import re
    from urllib.parse import unquote

    path = state_path(site, cfg)
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    for cookie in data.get("cookies") or []:
        if isinstance(cookie, dict) and str(cookie.get("name") or "") == "nickname":
            raw = unquote(str(cookie.get("value") or ""))
            # 小黑盒把昵称存成 %u4E5D%u513F... 这种 JS escape，不是 URL 编码，
            # 直接 unquote 只会得到一串 %uXXXX，所以这里再解一次。
            raw = re.sub(
                r"%u([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), raw
            )
            return raw.strip()
    return ""


def read_logged_uin(site: str, cfg: Any = None) -> str:
    """从已存的 state 里解析出登录账号。解析不出就返回空串。"""
    path = state_path(site, cfg)
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        return ""
    found: dict[str, str] = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if name in _UIN_COOKIES and value:
            found.setdefault(name, value)
    for name in _UIN_COOKIES:
        if found.get(name):
            return _normalize_uin(found[name])
    return ""


def assert_not_owner_account(site: str, cfg: Any) -> str:
    """登的是主人的主号就拒绝。返回解析到的账号（可能为空）。

    为什么要在"登录完"和"每次用之前"都查一遍：主人可能事后又用主号登了一次，
    那时旧的 state 会被覆盖成主号——所以真正发东西之前必须再验一次。
    """
    if not bool(getattr(cfg, "web_login_guard_owner", True)):
        return read_logged_uin(site, cfg)
    uin = read_logged_uin(site, cfg)
    owners = set()
    try:
        owners = set(cfg.owner_id_set())
    except Exception:  # noqa: BLE001
        owners = set()
    if uin and uin in owners:
        raise WebLoginError(
            f"{SITES.get(site, (site, ''))[0]} 上登的是你的主号（{uin}）。"
            "换个小号重新登录，主号不许用来发东西。"
        )
    return uin


async def save_login(
    site: str,
    cfg: Any = None,
    *,
    timeout_sec: float = 300.0,
    prompt: Callable[[str], None] = print,
    start_url: str | None = None,
    require_uin: bool = True,
) -> Path:
    """开一个**有头**浏览器，让主人手动登录，然后落盘。

    为什么要有头：扫码/滑块这些没法自动化，也不该自动化。
    登录完在终端按一次回车即可（不给回车也行，到超时自动保存）。
    """
    if site not in SITES:
        raise WebLoginError(f"不认识的站点：{site}（支持：{', '.join(SITES)}）")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise WebLoginError('没装 playwright：pip install -e ".[browser]" && playwright install chromium') from exc

    from .browser_agent import _UA

    label, default_url = SITES[site]
    target_url = start_url or default_url
    path = state_path(site, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    if require_uin:
        prompt(f"即将打开浏览器，请在窗口里登录{label}（扫码/密码都行）。")
    else:
        prompt(f"即将打开 {target_url}")
        prompt("如果弹出人机验证（滑块/点选），在这个窗口里过掉它。")
    prompt("完成后回到这个终端按回车保存（不按也行，到时会自动保存）。")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(locale="zh-CN", user_agent=_UA)
        page = await context.new_page()
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)

        # 边等边看：主人按回车 / 或者页面自己跳进了登录后的地址
        loop = asyncio.get_running_loop()
        pressed = loop.create_future()

        def _on_enter() -> None:
            if not pressed.done():
                pressed.set_result(True)

        try:
            await loop.run_in_executor(None, lambda: (input(), _on_enter()))
        except Exception:  # noqa: BLE001 — 没有 stdin（比如后台跑）就走超时
            pass

        deadline = time.time() + max(5.0, timeout_sec)
        while not pressed.done() and time.time() < deadline:
            await asyncio.sleep(1.0)

        await context.storage_state(path=str(path))
        await browser.close()

    uin = read_logged_uin(site, cfg)
    if not uin and require_uin:
        path.unlink(missing_ok=True)
        raise WebLoginError(f"没抓到 {label} 的登录凭证，可能登录没成功，重来一次")
    if uin:
        assert_not_owner_account(site, cfg)
    prompt(f"已保存：{path}（账号 {uin}）")
    return path


@dataclass
class LoginInfo:
    site: str
    label: str = ""
    uin: str = ""
    saved_at: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.uin)


def describe_login(site: str, cfg: Any = None) -> LoginInfo:
    """给健康检查/状态展示用：有没有登录、登的是哪个号。"""
    label = SITES.get(site, (site, ""))[0]
    path = state_path(site, cfg)
    if not path.exists():
        return LoginInfo(site=site, label=label)
    stamp = 0.0
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = 0.0
    return LoginInfo(site=site, label=label, uin=read_logged_uin(site, cfg), saved_at=stamp)


async def with_context(
    site: str,
    cfg: Any,
    fn: Callable[[Any], Any],
    *,
    headless: bool = True,
) -> Any:
    """带着已保存的登录态起一次浏览器，把 page 交给 fn。

    和 browser_agent 共用一把浏览器锁：同一台机器同时开两个 chromium
    既慢又容易被站点判定成异常。
    """
    if site not in SITES:
        raise WebLoginError(f"不认识的站点：{site}")
    path = state_path(site, cfg)
    if not path.exists():
        raise WebLoginError(
            f"{SITES[site][0]} 还没登录过。先在仓库根目录运行："
            f"python scripts\\login-web.py {site}"
        )
    assert_not_owner_account(site, cfg)

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise WebLoginError('没装 playwright：pip install -e ".[browser]"') from exc

    from .browser_agent import _UA, _browser_lock
    from .browser_stealth import close_page, open_page

    async with _browser_lock:
        async with async_playwright() as p:
            # 和 browser_agent 走同一套开页逻辑：持久 profile + 反检测脚本。
            # 评论是最容易被风控抓的动作，指纹不一致会更惨。
            context, browser, page = await open_page(
                p,
                cfg,
                site=site,
                headless=headless,
                storage_state=str(path),
                persist_path=str(path),
                viewport={"width": 1280, "height": 900},
                user_agent=_UA,
            )
            try:
                page.set_default_timeout(20000)
                return await fn(page)
            finally:
                await close_page(context, browser, persist_path=str(path))
