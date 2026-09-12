#!/usr/bin/env python3
"""把 qq-chat-exporter 的 friend_*.json 转成 MirrorFlow 可用的 CSV。

MirrorFlow `data clean raw` 需要 CSV 列：
  id, MsgSvrID, type_name, is_sender, talker, msg, src, CreateTime, room_name, is_forward

关键点：--ai-uin 填「要学谁说话」的 QQ 号（对方），不是你自己的号。
  is_sender=1 → assistant（被学的人）
  is_sender=0 → user（你）
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path


COLS = [
    "id",
    "MsgSvrID",
    "type_name",
    "is_sender",
    "talker",
    "msg",
    "src",
    "CreateTime",
    "room_name",
    "is_forward",
]


def _text_of(msg: dict) -> str:
    content = msg.get("content") or {}
    if isinstance(content, dict):
        text = (content.get("text") or "").strip()
        if text:
            return text
        parts: list[str] = []
        for el in content.get("elements") or []:
            if not isinstance(el, dict):
                continue
            data = el.get("data") or {}
            t = el.get("type")
            if t == "text" and data.get("text"):
                parts.append(str(data["text"]))
            elif t == "face" and data.get("name"):
                parts.append(str(data["name"]))
        return "".join(parts).strip()
    return str(content or "").strip()


def _ts(msg: dict) -> str:
    raw = msg.get("timestamp") or 0
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return "1970-01-01 00:00:00"
    if ms > 10_000_000_000:
        ms //= 1000
    dt = datetime.fromtimestamp(ms, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def convert(src: Path, out_csv: Path, ai_uin: str) -> int:
    data = json.loads(src.read_text(encoding="utf-8"))
    chat = data.get("chatInfo") or {}
    room = str(chat.get("name") or chat.get("peerUin") or src.stem)
    peer = str(chat.get("peerUin") or "")
    self_uin = str(chat.get("selfUin") or "")
    ai = str(ai_uin).strip()
    if not ai:
        raise SystemExit("必须指定 --ai-uin（要学谁说话的 QQ 号）")

    rows: list[dict[str, str]] = []
    for i, msg in enumerate(data.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("system") or msg.get("recalled"):
            continue
        sender = msg.get("sender") or {}
        uin = str(sender.get("uin") or "")
        text = _text_of(msg)
        if not text:
            continue
        # 跳过纯媒体占位（对语言风格微调无帮助）
        if re.fullmatch(r"\[(图片|语音|视频|文件|表情)[^\]]*\]", text):
            continue
        if text.startswith("[图片:") and len(text) < 120 and "\n" not in text:
            continue
        # 粗脱敏
        text = re.sub(r"1[3-9]\d{9}", "[手机号]", text)
        mtype = str(msg.get("type") or "text")
        if mtype not in {"text", "mixed"}:
            # MirrorFlow include_type 默认只收 text；表情夹杂也当 text
            if mtype not in {"face", "reply"}:
                continue
            mtype = "text"
        rows.append(
            {
                "id": str(msg.get("id") or i),
                "MsgSvrID": str(msg.get("seq") or msg.get("id") or i),
                "type_name": "text",
                "is_sender": "1" if uin == ai else "0",
                "talker": uin or peer or self_uin,
                "msg": text,
                "src": "",
                "CreateTime": _ts(msg),
                "room_name": room,
                "is_forward": "0",
            }
        )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="QCE JSON → MirrorFlow CSV")
    ap.add_argument("json_path", type=Path, help="friend_*.json 路径")
    ap.add_argument(
        "--ai-uin",
        required=True,
        help="被学风格的人的 QQ 号（对方），例如高冷女神的号",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="输出 CSV（默认 MirrorFlow/runs/chat/qce_manual/csv/chat.csv）",
    )
    args = ap.parse_args()
    out = args.output or Path("runs/chat/qce_manual/csv/chat.csv")
    n = convert(args.json_path, out, args.ai_uin)
    print(f"OK: {n} 条 → {out.resolve()}")
    print("下一步（在 MirrorFlow 目录）：")
    print("  1) 把 CSV 放到 runs/chat/<run_id>/csv/")
    print("  2) setting.jsonc 里 qq_number_ai 填同一个 --ai-uin")
    print("  3) python cli.py data clean raw --input runs/chat/<run_id>/csv --run-id <run_id>")


if __name__ == "__main__":
    main()
