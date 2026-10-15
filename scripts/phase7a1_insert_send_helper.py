"""向 reply_send.py 插入 _instrument_send helper（Phase 7A.1）。

用独立脚本而不是 PowerShell 内联，避免嵌套引号问题。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "plugins" / "dl_senpai" / "reply_send.py"

HELPER = '''def _instrument_send(
    *,
    text: str = "",
    success: bool = True,
    media_present: bool = False,
    media_failure: bool = False,
    fallback: bool = False,
    duration_ms: float = 0.0,
) -> None:
    """记录一次发送（Phase 7A.1 第三十六/三十八节）。

    **统一边界**：只有 reply_send 记，legacy / runtime / 其它调用方
    都不得各自再记一次，否则一次发送被算多遍（第三十七条）。

    第三十九节：一次 Agent response 可能因为多气泡/碎句触发多次
    QQ send，因此这里每次调用记一次 send operation；response
    与 send operation 的区分由 telemetry 侧表达。

    失败静默：观测绝不能影响发送。
    """
    try:
        from .validation.telemetry import record_send

        record_send(
            success=success,
            operations=1,
            bubbles=1,
            media_failure=media_failure,
            fallback=fallback,
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001 — 观测失败不影响发送
        pass


'''


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")

    anchor = "def _segment_file_hint(segment: MessageSegment) -> str:"
    if "_instrument_send" in src:
        print("已存在 _instrument_send，跳过插入")
    elif anchor not in src:
        print("FAIL: 找不到锚点")
        return 1
    else:
        # 修复上一轮被破坏的缩进：把 anchor 行恢复为顶格
        src = src.replace(f"    {anchor}", anchor)
        src = src.replace(anchor, HELPER + anchor, 1)
        print("已插入 _instrument_send")

    # 确保有 import time
    if "\nimport time" not in src.split("def ")[0]:
        src = src.replace(
            "from __future__ import annotations",
            "from __future__ import annotations\n\nimport time",
            1,
        )
        print("已添加 import time")

    TARGET.write_text(src, encoding="utf-8")
    raw = TARGET.read_bytes()
    print("BOM:", raw[:3] == b"\xef\xbb\xbf")
    print("行数:", len(src.splitlines()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
