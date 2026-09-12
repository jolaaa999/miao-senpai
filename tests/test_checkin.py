from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dl_senpai.checkin import (
    CheckinResult,
    CheckinStore,
    format_checkin_reply,
    format_leaderboard,
    parse_checkin_command,
    title_for_streak,
)

TZ = timezone(timedelta(hours=8))
_WORK_TMP = Path(__file__).resolve().parents[1] / ".pytest_tmp"


@pytest.fixture
def store() -> CheckinStore:
    _WORK_TMP.mkdir(parents=True, exist_ok=True)
    d = _WORK_TMP / f"checkin-{uuid.uuid4().hex}"
    d.mkdir(parents=True, exist_ok=True)
    return CheckinStore(d)


def test_parse_commands() -> None:
    assert parse_checkin_command("签到") == "checkin"
    assert parse_checkin_command("打卡") == "checkin"
    assert parse_checkin_command("我的积分") == "status"
    assert parse_checkin_command("积分排行") == "leaderboard_points"
    assert parse_checkin_command("连续排行") == "leaderboard_streak"
    assert parse_checkin_command("完成任务") == "task_done"
    assert parse_checkin_command("今日任务") == "task_view"
    assert parse_checkin_command("签到帮助") == "help"
    assert parse_checkin_command("你好") == "none"


def test_title_milestones() -> None:
    assert title_for_streak(2) == ""
    assert title_for_streak(3) == "打卡达人"
    assert title_for_streak(7) == "一周卷王候补"
    assert title_for_streak(30) == "学姐认证钉子户"


def test_task_pool_has_fun_interactions() -> None:
    from dl_senpai.checkin import TASK_POOL

    ids = {t[0] for t in TASK_POOL}
    texts = " ".join(t[1] for t in TASK_POOL)
    assert "meow_three" in ids
    assert "喵喵喵" in texts
    assert "send_sticker" in ids
    # 互动向应明显多于纯刷题向
    study_ids = {"ask_algo", "ask_debug", "share_note", "read_docs"}
    fun_count = len(ids - study_ids)
    assert fun_count >= len(study_ids)


def test_checkin_streak_and_break(store: CheckinStore) -> None:
    day0 = datetime(2026, 8, 18, 10, 0, tzinfo=TZ)
    r1 = store.checkin(1, 100, display_name="甲", now=day0)
    assert r1.ok and r1.streak == 1 and r1.points_gained == 11

    day1 = day0 + timedelta(days=1)
    r2 = store.checkin(1, 100, display_name="甲", now=day1)
    assert r2.ok and r2.streak == 2

    r3 = store.checkin(1, 100, now=day1)
    assert not r3.ok and r3.already

    day4 = day1 + timedelta(days=2)
    r4 = store.checkin(1, 100, now=day4)
    assert r4.ok and r4.streak_broken and r4.streak == 1


def test_task_complete_adds_points(store: CheckinStore) -> None:
    now = datetime(2026, 8, 20, 9, 0, tzinfo=TZ)
    r = store.checkin(1, 200, display_name="乙", now=now)
    assert r.task is not None
    before = r.total_points
    ok, msg, user = store.complete_task(1, 200, now=now)
    assert ok
    assert user.points == before + r.task.reward
    assert "验收通过" in msg
    ok2, _, _ = store.complete_task(1, 200, now=now)
    assert not ok2


def test_leaderboard(store: CheckinStore) -> None:
    now = datetime(2026, 8, 20, 9, 0, tzinfo=TZ)
    store.checkin(9, 1, display_name="A", now=now)
    store.checkin(9, 2, display_name="B", now=now)
    users = store.load_group(9)
    users["2"].points = 999
    store.save_group(9, users)
    board = store.leaderboard(9, by="points", limit=5)
    assert board[0].user_id == "2"
    text = format_leaderboard(board, by="points")
    assert "积分排行榜" in text
    assert "B" in text


def test_format_checkin_reply_broken() -> None:
    msg = format_checkin_reply(
        CheckinResult(
            ok=True,
            streak_broken=True,
            points_gained=11,
            streak=1,
            max_streak=5,
            total_points=11,
            title="",
            fortune="今日宜写代码",
        ),
        name="小明",
    )
    assert "断签" in msg
    assert "今日宜写代码" in msg or "寄语" in msg
    assert "📌" in msg
    assert "────────" in msg


def test_format_checkin_card_layout() -> None:
    from dl_senpai.checkin import PendingTask

    msg = format_checkin_reply(
        CheckinResult(
            ok=True,
            points_gained=12,
            streak=3,
            max_streak=3,
            total_points=30,
            title="打卡达人",
            title_upgraded=True,
            fortune="今日宜问问题",
            task=PendingTask(
                id="ask_algo",
                text="今天去问一道算法题",
                reward=8,
                assigned_date="2026-08-21",
            ),
        ),
        name="小红",
    )
    assert "签到成功" in msg
    assert "连续 3 天" in msg
    assert "新称号" in msg
    assert "今日任务" in msg
    assert "完成任务" in msg


def test_format_status_and_help_layout() -> None:
    from dl_senpai.checkin import UserCheckin, format_help, format_status

    text = format_status(
        UserCheckin(user_id="1", points=20, streak=2, max_streak=5, total_checkins=6),
        name="小明",
    )
    assert "签到档案" in text
    assert "积分 20" in text
    help_text = format_help()
    assert "签到玩法" in help_text
    assert "完成任务" in help_text
