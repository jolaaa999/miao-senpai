"""打开一个有头浏览器，把站点的 cookie 罐（含验证结果）存下来。

用在哪
------
小黑盒的帖子接口对陌生访客会直接回 {"status": "show_captcha"}，页面只剩页头页脚。
这种情况**不需要账号**，只需要"同一份 cookie 罐里带着过了验证的凭据"。

所以这个脚本不登录、不输密码：
    1. 打开目标帖子（默认拿最近一次搜到的第一篇，也可以自己给 URL）
    2. 如果弹出滑块/点选，你在窗口里过掉它
    3. 回终端按回车 -> cookie 落盘到 data/dl_senpai/web_login/heybox.json
    4. 之后机器人读帖/搜帖都会带上这份 cookie 罐

用法（仓库根目录）::

    python scripts\\warm-cookies.py heybox
    python scripts\\warm-cookies.py heybox https://www.xiaoheihe.cn/app/bbs/link/xxxx

过期了（又要求验证）就再跑一次。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "plugins"))


async def _run(argv: list[str]) -> int:
    from dl_senpai.config import get_config
    from dl_senpai.web_login import SITES, WebLoginError, save_login, state_path

    site = (argv[0] if argv else "heybox").strip().lower()
    if site not in SITES:
        print(f"不认识的站点：{site}（支持：{', '.join(SITES)}）")
        return 2
    url = argv[1].strip() if len(argv) > 1 else "https://www.xiaoheihe.cn/"

    cfg = get_config()
    try:
        path = await save_login(site, cfg, start_url=url, require_uin=False, timeout_sec=600)
    except WebLoginError as exc:
        print(f"失败：{exc}")
        return 1
    print(f"cookie 罐已保存：{path}")
    print(f"（账号：{'已登录' if 'uin' else '匿名'}）" if path.exists() else "")
    print("再跑一次 python scripts\\login-web.py status 可以确认。")
    return 0


def main(argv: list[str]) -> int:
    return asyncio.run(_run(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
