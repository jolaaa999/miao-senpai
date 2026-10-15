"""高风险工具包的注册入口。

为什么单独一个模块、而不是塞进 `builtin.build_production_tools()`：

`tests/test_phase5_acceptance.py` 钉死了生产工具集合**恰好 8 个**，
且每个工具的 executor 必须与 `REAL_EXECUTORS` 里的实现同一。
这是「不破坏既有契约」的护栏，不该为了加新功能去改它。

所以新工具一律走**独立注册**（与 knowledge_tool / memory_tool 同款）：
既有的 8 个纹丝不动，新增能力各归各的 owner，
`registry.unregister_owner("high_risk_layer")` 一条命令就能整体摘掉。
"""

from __future__ import annotations

from typing import Any

from .exec_tools import build_exec_tools
from .file_tools import build_file_tools

__all__ = ["HIGH_RISK_OWNER", "build_high_risk_tools", "register_high_risk_tools"]

HIGH_RISK_OWNER = "high_risk_layer"


def build_high_risk_tools() -> list[Any]:
    """构造全部 7 个高风险工具定义（5 个文件类 + 2 个执行类）。"""
    return [*build_file_tools(), *build_exec_tools()]


def register_high_risk_tools(registry: Any) -> list[str]:
    """把高风险工具注册进注册表，返回实际注册的工具名。

    注册本身不代表可用：真正的门在 `permissions.HIGH_RISK_TOOLS` 那一层，
    非管理员调用时 `visible()` 会把这些工具从 prompt 里整个去掉。
    """
    names: list[str] = []
    for tool in build_high_risk_tools():
        registry.register(tool)
        names.append(tool.name)
    return names