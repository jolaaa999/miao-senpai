"""浏览器这一层的"像个人"改造：反检测指纹、持久 profile、人味操作。

背景（2026-10-28 查出来）
------------------------
小黑盒一天撞 4 次人机验证，而同一篇帖子 12:32 读是 0 字、16:12 再读有 1993 字——
说明不是封号，是"这个访客可疑"。可疑点有五处，全都写在代码里：

1. 每次 tick 都 launch 一个全新 chromium + new_context：指纹、插件、显存、
   Canvas、字体全是新的，只有 cookie 是旧熟人。真人不会一周换 70 个浏览器。
2. navigator.webdriver = true（自动化框架默认值，一行 JS 就能读到）。
3. 插件列表为空（真实 Chrome 有 5 个 PDF 内置插件）。
4. 没有 window.chrome 对象，languages 只有 zh-CN。
5. WebGL 报 SwiftShader（软件渲染，无头环境的特征）。

这一层改的就是这五处，外加"进页面先滚两下、鼠标动一动"和"从站内点进去"。

边界
----
不可能清零。社区类站点风控是持续对抗，这套东西只是把"一眼假"变成"要看一会儿"。
真要做到像人，得用真浏览器 + 真人操作节奏，那是另一个量级的事。
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any

__all__ = [
    "LAUNCH_ARGS",
    "STEALTH_JS",
    "apply_stealth",
    "human_touch",
    "open_page",
    "close_page",
    "profile_dir",
]

# 关掉自动化痕迹的启动参数
LAUNCH_ARGS = [
    # 最有用的一条：别让 chromium 自己声明"我是被自动化控制的"
    "--disable-blink-features=AutomationControlled",
    # 无头模式下这几个开关会让 WebGL / 字体 / 硬件能力更接近真机
    "--use-gl=swiftshader-webgl",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-first-run",
    "--no-default-browser-check",
]

# playwright 默认会带 --enable-automation，把它忽略掉
IGNORE_DEFAULT_ARGS = ["--enable-automation"]

STEALTH_JS = """
// 在页面脚本跑起来之前改掉那些"一眼假"的地方。
// 用 Object.defineProperty 而不是直接赋值：直接赋值会被检测到 getter/setter 不一致。
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 插件列表：真实 Chrome 内置 PDF 查看器等，chromium 无头是 0 个
const fakePlugins = [
  { name: 'PDF Viewer' }, { name: 'Chrome PDF Viewer' },
  { name: 'Chromium PDF Viewer' }, { name: 'Microsoft Edge PDF Viewer' },
  { name: 'WebKit built-in PDF' },
];
Object.defineProperty(navigator, 'plugins', { get: () => fakePlugins });
Object.defineProperty(navigator, 'mimeTypes', { get: () => [{ type: 'application/pdf' }] });

// 语言列表：只用 zh-CN 不像普通中文用户
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });

// 无头环境没有 window.chrome
if (!window.chrome) {
  Object.defineProperty(window, 'chrome', { value: { runtime: {}, loadTimes: () => {}, csi: () => {} } });
}

// 硬件并发数：有些环境报 0/1，一看就是容器
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

// WebGL 的 vendor/renderer 是最常用的无头检测点（SwiftShader 直接暴露）
for (const proto of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) {
  if (!proto) continue;
  const orig = proto.prototype.getParameter;
  proto.prototype.getParameter = function (p) {
    if (p === 0x9245) return 'Google Inc. (Intel)';            // UNMASKED_VENDOR_WEBGL
    if (p === 0x9246) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11, D3D11)';
    return orig.call(this, p);
  };
}

