"""回归测试：锁死 Phase 2 修复的真实缺陷，防止再次退化。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from dl_senpai.cache import BoundedTTLMap, SessionCooldown


# ================================================================
# Bug 1（P0）：单测绝不产生真实网络请求 / 读取生产 secret
# ================================================================


def test_network_guard_is_active() -> None:
    """测试进程必须具备"禁止访问真实网络"的能力。

    直接尝试连接一个公网地址，必须被 conftest 的 block_network 拦下。
    如果这条测试失败，说明兜底闸门失效——后续任何测试都可能偷偷联网计费。
    """
    import socket

    with pytest.raises(RuntimeError, match="test-network-guard"):
        socket.create_connection(("93.184.216.34", 80), timeout=1)


def test_loopback_still_allowed() -> None:
    """回环地址放行：asyncio 在 Windows 上依赖 socketpair 自建唤醒管道。"""
    import socket

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        client.close()
    finally:
        server.close()


def test_test_mode_env_is_set() -> None:
    """测试模式必须已打开，否则 PluginConfig 会读真实 .env。"""
    import os

    assert os.environ.get("DL_SENPAI_TEST_MODE") == "1"


def test_no_production_endpoint_reachable_from_config() -> None:
    """配置里不应出现生产中转域名或本地真实模型端点。"""
    from dl_senpai.config import PluginConfig

    cfg = PluginConfig()
    active = cfg.active_base_url()
    assert "airelay.buzz" not in active
    assert "127.0.0.1:8001" not in active


def test_explicit_config_not_overridden_by_dotenv() -> None:
    """Bug 1 最小复现：显式 key 必须胜出。"""
    from dl_senpai.config import PluginConfig

    cfg = PluginConfig(OPENAI_API_KEY="sk-unit-test-only")
    assert cfg.openai_api_key == "sk-unit-test-only"
    assert cfg.active_api_key() == "sk-unit-test-only"


@pytest.mark.asyncio
async def test_llm_test_does_not_hit_real_api() -> None:
    """完整走一次 chat()，全程没有真实 HTTP。"""
    from unittest.mock import AsyncMock, MagicMock

    from dl_senpai.config import PluginConfig
    from dl_senpai.llm import SenpaiLLM
    from dl_senpai.observability.retry import NoWaitRetryPolicy

    cfg = PluginConfig(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="test-model",
    )
    llm = SenpaiLLM(cfg, retry=NoWaitRetryPolicy())
    msg = MagicMock()
    msg.content = "在呢～"
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=resp)
    llm.use_client(client)

    await llm.chat([], "在吗", sender_name="小明")
    assert client.chat.completions.create.await_count == 1


# ================================================================
# Bug 2：表情包不重复收集
# ================================================================


class _FakeSegment:
    def __init__(self, seg_type: str, data: dict) -> None:
        self.type = seg_type
        self.data = data


class _FakeEvent:
    """最小 MessageEvent 替身，只带 StickerStore 需要的字段。"""

    def __init__(self, message_id: str, segments: list[_FakeSegment]) -> None:
        self.message_id = message_id
        self.message = segments


@pytest.mark.asyncio
async def test_sticker_collected_only_once_per_message(tmp_path: Path) -> None:
    """同一条消息被两个入口调用时，只应真正收集一次。

    背景：重构前 `sticker_collector`(priority 100) 与 `_handle_chat()`
    都会调用 collect_from_event，导致同一消息重复收集。
    """
    from dl_senpai.stickers import StickerStore

    store = StickerStore(tmp_path / "stickers", max_per_session=50)
    seg = _FakeSegment("image", {"file": "abc123.jpg", "sub_type": "1"})
    event = _FakeEvent("msg-1", [seg])

    first = await store.collect_from_event(
        event, session_id="group:1", context_text="哈哈", source_user="小明"
    )
    second = await store.collect_from_event(
        event, session_id="group:1", context_text="哈哈", source_user="小明"
    )

    assert first == 1
    assert second == 0, "同一条消息不应被重复收集"
    assert store.count("group:1") == 1


@pytest.mark.asyncio
async def test_different_messages_both_collected(tmp_path: Path) -> None:
    """去重不能误伤不同消息。"""
    from dl_senpai.stickers import StickerStore

    store = StickerStore(tmp_path / "stickers", max_per_session=50)
    seg_a = _FakeSegment("image", {"file": "a.jpg", "sub_type": "1"})
    seg_b = _FakeSegment("image", {"file": "b.jpg", "sub_type": "1"})

    a = await store.collect_from_event(
        _FakeEvent("msg-a", [seg_a]),
        session_id="group:1",
        context_text="A",
        source_user="小明",
    )
    b = await store.collect_from_event(
        _FakeEvent("msg-b", [seg_b]),
        session_id="group:1",
        context_text="B",
        source_user="小明",
    )
    assert (a, b) == (1, 1)
    assert store.count("group:1") == 2


@pytest.mark.asyncio
async def test_same_sticker_different_message_merges_not_duplicates(tmp_path: Path) -> None:
    """同一个表情在不同消息里出现：仍是同一条记录（原有幂等语义不变）。"""
    from dl_senpai.stickers import StickerStore

    store = StickerStore(tmp_path / "stickers", max_per_session=50)
    seg1 = _FakeSegment("image", {"file": "same.jpg", "sub_type": "1"})
    seg2 = _FakeSegment("image", {"file": "same.jpg", "sub_type": "1"})

    await store.collect_from_event(
        _FakeEvent("m1", [seg1]),
        session_id="group:1",
        context_text="第一次",
        source_user="小明",
    )
    added = await store.collect_from_event(
        _FakeEvent("m2", [seg2]),
        session_id="group:1",
        context_text="第二次",
        source_user="小明",
    )
    assert added == 0
    assert store.count("group:1") == 1
    # 但上下文关键词被合并了（原行为保留）
    record = store.load("group:1")[0]
    assert "第一次" in record.context_text


@pytest.mark.asyncio
async def test_dedupe_table_is_bounded(tmp_path: Path) -> None:
    """去重表本身不能无限增长。"""
    from dl_senpai.stickers import StickerStore

    store = StickerStore(tmp_path / "stickers", max_per_session=50)
    store._max_seen_events = 10
    seg = _FakeSegment("image", {"file": "x.jpg", "sub_type": "1"})
    for i in range(50):
        await store.collect_from_event(
            _FakeEvent(f"m{i}", [seg]),
            session_id="group:1",
            context_text=f"c{i}",
            source_user="小明",
        )
    assert len(store._seen_events) <= 10


# ================================================================
# Bug 5：无界 dict 治理（不得改变冷却语义）
# ================================================================


def test_bounded_map_respects_capacity() -> None:
    now = [0.0]
    m = BoundedTTLMap(ttl=0, max_size=5, clock=lambda: now[0])
    for i in range(50):
        now[0] = float(i)
        m.set(f"k{i}", i)
    assert len(m) <= 5


def test_bounded_map_expires() -> None:
    now = [0.0]
    m = BoundedTTLMap(ttl=10.0, clock=lambda: now[0])
    m.set("k", "v")
    assert m.get("k") == "v"
    now[0] = 11.0
    assert m.get("k") is None


def test_bounded_map_keeps_live_entries() -> None:
    now = [0.0]
    m = BoundedTTLMap(ttl=10.0, clock=lambda: now[0])
    m.set("k", "v")
    now[0] = 9.9
    assert m.get("k") == "v"


def test_interrupt_cooldown_still_enforces_cooldown() -> None:
    """TTL 治理后，冷却判定语义必须逐字不变。"""
    from dl_senpai.trigger import InterruptCooldown

    cd = InterruptCooldown()
    assert cd.ready("group:1", cooldown_sec=90) is True
    cd.mark("group:1")
    assert cd.ready("group:1", cooldown_sec=90) is False
    # 未到冷却时间仍不算 ready
    assert cd.ready("group:1", cooldown_sec=0) is True  # 0 表示不限制


def test_interrupt_cooldown_ttl_far_exceeds_window() -> None:
    """冷却记录必须容量有界（防内存泄漏），但**不得**按墙钟过期。

    按墙钟 TTL 过期会让"注入 now 验证冷却解禁"的调用方失效——
    合成时间戳会被判为过期记录清除，冷却静默失效。
    """
    from dl_senpai.trigger import InterruptCooldown

    cd = InterruptCooldown()
    assert cd._last._ttl == 0.0, "冷却记录不应按时间过期"
    assert cd._last._max_size > 0, "冷却记录必须有容量上限"


def test_attention_window_semantics_unchanged() -> None:
    from dl_senpai.trigger import AttentionWindow

    win = AttentionWindow()
    assert win.active("group:1", 7, window_sec=60) is False
    win.arm("group:1", 7)
    assert win.active("group:1", 7, window_sec=60) is True
    assert win.active("group:1", 8, window_sec=60) is False  # 别的用户


def test_attention_window_uses_injected_time() -> None:
    """注入合成时间戳必须照常生效（不被墙钟 TTL 清掉）。"""
    from dl_senpai.trigger import AttentionWindow

    win = AttentionWindow()
    win.arm("group:1", 7, now=100.0)
    assert win.active("group:1", 7, window_sec=60, now=150.0) is True
    assert win.active("group:1", 7, window_sec=60, now=161.0) is False


def test_attention_window_per_user_isolation() -> None:
    from dl_senpai.trigger import AttentionWindow

    win = AttentionWindow()
    win.arm("group:1", 1)
    win.arm("group:2", 1)
    assert win.active("group:1", 1, 60) is True
    assert win.active("group:2", 1, 60) is True
    assert win.active("group:3", 1, 60) is False


def test_session_cooldown_matches_legacy_semantics() -> None:
    """统一基类必须复刻原 4 份副本的行为。"""
    cd = SessionCooldown()
    assert cd.ready("s", 0) is True           # cooldown_sec <= 0 → 不限
    assert cd.ready("s", 100) is True         # 无记录 → ready
    cd.mark("s")
    assert cd.ready("s", 100) is False        # 刚 mark → 未就绪
    assert cd.ready("s", 0) is True           # 0 仍是不限


def test_session_cooldown_ready_after_window() -> None:
    cd = SessionCooldown()
    base = time.time()
    cd.mark("s", now=base)
    assert cd.ready("s", 60, now=base + 59) is False
    assert cd.ready("s", 60, now=base + 61) is True


def test_session_cooldown_is_bounded() -> None:
    cd = SessionCooldown()
    cd.max_sessions = 8
    cd._last = BoundedTTLMap(ttl=0.0, max_size=8)
    for i in range(100):
        cd.mark(f"s{i}")
    assert len(cd) <= 8


def test_cooldowns_survive_injected_timestamps() -> None:
    """各 Cooldown 用合成时间戳时，冷却判定必须照常工作。

    这是容量治理不能退化成墙钟 TTL 的原因：一旦按真实时间过期，
    注入的 now（如 100.0）会被当成远古记录清除，冷却静默失效。
    """
    from dl_senpai.browser_agent import BrowseCooldown
    from dl_senpai.group_card import CardCooldown
    from dl_senpai.image_gen import DrawCooldown

    for cls in (BrowseCooldown, CardCooldown, DrawCooldown):
        cd = cls()
        assert cd.ready("g:1", 60, now=100.0) is True
        cd.mark("g:1", now=100.0)
        assert cd.ready("g:1", 60, now=150.0) is False, cls.__name__
        assert cd.ready("g:1", 60, now=161.0) is True, cls.__name__


def test_draw_cooldown_still_works() -> None:
    """迁移到 SessionCooldown 后，原有各 Cooldown 行为不变。"""
    from dl_senpai.image_gen import DrawCooldown

    cd = DrawCooldown()
    assert cd.ready("group:1", 60) is True
    cd.mark("group:1")
    assert cd.ready("group:1", 60) is False


def test_card_cooldown_still_works() -> None:
    from dl_senpai.group_card import CardCooldown

    cd = CardCooldown()
    assert cd.ready("group:1", 0) is True
    cd.mark("group:1")
    assert cd.ready("group:1", 300) is False


def test_mute_cooldown_inherits_card_cooldown() -> None:
    from dl_senpai.group_card import CardCooldown
    from dl_senpai.group_mute import MuteCooldown

    assert issubclass(MuteCooldown, CardCooldown)
    cd = MuteCooldown()
    assert cd.ready("group:1", 60) is True


def test_browse_cooldown_still_works() -> None:
    from dl_senpai.browser_agent import BrowseCooldown

    cd = BrowseCooldown()
    assert cd.ready("group:1", 60) is True
    cd.mark("group:1")
    assert cd.ready("group:1", 60) is False


def test_group_name_throttle_still_works() -> None:
    """群名刷新节流表：仍必须"每群每日最多一次"。"""
    from dl_senpai import group_features

    assert isinstance(group_features._name_fetch_ts, BoundedTTLMap)
    assert group_features._name_fetch_ts._max_size > 0
    # 用真实墙钟写入，验证节流生效
    import time as _time

    gid = "throttle-probe"
    group_features._name_fetch_ts.set(gid, _time.time())
    last = group_features._name_fetch_ts.timestamp(gid)
    assert last is not None
    assert _time.time() - last < group_features._NAME_REFRESH_SEC


# ================================================================
# Bug 4：RetryPolicy 抽取后测试不再真实等待
# ================================================================


@pytest.mark.asyncio
async def test_llm_retry_does_not_really_sleep(monkeypatch) -> None:
    """503 重试路径必须被覆盖，但**不能**真实等待 60×3 秒。

    这条测试是套件从 35 分钟降到 1 分钟以内的守门人。
    """
    from unittest.mock import AsyncMock, MagicMock

    from openai import APIStatusError

    from dl_senpai.config import PluginConfig
    from dl_senpai.llm import SenpaiLLM
    from dl_senpai.observability.retry import NoWaitRetryPolicy

    cfg = PluginConfig(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="test-model",
    )
    llm = SenpaiLLM(cfg, retry=NoWaitRetryPolicy())

    err = APIStatusError("boom", response=MagicMock(status_code=503), body=None)
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=err)
    llm.use_client(client)

    started = time.perf_counter()
    with pytest.raises(Exception):
        await llm.chat([], "你好", sender_name="小明")
    elapsed = time.perf_counter() - started

    assert client.chat.completions.create.await_count == 4  # 重试语义保留
    assert elapsed < 5.0, f"重试仍在真实等待：{elapsed:.1f}s"


def test_bounded_map_has_distinguishes_none_value() -> None:
    """值为 None 与"键不存在"必须可区分（这是 cooldown 的正确性前提）。"""
    m = BoundedTTLMap(ttl=100)
    m.set("k", None)
    assert m.has("k") is True
    assert m.get("k") is None
    assert m.has("missing") is False


def test_bounded_map_timestamp_is_business_value_not_clock() -> None:
    """条目内必须分开保存「map 自己的时钟」与「调用方写入的业务时间戳」。

    重构中曾把两者合并，导致 `mark(now=100.0)` 的业务时间戳被墙钟覆盖，
    注入合成时间的冷却测试与调用方全部失效。
    """
    m = BoundedTTLMap(ttl=0.0)
    m.set("k", 100.0)
    assert m.timestamp("k") == 100.0
    # 与真实墙钟（~1.7e9）明显不同，证明存的是业务值
    assert m.timestamp("k") < 1e6
