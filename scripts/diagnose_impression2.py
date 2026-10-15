"""精确诊断 impression 解析。"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "plugins"))
os.environ.setdefault("DL_SENPAI_TEST_MODE", "1")


def main() -> int:
    from dl_senpai.group_card import loads_marker_json
    from dl_senpai.person_memory import _IMPRESSION_MARKER_RE, _normalize_trait_list

    payload = '{"add":["x"],"remove":[],"notes":null}'
    text = f"<<<IMPRESSION {payload}>>>"
    print("text:", text)
    matches = list(_IMPRESSION_MARKER_RE.finditer(text))
    print("regex matches:", len(matches))
    if matches:
        group = matches[0].group(1)
        print("group1:", repr(group))
        data = loads_marker_json(group)
        print("loads_marker_json ->", data)
        if data:
            print("add raw:", data.get("add"))
            print("normalized add:", _normalize_trait_list(data.get("add") or []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
