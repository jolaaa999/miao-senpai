"""测试全局夹具：保证单测**绝不**读取生产配置、**绝不**访问真实网络。

Phase 1 审计发现的两个严重问题，本文件是修复的核心：

1. **配置污染**：`PluginConfig(OPENAI_API_KEY="sk-test")` 会被磁盘上的
   `.env` 覆盖——真实 base_url（`airelay.buzz`）、真实 API Key、
   `speak_styles/styles.json` 里的本地微调端点（`127.0.0.1:8001`）全部泄漏
   进单测。13 个测试因此失败，`test_llm.py` 甚至真的发出了 HTTP 请求并
   拿到了 502 —— 这意味着**跑测试会产生真实 API 费用**。

2. **真实的 60 秒退避**：`llm.py` 对瞬时错误固定 sleep 60s × 3 次重试，
   导致测试套件跑了 35 分钟。

本 conftest 用三道闸门解决：

  闸门 1  `DL_SENPAI_TEST_MODE=1` → PluginConfig 改用 TestConfigProvider，
         不读 .env、不读环境变量。
  闸门 2  每个测试自动 `set_config_provider(TestConfigProvider())`，
         并在结束后 reset，避免跨用例串味。
  闸门 3  `block_network` 自动夹具，把 socket 连接拦掉。任何漏网的
         真实网络调用会立刻失败，而不是悄悄计费。
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

# pytest 的 pythonpath = ["src/plugins", "."]，因此 dl_senpai 可直接 import
# 但 conftest 本身在收集早期运行，需要确保 src/plugins 已在 sys.path
_ROOT = Path(__file__).resolve().parent
_SRC_PLUGINS = _ROOT / "src" / "plugins"
if str(_SRC_PLUGINS) not in sys.path:
    sys.path.insert(0, str(_SRC_PLUGINS))

# 必须在 import dl_senpai.config 之前设置，让 default_provider() 就选测试来源
os.environ["DL_SENPAI_TEST_MODE"] = "1"


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    """每个测试用干净的 TestConfigProvider，并隔离**运行时开关**。

    Phase 7A.1：`config_env.get_env()` 会回退到仓库根的 `.env`。
    开发机的 `.env` 里可能写着 `DL_SENPAI_AGENT_MODE=shadow` /
    `DL_SENPAI_VALIDATION=1`，那会让"依赖默认值"的测试失败 ——
    这是测试隔离问题（生产行为是正确的）。

    因此这里把这些开关设为哨兵值 `__UNSET__` ——
    `get_env` 会把它当作"显式未设置"，不再回退 .env。
    需要特定值的测试自己 monkeypatch.setenv 覆盖。
    """
    for name in (
        "DL_SENPAI_AGENT_MODE",
        "DL_SENPAI_AGENT_SHADOW",
        "DL_SENPAI_AGENT_RUNTIME_GROUPS",
        "DL_SENPAI_VALIDATION",
        "DL_SENPAI_VALIDATION_DIR",
        "DL_SENPAI_VALIDATION_SALT",
        "DL_SENPAI_DEBUG_CONTENT",
    ):
        monkeypatch.setenv(name, "__UNSET__")
    try:
        from dl_senpai.config_env import reload_dotenv

        reload_dotenv()
    except Exception:  # noqa: BLE001 — 早期导入阶段允许失败
        pass

    from dl_senpai.config_runtime import providers
    from dl_senpai.config_runtime.runtime import get_runtime

    runtime = get_runtime()
    previous = runtime._provider  # noqa: SLF001 — 夹具需要精确还原现场
    runtime.reset()
    runtime.set_provider(providers.TestConfigProvider())
    yield runtime
    runtime.reset()
    runtime.set_provider(previous)


@pytest.fixture(autouse=True)
def isolate_recent_chat(tmp_path_factory, monkeypatch):
    """把「当天聊天流水」改道到临时目录。

    为什么必须隔离：recent_chat 是**进程级单例**，目录从全局配置读。
    测试里 dispatch_observers 会真的触发它，于是测试会话（group:100、
    private:7 这类）的数据会写进仓库真实的 data/dl_senpai/recent_chat/。
    其它 store 没这个问题——它们由测试显式注入临时目录，而观察者走的是
    全局配置那条路。

    这里直接替换取值函数，而不是改 _store：调用方都是"函数内 import"，
    替换模块属性就能覆盖 handlers 与 legacy_bridge 两处。

    目录用 tmp_path_factory 另建，**不能**用测试自己的 tmp_path：
    RecentChatStore 构造时会 mkdir，而不少测试会断言「tmp_path 里只有我
    写的那几个文件」（atomic_write 残留检查、local KB 源扫描），多出来的
    recent_chat 子目录会让它们误判。
    """
    try:
        from dl_senpai import recent_chat
    except Exception:  # noqa: BLE001 — 早期导入阶段允许失败
        yield
        return
    store = recent_chat.RecentChatStore(
        tmp_path_factory.mktemp("recent_chat"), flush_interval=0.0
    )
    monkeypatch.setattr(
        recent_chat, "get_recent_chat_store", lambda root=None: store, raising=True
    )
    yield


@pytest.fixture(autouse=True)
def no_wait_retry(monkeypatch):
    """把 LLM 重试等待归零。

    行为仍然被完整覆盖（照样重试到 max_attempts、照样返回友好话术），
    只是不消耗真实时间。修复「13 个测试失败用例合计 sleep 了 180 秒」。
    """
    try:
        from dl_senpai.observability import retry as retry_mod
    except Exception:  # noqa: BLE001
        yield
        return

    async def _instant_sleep(self, seconds: float) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(retry_mod.RetryPolicy, "sleep", _instant_sleep, raising=False)
    yield


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """兜底闸门：禁止测试发起**真实**的对外网络连接。

    为什么需要它：即使配置隔离做对了，将来某个模块也可能硬编码外部 URL
    （例如热榜 API、Bing 搜索）。与其让测试悄悄联网甚至计费，
    不如让它在连接的一瞬间失败。

    实现细节：只拦截**非回环**地址的 connect。
    - 回环（127.0.0.1 / ::1 / localhost）放行：asyncio 在 Windows 上的
      ProactorEventLoop 会自建 socketpair 做内部唤醒，拦掉会让整个
      event loop 起不来；
    - `httpx.AsyncClient` 构造时也可能触碰 socket 基础设施。
    真正的业务风险是"连到外网某台服务器"，而不是"用了 socket"，
    因此按地址判定最准确。
    """
    import ipaddress

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def _is_loopback(address) -> bool:
        if not isinstance(address, tuple) or not address:
            return False
        host = address[0]
        if not isinstance(host, str):
            return False
        if host in {"localhost", "", "::", "::1"}:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            # 主机名（非 IP）一律视为外部，交由下面的判定拦截
            return False

    def _deny(address, *args, **kwargs):
        raise RuntimeError(
            f"[test-network-guard] 测试禁止访问真实网络：{address!r}\n"
            "如果这是被测代码的正常行为，请在测试中用 monkeypatch 注入假实现"
            "（参考 tests/test_llm.py 里替换 client.chat.completions.create 的写法）。"
        )

    def _guarded_connect(self, address, *args, **kwargs):
        if _is_loopback(address):
            return real_connect(self, address, *args, **kwargs)
        _deny(address)

    def _guarded_connect_ex(self, address, *args, **kwargs):
        if _is_loopback(address):
            return real_connect_ex(self, address, *args, **kwargs)
        _deny(address)

    def _guarded_create_connection(address, *args, **kwargs):
        if _is_loopback(address):
            return real_create_connection(address, *args, **kwargs)
        _deny(address)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex, raising=False)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection, raising=False)
    yield
    monkeypatch.setattr(socket.socket, "connect", real_connect, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", real_connect_ex, raising=False)
    monkeypatch.setattr(socket, "create_connection", real_create_connection, raising=False)
