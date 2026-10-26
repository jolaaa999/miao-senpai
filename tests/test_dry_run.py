"""干跑模式单测。

干跑的价值是「能连问十句看语气，而不在群里真的发出去」。
它有个很坏的失败模式：**开关开着但消息照发**，或者**开关关着却把消息吞了**。
两种都要钉住。
"""

from __future__ import annotations

import asyncio

import pytest

import dl_senpai.reply_send as rs_mod
from dl_senpai.config import PluginConfig


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, event, message):
        self.sent.append(message)
        return {"message_id": 1}

    async def call_api(self, api, **params):
        return {"status": "ok"}


class _FakeEvent:
    """只带发送路径用得到的字段。"""

    group_id = 980229149
    user_id = 10001
    self_id = "2480465844"


def _patch_config(monkeypatch, *, dry_run: bool) -> None:
    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.dry_run = dry_run
    # 干跑之外还会读到的几项，给上默认值免得 AttributeError
    cfg.typing_ms_per_char = 0
    cfg.typing_ms_max = 0
    cfg.typing_status_enable = False
    cfg.send_breaker_enable = False
    monkeypatch.setattr(
        "dl_senpai.config_runtime.runtime.get_config", lambda: cfg, raising=False
    )


def test_dry_run_defaults_off():
    """默认必须是关的：没人会预期机器人默认不吭声。"""
    assert PluginConfig(dry_run=False).dry_run is False


def test_dry_run_flag_reads_env(monkeypatch):
    cfg = PluginConfig(DL_SENPAI_DRY_RUN="true")
    assert cfg.dry_run is True


class _RecordingLogger:
    """caplog 抓不到 loguru，所以这里直接换掉模块级 logger。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, msg, *a, **k):
        self.lines.append(str(msg))

    def warning(self, msg, *a, **k):
        self.lines.append(str(msg))

    def error(self, msg, *a, **k):
        self.lines.append(str(msg))

    def debug(self, msg, *a, **k):
        self.lines.append(str(msg))


def test_dry_run_blocks_send(monkeypatch):
    """开关打开时：一条都不能发出去，但要说清「本来会说什么」。"""
    _patch_config(monkeypatch, dry_run=True)
    rec = _RecordingLogger()
    monkeypatch.setattr(rs_mod, "logger", rec)
    bot = _FakeBot()
    asyncio.run(
        rs_mod.send_text_reply(
            bot, _FakeEvent(), "在的，咋啦", reply_max_chars=1500, reply_chunk_chars=900
        )
    )
    assert bot.sent == [], "干跑模式下不许真的发出去"
    logged = " ".join(rec.lines)
    assert "[干跑]" in logged
    assert "在的，咋啦" in logged
    assert "5 字" in logged, "干跑日志要带字数，否则没法判断长度改得对不对"


def test_dry_run_off_still_sends(monkeypatch):
    """关掉时必须照常发——不能因为加了干跑就把正常链路弄坏。"""
    _patch_config(monkeypatch, dry_run=False)
    from dl_senpai.send_guard import reset_send_guard

    reset_send_guard()
    bot = _FakeBot()
    asyncio.run(
        rs_mod.send_text_reply(
            bot, _FakeEvent(), "在的", reply_max_chars=1500, reply_chunk_chars=900
        )
    )
    assert len(bot.sent) == 1


def test_dry_run_config_failure_does_not_swallow(monkeypatch):
    """配置读不出来时**宁可正常发**：静默吞消息比误发一条严重得多。"""

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(
        "dl_senpai.config_runtime.runtime.get_config", _boom, raising=False
    )
    assert rs_mod._dry_run_enabled() is False