// 权限查询：无头环境对 notifications 的返回跟真人不一致
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (params) =>
    params && params.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery.call(window.navigator.permissions, params);
}
"""


def profile_dir(cfg: Any, site: str) -> Path:
    """一个站点一个 profile 目录。

    为什么要分站点：同一个 profile 里 cookie 越攒越多、指纹也稳定，看起来就是
    "这个人一直在用这台机器"。分站点是为了避免 A 站的 cookie 污染 B 站，
    也方便单独删掉某一个站重来。
    """
    base = str(getattr(cfg, "browser_profile_root", "") or "")
    root = Path(base) if base else Path("data") / "dl_senpai" / "browser_profiles"
    return root / (site or "shared")


async def apply_stealth(context: Any) -> None:
    """把反检测脚本挂到 context 上（对之后打开的每个页面都生效）。"""
    try:
        await context.add_init_script(STEALTH_JS)
    except Exception:  # noqa: BLE001 — 挂不上就别挂，不能因此打不开页面
        pass


async def human_touch(page: Any, *, scrolls: tuple[int, int] = (2, 5)) -> None:
    """进页面后随便动两下：鼠标移过去、滚几屏、停一会儿。

    为什么要做：原来的流程是 goto 完直接抽 innerText，页面上的行为轨迹是零。
    真人不会这样——他会把页面往下翻，鼠标也会乱动。
    """
    try:
        size = page.viewport_size or {"width": 1280, "height": 900}
        width, height = int(size["width"]), int(size["height"])
        for _ in range(random.randint(2, 4)):
            await page.mouse.move(
                random.randint(40, max(41, width - 40)),
                random.randint(40, max(41, height - 40)),
                steps=random.randint(4, 12),
            )
            await asyncio.sleep(random.uniform(0.15, 0.45))
        for _ in range(random.randint(*scrolls)):
            await page.mouse.wheel(0, random.randint(180, 520))
            await asyncio.sleep(random.uniform(0.4, 1.2))
        # 停一下再抽正文：XHR 回来的正文也有时间挂上 DOM
        await asyncio.sleep(random.uniform(0.6, 1.6))
    except Exception:  # noqa: BLE001 — 动两下失败不影响读正文
        pass


async def open_page(
    p: Any,
    cfg: Any,
    *,
    site: str = "shared",
    headless: bool = True,
    storage_state: str | None = None,
    persist_path: str | None = None,
    viewport: dict[str, int] | None = None,
    user_agent: str | None = None,
) -> tuple[Any, Any, Any]:
    """开一个页面，返回 (context, browser, page)。

    两种模式：
    - 持久 profile（默认）：同一个目录反复用，cookie 和指纹都长在这个目录里，
      像是同一个人在反复来看。这是"每次都是新设备"那个最大破绽的解药。
    - 临时 context：老路子，只在 profile 关掉时用。

    browser 在持久模式下是 None（持久 context 自己就是浏览器），close_page 会处理。
    """
    args = list(LAUNCH_ARGS) if bool(getattr(cfg, "browser_stealth_enable", True)) else []
    use_profile = bool(getattr(cfg, "browser_profile_enable", True))
    vp = viewport or {"width": 1280, "height": 900}
    ua = user_agent or None

    if use_profile:
        target = profile_dir(cfg, site)
        first_time = not target.exists()
        target.mkdir(parents=True, exist_ok=True)
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(target),
            headless=headless,
            args=args,
            ignore_default_args=IGNORE_DEFAULT_ARGS,
            locale="zh-CN",
            user_agent=ua,
            viewport=vp,
        )
        # 第一次建 profile：把旧的 storage_state（登录态 json）灌进去，
        # 否则她得重新登录一次——登录是有头窗口，得人扫码。
        if first_time and storage_state:
            try:
                import json as _json

                cookies = (_json.loads(Path(storage_state).read_text(encoding="utf-8")) or {}).get("cookies") or []
                if cookies:
                    await context.add_cookies(cookies)
            except Exception:  # noqa: BLE001
                pass
        await apply_stealth(context)
        page = context.pages[0] if context.pages else await context.new_page()
        return context, None, page

    browser = await p.chromium.launch(
        headless=headless, args=args, ignore_default_args=IGNORE_DEFAULT_ARGS
    )
    context = await browser.new_context(
        locale="zh-CN",
        user_agent=ua,
        viewport=vp,
        storage_state=storage_state if storage_state else None,
    )
    await apply_stealth(context)
    page = await context.new_page()
    return context, browser, page


async def close_page(context: Any, browser: Any, *, persist_path: str | None = None) -> None:
    """关掉，并把 cookie 回写出去（回写失败不致命）。"""
    if persist_path:
        try:
            Path(persist_path).parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(persist_path))
        except Exception:  # noqa: BLE001
            pass
    try:
        if browser is not None:
            await browser.close()
        else:
            await context.close()
    except Exception:  # noqa: BLE001
        pass
