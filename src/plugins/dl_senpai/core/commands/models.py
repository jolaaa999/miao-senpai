"""命令定义模型。

一个 Command 是「一段可匹配的文本模式 + 一个处理器 + 一组执行约束」。

设计要点：
1. **正则来自各业务模块的现有常量**，注册时引用而非复制——保证行为等价。
2. `enabled` 与 `owner` 让 Plugin Runtime 能整体启停/注销。
3. `consume` 默认 True：命令命中后不应再进入 AI（这是当前 matcher
   `block=True` 的语义）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Pattern, Sequence


@dataclass(frozen=True)
class CommandMatch:
    """一次匹配结果。"""

    name: str
    namespace: str
    # 命中的子命令标识（例如 checkin 的 status / leaderboard_points）
    action: str
    # 从正则里捕获的剩余参数（例如商店的「购买 xxx」→ "xxx"）
    args: tuple[str, ...] = ()
    # 原始待匹配文本
    text: str = ""

    def arg(self, index: int = 0, default: str = "") -> str:
        if 0 <= index < len(self.args):
            return self.args[index]
        return default


# 流程钩子签名：(context) -> bool；返回 False 表示本命令不适用
class CommandPermissionDenied(Exception):
    """命令命中了，但这个发言者没有权限。

    为什么用异常，而不是让 handler 返回 False 或回一句「你没权限」：

    - 命令确实命中了（这条输入就是给指令用的），只是这个人不许用；
    - Router 需要**静默跳过且不 consume**——回一句等于向全群复述谁有权限，
      consume 了又等于把消息吃掉，AI 连正常接话都做不到；
    - 它也不能按「代码报错」处理，那会打 warning，真正的异常会被淹掉。

    所以给一个专用类型，让 Router 一眼分得清「没权限 / 正常 / 出 bug」三种情况。
    定义在 models 而不是 admin_commands：reset 与 persona 也要用它，
    不该为了一个异常去依赖「管理员指令」那个模块。
    """


CommandGuard = Callable[[Any], bool]
# 处理器签名：(context, match) -> None（是否回复由内部决定）
CommandHandler = Callable[[Any, CommandMatch], Any] | Callable[[Any, CommandMatch], Awaitable[Any]]


@dataclass
class Command:
    """一条命令注册项。

    Attributes:
        name: 命令唯一名（如 `checkin`、`shop`）。
        namespace: 归属命名空间（用于按域分组与批量启停）。
        patterns: 匹配模式列表。元素可以是
            - `(action, compiled_pattern)`：标准写法，正则直接复用业务模块常量；
            - `compiled_pattern`：简写，action 取命令名。
        handler: 命中后的处理器。
        description: 人类可读说明（供 `list()` 输出与后续帮助系统使用）。
        priority: 同 namespace 内排序，数值小者先匹配。
        permission: 权限标识（Phase 3 只做登记，不做强制校验）。
        consume: 命中后是否短路后续 stage。
        enabled: 是否启用。
        owner: 归属者（插件 id），用于批量注销。
        guard: 额外前置条件（群白名单、功能开关等）。
              与 `patterns` 分离是刻意的：**只有正则属于「命令语法」**，
              环境相关的开关属于「是否生效」，混在一起会让注册表无法
              静态描述命令表。
    """

    name: str
    patterns: Sequence[Any]
    handler: CommandHandler
    namespace: str = "default"
    description: str = ""
    priority: int = 100
    permission: str = ""
    consume: bool = True
    enabled: bool = True
    owner: str = ""
    guard: CommandGuard | None = None
    # 允许非管理员用「喵」前缀触发本条命令（内容再按身份区分）。
    # 只给 help 用：群友发「喵help」要能看到自己能用的指令，
    # 而不是被「喵 = 管理员专用」整条规则静默吞掉。
    open_admin_prefix: bool = False
    # 注册后由 registry 填充：编译好的 (action, regex) 列表
    _compiled: list[tuple[str, Pattern[str]]] = field(default_factory=list, repr=False)

    def compile(self) -> None:
        """编译/规整 patterns。**启动时执行一次**，不在每条消息上重复编译。

        Phase 3 性能要求：禁止每条消息重新编译全部正则。
        """
        compiled: list[tuple[str, Pattern[str]]] = []
        for item in self.patterns:
            if isinstance(item, tuple) and len(item) == 2:
                action, pattern = item
                compiled.append((str(action), _as_pattern(pattern)))
            else:
                compiled.append((self.name, _as_pattern(item)))
        self._compiled = compiled

    @property
    def compiled_patterns(self) -> list[tuple[str, Pattern[str]]]:
        if not self._compiled:
            self.compile()
        return self._compiled

    def match(self, text: str) -> CommandMatch | None:
        """按注册顺序尝试匹配；返回第一个命中。"""
        if not self.enabled:
            return None
        stripped = (text or "").strip()
        if not stripped:
            return None
        for action, pattern in self.compiled_patterns:
            m = pattern.match(stripped)
            if m is None:
                continue
            groups = tuple(g for g in (m.groups() or ()) if g is not None)
            return CommandMatch(
                name=self.name,
                namespace=self.namespace,
                action=action,
                args=groups,
                text=stripped,
            )
        return None


def _as_pattern(value: Any) -> Pattern[str]:
    """把 str / Pattern 统一成 Pattern。

    业务模块常量本身就是 `re.compile(...)` 的结果，因此这里通常是直通。
    """
    if isinstance(value, re.Pattern):
        return value
    return re.compile(str(value))


__all__ = [
    "Command",
    "CommandMatch",
    "CommandHandler",
    "CommandGuard",
    "CommandPermissionDenied",
]
