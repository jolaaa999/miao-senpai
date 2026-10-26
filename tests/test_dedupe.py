"""重复消息去重单测。

场景就是 NapCat 断线重连重放事件：同一条消息进来两次，
她只能回一次，而且第二次不能再去写「最近聊天流水」。
"""

from __future__ import annotations

from dl_senpai.dedupe import MessageDeduper


class _Ctx:
    """最小上下文：只要 self_id / message_id。"""

    def __init__(self, message_id: str, self_id: str = "2480465844") -> None:
        self.message_id = message_id
        self.self_id = self_id


def test_same_message_twice_is_duplicate():
    d = MessageDeduper()
    assert d.is_duplicate(_Ctx("1001")) is False
    assert d.is_duplicate(_Ctx("1001")) is True


def test_different_messages_pass():
    d = MessageDeduper()
    assert d.is_duplicate(_Ctx("1001")) is False
    assert d.is_duplicate(_Ctx("1002")) is False


def test_same_id_different_bot_is_not_duplicate():
    """两个 bot 账号收到同一条消息不算重复（多开场景）。"""
    d = MessageDeduper()
    assert d.is_duplicate(_Ctx("1001", self_id="111")) is False
    assert d.is_duplicate(_Ctx("1001", self_id="222")) is False


def test_missing_message_id_never_blocks():
    """没有 message_id 的事件必须放行——不能因为缺 id 丢正常消息。"""
    d = MessageDeduper()
    assert d.is_duplicate(_Ctx("")) is False
    assert d.is_duplicate(_Ctx("")) is False


def test_expires_after_window():
    """超出窗口的同 id 视为新消息，不永久占着内存判重。"""
    d = MessageDeduper(window_sec=10)
    assert d.is_duplicate(_Ctx("1001"), now=0.0) is False
    assert d.is_duplicate(_Ctx("1001"), now=5.0) is True
    assert d.is_duplicate(_Ctx("1001"), now=100.0) is False


def test_fifo_eviction_beyond_max_items():
    """超出容量按 FIFO 丢最老的，防止重连风暴把内存涨爆。"""
    d = MessageDeduper(window_sec=1000, max_items=3)
    for i in range(5):
        d.is_duplicate(_Ctx(str(i)), now=float(i))
    assert d.size() == 3


def test_reset_clears_history():
    d = MessageDeduper()
    d.is_duplicate(_Ctx("1001"))
    d.reset()
    assert d.size() == 0
    assert d.is_duplicate(_Ctx("1001")) is False


def test_default_config_enables_dedupe():
    """默认必须开着：这是会真出事故的那一类。"""
    from dl_senpai.config import PluginConfig

    cfg = PluginConfig()
    assert cfg.dedupe_enable is True
    assert cfg.dedupe_window_sec > 0
    assert cfg.dedupe_max_items > 0
