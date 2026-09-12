#!/usr/bin/env python3
"""本机 QQ RPA：按名单依次打开资料卡并点加好友（默认不校验是否成功）。

用法：
  python scripts/qq_friend_add_rpa.py --calibrate
  python scripts/qq_friend_add_rpa.py --reset-progress --max 20
  # 若要恢复校验模式：
  python scripts/qq_friend_add_rpa.py --require-verify --max 5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

from dl_senpai.friend_add_rpa import (  # noqa: E402
    RpaConfig,
    run_batch,
    run_calibrate,
)


def main() -> int:
    p = argparse.ArgumentParser(description="QQ 客户端 RPA 加好友（依次点完即过）")
    p.add_argument("--calibrate", action="store_true", help="采集坐标+按钮截图模板")
    p.add_argument(
        "--mode",
        default="deeplink",
        choices=("deeplink", "profile", "addcontact", "legacy"),
    )
    p.add_argument("--verify-msg", default="我是群里的学姐~")
    p.add_argument("--delay-min", type=float, default=8.0)
    p.add_argument("--delay-max", type=float, default=18.0)
    p.add_argument("--open-wait", type=float, default=3.5)
    p.add_argument("--max", type=int, default=0)
    p.add_argument("--list", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--reset-progress", action="store_true", help="清空 done/failed 后开跑")
    p.add_argument(
        "--require-verify",
        action="store_true",
        help="开启校验（资料卡不再显示加好友才算成功）",
    )
    p.add_argument("--confidence", type=float, default=0.82, help="找图置信度 0~1")
    args = p.parse_args()

    if args.calibrate:
        run_calibrate()
        return 0

    if args.reset_progress:
        from dl_senpai.friend_add_rpa import _PROGRESS_PATH, save_progress

        save_progress([], [])
        print(f"已清空进度: {_PROGRESS_PATH}")

    cfg = RpaConfig(
        mode=args.mode,
        verify_msg=args.verify_msg,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        open_wait=args.open_wait,
        dry_run=args.dry_run,
        max_count=args.max,
        list_path=args.list,
        locate_confidence=args.confidence,
        require_verified=bool(args.require_verify),
    )
    run_batch(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
