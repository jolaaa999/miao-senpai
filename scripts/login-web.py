"""手动登录一次，把 cookie 存下来（发说说 / 黑盒评论要用）。

为什么要手动
------------
扫码、滑块、短信验证这些没法自动化，也不该自动化。这个脚本只做一件事：
开一个**有头**浏览器，你在窗口里正常登录，登录完回终端按回车，
它把 cookie 存到 data/dl_senpai/web_login/<站点>.json，之后机器人复用这份
登录态去做写操作。脚本不接收、不保存任何密码。

用法（在仓库根目录）::

    python scripts\\login-web.py qzone        # QQ空间（发说说用）
    python scripts\\login-web.py heybox       # 小黑盒（评论用）
    python scripts\\login-web.py status       # 看现在登的是哪个号

注意
----
1. **必须用小号**。存完会解析出登录账号，如果是主人自己的号（DL_SENPAI_OWNER_IDS
   里那个），脚本拒收并删掉这份登录态。
2. 登录态文件等同于密码，别提交、别外传。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 允许命令行直接运行：复用插件包里的实现
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "plugins"))


async def _run(site: str) -> int:
    from dl_senpai.config import get_config
    from dl_senpai.web_login import (
        SITES,
        WebLoginError,
        describe_login,
        save_login,
    )

    cfg = get_config()

    if site == "status":
        print("登录态：")
        for name in SITES:
            info = describe_login(name, cfg)
            if info.ok:
                print(f"  {info.label}：已登录（账号 {info.uin}）")
            else:
                print(f"  {info.label}：没登录（跑 python scripts\\login-web.py {name}）")
        return 0

    if site not in SITES:
        print(f"不认识的站点：{site}（支持：{', '.join(SITES)}、status）")
        return 2

    try:
        path = await save_login(site, cfg)
    except WebLoginError as exc:
        print(f"失败：{exc}")
        return 1
    print(f"完成：{path}")
    print("以后想换号，重新跑一次这个命令就行（会覆盖）。")
    return 0


def main(argv: list[str]) -> int:
    site = (argv[0] if argv else "status").strip().lower()
    return asyncio.run(_run(site))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
