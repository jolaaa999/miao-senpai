"""给电脑控制开/查/收一张限时租约（授权时间窗）。

为什么授权不走聊天消息
----------------------
「群里/私聊里说一句『你随便动吧』」不该等于把桌面交出去：
那句话可能是玩笑、可能是被别人诱导说的、也可能被模型误读成授权。
所以授权只能从命令行来——必须在电脑前、亲手敲。

用法（在仓库根目录）::

    python scripts\\computer-lease.py status          # 现在有没有授权、还剩多久
    python scripts\\computer-lease.py allow 10        # 授权 10 分钟
    python scripts\\computer-lease.py allow 30 --by 你的QQ号
    python scripts\\computer-lease.py revoke          # 立刻收回

到期自动失效，不需要手动收——这是刻意的：忘了收的授权不该永久有效。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许直接双击/命令行运行：把 src/plugins 加进 sys.path，复用插件包里的实现
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "plugins"))


def main(argv: list[str]) -> int:
    from dl_senpai.computer_control import (
        IS_WINDOWS,
        issue_lease,
        lease_remaining,
        read_lease,
        revoke_lease,
        status,
    )
    from dl_senpai.config import get_config

    cfg = get_config()
    action = (argv[0] if argv else "status").strip().lower()

    if action == "status":
        info = status(cfg)
        print("电脑控制状态：")
        for key, value in info.items():
            print(f"  {key}: {value}")
        data = read_lease(cfg)
        remain = lease_remaining(cfg)
        if remain > 0:
            print(f"  租约：有效，还剩 {int(remain)} 秒（{int(remain // 60)} 分钟）")
        else:
            print("  租约：无（控制类动作会被拒）")
        if data.get("revoked_by"):
            print(f"  最近一次回收：{data.get('revoked_by')}")
        return 0

    if action == "allow":
        minutes = 10.0
        actor = ""
        rest = argv[1:]
        i = 0
        while i < len(rest):
            token = rest[i]
            if token == "--by" and i + 1 < len(rest):
                actor = rest[i + 1]
                i += 2
                continue
            try:
                minutes = float(token)
            except ValueError:
                print(f"看不懂的分钟数：{token}")
                return 2
            i += 1
        if minutes <= 0:
            print("分钟数必须大于 0")
            return 2
        if minutes > 240:
            print("单次最多授权 240 分钟；要更久请分几次开。")
            return 2
        if not IS_WINDOWS:
            print("当前不是 Windows，开了也没用（GUI 工具不会注册）。")
            return 2
        issue_lease(cfg, minutes * 60, actor=actor)
        print(f"已授权 {minutes:.0f} 分钟，到期自动失效。")
        print("提醒：这只解开「租约」这道门，白名单/敏感词/限流照旧生效。")
        return 0

    if action == "revoke":
        revoke_lease(cfg, actor="cli")
        print("已收回授权。")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
