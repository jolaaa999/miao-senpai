# -*- coding: utf-8 -*-
"""驱动本机桌面端应用生成 PPT，并取回产物文件。

为什么单独成一个模块：
    这套流程涉及「进程管理 + 浏览器协议 + 下载捕获」三类脏活，
    把它关在这里，creator_tools 那边就只剩「给主题、拿文件」两句话。

对外只暴露 make_deck()，且**不向上层泄漏任何 UI 细节**——
上层的提示词与给群友的反馈都不允许提到它到底怎么实现的。

三个关键约束：
1. **全局串行**：底层只有一个窗口、一个会话，同时跑两份会互相污染，
   所以用模块级 Lock 排队，而不是各跑各的。
2. **进程自愈**：调试端口不通就杀掉重拉。用户平时双击图标启动的实例
   不带端口，必须由这里负责用正确参数拉起。
3. **同步代码跑在子线程**：驱动库是同步 API，直接在事件循环里跑会卡死
   bot 的所有并发任务（群聊、心跳、其他工具），所以一律 asyncio.to_thread。
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time
from pathlib import Path

# 应用可执行文件与调试端口。
# 路径写在这里而不是配置项：这是本机安装位置，换机器才会变。
_APP_EXE = Path(r"F:\Doubao\app\Doubao.exe")
_DEBUG_PORT = 9337
_APP_PROCESS = "Doubao"

# 生成一份 PPT 的实测耗时约 5 分钟，留足余量到 12 分钟。
# 超时不代表失败——可能只是页数多，但总要有个上限把窗口释放出来。
_GENERATE_TIMEOUT = 1080.0

_lock = asyncio.Lock()


class DeckError(RuntimeError):
    """对外统一异常类型，避免把底层符号泄漏到上层。"""


# ----------------------------------------------------------------
# 进程管理
# ----------------------------------------------------------------


def _port_open(port: int = _DEBUG_PORT) -> bool:
    """探测调试端口是否已在监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _kill_app() -> None:
    """杀掉应用全部进程。

    它是 Electron 系的多进程结构（主进程 + 若干渲染进程），
    只杀主进程会留下孤儿渲染进程占着单实例锁。
    """
    from .winproc import no_window_kwargs

    subprocess.run(
        ["taskkill", "/F", "/IM", _APP_PROCESS + ".exe", "/T"],
        capture_output=True, check=False,
        # taskkill 也是控制台程序，不加这个会闪一下
        **no_window_kwargs(),
    )
    # 单实例锁释放需要一点时间，立刻重启会被判定为「已有一个实例」而直接退出
    time.sleep(4.0)


