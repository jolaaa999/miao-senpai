from __future__ import annotations

from dl_senpai.persona import (
    FEWSHOT_DIALOGUES,
    build_system_prompt,
    format_fewshot_block,
)


def test_format_fewshot_block_contains_pairs() -> None:
    block = format_fewshot_block()
    assert "【说话样例】" in block
    assert "群友：" in block
    assert "学姐：" in block
    assert len(FEWSHOT_DIALOGUES) >= 10
    user0, asst0 = FEWSHOT_DIALOGUES[0]
    assert user0 in block
    assert asst0 in block


def test_build_system_prompt_injects_fewshot_when_enabled() -> None:
    with_fs = build_system_prompt(fewshot_enable=True, interrupt=False)
    assert "【说话样例】" in with_fs
    without = build_system_prompt(fewshot_enable=False, interrupt=False)
    assert "【说话样例】" not in without


def test_fewshot_skipped_on_interrupt() -> None:
    prompt = build_system_prompt(fewshot_enable=True, interrupt=True)
    assert "【说话样例】" not in prompt
