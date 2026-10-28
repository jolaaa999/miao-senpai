"""让学姐真的看一眼：抽帧 -> 视觉模型 -> 一句画面描述 + 画面上的字。

为什么要有这一层
----------------
模型是纯文本时代她只能读标题、简介、字幕，写出来的日记全是「听说」。2026-10-27
实测这条链路走得通：B 站 playurl 拿直链，ffmpeg 按时间点抽帧，发给
deepseek-v4.1-flash（它接受 image_url，多图也行），她能说出「正在毛毯上画 4399
的复古网页界面，带 Logo 和搜索框」——这不是标题里有的东西。

抽帧的边界（必须记住）
----------------------
1. 抽 8 帧不是「看完视频」。连续动作、剪辑节奏、一闪而过的字都看不到；
2. 画面上的字交给模型读，没装 OCR，所以小字会漏（提示词里明确要求"看不清就说看不清"）；
3. 抖音没有字幕接口，所以抖音那边画面描述就是她的主要信息来源；
4. 每次多花 8~20 秒 + 一两千 token，所以只在自主刷和明确要求时才调。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["describe_video", "frame_times", "ffmpeg_available"]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_VISION_PROMPT = """这是同一个视频里按时间均匀抽出来的 {n} 帧（第 1 帧最靠前）。

只回答 JSON，不要别的话：
{{"scene": "画面里在干什么，一到两句。具体到人物/物体/动作，别写'很精彩'这种空话",
 "text": "画面上出现的文字（标题、字幕、界面文字、游戏里的字），照抄，没有就空串",
 "style": "实拍 / 动画 / 游戏画面 / AI 生成 之类，一句话",
 "summary": "如果只凭这些画面跟朋友提这个视频，你会怎么说，25 字以内，第一人称"}}

规矩：
- 看不清的地方写"看不清"，不许猜，不许编
- 帧与帧之间可能是同一场景，别当成多个视频
- 画面里没有字就老实给空串"""


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def frame_times(duration: float, count: int) -> list[float]:
    """按时间均匀取点，掐掉开头和结尾。

    为什么掐掉：开头常是片头/logo，结尾常是关注引导，两头各 5% 基本没有信息量。
    """
    total = float(duration or 0)
    n = max(1, int(count))
    if total <= 0:
        # 不知道时长就按「大概 1 分钟」取前 50 秒，能抽几个算几个
        total = 50.0
    start = total * 0.05
    end = total * 0.95
    if end <= start:
        return [total / 2]
    step = (end - start) / max(1, n - 1) if n > 1 else 0.0
    return [round(start + step * i, 2) for i in range(n)]


async def _grab_frame(
    url: str, ts: float, target: Path, *, width: int, referer: str, timeout: float
) -> bool:
    """用 ffmpeg 从直链里抠一帧。失败返回 False，不抛。"""
    # -ss 放在 -i 前面是「按关键帧快进」，对 http 直链只会拉需要的那一段，
    # 不会把整片下下来（实测 4 分钟视频每帧 0.13~0.22 秒）。
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-headers", f"Referer: {referer}\r\nUser-Agent: {_UA}\r\n",
        "-ss", f"{ts:.2f}", "-i", url,
        "-frames:v", "1", "-vf", f"scale={width}:-2",
        str(target),
    ]
    # 一定要显式要求"别开窗口"：ffmpeg 是控制台程序，如果哪天 bot 换成无控制台的
    # 方式启动（pythonw、某些计划任务配置），子进程会自己新建一个黑窗口闪一下。
    # 一天抽几百帧，那就是几百次闪窗——所以这里不靠"父进程刚好有隐藏控制台"。
    from .winproc import no_window_kwargs

    kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.PIPE,
        **no_window_kwargs(),
    }
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    return target.exists() and target.stat().st_size > 0


async def describe_video(
    url: str,
    *,
    cfg: Any = None,
    duration: float = 0.0,
    count: int = 8,
    title: str = "",
    referer: str = "https://www.bilibili.com/",
) -> dict[str, Any]:
    """抽帧 + 让模型看一眼。返回 {scene,text,style,summary,frames}。

    任何一步失败都返回空 dict——看不了画面不该把她这轮刷帖子搞崩。
    """
    if not url:
        return {}
    if not ffmpeg_available():
        return {"error": "ffmpeg_missing"}
    width = int(getattr(cfg, "video_frame_width", 960) or 960)
    timeout = float(getattr(cfg, "video_frame_timeout", 30.0) or 30.0)
    max_frames = max(1, min(int(count or 8), 12))

    times = frame_times(duration, max_frames)
    frames: list[Path] = []
    tmp = Path(tempfile.mkdtemp(prefix="dlsp-frames-"))
    try:
        for i, ts in enumerate(times):
            target = tmp / f"f{i:02d}.jpg"
            if await _grab_frame(url, ts, target, width=width, referer=referer, timeout=timeout):
                frames.append(target)
        if not frames:
            return {"error": "no_frames"}

        budget = int(getattr(cfg, "video_vision_max_tokens", 2000) or 2000)
        prompt = _VISION_PROMPT.format(n=len(frames))
        answer, finish = await _ask_vision(cfg, prompt, frames, max_tokens=budget)
        data = _first_json_obj(answer)
        if not data and finish == "length":
            # 这模型带思考链，帧一多思考就把额度吃掉了，JSON 被截断。
            # 加倍再来一次，比直接放弃划算（多花一次调用，但能拿到内容）。
            answer, finish = await _ask_vision(cfg, prompt, frames, max_tokens=budget * 2)
            data = _first_json_obj(answer)
        if not data:
            # 兜底：拿不到 JSON 也要留下点什么，总比"看到 8 帧但什么都没说"强
            text = " ".join((answer or "").split())
            if len(text) > 12:
                return {"scene": text[:200], "frames": len(frames), "degraded": True}
            return {"error": "bad_answer", "raw": (answer or "")[:120], "frames": len(frames), "finish": finish}
        data["frames"] = len(frames)
        if title:
            data["title"] = title[:60]
        return data
    finally:
        for path in frames:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            tmp.rmdir()
        except OSError:
            pass


async def _ask_vision(cfg: Any, prompt: str, frames: list[Path], *, max_tokens: int) -> str:
    """把几帧发给视觉模型。

    为什么不复用 llm.py：那边负责的是聊天人格、温度、字数这些，本模块只是
    "看一眼并回答 JSON"，两件事混在一起会让提示词互相污染。这里直连同一个中转。
    实测这个模型是**带思考链**的，max_tokens 给小了会全被思考吃掉、正文返回空串，
    所以默认 1200 起步。
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=str(getattr(cfg, "openai_api_key", "") or ""),
        base_url=str(getattr(cfg, "openai_base_url", "") or ""),
        timeout=float(getattr(cfg, "video_vision_timeout", 60.0) or 60.0),
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in frames:
        b64 = base64.b64encode(path.read_bytes()).decode()
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        )
    resp = await client.chat.completions.create(
        model=str(getattr(cfg, "openai_model", "") or ""),
        messages=[{"role": "user", "content": content}],
        max_tokens=max_tokens,
    )
    choice = resp.choices[0]
    return (choice.message.content or ""), str(choice.finish_reason or "")


def _first_json_obj(text: str) -> dict[str, Any]:
    """从模型回复里抠第一个 JSON 对象。它偶尔会带一段废话或思考。"""
    raw = (text or "").strip()
    start = raw.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(raw[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = raw.find("{", start + 1)
    return {}
