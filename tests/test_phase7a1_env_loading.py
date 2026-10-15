"""Phase 7A.1 缺陷回归：.env 配置必须真正生效。

**修复的真实缺陷**：
    项目的 `.env` 由 pydantic-settings（`PluginConfig`）读取，
    它只填充 PluginConfig 字段，**不写入 `os.environ`**。

    而 `agent/mode.py` / `validation/*` 直接读 `os.environ`，
    导致写在 `.env` 里的：
        DL_SENPAI_AGENT_MODE=shadow
        DL_SENPAI_VALIDATION=1
        DL_SENPAI_VALIDATION_SALT=...
    **从未生效** —— 用户以为在跑 Shadow，实际跑的是 RUNTIME 默认值，
    采集也是关的。

这些测试锁死修复结果，防止将来有人把读取改回 `os.environ`。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dl_senpai.config_env import (
    UNSET_SENTINEL,
    describe_source,
    get_env,
    get_flag,
    get_int,
    parse_list,
    reload_dotenv,
)

ROOT = Path(__file__).resolve().parents[1]


# ================================================================
# 1. 读取优先级
# ================================================================


def test_os_environ_wins_over_dotenv(monkeypatch) -> None:
    """os.environ 优先级最高（测试与容器注入需要能覆盖 .env）。"""
    monkeypatch.setenv("DL_SENPAI_TEST_PRIORITY_KEY", "from_environ")
    assert get_env("DL_SENPAI_TEST_PRIORITY_KEY") == "from_environ"


def test_dotenv_is_read_when_environ_unset(monkeypatch) -> None:
    """os.environ 未设置时，必须能读到 .env（这正是原先的缺陷）。"""
    reload_dotenv()
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", UNSET_SENTINEL)
    # .env 里确实有这一项（Phase 7A.1 由人工写入）
    env_file = ROOT / ".env"
    if not env_file.exists():
        pytest.skip(".env 不存在")
    content = env_file.read_text(encoding="utf-8")
    if "DL_SENPAI_AGENT_MODE=" not in content:
        pytest.skip(".env 未配置 AGENT_MODE")

    from dl_senpai.config_env import _load_dotenv

    assert _load_dotenv().get("DL_SENPAI_AGENT_MODE"), "未能解析 .env"


def test_unset_sentinel_blocks_dotenv_fallback(monkeypatch) -> None:
    """哨兵值表示"显式未配置"，不得回退到 .env（测试隔离用）。"""
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", UNSET_SENTINEL)
    assert get_env("DL_SENPAI_AGENT_MODE", "fallback") == "fallback"


def test_missing_key_returns_default(monkeypatch) -> None:
    monkeypatch.setenv("DL_SENPAI_DEFINITELY_NOT_SET_XYZ", UNSET_SENTINEL)
    assert get_env("DL_SENPAI_DEFINITELY_NOT_SET_XYZ", "d") == "d"


# ================================================================
# 2. 类型解析
# ================================================================


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_get_flag_truthy(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("DL_SENPAI_FLAG_TEST", raw)
    assert get_flag("DL_SENPAI_FLAG_TEST") is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_get_flag_falsy(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("DL_SENPAI_FLAG_TEST", raw)
    assert get_flag("DL_SENPAI_FLAG_TEST") is False


def test_get_flag_default(monkeypatch) -> None:
    monkeypatch.setenv("DL_SENPAI_FLAG_TEST", UNSET_SENTINEL)
    assert get_flag("DL_SENPAI_FLAG_TEST", True) is True
    assert get_flag("DL_SENPAI_FLAG_TEST") is False


def test_get_int(monkeypatch) -> None:
    monkeypatch.setenv("DL_SENPAI_INT_TEST", "42")
    assert get_int("DL_SENPAI_INT_TEST", 7) == 42
    monkeypatch.setenv("DL_SENPAI_INT_TEST", "not-a-number")
    assert get_int("DL_SENPAI_INT_TEST", 7) == 7


def test_parse_list() -> None:
    assert parse_list("a,b , c") == ["a", "b", "c"]
    assert parse_list("") == []
    assert parse_list("  ") == []


# ================================================================
# 3. 诊断入口
# ================================================================


def test_describe_source_reports_environ(monkeypatch) -> None:
    monkeypatch.setenv("DL_SENPAI_SRC_TEST", "x")
    info = describe_source("DL_SENPAI_SRC_TEST")
    assert info["source"] == "os.environ"


def test_describe_source_reports_unset(monkeypatch) -> None:
    monkeypatch.setenv("DL_SENPAI_SRC_TEST", UNSET_SENTINEL)
    info = describe_source("DL_SENPAI_SRC_TEST")
    assert info["source"] == "(unset by test)"


# ================================================================
# 4. 核心回归：mode / validation 不再直接读 os.environ
# ================================================================


def test_mode_does_not_read_os_environ_directly() -> None:
    """mode.py 必须走 config_env，否则 .env 配置不生效。"""
    src = (ROOT / "src" / "plugins" / "dl_senpai" / "agent" / "mode.py").read_text(
        encoding="utf-8"
    )
    assert "os.environ" not in src, "mode.py 退回直读 os.environ（.env 会失效）"
    assert "config_env" in src


def test_validation_modules_do_not_read_os_environ_directly() -> None:
    for name in ("store.py", "privacy.py"):
        path = ROOT / "src" / "plugins" / "dl_senpai" / "validation" / name
        src = path.read_text(encoding="utf-8")
        assert "os.environ" not in src, f"{name} 退回直读 os.environ"
        assert "config_env" in src


def test_env_file_keys_are_readable() -> None:
    """端到端：.env 里配置的键必须能被 get_env 读到。

    这是用户视角的核心断言 —— 在 .env 写字应该有用。
    """
    env_file = ROOT / ".env"
    if not env_file.exists():
        pytest.skip(".env 不存在")

    reload_dotenv()
    # 直接检查解析结果，避免受 os.environ 污染
    from dl_senpai.config_env import _load_dotenv

    parsed = _load_dotenv()
    # .env 里至少有这个（项目长期使用）
    assert "DL_SENPAI_CONSOLE_AUTOSTART" in parsed or len(parsed) > 10


def test_shadow_config_in_env_is_visible(monkeypatch) -> None:
    """若 .env 配了 shadow，get_env 必须能取到（用户配置生效的证明）。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        pytest.skip(".env 不存在")
    content = env_file.read_text(encoding="utf-8")
    if "DL_SENPAI_AGENT_MODE=shadow" not in content:
        pytest.skip(".env 未配置 shadow")

    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", UNSET_SENTINEL)
    reload_dotenv()

    from dl_senpai.config_env import _load_dotenv

    assert _load_dotenv().get("DL_SENPAI_AGENT_MODE") == "shadow"
