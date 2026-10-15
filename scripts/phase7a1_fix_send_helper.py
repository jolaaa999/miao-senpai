"""修正 reply_send.py 缺失的 _instrument_send 定义（Phase 7A.1）。

上一次插入脚本因为锚点行被之前的编辑破坏而静默失败。
这次直接检查并强制插入。
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

    **统一边界**：只有 reply_send 记。legacy / runtime / 其它调用方
    都不得各自再记一次，否则一次发送被算多遍（第三十七条）。

    第三十九节：一次 Agent response 可能因多气泡/碎句触发多次
    QQ send，因此这里每次调用记一次 send operation；response 与
    send operation 的区分由 telemetry 侧表达。

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

    if "def _instrument_send(" in src:
        print("已定义，无需插入")
    else:
        # 插到第一个模块级 async def 之前（保证在调用点之前定义）
        anchor = "async def _send_image_with_retry("
        if anchor not in src:
            print("FAIL: 找不到锚点")
            return 1
        src = src.replace(anchor, HELPER + anchor, 1)
        print("已插入 _instrument_send 定义")

    # 确保 import time 在文件头部
    head = src.split("def ", 1)[0]
    if "\nimport time" not in head:
        src = src.replace(
            "from __future__ import annotations",
            "from __future__ import annotations\n\nimport time",
            1,
        )
        print("已添加 import time")

    TARGET.write_text(src, encoding="utf-8")
    print("BOM:", TARGET.read_bytes()[:3] == b"\xef\xbb\xbf")
    print("定义数:", src.count("def _instrument_send("))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
