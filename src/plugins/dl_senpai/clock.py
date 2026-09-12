"""系统当前时间：供学姐回答「今天几号」等，避免用训练记忆里的旧日期。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001
    TZ = timezone(timedelta(hours=8))

_WEEKDAY_CN = "一二三四五六日"

_ASK_DATETIME_RE = re.compile(
    r"(几月几日|几月几号|几号|星期几|周几|礼拜几|几点|什么时间|几几年|哪年|"
    r"今天(是)?几|现在(是)?几|当前日期|今天日期|现在几点|农历|公历)",
    re.IGNORECASE,
)


def beijing_now() -> datetime:
    return datetime.now(TZ)


def current_beijing_year() -> int:
    return beijing_now().year


def format_current_time_brief() -> str:
    """注入提示词的一行当前时间（北京时间）。"""
    now = datetime.now(TZ)
    wd = _WEEKDAY_CN[now.weekday()]
    return (
        f"【当前时间】{now.year}年{now.month}月{now.day}日 星期{wd} "
        f"{now.hour:02d}:{now.minute:02d}（北京时间，系统时钟为准，优先于你的记忆）"
    )


def is_asking_current_datetime(text: str) -> bool:
    """是否在问当前日期/时间。"""
    return bool(_ASK_DATETIME_RE.search((text or "").strip()))
