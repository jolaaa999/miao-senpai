"""统一的消息前缀：`[名字] 说：内容`。

为什么单独测这件事：名字可能带标点、本身又是常用词（「失语.」），
旧格式「失语.：123」被模型拆成「「失语.」是个名字？还是在说你自己无语了？」。
这类问题不会报错，只会让群里看着像 AI 在胡说，所以三处拼装点都要钉住。
"""

from __future__ import annotations

from pathlib import Path

from dl_senpai.msg_format import format_user_turn

PLUGIN = Path(__file__).resolve().parents[1] / "src" / "plugins" / "dl_senpai"


def test_format_puts_name_in_brackets() -> None:
    assert format_user_turn("失语.", "123") == "[失语.] 说：123"
    assert format_user_turn("小橙子", "认证") == "[小橙子] 说：认证"


def test_format_handles_empty_and_missing() -> None:
    # 空正文：不能让模型看到「[某人] 说：」这种半截
    assert format_user_turn("失语.", "") == "[失语.] 说：（空消息）"
    assert format_user_turn("", "在吗") == "[对方] 说：在吗"
    assert format_user_turn("  ", "  ") == "[对方] 说：（空消息）"


def test_all_three_assembly_points_use_the_helper() -> None:
    """主链路、补救重试、记忆格式化三处必须同源，漏一处就等于没改。"""
    for rel in ("llm.py", "agent/recovery.py", "vision.py"):
        text = (PLUGIN / rel).read_text(encoding="utf-8")
        assert "format_user_turn(" in text, rel
        # 旧写法不能残留：一旦某处又拼回「名字：内容」，模型就会在那一份里捡回旧读法
        assert 'f"{sender_name}：' not in text, rel
        assert 'f"{sender}：' not in text, rel


def test_system_prompt_explains_the_format() -> None:
    """提示词里必须点明「方括号里是人名」，否则模型只能靠猜。"""
    from dl_senpai.persona import build_system_prompt

    prompt = build_system_prompt()
    assert "【消息格式】" in prompt
    assert "发言人名字" in prompt