def _launch_app() -> None:
    """带调试端口启动应用。

    必须用 subprocess.Popen 而不是 run：GUI 程序不会退出，
    run 会一直等下去。同时要脱离父进程，避免 bot 重启时把它带走。
    """
    subprocess.Popen(
        [str(_APP_EXE), "--remote-debugging-port=" + str(_DEBUG_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        # DETACHED_PROCESS 本身就没有控制台；仍然叠上 NO_WINDOW，
        # 免得以后有人改 flag 组合时又把窗口放出来
        **no_window_kwargs(
            0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        ),
    )


def _ensure_app(timeout: float = 45.0) -> None:
    """确保应用在带调试端口的状态下运行。"""
    if not _APP_EXE.exists():
        raise DeckError("本机找不到所需的应用程序，无法生成")

    if _port_open():
        return

    _kill_app()
    _launch_app()

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.5)
        if _port_open():
            # 端口通了不代表窗口渲染完了，多给几秒让界面就绪
            time.sleep(6.0)
            return
    raise DeckError("应用启动失败或未响应")


# ----------------------------------------------------------------
# 页面操作（全部是同步代码，只在子线程里跑）
# ----------------------------------------------------------------

# 判定「本轮生成完成」的信号。
#
# 走过的弯路：一开始用「卡片上出现下载按钮」，结果提交后几秒就误判完成 ——
# 进度卡片一出现就自带下载入口，那时正文还在第 2 页。
# 后一版用「下载按钮数量比开工前多」，同样早：卡片出现即满足。
#
# 真正只在完成时出现的是「共用时 X 分 Y 秒」；
# 生成全程则常驻「正在写入文件 / 正在思考」这类活动指示。
# 两个信号一起用：先看用时标记的增量（精确），再用活动指示静默做兜底（容错）。
_COUNT_DL_JS = """() => {
  let n = 0;
  for (const e of document.querySelectorAll('span[aria-label="下载"]')) {
    const r = e.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) n++;
  }
  return n;
}"""

# 只在完成态出现的用时标记。按「元素自身文本」匹配，避免把父容器的长文本也算进来。
_COUNT_ELAPSED_JS = """() => {
  let n = 0;
  for (const e of document.querySelectorAll('*')) {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const own = [...e.childNodes].filter((x) => x.nodeType === 3).map((x) => x.textContent).join('').trim();
    if (own && own.length < 30 && own.indexOf('共用时') === 0) n++;
  }
  return n;
}"""

# 生成中的活动指示个数。为 0 且持续若干轮，说明这一轮真的停下来了。
_COUNT_BUSY_JS = """() => {
  let n = 0;
  for (const e of document.querySelectorAll('*')) {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const own = [...e.childNodes].filter((x) => x.nodeType === 3).map((x) => x.textContent).join('').trim();
    if (!own || own.length > 24) continue;
    if (own.indexOf('正在') === 0 || own.indexOf('停止生成') >= 0) n++;
  }
  return n;
}"""

# 读取对话末尾文本，用来判断卡在哪一步以及是否弹了反问问卷。
# 正文里出现过的最大页码。生成中这个数会一页页往上走，停住说明不再出页了。
_COUNT_PAGE_JS = r"""() => {
  let mx = 0;
  const t = document.body.innerText || '';
  const re = /第\s*(\d{1,3})\s*页/g;
  let m;
  while ((m = re.exec(t)) !== null) {
    const n = parseInt(m[1], 10);
    if (n > mx) mx = n;
  }
  return mx;
}"""

_TAIL_JS = """() => (document.body.innerText || '').trim().slice(-700)"""

# 反问问卷：屏幕上出现若干可选项时，取第一个（产品把推荐项排在首位）。
_QUESTION_JS = """() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const items = [];
  for (const e of document.querySelectorAll('[class*="option"], [role="option"], [role="menuitem"]')) {
    if (!vis(e)) continue;
    const t = (e.innerText || '').trim();
    if (t && t.length < 60) items.push(e);
  }
  return items.length;
}"""


def _find_page(browser):
    """找到承载对话的主页面。

    窗口里有多个页面（背景页、跨站支持页、启动器外壳）。
    只有启动器那一页是用户实际在看的对话界面。
    """
    for ctx in browser.contexts:
        for page in ctx.pages:
            if "launcher" in page.url:
                return page
    for ctx in browser.contexts:
        for page in ctx.pages:
            if "background" not in page.url and page.url:
                return page
    raise DeckError("找不到对话窗口")


def _new_chat(page) -> None:
    """开一个新会话。

    为什么每份都新开：会话是复用的，历史会一直堆下去——
    既让页面越来越慢，也让对方在同一段上下文里被前面几份的内容干扰。
    为什么用快捷键而不是点侧边栏：侧边栏是折叠的，元素坐标随窗口变化；
    快捷键与布局无关，稳得多。
    做不成也不报错：退化成在当前会话里继续做，功能仍然可用。
    """
    before = page.url
    try:
        page.keyboard.press("Control+Shift+K")
        # 靠 URL 变化确认真的换了会话，而不是盲等固定秒数
        for _ in range(16):
            time.sleep(0.5)
            if page.url != before:
                time.sleep(2.0)
                return
    except Exception:
        pass


def _click_ppt_mode(page) -> None:
    """挂上 PPT 模式标签。

    正常情况下输入「做一份XX的PPT」会自动进入该模式；
    但显式点一次按钮更稳，也能避免模型措辞变化导致没进模式。
    """
    try:
        page.evaluate(
            """() => {
              const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
              for (const b of document.querySelectorAll('button')) {
                if (vis(b) && (b.innerText || '').trim() === 'PPT 生成') { b.click(); return; }
              }
            }"""
        )
        time.sleep(1.5)
    except Exception:
        # 已经处于 PPT 模式时按钮可能不存在，属于正常情况
        pass


def _submit(page, text: str) -> None:
    """把主题写进输入框并提交。"""
    el = page.query_selector('div[contenteditable="true"]')
    if el is None:
        raise DeckError("对话窗口的输入区不可用")

    el.click()
    page.wait_for_timeout(300)
    # 清掉可能残留的草稿，否则会和本次主题拼在一起
    page.keyboard.press("Control+a")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(200)
    # 用 insert_text 而不是 type：中文逐字输入会触发输入法逻辑，极不可靠
    page.keyboard.insert_text(text)
    page.wait_for_timeout(600)
    page.keyboard.press("Enter")


def _probe(page, js: str, default: int = 0) -> int:
    """取一个计数探针。页面重绘期间的偶发失败不该终止整个流程。"""
    try:
        return int(page.evaluate(js))
    except Exception:
        return default


def _count_downloads(page) -> int:
    """当前画面上有几个下载入口。"""
    return _probe(page, _COUNT_DL_JS, -1)


def _wait_done(page, timeout: float, baseline: int, elapsed_base: int) -> None:
    """等到本轮生成完成。

    信号按可信度排序：
    1. 「共用时」标记比开工前多 —— 只在完成态出现，最精确，直接放行；
    2. 页码不再增长 + 无活动指示 + 下载入口已渲染 —— 兜底，防止文案改版后卡死。

    走过的弯路：早先只按「活动指示静默 40 秒」判定完成，结果一份要 10 页的活
    干到第 3 页就被导出，产物是残缺的。原因是底层跑 CLI 命令时会出现分钟级的
    静默空档（重试、截图），那期间活动指示本来就是 0。
    所以现在：静默必须同时满足「没有任何新页产出」，且要连续 4 分钟才算数。
    """
    started = time.time()
    answered = 0
    idle = 0
    last_page = -1
    while time.time() - started < timeout:
        page.wait_for_timeout(5000)
        card_ready = _count_downloads(page) > baseline
        elapsed = _probe(page, _COUNT_ELAPSED_JS, 0)

        # 精确信号：完成态才会出现的用时标记
        if card_ready and elapsed > elapsed_base:
            return

        busy = _probe(page, _COUNT_BUSY_JS, 1)
        page_no = _probe(page, _COUNT_PAGE_JS, 0)

        if busy == 0 and page_no <= last_page:
            idle += 1
            # 4 分钟毫无进展才认完成。阈值必须这么高：
            # CLI 重试/截图时的空档实测能到几分钟，短阈值会导出残次品。
            if card_ready and idle >= 48:
                return
        else:
            idle = 0
        if page_no > last_page:
            last_page = page_no

        # 问卷兜底：最多代答两轮，再多就说明需求给得不够，宁可让它继续也别乱点。
        # 这里刻意不做「静默即完成」的提前返回 —— 那正是导出残次品的入口。
        try:
            if answered < 2 and page.evaluate(_QUESTION_JS) > 0:
                tail = page.evaluate(_TAIL_JS)
                if "？" in tail[-200:] or "?" in tail[-200:]:
                    page.evaluate(
                        """() => {
                          const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                          for (const e of document.querySelectorAll('[class*="option"], [role="option"], [role="menuitem"]')) {
                            if (!vis(e)) continue;
                            const t = (e.innerText || '').trim();
                            if (t && t.length < 60) { e.click(); return; }
                          }
                        }"""
                    )
                    answered += 1
                    time.sleep(2.0)
        except Exception:
            continue

    raise DeckError("生成超时")


def _looks_like_pptx(path: Path) -> bool:
    """是不是一份像样的 pptx：够大，而且是 zip 容器。

    只比体积不够 —— 下载到半截的文件也有体积，但根本解不开。
    0 字节尤其要拦住：点得太早时服务端会先给一个空壳。
    """
    try:
        if path.stat().st_size < 5000:
            return False
        with path.open("rb") as fh:
            return fh.read(2) == b"PK"
    except OSError:
        return False


def _scroll_to_last_card(page) -> None:
    """把会话滚到底，让最新卡片回到视野。

    卡片在虚拟列表里，滚出去会被卸载，元素也就找不到了。
    """
    page.evaluate(
        """() => {
          for (const e of document.querySelectorAll('div')) {
            const s = getComputedStyle(e);
            if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && e.scrollHeight > e.clientHeight + 50) {
              e.scrollTop = e.scrollHeight;
            }
          }
        }"""
    )
    page.wait_for_timeout(1500)


def _grab_file(page, out_dir: Path, stem: str) -> Path:
    """点下载、选 PPTX、把文件落到本地。

    为什么要有重试：生成刚结束时产物未必立刻可用，实测遇到过点下去拿到
    0 字节空壳的情况（同一份文件稍后再点就是完整的 80KB）。旧的实现只试一次、
    失败就整轮白等十分钟，代价太大。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / (stem + ".pptx")
    last_err = "未开始"

    for attempt in range(3):
        _scroll_to_last_card(page)
        try:
            _click_download_pptx(page, dest)
        except Exception as exc:  # noqa: BLE001 — 单次点空不该让整轮作废
            last_err = f"{type(exc).__name__}: {exc}"
            page.wait_for_timeout(3000)
            continue

        if _looks_like_pptx(dest):
            return dest

        try:
            last_err = f"文件不可用({dest.stat().st_size} 字节)"
        except OSError:
            last_err = "文件没落地"
        page.wait_for_timeout(4000)

    raise DeckError("生成的文件不完整: " + last_err)


def _click_download_pptx(page, dest: Path) -> None:
    """点开下载菜单并选 PPTX，把文件落到 dest。

    下载入口是两级菜单，必须分两步点：先点下载图标，再在弹出菜单里选格式。
    """
    with page.expect_download(timeout=180000) as info:
        # 重试的意义：菜单是异步渲染的，偶发一次点空不该让整轮白等
        for _ in range(4):
            # 取最后一个 —— 本次产出的卡片排在会话末尾，前面的是历史产物。
            box = page.evaluate(
                """() => {
                  const all = [...document.querySelectorAll('span[aria-label="下载"]')];
                  if (!all.length) return null;
                  const e = all[all.length - 1];
                  e.scrollIntoView({block: 'center'});
                  const r = e.getBoundingClientRect();
                  return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
                }"""
            )
            if not box:
                continue
            # 必须走真实鼠标事件：这个下载图标是 Radix 的菜单触发器，靠 pointerdown 打开，
            # element.click() 只派发 click 事件，菜单根本不会弹出来（踩过这个坑）。
            # 也不能用 locator.click()/hover()：图标上有遮挡层，可操作性检查会直接超时。
            page.mouse.move(box[0], box[1])
            page.wait_for_timeout(300)
            page.mouse.down()
            page.wait_for_timeout(120)
            page.mouse.up()
            page.wait_for_timeout(1800)
            picked = page.evaluate(
                """() => {
                  const vis = (x) => { const r = x.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                  const cands = [...document.querySelectorAll('[role="menuitem"], [role="menu"] *')]
                    .filter((x) => vis(x) && (x.innerText || '').trim() === 'PPTX');
                  if (!cands.length) return false;
                  cands[cands.length - 1].click();
                  return true;
                }"""
            )
            if picked:
                break

    download = info.value
    # 用调用方给的安全名落盘，不用服务端的原文件名 ——
    # 它含全角冒号等字符，会让后续上传失败。
    download.save_as(str(dest))


def _generate_sync(prompt: str, out_dir: Path, stem: str) -> Path:
    """同步版的完整流程，由 make_deck 丢进线程池执行。"""
    _ensure_app()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://127.0.0.1:" + str(_DEBUG_PORT))
        try:
            page = _find_page(browser)
            _new_chat(page)
            _click_ppt_mode(page)
            # 两个基数都必须在提交之前取：提交之后新旧卡片会同时在 DOM 里
            baseline = max(0, _count_downloads(page))
            elapsed_base = _probe(page, _COUNT_ELAPSED_JS, 0)
            _submit(page, prompt)
            _wait_done(page, _GENERATE_TIMEOUT, baseline, elapsed_base)
            return _grab_file(page, out_dir, stem)
        finally:
            # 只断开连接，**不要关浏览器**：那是用户的桌面应用本身
            pass


# ----------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------


async def make_deck(brief: str, out_dir: Path, stem: str) -> Path:
    """生成一份 PPT 并返回落地路径。

    brief 应当是上层**加工过的完整制作需求**（理解 + 补全 + 润色之后的）。
    这里只加一句意图声明，正文原样透传 —— 不做裁剪、不改写：
    上游比这里更清楚对方要什么，中间再动一次手只会丢信息。

    全局串行：底层一次只能跑一份，并发请求在这里排队而不是互相踩。
    """
    brief = (brief or "").strip()
    if not brief:
        raise DeckError("没有给出制作需求")

    # 加一句意图声明：即使模式标签没挂上，光看文字也知道要做什么
    prompt = "帮我生成一份 PPT。\n\n" + brief

    async with _lock:
        return await asyncio.to_thread(_generate_sync, prompt, out_dir, stem)
