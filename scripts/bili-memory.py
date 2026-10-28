"""看学姐在B站记住了什么、顺手看最近的B站日记。

用法（仓库根目录）::

    python scripts\bili-memory.py            # 最近 10 条记忆 + 最近 5 条B站日记
    python scripts\bili-memory.py 买车        # 按主题搜
    python scripts\bili-memory.py --diary     # 只看日记

她自己在对话里也能查到这些（<<<BILIMEMO>>>），这个脚本是给主人直接翻的。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "plugins"))


def _diary_rows(cfg, limit: int = 5) -> list[dict]:
    """从日记本里挑出跟B站有关的条目：她自己写的，人也能直接看。"""
    try:
        from dl_senpai.diary import get_diary_store

        store = get_diary_store(getattr(cfg, "diary_dir", None) or None)
        rows = store.recent(limit) if hasattr(store, "recent") else []
    except Exception as exc:  # noqa: BLE001
        print(f"（日记读不出来：{type(exc).__name__}: {exc}）")
        return []
    out = []
    for entry in rows:
        tags = getattr(entry, "tags", None) or []
        title = getattr(entry, "title", "") or ""
        if "B站" in tags or "刷到" in title:
            out.append(
                {
                    "day": str(getattr(entry, "day", "")),
                    "title": title,
                    "text": str(getattr(entry, "text", ""))[:80],
                }
            )
    return out[:limit]


def main(argv: list[str]) -> int:
    from dl_senpai import bili_memory as bm
    from dl_senpai.config import get_config

    cfg = get_config()
    diary_only = "--diary" in argv
    args = [a for a in argv if not a.startswith("--")]
    query = args[0] if args else ""

    if not diary_only:
        rows = bm.search(cfg, query, limit=10)
        print(bm.render(rows, limit=10))
        print()

    rows = _diary_rows(cfg)
    if rows:
        print("最近的B站日记：")
        for row in rows:
            print(f"  {row['day']} {row['title']}")
            print(f"    {row['text']}")
    elif diary_only:
        print("最近没有B站相关的日记。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
