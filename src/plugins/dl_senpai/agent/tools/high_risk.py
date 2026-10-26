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

from .computer_tools import build_computer_tools
from .gallery_tool import build_gallery_tools
from .link_tools import build_link_tools
from .social_tools import build_web_extra_tools
from .write_tools import build_write_tools
from .exec_tools import build_exec_tools
from .file_tools import build_file_tools

__all__ = [
    "COMPUTER_OWNER",
    "HIGH_RISK_OWNER",
    "build_high_risk_tools",
    "register_extra_layers",
    "register_high_risk_tools",
]

HIGH_RISK_OWNER = "high_risk_layer"
# 电脑类工具单独一个 owner：出问题时一条 unregister_owner 就能整体摘掉，
# 不用动文件/命令那两层。
COMPUTER_OWNER = "computer_layer"


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


def register_extra_layers(registry: Any) -> list[str]:
    """注册「非高风险但也需要开关控制」的几层：电脑 / 相册 / 读链接。

    为什么跟高风险分开，而不是塞进 register_high_risk_tools：
    那个函数的语义是「管理员专属工具」，它有快照测试盯着数量（7 个）。
    把低风险工具混进去，既让语义失真，也会让「加了什么工具」变得看不出来。
    单独一层的好处是：每一层一个 owner，出问题一条
    unregister_owner 就能整体摘掉，互不影响。
    """
    names: list[str] = []
    # 电脑类工具：computer_enable=false 或非 Windows 时 build 返回空列表，
    # 这里就什么都不注册——「默认关」是这一层最硬的那道门。
    for tool in build_computer_tools():
        registry.register(tool)
        names.append(tool.name)
    # 相册：低风险但默认关，可见性由 gallery_enable 控制。
    for tool in build_gallery_tools():
        registry.register(tool)
        names.append(tool.name)
    # 读链接：只读、默认开，可见性由 link_read_enable 控制。
    for tool in build_link_tools():
        registry.register(tool)
        names.append(tool.name)
    # B站 / 小黑盒：只读上网，同样按各自的 enable 决定注册与否。
    for tool in build_web_extra_tools():
        registry.register(tool)
        names.append(tool.name)
    # 写入层（发说说 / 黑盒评论）：默认关，且工具名在 HIGH_RISK_TOOLS 里，
    # 非管理员既看不到也执行不了。
    for tool in build_write_tools():
        registry.register(tool)
        names.append(tool.name)
    return names