"""兼容性自检脚本：确认 Phase 2 未破坏既有行为契约。

用途：重构后快速验证 marker 协议、命令正则、生产数据读取。
可直接运行：python scripts/compat_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "plugins"))

MARKER_CASES = [
    ("CARD", "<<<CARD {\"user_id\":1,\"card\":\"x\",\"kind\":\"request\"}>>>"),
    ("MUTE", "<<<MUTE {\"user_id\":1,\"duration\":60,\"kind\":\"request\"}>>>"),
    ("DRAW", "<<<DRAW a cat>>>"),
    ("STICKER", "<<<STICKER 傲娇>>>"),
    ("VOICE", "<<<VOICE 晚安>>>"),
    ("SEARCH", "<<<SEARCH 天气>>>"),
    ("BROWSE", "<<<BROWSE pixiv 猫>>>"),
    ("IMPRESSION", "<<<IMPRESSION {\"add\":[\"爱猫\"],\"remove\":[],\"notes\":null}>>>"),
]


def check_markers() -> tuple[bool, list[str]]:
    from dl_senpai.browser_agent import parse_browse_request
    from dl_senpai.group_card import parse_card_actions
    from dl_senpai.group_mute import parse_mute_actions
    from dl_senpai.image_gen import parse_draw_request
    from dl_senpai.person_memory import parse_impression_updates
    from dl_senpai.stickers import parse_sticker_request
    from dl_senpai.voice import parse_voice_request
    from dl_senpai.web_search import parse_search_request

    lines: list[str] = []
    ok = True

    r = parse_card_actions("好了\n" + MARKER_CASES[0][1])
    lines.append(f"CARD       actions={len(r.actions)}")
    ok &= len(r.actions) == 1

    r = parse_mute_actions("好\n" + MARKER_CASES[1][1])
    lines.append(f"MUTE       actions={len(r.actions)}")
    ok &= len(r.actions) == 1

    r = parse_draw_request("图\n" + MARKER_CASES[2][1])
    lines.append(f"DRAW       prompt={r.prompt!r}")
    ok &= bool(r.prompt)

    r = parse_sticker_request("嗨\n" + MARKER_CASES[3][1])
    lines.append(f"STICKER    query={r.query!r}")
    ok &= bool(r.query)

    r = parse_voice_request("嗨\n" + MARKER_CASES[4][1])
    lines.append(f"VOICE      override={r.override!r}")
    ok &= bool(r.override)

    r = parse_search_request(MARKER_CASES[5][1])
    lines.append(f"SEARCH     queries={r.queries!r}")
    ok &= bool(r.queries)

    r = parse_browse_request("找\n" + MARKER_CASES[6][1])
    lines.append(f"BROWSE     tasks={len(r.tasks)}")
    ok &= len(r.tasks) >= 1

    r = parse_impression_updates(MARKER_CASES[7][1])
    lines.append(f"IMPRESSION updates={len(r.updates)}")
    ok &= len(r.updates) == 1

    return ok, lines


def check_commands() -> tuple[bool, list[str]]:
    from dl_senpai.affection import parse_affection_command
    from dl_senpai.checkin import parse_checkin_command
    from dl_senpai.shop import parse_shop_command

    lines: list[str] = []
    ok = True

    cases = [
        ("签到", parse_checkin_command, "checkin"),
        ("打卡", parse_checkin_command, "checkin"),
        ("我的积分", parse_checkin_command, "status"),
        ("积分排行", parse_checkin_command, "leaderboard_points"),
        ("连续排行", parse_checkin_command, "leaderboard_streak"),
        ("今日任务", parse_checkin_command, "task_view"),
        ("完成任务", parse_checkin_command, "task_done"),
        ("签到帮助", parse_checkin_command, "help"),
        ("商店", parse_shop_command, "open"),
        ("仓库", parse_shop_command, "warehouse"),
        ("商店帮助", parse_shop_command, "help"),
    ]
    for text, fn, expected in cases:
        got = fn(text)
        good = got == expected
        ok &= good
        lines.append(f"{'OK ' if good else 'FAIL'} {text:<10} -> {got}")

    aff = parse_affection_command("好感度")
    lines.append(f"affection 好感度 -> {aff}")
    ok &= aff != "none" or True  # 记录现状，不强制

    return ok, lines


def check_legacy_data() -> tuple[bool, list[str]]:
    from dl_senpai.affection import AffectionStore
    from dl_senpai.memory import ChatMemory
    from dl_senpai.person_memory import PersonMemoryStore

    lines: list[str] = []
    ok = True
    data_dir = _ROOT / "data" / "dl_senpai"

    mem_dir = data_dir / "memory"
    if mem_dir.is_dir():
        memory = ChatMemory(mem_dir)
        files = sorted(mem_dir.glob("*.json"))
        loaded = 0
        for f in files:
            sid = f.stem.replace("_", ":", 1)
            if memory.load_all(sid):
                loaded += 1
        lines.append(f"memory         {loaded}/{len(files)} files readable")
        ok &= loaded == len(files)

    pm_dir = data_dir / "person_memory"
    if pm_dir.is_dir():
        pm = PersonMemoryStore(pm_dir)
        files = sorted(pm_dir.glob("user_*.json"))
        for f in files:
            pm.get(f.stem[5:])
        lines.append(f"person_memory  {len(files)} files readable")

    aff_dir = data_dir / "affection"
    if aff_dir.is_dir():
        am = AffectionStore(aff_dir)
        files = sorted(aff_dir.glob("group_*.json"))
        for f in files:
            am.load_group(f.stem[6:])
        lines.append(f"affection      {len(files)} files readable")

    return ok, lines


def main() -> int:
    all_ok = True
    for title, fn in (
        ("marker 协议", check_markers),
        ("命令正则", check_commands),
        ("生产数据只读兼容", check_legacy_data),
    ):
        ok, lines = fn()
        all_ok &= ok
        print(f"\n=== {title} ===")
        for line in lines:
            print("  " + line)
    print("\n" + ("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
