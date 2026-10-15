"""插件运行时。

    from ..core.plugins import Plugin, PluginRuntime, PluginContext

区分「观察者」与「路由处理器」是本阶段的核心设计（见 models.py 说明）。
"""

from __future__ import annotations

from .models import Plugin, PluginContext, PluginSetup
from .runtime import PluginError, PluginRuntime

__all__ = ["Plugin", "PluginContext", "PluginSetup", "PluginRuntime", "PluginError"]
