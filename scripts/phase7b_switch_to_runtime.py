"""把 .env 的 DL_SENPAI_AGENT_MODE 从 shadow 改为 runtime。

保留 DL_SENPAI_VALIDATION=1：
    虽然不再做新旧对比，但 Runtime 自身的延迟 / token / 工具成败 /
    发送统计仍然有价值 —— 这些是生产运行的健康度指标。
    （SHADOW 专属的 compare 部分会自动变成空，不影响其它字段。）
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"

OLD = "DL_SENPAI_AGENT_MODE=shadow"
NEW = "DL_SENPAI_AGENT_MODE=runtime"


def main() -> int:
    src = ENV.read_text(encoding="utf-8")

    if NEW in src:
        print("已是 runtime，无需修改")
        return 0
    if OLD not in src:
        print(f"FAIL: 找不到 {OLD}")
        return 1

    src = src.replace(OLD, NEW, 1)
    ENV.write_text(src, encoding="utf-8")

    print("已切换:", OLD, "->", NEW)
    print("BOM:", ENV.read_bytes()[:3] == b"\xef\xbb\xbf")

    # 回显相关三行
    for line in src.splitlines():
        if line.startswith("DL_SENPAI_AGENT_MODE") or line.startswith("DL_SENPAI_VALIDATION"):
            print("  ", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
