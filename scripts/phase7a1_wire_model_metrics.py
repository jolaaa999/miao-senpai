"""把 summarize_model_calls 接入 metrics/__init__/report（Phase 7A.1）。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"


def patch(path: Path, pairs: list[tuple[str, str]]) -> int:
    src = path.read_text(encoding="utf-8")
    applied = 0
    for old, new in pairs:
        if new in src and old not in src:
            continue  # 已应用
        if old in src:
            src = src.replace(old, new, 1)
            applied += 1
    path.write_text(src, encoding="utf-8")
    print(f"  {path.name}: {applied} 处, BOM={path.read_bytes()[:3] == b'\\xef\\xbb\\xbf'}")
    return applied


def main() -> int:
    # 1) metrics.py 的 __all__
    patch(
        PKG / "validation" / "metrics.py",
        [
            (
                '    "summarize_tools",',
                '    "summarize_tools",\n    "summarize_model_calls",',
            )
        ],
    )

    # 2) validation/__init__.py 的 import 与 __all__
    patch(
        PKG / "validation" / "__init__.py",
        [
            (
                "    summarize_queue,\n    summarize_send,",
                "    summarize_model_calls,\n    summarize_queue,\n    summarize_send,",
            ),
            (
                '    "summarize_tools",',
                '    "summarize_tools",\n    "summarize_model_calls",',
            ),
        ],
    )

    # 3) 报告脚本
    patch(
        ROOT / "scripts" / "validation_report.py",
        [
            (
                '    summary["send"] = m.summarize_send(rows)',
                '    summary["send"] = m.summarize_send(rows)\n'
                '    summary["metrics_model_calls"] = m.summarize_model_calls(rows)',
            ),
            (
                "    print(render(summary, window=args.window, real_samples=len(rows)))",
                "    print(render(summary, window=args.window, real_samples=len(rows),\n"
                "                 coverage=instrumentation_coverage(rows)))",
            ),
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
