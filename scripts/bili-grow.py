"""跑一遍 B 站养号功课（登录 + 看视频 + 分享），打印经验和进度。

用法（仓库根目录）::

    python scripts\bili-grow.py            # 跑一次
    python scripts\bili-grow.py --check    # 只看今天任务做没做、离 Lv2 还差多少

注意：B站协议不允许自动化操作账号。这个脚本只做"页面自己也会发的那几个
请求"，参数压得很保守（一次两个视频），但它仍然属于灰色地带，风险自负。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "plugins"))


async def _run(check_only: bool) -> int:
    from dl_senpai import bili as bl
    from dl_senpai.bili_grow import daily_ritual, _API, _headers
    from dl_senpai.config import get_config

    cfg = get_config()
    if check_only:
        import httpx

        cookies = bl._state_cookies(cfg)
        async with httpx.AsyncClient(headers=_headers(), cookies=cookies, timeout=25.0) as c:
            who = await bl.bili_whoami(cfg=cfg)
            reward = ((await c.get(f"{_API}/x/member/web/exp/reward")).json() or {}).get("data") or {}
            nav = (await c.get(f"{_API}/x/web-interface/nav")).json() or {}
            info = ((nav.get("data") or {}).get("level_info")) or {}
        exp = int(info.get("current_exp") or 0)
        need = int(info.get("next_exp") or 0)
        print(f"账号：{who['uname']}（Lv{who['level']}）")
        print(f"经验：{exp} / {need}（离下一级还差 {max(0, need - exp)}）")
        print(f"今日任务：登录={reward.get('login')} 观看={reward.get('watch')} 分享={reward.get('share')}")
        return 0

    record = await daily_ritual(cfg)
    print(f"账号：{record.get('uname')}（Lv{record.get('level_before')} -> Lv{record.get('level_after')}）")
    print(f"经验：{record.get('exp_before')} -> {record.get('exp_after')}（+{record.get('exp_gain')}）")
    if record.get("skipped"):
        print(f"跳过：{record['skipped']}（今天的功课已经做过了）")
    for step in record.get("steps") or []:
        print(f"  {step}")
    return 0


def main() -> int:
    check_only = "--check" in sys.argv
    return asyncio.run(_run(check_only))


if __name__ == "__main__":
    raise SystemExit(main())
