"""扫群导出：非自己、尚未好友的成员 QQ 号清单。

只做名单落盘，不发起加好友、不操控 QQ 客户端 UI。
私聊发送「导出未加好友」触发（SUPERUSER，或 DL_SENPAI_OWNER_IDS）。
"""

from __future__ import annotations

import asyncio
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nonebot import get_driver, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, PrivateMessageEvent
from nonebot.rule import Rule

from .config import get_config
from .social_proactive import _exclude_ids, _get_friend_ids, _list_scan_group_ids

_TZ = ZoneInfo("Asia/Shanghai")
_OUT_DIR = Path(__file__).resolve().parents[3] / "data" / "dl_senpai" / "social" / "candidates"


def _export_dir() -> Path:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    return _OUT_DIR


def _owner_ids() -> set[str]:
    """SUPERUSERS ∪ DL_SENPAI_OWNER_IDS。"""
    out: set[str] = set()
    try:
        for u in get_driver().config.superusers:
            out.add(str(u).strip())
    except Exception:  # noqa: BLE001
        pass
    raw = (getattr(get_config(), "owner_ids", "") or "").strip()
    for part in raw.split(","):
        p = part.strip()
        if p:
            out.add(p)
    return out


async def collect_non_friend_candidates(bot: Bot) -> dict[str, Any]:
    """遍历注册群，收集未好友成员。"""
    config = get_config()
    self_id = str(getattr(bot, "self_id", "") or "")
    friends = await _get_friend_ids(bot)
    excludes = _exclude_ids(config)
    gids = await _list_scan_group_ids(bot, config)

    people: dict[str, dict[str, Any]] = {}
    by_group: dict[str, list[str]] = {}

    for gid in gids:
        try:
            members = await bot.call_api("get_group_member_list", group_id=int(gid))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"dl_senpai friend_candidates: get_group_member_list "
                f"failed group={gid}: {exc}"
            )
            continue
        g_uids: list[str] = []
        for m in members or []:
            if not isinstance(m, dict):
                continue
            uid = str(m.get("user_id") or "").strip()
            if not uid or uid == self_id or uid in friends or uid in excludes:
                continue
            g_uids.append(uid)
            row = people.setdefault(
                uid,
                {
                    "user_id": uid,
                    "nickname": str(m.get("nickname") or ""),
                    "card": str(m.get("card") or ""),
                    "groups": [],
                },
            )
            if gid not in row["groups"]:
                row["groups"].append(gid)
            nick = str(m.get("nickname") or "")
            card = str(m.get("card") or "")
            if nick:
                row["nickname"] = nick
            if card:
                row["card"] = card
        by_group[gid] = sorted(set(g_uids), key=int)
        await asyncio.sleep(0)

    candidates = sorted(people.values(), key=lambda x: int(x["user_id"]))
    return {
        "self_id": self_id,
        "friend_count": len(friends),
        "group_count": len(gids),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "by_group": by_group,
        "exported_at": datetime.now(_TZ).isoformat(timespec="seconds"),
    }


async def export_non_friend_candidates(bot: Bot) -> dict[str, Any]:
    """采集并写入 json / csv / txt，返回摘要（含路径）。"""
    payload = await collect_non_friend_candidates(bot)
    out = _export_dir()
    stamp = datetime.now(_TZ).strftime("%Y%m%d_%H%M%S")
    json_path = out / f"non_friends_{stamp}.json"
    csv_path = out / f"non_friends_{stamp}.csv"
    txt_path = out / f"non_friends_{stamp}.txt"
    latest_json = out / "non_friends_latest.json"
    latest_txt = out / "non_friends_latest.txt"

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path.write_text(text, encoding="utf-8")
    latest_json.write_text(text, encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["user_id", "nickname", "card", "groups"])
        for c in payload["candidates"]:
            w.writerow(
                [
                    c["user_id"],
                    c.get("nickname") or "",
                    c.get("card") or "",
                    " ".join(c.get("groups") or []),
                ]
            )

    lines = [str(c["user_id"]) for c in payload["candidates"]]
    body = "\n".join(lines) + ("\n" if lines else "")
    txt_path.write_text(body, encoding="utf-8")
    latest_txt.write_text(body, encoding="utf-8")

    logger.info(
        f"dl_senpai friend_candidates: exported {payload['candidate_count']} "
        f"uids friends={payload['friend_count']} groups={payload['group_count']} "
        f"dir={out}"
    )
    return {
        **{
            k: payload[k]
            for k in (
                "self_id",
                "friend_count",
                "group_count",
                "candidate_count",
                "exported_at",
            )
        },
        "json": str(json_path),
        "csv": str(csv_path),
        "txt": str(txt_path),
        "latest_txt": str(latest_txt),
        "latest_json": str(latest_json),
    }


