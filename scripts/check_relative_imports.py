"""扫描 dl_senpai 包内**过深的相对 import**（Phase 7B 缺陷排查）。

缺陷现象：
    运行时报 `ModuleNotFoundError: No module named 'src.plugins.llm'`

原因：
    相对 import 的点数超过文件所在深度。例如
        src/plugins/dl_senpai/agent/model_gateway.py    (包: ...dl_senpai.agent)
        from ...llm import get_llm      ← 三个点 → src.plugins.llm  (不存在)
        from ..llm import get_llm       ← 两个点 → src.plugins.dl_senpai.llm  (正确)

本脚本按"文件相对包根的深度"检查每个相对 import 的合法上界。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"


def package_depth(path: Path) -> int:
    """该文件所在包的深度（相对 dl_senpai）。

    dl_senpai/foo.py            → 0  （包是 dl_senpai 本身）
    dl_senpai/agent/foo.py      → 1  （包是 dl_senpai.agent）
    dl_senpai/agent/tools/a.py  → 2
    """
    rel = path.relative_to(PKG)
    return len(rel.parts) - 1


def module_exists(spec: str) -> bool:
    """按包内路径判断模块是否存在（只做文件名级别的判断）。"""
    parts = spec.split(".")
    base = PKG
    for i, part in enumerate(parts):
        candidate = base / part
        if candidate.is_dir():
            base = candidate
            continue
        if (base / f"{part}.py").exists():
            # 中途命中文件：还有剩余部分说明不合法
            return i == len(parts) - 1
        return False
    return (base / "__init__.py").exists()


def main() -> int:
    problems: list[str] = []

    for path in sorted(PKG.rglob("*.py")):
        depth = package_depth(path)
        rel = path.relative_to(PKG)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            # level 表示"上溯几层"：1 = 当前包
            ascends = node.level - 1
            if ascends > depth:
                target = "." * node.level + (node.module or "")
                problems.append(
                    f"{rel}:{node.lineno}  level={node.level}  from {target}  "
                    f"（文件深度={depth}，最多只能上溯 {depth} 层）"
                )
                continue

            is_pkg = path.name == "__init__.py"
            # 若上溯到包根且模块名存在，做存在性校验
            if ascends == depth and node.module and not is_pkg:
                if not module_exists(node.module):
                    problems.append(
                        f"{rel}:{node.lineno}  from {'.' * node.level}{node.module}  "
                        f"（该模块在包内不存在）"
                    )

    print(f"扫描 {len(list(PKG.rglob('*.py')))} 个文件")
    if problems:
        print(f"\n发现 {len(problems)} 处可疑相对 import：")
        for p in problems:
            print(f"  {p}")
    else:
        print("\n未发现过深的相对 import")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
