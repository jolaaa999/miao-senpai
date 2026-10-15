"""配置读取：统一从 os.environ 与 .env 两处取值。

Phase 7A.1 修复的真实缺陷：

    项目的 `.env` 由 pydantic-settings（`PluginConfig`）读取，
    它**只填充 PluginConfig 字段，不会写入 `os.environ`**。

    而 `agent/mode.py`、`validation/*` 等模块直接读 `os.environ`
    —— 结果就是：**写在 .env 里的 `DL_SENPAI_AGENT_MODE` 从未生效**。

    表现：用户按文档在 .env 设置 `DL_SENPAI_AGENT_MODE=shadow`，
    机器人实际仍跑 RUNTIME（默认值），采集开关也永远是关的。

修复方式（优先顺序）：
    1. `os.environ` —— 测试与容器注入（最高优先级，可覆盖）
    2. `.env` / `.env.dev` —— 生产配置文件
    3. 调用方给的默认值

**为什么不用 PluginConfig**：`DL_SENPAI_AGENT_MODE` 这类开关不属于
PluginConfig 的字段（它是运行时策略而非插件配置），强行加字段会
让 PluginConfig 变成杂项集合。单独的轻量读取器更清晰。

**缓存**：`_dotenv_cache` 惰性加载一次。文件很小（~160 行），
解析成本可忽略；缓存避免每次调用都读盘。
测试需要改 .env 时调用 `reload_dotenv()`。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# repo 根：本文件位于 src/plugins/dl_senpai/config_env.py
#   parents[0] = dl_senpai/
#   parents[1] = plugins/
#   parents[2] = src/
#   parents[3] = <repo root>   ← .env 在这里
_ROOT = Path(__file__).resolve().parents[3]

_ENV_FILES = (".env", ".env.dev")

# 哨兵值：表示"显式未设置"，用于**测试隔离**。
#
# 为什么需要它：开发机的 .env 里可能有 DL_SENPAI_AGENT_MODE=shadow，
# 而测试需要验证"未配置时的默认值"。删掉环境变量会让 get_env 回退到
# .env；把变量设为空串在 PowerShell 里又等价于删除。
# 因此用一个不可能真实出现的哨兵值来表达"我知道这个键存在，但请当作没配"。
UNSET_SENTINEL = "__UNSET__"

_dotenv_cache: dict[str, str] | None = None


def _load_dotenv() -> dict[str, str]:
    """解析 .env / .env.dev（后者优先，与 pydantic-settings 的声明顺序一致）。"""
    global _dotenv_cache
    if _dotenv_cache is not None:
        return _dotenv_cache

    values: dict[str, str] = {}
    # 先 .env 后 .env.dev —— 后者覆盖前者
    for name in _ENV_FILES:
        path = _ROOT / name
        if not path.exists():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # 去掉成对引号
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if key:
                    values[key] = value
        except OSError:
            continue

    _dotenv_cache = values
    return values


def reload_dotenv() -> None:
    """清缓存（测试用）。"""
    global _dotenv_cache
    _dotenv_cache = None


def get_env(name: str, default: str = "") -> str:
    """取配置值：os.environ 优先，其次 .env，最后 default。

    哨兵值 `__UNSET__` 视为"显式未配置" —— 直接返回 default，
    不再回退 .env（测试隔离用，见 UNSET_SENTINEL 的说明）。
    """
    value = os.environ.get(name)
    if value == UNSET_SENTINEL:
        return default
    if value is not None and value != "":
        return value
    dotenv_value = _load_dotenv().get(name)
    if dotenv_value is not None and dotenv_value != "":
        return dotenv_value
    return default


def get_flag(name: str, default: bool = False) -> bool:
    """取布尔开关（1/true/yes/on 为真）。"""
    raw = get_env(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def get_int(name: str, default: int) -> int:
    raw = get_env(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def parse_list(raw: str) -> list[str]:
    """解析逗号分隔列表。"""
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def describe_source(name: str) -> dict[str, Any]:
    """诊断用：该配置值来自哪里（排查"为什么没生效"）。"""
    value = os.environ.get(name)
    if value == UNSET_SENTINEL:
        return {"name": name, "source": "(unset by test)", "value": ""}
    if value:
        return {"name": name, "source": "os.environ", "value": value}
    dotenv = _load_dotenv()
    if dotenv.get(name):
        return {"name": name, "source": ".env", "value": dotenv[name]}
    return {"name": name, "source": "(default)", "value": ""}


__all__ = [
    "get_env",
    "get_flag",
    "get_int",
    "parse_list",
    "reload_dotenv",
    "describe_source",
    "UNSET_SENTINEL",
]
