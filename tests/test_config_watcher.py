"""config_watcher 的行为测试：确认热重载真的能改变生效配置。

这些测试直接改写 .env / .env.dev，因此必须**严格复原**——它们是生产配置，
里面是真实 API key 与模型名。fixture 同时备份文件内容与 os.environ。

**关于 .env.dev 的坑**（本文件最重要的背景）：

    `.env.dev` 的优先级高于 `.env`（pydantic-settings 的 env_file 顺序）。
    本机 `.env.dev` 重复声明了大量 DL_SENPAI_* 键，因此：

    - 只改 `.env` → 被 `.env.dev` 的旧值盖回，**开关不生效**；
    - 两个文件都改 → 生效。

    后台写入必须遵循后者。test_env_dev_only_is_insufficient 把这条
    约束钉成测试，避免以后改动后台时踩回去。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from src.plugins.dl_senpai.config_watcher import ConfigWatcher, reset_watcher

_ROOT = Path(__file__).resolve().parents[1]
_ENV = _ROOT / ".env"
_ENV_DEV = _ROOT / ".env.dev"
# 只在 .env 与 .env.dev 都存在的键，才能完整验证优先级语义
_KEY = "DL_SENPAI_INTERRUPT_COOLDOWN"


@pytest.fixture
def env_backup():
    """备份并还原 .env / .env.dev 与相关 os.environ 键。"""
    import os

    originals = {
        p: p.read_text(encoding="utf-8") if p.exists() else None
        for p in (_ENV, _ENV_DEV)
    }
    saved_env = {
        k: os.environ.get(k) for k in list(os.environ) if k.startswith("DL_SENPAI_")
    }
    yield
    for path, text in originals.items():
        if text is not None:
            path.write_text(text, encoding="utf-8")
    # 先删掉 DL_SENPAI_* 里**测试期间新增**的键，再恢复原有值。
    #
    # 只遍历 saved_env 是不够的：_apply() 会把 .env 里的键写进 os.environ，
    # 这些新键不在备份里，会残留下来污染后续测试（实测导致 test_mcp_env
    # 的 setdefault 拿到 5 而不是它自己 .env 里的 7）。
    for k in list(os.environ):
        if k.startswith("DL_SENPAI_") and k not in saved_env:
            os.environ.pop(k, None)
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _rewrite(key: str, value: str, paths: tuple[Path, ...]) -> None:
    """把指定文件里的 key 改成 value（只改第一个匹配）。"""
    for path in paths:
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8")
        path.write_text(
            re.sub(rf"^{key}=.*$", f"{key}={value}", body, count=1, flags=re.MULTILINE),
            encoding="utf-8",
        )


def test_snapshot_changes_when_env_rewritten(env_backup: None) -> None:
    """mtime/size 指纹必须能感知到文件被改写。"""
    from src.plugins.dl_senpai import config_watcher

    before = config_watcher._snapshot()
    _ENV.write_text(
        _ENV.read_text(encoding="utf-8") + "\n# watcher-test-marker\n",
        encoding="utf-8",
    )
    after = config_watcher._snapshot()
    assert before != after


@pytest.mark.asyncio
async def test_watcher_triggers_reload(env_backup: None) -> None:
    """改 .env 后 watcher 应自动重载，且 reload_count 递增。"""
    reset_watcher()
    watcher = ConfigWatcher(interval=0.05)
    watcher.start()
    try:
        await asyncio.sleep(0.15)
        base = watcher.reload_count
        _ENV.write_text(
            _ENV.read_text(encoding="utf-8") + "\n# reload-trigger\n",
            encoding="utf-8",
        )
        for _ in range(40):
            await asyncio.sleep(0.05)
            if watcher.reload_count > base:
                break
        assert watcher.reload_count > base, "改了 .env 却没有触发重载"
    finally:
        await watcher.stop()
        reset_watcher()


@pytest.mark.asyncio
async def test_stop_is_idempotent(env_backup: None) -> None:
    """重复 stop 不应抛异常（shutdown 可能被多次调用）。"""
    watcher = ConfigWatcher(interval=0.05)
    watcher.start()
    await watcher.stop()
    await watcher.stop()


def test_sync_covers_both_env_files(env_backup: None) -> None:
    """_sync_os_environ 必须**同时**读 .env 与 .env.dev。

    只读其中一个，就会漏掉另一个文件里的键，表现为部分开关不生效。
    """
    import os

    os.environ.pop(_KEY, None)
    ConfigWatcher()._sync_os_environ()
    # 两个文件都声明了这个键，按 .env.dev 优先，同步后应等于 .env.dev 的值
    dev_value = re.search(
        rf"^{_KEY}=(.*)$", _ENV_DEV.read_text(encoding="utf-8"), re.MULTILINE
    ).group(1).strip()
    assert os.environ.get(_KEY) == dev_value


def test_env_dev_overrides_env(env_backup: None) -> None:
    """把这条优先级约束钉死：只改 .env 会被 .env.dev 盖回。

    这不是 bug，是与 pydantic-settings 一致的行为；但它是「后台改 .env
    却没生效」的根因，因此必须有测试记录，防止改动后台时踩回去。
    """
    import os

    assert _ENV_DEV.exists(), "本用例依赖 .env.dev 的存在"
    _rewrite(_KEY, "9999", (_ENV,))  # 只改 .env
    os.environ.pop(_KEY, None)
    ConfigWatcher()._sync_os_environ()
    assert os.environ.get(_KEY) != "9999", "只改 .env 却生效了？优先级假设有变"

    _rewrite(_KEY, "9999", (_ENV, _ENV_DEV))  # 两个都改
    os.environ.pop(_KEY, None)
    ConfigWatcher()._sync_os_environ()
    assert os.environ.get(_KEY) == "9999", "两个文件都改了却没生效"