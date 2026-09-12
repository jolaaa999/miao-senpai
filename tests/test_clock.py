from __future__ import annotations

from dl_senpai.clock import format_current_time_brief, is_asking_current_datetime
from dl_senpai.persona import build_system_prompt


def test_format_current_time_brief() -> None:
    text = format_current_time_brief()
    assert text.startswith("【当前时间】")
    assert "年" in text and "月" in text and "日" in text
    assert "系统时钟" in text


def test_is_asking_current_datetime() -> None:
    assert is_asking_current_datetime("今天是几几年几月几日")
    assert is_asking_current_datetime("现在几点了")
    assert not is_asking_current_datetime("过拟合怎么办")


def test_system_prompt_includes_clock_on_mention() -> None:
    prompt = build_system_prompt(interrupt=False)
    assert "【当前时间】" in prompt
    assert build_system_prompt(interrupt=True).count("【当前时间】") == 0