async def _is_owner_private(event: MessageEvent) -> bool:
    if not isinstance(event, PrivateMessageEvent):
        return False
    owners = _owner_ids()
    if not owners:
        return False
    return str(event.user_id) in owners


async def _allowed(event: MessageEvent) -> bool:
    if not await _is_owner_private(event):
        return False
    text = event.get_plaintext().strip()
    return text in {"导出未加好友", "导出非好友", "export non friends"}


export_matcher = on_message(rule=Rule(_allowed), priority=5, block=True)


@export_matcher.handle()
async def handle_export_non_friends(bot: Bot, event: PrivateMessageEvent) -> None:
    await export_matcher.send("开始扫群导出未加好友名单，稍等…")
    try:
        info = await export_non_friend_candidates(bot)
    except Exception as exc:  # noqa: BLE001
        logger.exception("dl_senpai friend_candidates export failed")
        await export_matcher.finish(f"导出失败：{exc}")
        return
    await export_matcher.finish(
        "导出完成\n"
        f"未加好友：{info['candidate_count']} 人\n"
        f"已好友：{info['friend_count']}｜扫群：{info['group_count']}\n"
        f"txt：{info['latest_txt']}\n"
        f"csv：{info['csv']}\n"
        f"接着可发：开始RPA加好友"
    )


# ── RPA 控制（本机 QQ UI）──────────────────────────────────────────

_RPA_START = {"开始RPA加好友", "开始rpa加好友", "rpa加好友"}
_RPA_STOP = {"停止RPA加好友", "停止rpa加好友", "停止加好友rpa"}
_RPA_STATUS = {"RPA加好友进度", "rpa加好友进度", "加好友进度"}
_RPA_HELP = {"RPA加好友说明", "rpa加好友说明", "加好友rpa说明"}


async def _rpa_cmd(event: MessageEvent) -> bool:
    if not await _is_owner_private(event):
        text = event.get_plaintext().strip() if isinstance(event, PrivateMessageEvent) else ""
        if text in _RPA_START | _RPA_STOP | _RPA_STATUS | _RPA_HELP:
            owners = _owner_ids()
            if not owners:
                logger.warning(
                    "dl_senpai friend_rpa: 未配置 SUPERUSERS / DL_SENPAI_OWNER_IDS"
                )
        return False
    text = event.get_plaintext().strip()
    return text in _RPA_START | _RPA_STOP | _RPA_STATUS | _RPA_HELP


rpa_matcher = on_message(rule=Rule(_rpa_cmd), priority=5, block=True)


