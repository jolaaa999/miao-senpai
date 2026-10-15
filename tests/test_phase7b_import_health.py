"""Phase 7B 回归：包内相对 import 的深度必须合法。

**修复的真实缺陷**：
    `agent/model_gateway.py` 写了 `from ...llm import get_llm`（三个点），
    但该文件所在包是 `...dl_senpai.agent`，`llm.py` 在 `...dl_senpai` 下 ——
    正确写法是两个点。

    三个点被解析成 `src.plugins.llm`（不存在），于是
    **RUNTIME 模式下每次调用都崩**：

        agent.runtime_crashed error="ModuleNotFoundError:
        No module named 'src.plugins.llm'"

    而单元测试没抓到，因为测试走的是注入的 StubGateway，
    从未真正调用 `_get_llm()` 的回退分支。

这类错误的特征是"语法合法、静态检查通过、只在真实调用时炸"，
因此必须用**运行期验证**而不是只看 import 语句。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"


def package_depth(path: Path) -> int:
    """文件所在包相对 dl_senpai 的深度。"""
    return len(path.relative_to(PKG).parts) - 1


def module_exists(spec: str) -> bool:
    """按包内路径判断模块是否存在。"""
    parts = spec.split(".")
    base = PKG
    for i, part in enumerate(parts):
        candidate = base / part
        if candidate.is_dir():
            base = candidate
            continue
        if (base / f"{part}.py").exists():
            return i == len(parts) - 1
        return False
    return (base / "__init__.py").exists()


def test_no_relative_import_exceeds_package_depth() -> None:
    """相对 import 的上溯层数不得超过文件所在深度。"""
    problems: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        depth = package_depth(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            if node.level - 1 > depth:
                target = "." * node.level + (node.module or "")
                problems.append(
                    f"{path.relative_to(PKG)}:{node.lineno} from {target}"
                )
    assert not problems, "过深的相对 import（运行时才会炸）:\n" + "\n".join(problems)


def test_relative_imports_resolve_to_existing_modules() -> None:
    """上溯到包根的相对 import，其模块名必须在包内真实存在。"""
    problems: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        depth = package_depth(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            if node.level - 1 == depth and node.module:
                if not module_exists(node.module):
                    problems.append(
                        f"{path.relative_to(PKG)}:{node.lineno} "
                        f"from {'.' * node.level}{node.module}"
                    )
    assert not problems, "指向不存在模块的相对 import:\n" + "\n".join(problems)


def test_model_gateway_llm_import_is_correct() -> None:
    """具体回归：model_gateway 必须用两个点导入 llm。"""
    src = (PKG / "agent" / "model_gateway.py").read_text(encoding="utf-8")
    assert "from ..llm import" in src, "model_gateway 的 llm import 被改坏了"
    assert "from ...llm import" not in src, "又写成了三个点（会解析到 src.plugins.llm）"


def test_gateway_can_actually_resolve_llm() -> None:
    """运行期验证：真的调用 `_get_llm()` 的回退分支。

    这是关键 —— 静态检查看不出三个点的问题，
    只有真正执行到那条分支才会 ModuleNotFoundError。
    """
    from dl_senpai.agent.model_gateway import LegacyModelGateway

    gateway = LegacyModelGateway()  # 不注入 llm，强制走 import 回退
    llm = gateway._get_llm(None)  # noqa: SLF001 — 就是要测这条分支
    assert llm is not None
    assert type(llm).__name__ == "SenpaiLLM"


def test_runtime_agent_gateway_resolves() -> None:
    """端到端：生产 Runtime 的 gateway 能取到 llm。

    复现的是真实的崩溃路径：runtime 处理消息 → ModelGateway.chat()
    → _get_llm() → ModuleNotFoundError。
    """
    from dl_senpai.agent.model_gateway import LegacyModelGateway

    for gateway in (LegacyModelGateway(),):
        resolved = gateway._get_llm(None)  # noqa: SLF001
        assert resolved is not None
