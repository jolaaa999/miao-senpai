"""配置来源（Provider）：决定「配置从哪里读、以什么优先级合并」。

Phase 1 审计发现的核心缺陷：`PluginConfig(OPENAI_API_KEY="sk-test")` 这种
显式构造**仍会被磁盘上的 `.env` 覆盖**。原因是 pydantic-settings 的默认
优先级是

    init kwargs  >  环境变量  >  dotenv(.env)  >  secrets

顺序本身是对的，但 `dotenv` 源会把 `.env` 里的值当成「默认值」参与合并，
而在测试里我们既要构造一个干净配置、又不能破坏生产读 `.env` 的能力。

因此本模块引入两个明确的 Provider：

    ProductionConfigProvider —— 读 .env / .env.dev / 真实环境变量（默认）
    TestConfigProvider       —— 不读任何磁盘文件、不读任何环境变量

并通过 `ConfigProvider.dotenv_files()` 控制实际传给 pydantic-settings 的
文件列表。测试通过 `use_config_provider(TestConfigProvider())` 或
`DL_SENPAI_TEST_MODE=1` 切换。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 测试模式环境变量：设成 1/true 时全局使用 TestConfigProvider
TEST_MODE_ENV = "DL_SENPAI_TEST_MODE"

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass
class ConfigProvider:
    """配置来源基类。

    Attributes:
        env_file: 要读的 dotenv 文件列表（相对项目根）。
        read_os_environ: 是否允许读进程环境变量。
        overrides: 无视一切来源、强制生效的键值（测试用）。
        project_root: 解析相对路径的基准目录。
    """

    env_file: tuple[str, ...] = ()
    read_os_environ: bool = True
    overrides: dict[str, Any] = field(default_factory=dict)
    project_root: Path | None = None

    def dotenv_files(self) -> tuple[str, ...]:
        return self.env_file

    def resolve_root(self) -> Path:
        if self.project_root is not None:
            return self.project_root
        # config/providers.py -> config/ -> dl_senpai/ -> plugins/ -> src/ -> 项目根
        return Path(__file__).resolve().parents[4]


class ProductionConfigProvider(ConfigProvider):
    """生产来源：`.env` + `.env.dev` + 真实环境变量。

    与重构前行为完全一致——优先级、文件顺序、文件名都不变。
    密钥（API Key / Token / Secret）只允许从这里来（环境变量或 .env），
    本阶段**不引入** yaml / database / json 作为密钥来源。
    """

    def __init__(self, project_root: Path | None = None) -> None:
        super().__init__(
            env_file=(".env", ".env.dev"),
            read_os_environ=True,
            project_root=project_root,
        )


class TestConfigProvider(ConfigProvider):
    """测试来源：**不读磁盘、不读环境变量**。

    这是修复「13 个测试失败 + 单测真实打外网」的关键：

    - 不读 `.env` → 拿不到真实 API Key、真实中转地址、真实 speak style；
    - 不读 `os.environ` → 不会被 CI/本机残留的 `OPENAI_API_KEY` 污染；
    - 只认显式传入的 kwargs 与 `overrides`。

    结果：测试里 `PluginConfig(OPENAI_API_KEY="sk-test")` 拿到的
    **一定是** `"sk-test"`，绝不会偷偷变成真实 key。
    """

    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        super().__init__(
            env_file=(),
            read_os_environ=False,
            overrides=dict(overrides or {}),
        )


def is_test_mode() -> bool:
    """是否处于测试模式（环境变量开关）。"""
    return os.environ.get(TEST_MODE_ENV, "").strip().lower() in _TRUTHY


def default_provider() -> ConfigProvider:
    """按当前环境选择默认 Provider。"""
    if is_test_mode():
        return TestConfigProvider()
    return ProductionConfigProvider()


__all__ = [
    "ConfigProvider",
    "ProductionConfigProvider",
    "TestConfigProvider",
    "default_provider",
    "is_test_mode",
    "TEST_MODE_ENV",
]