@rpa_matcher.handle()
async def handle_friend_rpa(bot: Bot, event: PrivateMessageEvent) -> None:
    from .friend_add_rpa import (
        _CALIB_PATH,
        _CAND_DIR,
        _PROGRESS_PATH,
        Calibration,
        load_progress,
        load_uin_list,
        request_stop,
        spawn_rpa_process,
    )

    cfg = get_config()
    text = event.get_plaintext().strip()

    if text in _RPA_HELP:
        await rpa_matcher.finish(
            "RPA加好友流程：\n"
            "1) 发「导出未加好友」\n"
            "2) QQ 打开仍显示「加好友」的资料卡，本机运行：\n"
            "   python scripts/qq_friend_add_rpa.py --calibrate\n"
            "3) 发「开始RPA加好友」（按名单依次点，不校验是否成功）\n"
            "4) 「停止RPA加好友」/「RPA加好友进度」\n"
            f"失败截图：{_CAND_DIR / 'rpa_debug'}\n"
            f"模板：{_CAND_DIR / 'rpa_templates'}"
        )
        return

    if text in _RPA_STATUS:
        try:
            total = len(load_uin_list())
        except FileNotFoundError:
            total = 0
        prog = load_progress()
        done = len(prog.get("done") or [])
        failed = len(prog.get("failed") or [])
        calib_ok = Calibration.load().ready_for_clicks()
        last = (prog.get("last_results") or [])[-3:]
        last_txt = ""
        if last:
            bits = [
                f"{x.get('uin')}:{'OK' if x.get('ok') else 'FAIL'}({x.get('reason', '')[:40]})"
                for x in last
            ]
            last_txt = "\n最近：" + "；".join(bits)
        meta_path = _CAND_DIR / "rpa_process.json"
        meta = ""
        if meta_path.is_file():
            meta = meta_path.read_text(encoding="utf-8")[:300]
        await rpa_matcher.finish(
            f"名单总数：{total}\n"
            f"已处理：{done}｜点偏/异常：{failed}｜剩余：{max(0, total - done)}\n"
            f"校准：{'OK' if calib_ok else '未校准'}\n"
            f"进度文件：{_PROGRESS_PATH}"
            f"{last_txt}\n"
            f"{meta}"
        )
        return

    if text in _RPA_STOP:
        request_stop()
        await rpa_matcher.finish("已写入停止标记，RPA 进程将在当前步骤后退出。")
        return

    # start
    if not getattr(cfg, "friend_rpa_enable", True):
        await rpa_matcher.finish("RPA 已关闭（DL_SENPAI_FRIEND_RPA_ENABLE=false）")
        return

    # 若无名单则先导出
    latest = _CAND_DIR / "non_friends_latest.txt"
    if not latest.is_file():
        await rpa_matcher.send("尚无名单，先自动导出…")
        try:
            info = await export_non_friend_candidates(bot)
            await rpa_matcher.send(f"导出完成，未加好友 {info['candidate_count']} 人")
        except Exception as exc:  # noqa: BLE001
            await rpa_matcher.finish(f"导出失败：{exc}")
            return

    if not Calibration.load().ready_for_clicks():
        await rpa_matcher.finish(
            "尚未校准。请先在本机运行：\n"
            "python scripts/qq_friend_add_rpa.py --calibrate\n"
            "（QQ 打开仍显示「加好友」的资料卡，按提示采集按钮截图）"
        )
        return

    try:
        proc = spawn_rpa_process(
            max_count=int(getattr(cfg, "friend_rpa_max_per_run", 20) or 0),
            dry_run=False,
            mode=str(getattr(cfg, "friend_rpa_mode", "deeplink") or "deeplink"),
            verify_msg=str(
                getattr(cfg, "friend_rpa_verify_msg", "") or cfg.friend_add_verify_msg
            ),
            delay_min=float(getattr(cfg, "friend_rpa_delay_min", 8.0) or 8.0),
            delay_max=float(getattr(cfg, "friend_rpa_delay_max", 18.0) or 18.0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("spawn rpa failed")
        await rpa_matcher.finish(f"启动 RPA 失败：{exc}")
        return

    log_path = _CAND_DIR / "rpa_run.log"
    await rpa_matcher.finish(
        f"RPA 已启动 pid={proc.pid}\n"
        f"本轮上限：{cfg.friend_rpa_max_per_run}\n"
        f"按名单依次加，点完即过（不校验是否成功）。\n"
        f"请保持 QQ 在前台。停止：发「停止RPA加好友」\n"
        f"日志：{log_path}"
    )
