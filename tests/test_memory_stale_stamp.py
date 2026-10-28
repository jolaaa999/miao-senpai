"""跨天历史必须让模型看得见「这是昨天的」。

回归的 bug（2026-10-28 线上）：`memory/group_*.json` 只存 role/content，没有时间戳。
于是 10-27 21:20 那句「说是能不能帮我整个谷歌账号我想去玩旅人国际服了」在
11:33 的模型眼里和刚收到的话毫无区别——它照着办，还把检索摘要搬出来当答案。
用户当场说：「你这BOT怎么还在昨天的话题」。

这里钉住三件事：ts 写得进去、读得出来、跨天的消息在注入前带上了日期前缀。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from dl_senpai.memory import ChatMemory, stamp_stale_messages


@pytest.fixture()
def mem_dir(tmp_path: Path) -> Path:
    return tmp_path / "memory"


def test_append_turn_writes_timestamp(mem_dir: Path) -> None:
    """写入必须记时间，但主文件结构不许变（admin 按 role/content 读）。"""
    mem = ChatMemory(mem_dir)
    mem.append_turn("group:1", "u1", "a1")
    raw = json.loads((mem_dir / "group_1.json").read_text(encoding="utf-8"))
    assert raw == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    stamps = json.loads((mem_dir / ".stamps" / "group_1.json").read_text(encoding="utf-8"))
    assert len(stamps) == 2
    assert all(stamp > time.time() - 60 for stamp in stamps)


def test_stamps_file_survives_reload(mem_dir: Path) -> None:
    """时间轴要能跨进程读回来：换个实例照样认得这些消息是刚写的。"""
    ChatMemory(mem_dir).append_turn("group:1", "u1", "a1")
    history = ChatMemory(mem_dir).load_all("group:1")
    assert all(isinstance(m.get("ts"), float) for m in history)


def test_stamps_misaligned_is_ignored(mem_dir: Path) -> None:
    """时间轴长度对不上就当没有——错位盖时间比没有 ts 更糟。"""
    mem = ChatMemory(mem_dir)
    mem.append_turn("group:1", "u1", "a1")
    (mem_dir / ".stamps" / "group_1.json").write_text("[1.0]", encoding="utf-8")
    history = mem.load_all("group:1")
    assert all("ts" not in m for m in history)


def test_load_all_keeps_timestamp(mem_dir: Path) -> None:
    """读出来还得留着 ts，否则上层没法判断新旧。"""
    mem = ChatMemory(mem_dir)
    mem.append_turn("group:1", "u1", "a1")
    history = mem.load_all("group:1")
    assert history and all(isinstance(m.get("ts"), float) for m in history)


def test_old_records_without_ts_still_load(mem_dir: Path) -> None:
    """旧文件没有 ts：照样能读，只是不带 ts，不许拿当前时间补。"""
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "group_9.json").write_text(
        json.dumps([{"role": "user", "content": "老消息"}], ensure_ascii=False),
        encoding="utf-8",
    )
    history = ChatMemory(mem_dir).load_all("group:9")
    assert history == [{"role": "user", "content": "老消息"}]


def test_stamp_prefixes_other_days_only() -> None:
    """跨天加前缀，当天不加：每句都顶时间戳比不加更假。"""
    now = time.mktime((2026, 10, 28, 11, 33, 0, 0, 0, -1))
    yesterday = time.mktime((2026, 10, 27, 21, 20, 0, 0, 0, -1))
    today_early = time.mktime((2026, 10, 28, 8, 0, 0, 0, 0, -1))
    out = stamp_stale_messages(
        [
            {"role": "user", "content": "帮我整个谷歌账号", "ts": yesterday},
            {"role": "user", "content": "我脑子不行", "ts": today_early},
        ],
        now=now,
    )
    assert out[0]["content"] == "[10-27 21:20] 帮我整个谷歌账号"
    assert out[1]["content"] == "我脑子不行"


def test_stamp_drops_ts_key() -> None:
    """ts 是内部字段，输出必须只剩 role/content，否则拼 messages 会报错。"""
    out = stamp_stale_messages([{"role": "user", "content": "x", "ts": time.time()}])
    assert set(out[0]) == {"role", "content"}


def test_stamp_marks_legacy_messages_by_position() -> None:
    """升级前的存量没有 ts：最后两轮当新鲜，其余标 [之前]。

    为什么不干脆不动：昨天的存量消息如果原样喂回去，隔夜请求照样被当成待办，
    今天的 bug 就等于没修。也不猜具体时刻——只表达「这不是刚说的」。
    """
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(4)]
    out = stamp_stale_messages(msgs)
    assert out[0]["content"] == "[之前] m0"
    assert out[1]["content"] == "[之前] m1"
    assert out[2]["content"] == "m2"
    assert out[3]["content"] == "m3"


def test_stamp_is_idempotent_for_today() -> None:
    """当天的消息反复过一遍也不能长出前缀。"""
    msg = {"role": "user", "content": "刚说的", "ts": time.time()}
    once = stamp_stale_messages([msg])
    twice = stamp_stale_messages([dict(once[0], ts=time.time())])
    assert once[0]["content"] == twice[0]["content"] == "刚说的"

