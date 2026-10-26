"""B站小号养号：每天做点"人类会做的事"攒经验，直到能评论（Lv2）。

先把话说明白
------------
B 站用户协议**不允许**自动化操作账号。这个模块做的事（模拟播放、上报心跳、
调分享接口）属于灰色地带，代价可能是账号被限流甚至封禁。它默认关闭，
只在主人明确要求时打开；参数也刻意压得很保守：一天一次、看两三个视频、
不刷播放量、不做批量投币。

为什么需要它
------------
实测（2026-10-26）：Lv0 发评论 -107 未转正；Lv1 发评论 12074「努力升级到
lv2 再来评论吧」。Lv1→Lv2 要 200 经验，而经验只有这几条路：

    每日登录  +5
    每日观看  +5
    每日分享  +5
    每日投币  +10（每枚硬币 1 经验，上限 10，消耗真硬币）

所以一天最多约 +25（不计投币）。200 经验 = 一周起步。

设计原则
--------
1. **只做官方页面自己会发的那些请求**，顺序也照页面的来（点开视频→心跳→分享）
2. 每天只跑一次，时间随机偏移；两天没涨经验也不重试（重试才是脚本特征）
3. 投币默认 0 枚：硬币是她的财产，花在谁的视频上该由主人决定
4. 每次都把"跑之前的经验 / 跑之后的经验"写进日志，涨没涨一目了然
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = ["daily_ritual", "bili_grow_loop", "recent_runs"]

_API = "https://api.bilibili.com"
_HOME = "https://www.bilibili.com/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _headers(referer: str = _HOME) -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def _log_path(cfg: Any) -> Path:
    raw = str(getattr(cfg, "bili_grow_log_file", "") or "")
    return Path(raw or "data/dl_senpai/bili_grow_log.jsonl")


def _append_log(cfg: Any, record: dict[str, Any]) -> None:
    path = _log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def recent_runs(cfg: Any, limit: int = 10) -> list[dict[str, Any]]:
    path = _log_path(cfg)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


async def _exp_now(client: Any) -> tuple[int, str, int]:
    """当前经验 / 昵称 / 等级。用 nav 读，最省事。"""
    resp = await client.get(f"{_API}/x/web-interface/nav")
    data = (resp.json() or {}).get("data") or {}
    if not data.get("isLogin"):
        raise RuntimeError("B站登录态失效了，重跑一次 python scripts\\login-web.py bili")
    info = data.get("level_info") or {}
    return int(info.get("current_exp") or 0), str(data.get("uname") or ""), int(
        info.get("current_level") or 0
    )


async def daily_ritual(
    cfg: Any,
    *,
    videos: int = 2,
    coins: int = 0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """跑一遍"每日功课"：登录态访问首页 → 看几个短视频 → 分享一个 →（可选）投币。

    每一步都记录接口返回码：涨没涨经验要能解释清楚，不能凭感觉。
    """
    import httpx

    from . import bili as bl

    cookies = bl._state_cookies(cfg)
    csrf = cookies.get("bili_jct") or ""
    record: dict[str, Any] = {
        "ts": time.time(),
        "at": datetime.now().isoformat(timespec="seconds"),
        "steps": [],
    }

    async with httpx.AsyncClient(
        headers=_headers(), cookies=cookies, timeout=25.0, follow_redirects=True
    ) as client:
        exp_before, uname, level = await _exp_now(client)
        record.update({"uname": uname, "level_before": level, "exp_before": exp_before})

        # 今天的每日任务已经全做完就别再折腾一遍：重复动作才是脚本特征
        try:
            reward = (await client.get(f"{_API}/x/member/web/exp/reward")).json() or {}
            todo = reward.get("data") or {}
            record["reward_before"] = todo
            if todo.get("login") and todo.get("watch") and todo.get("share"):
                record["skipped"] = "already_done_today"
                record["exp_after"] = exp_before
                record["exp_gain"] = 0
                _append_log(cfg, record)
                return record
        except Exception:  # noqa: BLE001 — 查不到就照做，别因为一个只读接口卡住
            pass

        # 1) 每日登录经验：访问首页就会发（页面自己也会打这些请求）
        home = await client.get(_HOME)
        record["steps"].append({"step": "home", "status": home.status_code})
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # 2) 每天观看经验：挑几个短视频，走"点击上报 + 心跳"这套
        picks = await bl.bili_hot(limit=max(6, videos + 3), cfg=cfg)
        played = 0
        for video in picks:
            if played >= videos:
                break
            try:
                detail = await client.get(
                    f"{_API}/x/web-interface/view", params={"bvid": video.bvid}
                )
                info = (detail.json() or {}).get("data") or {}
                aid = int(info.get("aid") or 0)
                cid = int(info.get("cid") or 0)
                if not aid or not cid:
                    continue
                referer = f"https://www.bilibili.com/video/{video.bvid}"
                click = await client.post(
                    f"{_API}/x/click-interface/click/web/h5",
                    data={
                        "aid": aid,
                        "cid": cid,
                        "bvid": video.bvid,
                        "part": 1,
                        "mid": record.get("mid") or "",
                        "type": 3,
                        "sub_type": 0,
                        "platform": "html5",
                        "jsonp": "jsonp",
                        "csrf": csrf,
                    },
                    headers=_headers(referer),
                )
                record["steps"].append(
                    {"step": "click", "bvid": video.bvid, "code": (click.json() or {}).get("code")}
                )
                # 心跳：分几次报，间隔像人看视频，不要一秒报十次
                start_ts = int(time.time())
                played_time = 0
                for beat in range(3):
                    played_time += random.randint(8, 14)
                    hb = await client.post(
                        f"{_API}/x/click-interface/web/heartbeat",
                        data={
                            "aid": aid,
                            "cid": cid,
                            "bvid": video.bvid,
                            "played_time": played_time,
                            "real_played_time": played_time,
                            "realtime": played_time,
                            "start_ts": start_ts,
                            "type": 3,
                            "sub_type": 0,
                            "dt": 2,
                            "play_type": 0,
                            "csrf": csrf,
                        },
                        headers=_headers(referer),
                    )
                    record["steps"].append(
                        {"step": "heartbeat", "beat": beat + 1, "code": (hb.json() or {}).get("code")}
                    )
                    await asyncio.sleep(random.uniform(8.0, 14.0))
                played += 1
                record.setdefault("watched", []).append(video.bvid)
            except Exception as exc:  # noqa: BLE001 — 单个视频失败不影响别的
                record["steps"].append({"step": "watch_error", "error": str(exc)[:80]})
                continue

        # 3) 每日分享经验：分享一个（不投币就不花钱）
        if played:
            share_bvid = (record.get("watched") or [picks[0].bvid])[0]
            try:
                detail = await client.get(
                    f"{_API}/x/web-interface/view", params={"bvid": share_bvid}
                )
                aid = int(((detail.json() or {}).get("data") or {}).get("aid") or 0)
                if not dry_run and aid:
                    share = await client.post(
                        f"{_API}/x/web-interface/share/add",
                        data={"aid": aid, "csrf": csrf},
                        headers=_headers(f"https://www.bilibili.com/video/{share_bvid}"),
                    )
                    record["steps"].append(
                        {"step": "share", "bvid": share_bvid, "body": (share.json() or {})}
                    )
            except Exception as exc:  # noqa: BLE001
                record["steps"].append({"step": "share_error", "error": str(exc)[:80]})

        # 4) 投币（默认 0 枚：硬币是她的财产，花在谁身上由主人决定）
        if coins > 0 and record.get("watched"):
            for bvid in (record.get("watched") or [])[:coins]:
                try:
                    detail = await client.get(
                        f"{_API}/x/web-interface/view", params={"bvid": bvid}
                    )
                    aid = int(((detail.json() or {}).get("data") or {}).get("aid") or 0)
                    if not aid or dry_run:
                        continue
                    coin = await client.post(
                        f"{_API}/x/web-interface/coin/add",
                        data={"aid": aid, "multiply": 1, "select_like": 0, "csrf": csrf},
                        headers=_headers(f"https://www.bilibili.com/video/{bvid}"),
                    )
                    record["steps"].append(
                        {"step": "coin", "bvid": bvid, "code": (coin.json() or {}).get("code")}
                    )
                    await asyncio.sleep(random.uniform(5.0, 9.0))
                except Exception as exc:  # noqa: BLE001
                    record["steps"].append({"step": "coin_error", "error": str(exc)[:80]})

        try:
            reward = (await client.get(f"{_API}/x/member/web/exp/reward")).json() or {}
            record["reward_after"] = reward.get("data") or {}
        except Exception:  # noqa: BLE001
            pass
        exp_after, _, level_after = await _exp_now(client)
        record.update(
            {
                "exp_after": exp_after,
                "exp_gain": exp_after - exp_before,
                "level_after": level_after,
            }
        )

    _append_log(cfg, record)
    return record


async def bili_grow_loop() -> None:
    """后台循环：每天在设定的小时附近跑一次（默认关闭）。"""
    from nonebot import logger

    from .config import get_config

    cfg = get_config()
    if not bool(getattr(cfg, "bili_grow_enable", False)):
        return
    hour = int(getattr(cfg, "bili_grow_hour", 12) or 12)
    coins = int(getattr(cfg, "bili_grow_coins", 0) or 0)
    logger.info(f"dl_senpai bili_grow loop started hour={hour} coins={coins}")

    def _wait_seconds() -> float:
        """离下一个"设定小时 + 随机分钟"还有多久。随机分钟是别每天同一秒动手。"""
        now = datetime.now()
        target = now.replace(
            hour=hour, minute=random.randint(0, 59), second=0, microsecond=0
        )
        if target <= now:
            target = target + timedelta(days=1)
        return max(300.0, (target - now).total_seconds())

    while True:
        await asyncio.sleep(_wait_seconds())
        try:
            record = await daily_ritual(cfg, coins=coins)
            logger.info(
                f"dl_senpai bili_grow exp {record.get('exp_before')}->{record.get('exp_after')} "
                f"(+{record.get('exp_gain')}) level={record.get('level_after')} "
                f"skip={record.get('skipped') or '-'}"
            )
        except Exception as exc:  # noqa: BLE001 — 养号失败不该拖垮循环
            logger.warning(f"dl_senpai bili_grow failed: {type(exc).__name__}: {exc}")
