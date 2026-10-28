"""看学姐在小黑盒干了什么：每次刷帖的记录 + 按天小结。

用法（仓库根目录）::

    python scripts\\heybox-log.py            # 最近 15 条
    python scripts\\heybox-log.py 40         # 最近 40 条
    python scripts\\heybox-log.py --day      # 只看按天汇总

原始台账在 data/dl_senpai/heybox_browse_log.jsonl（一行一次），
这个脚本只是把它翻译成人看得懂的几列。
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_rows() -> list[dict]:
    path = ROOT / "data" / "dl_senpai" / "heybox_browse_log.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _verdict(row: dict) -> str:
    """一行记录最终落到哪个结果。台账里的字段就这几种组合。"""
    if row.get("commented"):
        extra = "（未回读确认）" if row.get("comment_unverified") else ""
        return f"评论：{row['commented']}{extra}"
    if row.get("error"):
        return f"失败：{row['error'][:60]}"
    skipped = row.get("skipped") or ""
    labels = {
        "dice": "骰子没过（没到说话那步）",
        "no_interest": "十二篇都不想看",
        "nothing_to_say": "看完了，但没写出话来",
        "empty_feed": "首页没抓到帖子",
        "empty_text": "正文太短/没抓到",
        "quiet_hours": "静默时段",
        "browse_disabled": "总开关关着",
    }
    if skipped.startswith("guard:"):
        return f"被节奏护栏挡下：{skipped[6:]}"
    return labels.get(skipped, skipped or "（无结果）")


def _print_rows(rows: list[dict], limit: int) -> None:
    for row in rows[-limit:]:
        when = str(row.get("at") or "")
        title = str(row.get("title") or "-")[:34]
        read = row.get("read_chars")
        read_txt = f"读{read}字" if read else ""
        print(f"{when}  {title:<34} {read_txt:<9} {_verdict(row)}")


def _print_days(rows: list[dict]) -> None:
    stat: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        day = str(row.get("at") or "")[:10]
        stat[day]["tick"] += 1
        if row.get("commented"):
            stat[day]["comment"] += 1
        elif row.get("skipped") == "dice":
            stat[day]["dice"] += 1
        elif row.get("skipped") == "no_interest":
            stat[day]["no_interest"] += 1
        elif row.get("skipped"):
            stat[day]["other"] += 1
        if row.get("error"):
            stat[day]["error"] += 1
    print(f"{'日期':<12}{'刷了':>4}{'评论':>4}{'骰子没过':>8}{'没兴趣':>7}{'出错':>5}")
    for day in sorted(stat):
        s = stat[day]
        print(f"{day:<12}{s['tick']:>4}{s['comment']:>4}{s['dice']:>9}{s['no_interest']:>8}{s['error']:>6}")


def main(argv: list[str]) -> int:
    rows = _read_rows()
    if not rows:
        print("还没有台账：她一次都没刷过。")
        return 0
    day_only = "--day" in argv
    nums = [a for a in argv if a.isdigit()]
    limit = int(nums[0]) if nums else 15
    if not day_only:
        _print_rows(rows, limit)
        print()
    _print_days(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
