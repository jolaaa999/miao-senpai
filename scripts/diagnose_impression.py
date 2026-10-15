"""诊断 impression marker 解析边界。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "plugins"))
os.environ.setdefault("DL_SENPAI_TEST_MODE", "1")


def main() -> int:
    from dl_senpai.person_memory import parse_impression_updates

    payload = '{"add":["x"],"remove":[],"notes":null}'
    for closer in (">>>", ">>", "＞＞＞", "】】", "】"):
        text = f"<<<IMPRESSION {payload}{closer}"
        parsed = parse_impression_updates(text)
        print(f"  closer={closer!r:12} updates={len(parsed.updates)} text={parsed.text!r}")

    print()
    # 带前缀文本
    text2 = "记住了\n" + f"<<<IMPRESSION {payload}>>>"
    parsed2 = parse_impression_updates(text2)
    print(f"  with prefix: updates={len(parsed2.updates)} text={parsed2.text!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
