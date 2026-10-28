"""抽帧看画面的测试。

视觉这步是锦上添花，任何一环失败都不许影响她刷帖子。所以钉三件事：
1. 取帧时间点均匀、掐掉首尾（片头片尾基本没信息量）；
2. 没装 ffmpeg / 没播放地址 -> 返回带 error 的 dict，不抛异常；
3. 模型输出被思考链截断时要加倍重试，重试还失败就退化成整段当描述，别白丢 8 帧。
"""

from __future__ import annotations

import asyncio

from dl_senpai import video_vision as vv


class Cfg:
    video_frame_width = 640
    video_frame_timeout = 5.0
    video_vision_max_tokens = 100
    video_vision_timeout = 5.0
    openai_api_key = "sk-test"
    openai_base_url = "http://127.0.0.1:1/v1"
    openai_model = "test-model"


def test_frame_times_are_even_and_trimmed() -> None:
    times = vv.frame_times(100.0, 8)
    assert len(times) == 8
    assert times[0] == 5.0      # 掐掉前 5%
    assert times[-1] == 95.0    # 掐掉后 5%
    assert times == sorted(times)


def test_frame_times_without_duration() -> None:
    # 不知道时长也得能给出取点，不能崩
    times = vv.frame_times(0, 4)
    assert len(times) == 4 and all(t >= 0 for t in times)


def test_no_url_returns_empty() -> None:
    assert asyncio.run(vv.describe_video("", cfg=Cfg())) == {}


def test_missing_ffmpeg_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(vv, "ffmpeg_available", lambda: False)
    out = asyncio.run(vv.describe_video("http://x/y.mp4", cfg=Cfg(), count=2))
    assert out.get("error") == "ffmpeg_missing"


def test_truncated_answer_retries_then_degrades(monkeypatch) -> None:
    calls: list[int] = []

    async def _grab(url, ts, target, *, width, referer, timeout):
        target.write_bytes(b"jpeg")
        return True

    async def _ask(cfg, prompt, frames, *, max_tokens):
        calls.append(max_tokens)
        if len(calls) == 1:
            return '{"scene": "一个人在手绘"', "length"   # 被截断的半截 JSON
        return "画面是一个人趴在毛毯上画 4399 网页", "stop"

    monkeypatch.setattr(vv, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(vv, "_grab_frame", _grab)
    monkeypatch.setattr(vv, "_ask_vision", _ask)

    out = asyncio.run(vv.describe_video("http://x/y.mp4", cfg=Cfg(), duration=60, count=2))
    assert calls == [100, 200]        # 第二次额度翻倍
    assert out.get("degraded") is True
    assert "4399" in out.get("scene", "")


def test_good_json_is_parsed(monkeypatch) -> None:
    async def _grab(url, ts, target, *, width, referer, timeout):
        target.write_bytes(b"jpeg")
        return True

    async def _ask(cfg, prompt, frames, *, max_tokens):
        payload = '{"scene": "狗在追尾巴", "text": "", "style": "实拍", "summary": "狗追尾巴"}'
        return "好的，结果如下：" + payload, "stop"

    monkeypatch.setattr(vv, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(vv, "_grab_frame", _grab)
    monkeypatch.setattr(vv, "_ask_vision", _ask)

    out = asyncio.run(vv.describe_video("http://x/y.mp4", cfg=Cfg(), duration=60, count=2))
    assert out["scene"] == "狗在追尾巴"
    assert out["frames"] == 2
