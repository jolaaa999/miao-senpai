"""配置热重载监听：让 Web 后台改 .env 后，运行中的 bot 实时生效。

背景（为什么需要这个模块）：

    后台管理页把开关写进 `.env`，但 bot 侧 `get_config()` 是**进程内缓存**的，
    不会自己发现文件变了。结果是「网站点了开关，机器人毫无反应」，必须重启
    才生效——这既打断服务，也让「动态配置」名不副实。

本模块轮询 .env / .env.dev 的修改时间，发现变化就重载。

**必须同时失效两条缓存**：

    1. `config_runtime.reload_config()` —— PluginConfig（pydantic）的实例缓存；
    2. `config_env.reload_dotenv()`     —— config_env 自己的 .env 文本缓存。

    只做 1 会漏掉 `DL_SENPAI_AGENT_MODE` 这类**不走 PluginConfig** 的开关
    （它们由 config_env.get_env/get_flag 直接读 .env），表现为「部分开关不生效」。

轮询间隔取 2 秒：够快，代价可忽略——只 stat 两个文件的 mtime，不读内容。
不用 watchdog 之类的库是为了零新增依赖（部署在 Windows，装 inotify 类方案麻烦）。

**重要前提**：`.env.dev` 的优先级**高于** `.env`（与 pydantic-settings 的
`env_file` 顺序一致）。本机的 `.env.dev` 里重复声明了大量 `DL_SENPAI_*` 键，
因此**只改 `.env` 对同名键是无效的**，会被 `.env.dev` 的旧值盖回。
后台写入时必须一并写进 `.env.dev`，或先把 `.env.dev` 里的重复键清掉。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from nonebot import logger

from .config_env import reload_dotenv

# 仓库根：本文件位于 src/plugins/dl_senpai/config_watcher.py
#   parents[0]=dl_senpai  parents[1]=plugins  parents[2]=src  parents[3]=<root>
_ROOT = Path(__file__).resolve().parents[3]
_WATCHED = (".env", ".env.dev")

# 轮询间隔（秒）。再快没有意义：人点开关的手速远低于此，
# 而更短的间隔只会增加无谓的 stat 调用。
POLL_INTERVAL = 2.0


def _snapshot() -> dict[str, float]:
    """取被监听文件的 mtime+size 指纹。文件不存在记 0。

    同时看 mtime 与 size：某些编辑器/容器会在极短时间内原子替换文件，
    mtime 精度可能不足以区分，加 size 能进一步降低漏检概率。
    """
    snap: dict[str, float] = {}
    for name in _WATCHED:
        path = _ROOT / name
        try:
            st = path.stat()
            # 用 mtime 与 size 拼成一个浮点指纹，便于整体比较
            snap[name] = st.st_mtime + st.st_size / 1e9
        except OSError:
            snap[name] = 0.0
    return snap


class ConfigWatcher:
    """轮询 .env 变化并触发重载。

    用类而不是裸函数，是为了让测试能注入自定义 root / 间隔，
    并在需要时 start/stop 多个实例（互不干扰）。
    """

    def __init__(self, interval: float = POLL_INTERVAL) -> None:
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._snapshot: dict[str, float] = {}
        self._reload_count = 0

    @property
    def reload_count(self) -> int:
        """已触发的重载次数（测试与诊断用）。"""
        return self._reload_count

    async def _loop(self) -> None:
        self._snapshot = _snapshot()
        while True:
            await asyncio.sleep(self._interval)
            current = _snapshot()
            if current == self._snapshot:
                continue
            self._snapshot = current
            self._apply()

    def force_reload(self) -> bool:
        """立刻重载一次配置（喵/重载配置 用）。

        为什么要有这个公开入口：管理员改完 .env 想马上生效，不应该
        只能等轮询（最长 2 秒）或者重启进程 —— 重启会丢掉正在生成的回复、
        正在跑的后台任务，代价远大于收益。

        返回是否成功；失败原因已经写进日志，指令侧只要回一句「没成功」。
        """
        before = self._reload_count
        self._apply()
        return self._reload_count > before


    def _apply(self) -> None:
        """失效三条缓存并刷新单例。

        异常一律吞掉：配置监听是**辅助功能**，绝不能因为它出问题
        而让整个 bot 崩溃或停止响应。
        """
        try:
            # 顺序有讲究，三步缺一不可：
            #   1. reload_dotenv  清 config_env 的文本缓存；
            #   2. _sync_os_environ 把新值写回 os.environ（**关键**）；
            #   3. reload_config  重建 PluginConfig。
            reload_dotenv()
            self._sync_os_environ()
            from .config_runtime.runtime import reload_config

            cfg = reload_config()
            # 已构造的 SenpaiLLM 单例仍持有旧 config 对象，必须换引用，
            # 否则「改开关→单例还是旧值」仍然不生效。
            from .llm import refresh_llm_config

            refresh_llm_config()
            self._reload_count += 1
            logger.info(
                "[dl_senpai] config hot-reloaded "
                f"model={cfg.active_model()} "
                f"sticker={cfg.sticker_enable} voice={cfg.voice_enable} "
                f"search={cfg.search_enable} draw={cfg.draw_enable}"
            )
        except Exception as e:  # noqa: BLE001 — 监听失败不能影响主流程
            logger.warning(f"dl_senpai config reload failed: {type(e).__name__}: {e}")

    def _sync_os_environ(self) -> None:
        """把 .env 的新值同步回 os.environ。

        为什么必须做这一步（本模块最反直觉的地方）：

            bot 启动时 `load_dotenv()` 会把 .env **灌进 os.environ**，
            而 `PluginConfig.settings_customise_sources` 的优先级是
            `(init, env, dotenv)` —— **环境变量高于 dotenv 文件**。

            于是热重载出现一个隐蔽的失效：.env 改成 false 后，
            `reload_config()` 重建时 `env_settings` 仍从 os.environ 读到
            旧的 true，且优先级更高，结果**永远读不到新值**。
            实测：不同步 os.environ 时 sticker_enable 恒为 True；
            同步后立刻变成 False。

            只覆盖 .env 里**实际出现**的键，不动其它环境变量——
            这样容器/CI 通过环境变量注入的配置（优先级本应更高）不被误伤。
        """
        # 合并两个文件，靠后的优先（与 pydantic 的 env_file 顺序一致）
        merged: dict[str, str] = {}
        for name in _WATCHED:
            path = _ROOT / name
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for raw in text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if key:
                    merged[key] = value

        # 只同步**与 os.environ 现有值不同**的键，而不是全量覆盖。
        #
        # 为什么不能全量灌入：其它模块（如 mcp_servers/_env.py）用
        # `os.environ.setdefault(...)` 读取 .env —— 一旦我们提前把所有键
        # 写进 os.environ，setdefault 会认为"已存在"而跳过，导致那些模块
        # 拿到的是这里的值而非它自己 .env 的值。实测会把
        # DL_SENPAI_SEARCH_MAX_RESULTS 从 7 覆盖成 5。
        #
        # 而不同步又不行：PluginConfig 的来源优先级是 (init, env, dotenv)，
        # os.environ 高于 dotenv 文件，旧值会让热重载失效（本模块的初衷）。
        # 折中：只写"确实变了"的键——既让热重载生效，又不无谓地污染
        # 那些从未被改过的键。
        for key, value in merged.items():
            if os.environ.get(key) != value:
                os.environ[key] = value

    def start(self) -> None:
        """启动后台轮询（重复调用无副作用）。"""
        if self._task is not None and not self._task.done():
            return
        self._snapshot = _snapshot()
        self._task = asyncio.create_task(self._loop(), name="dl_senpai-config-watcher")

    async def stop(self) -> None:
        """停止轮询。用于 shutdown 与测试清理。"""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# 进程内单例：由 __init__.py 的 startup 钩子启动
_watcher: ConfigWatcher | None = None


def get_watcher() -> ConfigWatcher:
    global _watcher
    if _watcher is None:
        _watcher = ConfigWatcher()
    return _watcher


def reset_watcher() -> None:
    """清空单例（测试用）。"""
    global _watcher
    _watcher = None


__all__ = [
    "ConfigWatcher",
    "POLL_INTERVAL",
    "get_watcher",
    "reset_watcher",
]