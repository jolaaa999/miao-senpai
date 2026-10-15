"""Phase 6.6 任务（十五/五十九/六十）：编码与语法验证。

第五十九/六十节要求：
    - 所有 Python 文件 UTF-8 无 BOM
    - 运行 compileall 或等价语法扫描
    - 若用自动脚本重写 import 必须限定文件 + 检查 diff + compile + pytest

Phase 6.5 曾出现畸形相对 import 与 BOM，本脚本用于回归防护。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = [ROOT / "src", ROOT / "tests", ROOT / "scripts"]

BOM = b"\xef\xbb\xbf"


def check_bom(paths: list[Path]) -> list[str]:
    bad: list[str] = []
    for path in paths:
        raw = path.read_bytes()
        if raw.startswith(BOM):
            bad.append(str(path.relative_to(ROOT)))
    return bad


def check_syntax(paths: list[Path]) -> list[tuple[str, str]]:
    bad: list[tuple[str, str]] = []
    for path in paths:
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            bad.append((str(path.relative_to(ROOT)), f"{exc.msg} (line {exc.lineno})"))
        except UnicodeDecodeError as exc:
            bad.append((str(path.relative_to(ROOT)), f"编码错误: {exc}"))
    return bad


def check_malformed_relative_imports(paths: list[Path]) -> list[tuple[str, int, str]]:
    """检测 Phase 6.5 出现过的畸形相对 import。

    症状：包内模块写成 `from group_card import ...`（丢了前导点），
    运行时才报 ModuleNotFoundError。

    判定：文件位于包内（同级有 __init__.py），却 import 了一个
    与**同级模块同名**的顶层模块。
    """
    bad: list[tuple[str, int, str]] = []
    for path in paths:
        if not path.name.endswith(".py"):
            continue
        # 只检查包内文件
        if not (path.parent / "__init__.py").exists():
            continue
        siblings = {
            p.stem for p in path.parent.glob("*.py") if p.name != "__init__.py"
        }
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                mod = node.module or ""
                top = mod.split(".")[0]
                if top in siblings:
                    bad.append(
                        (
                            str(path.relative_to(ROOT)),
                            node.lineno,
                            f"from {mod} import ... （疑似丢了前导点）",
                        )
                    )
    return bad


def main() -> int:
    paths: list[Path] = []
    for d in SCAN_DIRS:
        if d.exists():
            paths.extend(p for p in d.rglob("*.py") if "__pycache__" not in p.parts)

    print(f"扫描 {len(paths)} 个 Python 文件")
    print("=" * 74)

    bom = check_bom(paths)
    print(f"\n[1] BOM 检查: {'OK 无 BOM' if not bom else f'{len(bom)} 个文件含 BOM'}")
    for p in bom:
        print(f"    {p}")

    syntax = check_syntax(paths)
    print(f"\n[2] 语法检查: {'OK 全部可解析' if not syntax else f'{len(syntax)} 个失败'}")
    for p, msg in syntax:
        print(f"    {p}: {msg}")

    malformed = check_malformed_relative_imports(paths)
    print(
        f"\n[3] 畸形相对 import: "
        f"{'OK 未发现' if not malformed else f'{len(malformed)} 处'}"
    )
    for p, ln, msg in malformed:
        print(f"    {p}:{ln} {msg}")

    ok = not bom and not syntax and not malformed
    print("\n" + "=" * 74)
    print("结论:", "全部通过" if ok else "存在问题，见上文")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
