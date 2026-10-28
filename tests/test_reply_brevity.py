"""回复长度纪律单测。

这块管的是一件很具体的事：她在群里被问一句，回的是**一句话**
还是**三段小作文**。原来的病根在人设里写着「答疑目标 400~1500 字」，
所以除了改人设，还要保证：

1. 长度纪律真的被拼到提示词**最末**（位置错了等于没写）；
2. 碎句分条默认关掉（连发 2~3 条在群里像刷屏）。
"""

from __future__ import annotations

from dl_senpai.agent.prompt.builder import PromptBuilder, PromptRequest
from dl_senpai.brevity import brevity_hint_text, build_brevity_hint, is_explain_request
from dl_senpai.config import PluginConfig
from dl_senpai.reply_send import split_human_bubbles


def test_tone_hint_lives_in_the_last_block():
    """语气铁律必须和长度纪律一起贴在提示词最末。

    为什么钉这条：这两条规则在人设里都写得很清楚，但中段权重太低，
    实测「今天好累，作业写到两点」会被当成情绪求助开始安慰。
    位置比措辞重要——挪到末尾才压得住，所以它不能掉出这一块。
    """
    hint = build_brevity_hint(True)
    assert "【别演】" in hint
    assert "情绪求助" in hint
    assert "客服腔" in hint
    # 关掉时整块都不注入，不能只掐掉长度部分
    assert build_brevity_hint(False) == ""


def test_brevity_hint_can_be_disabled():
    assert build_brevity_hint(True).strip()
    assert build_brevity_hint(False) == ""


def test_brevity_hint_states_a_concrete_budget():
    """只写「要简洁」没用——得给可执行的字数，模型才照做。"""
    text = brevity_hint_text()
    assert "句" in text
    assert "字" in text
    # 必须留一个「被要求才展开」的出口，否则真需要长回答时她会拒绝展开
    assert "详细讲讲" in text or "步骤" in text


def test_persona_no_longer_demands_long_answers():
    """病根回归测试：「目标 400~1500 字」不能回来。"""
    from dl_senpai import persona

    prompt = persona.build_system_prompt()
    assert "400~1500" not in prompt
    assert "1500 字" not in prompt


def test_prompt_appends_brevity_hint_at_the_end():
    """位置必须在最末：模型对最后读到的那段最敏感。"""
    builder = PromptBuilder()
    hint = build_brevity_hint(True)
    built = builder.build(PromptRequest(brevity_hint=hint))
    assert built.text.rstrip().endswith(hint.strip())
    assert built.sections["brevity_hint"] is True


def test_prompt_skips_empty_brevity_hint():
    builder = PromptBuilder()
    built = builder.build(PromptRequest(brevity_hint=""))
    assert built.sections["brevity_hint"] is False


def test_bubble_defaults_split_into_short_blobs():
    """默认拆泡（2026-10-25 改回 3）：真人是一条一条蹦的。

    以前关掉是因为「把一句话切三条像刷屏」；现在改成只按她自己分的行拆，
    单段长文不硬切，所以拆出来的是三句话，不是一段话的三块。
    """
    cfg = PluginConfig()
    assert cfg.bubble_max == 3
    assert cfg.bubble_prefer_chars <= 40   # 每泡是一句口语的长度
    assert cfg.bubble_require_newline is True


def test_single_paragraph_is_never_split(monkeypatch):
    """没有换行就别拆：那是她想一口气说完，硬切就是替她断句。"""
    assert split_human_bubbles(
        "第一句。第二句。第三句。", min_chars=1, prefer_chars=10,
        max_bubbles=3, require_newline=True,
    ) == ["第一句。第二句。第三句。"]


def test_newlines_become_separate_bubbles():
    out = split_human_bubbles(
        "真的假的\n你认真的\n我不信", min_chars=1, prefer_chars=20,
        max_bubbles=3, require_newline=True,
    )
    assert out == ["真的假的", "你认真的", "我不信"]


def test_split_bubbles_returns_single_when_disabled():
    text = "第一句说点什么。第二句再补一句。第三句收个尾。第四句还有。"
    out = split_human_bubbles(text, min_chars=20, prefer_chars=60, max_bubbles=1)
    assert out == [text], "关掉分条时必须原样返回，不能经过合并重排"


def test_split_bubbles_still_works_when_enabled():
    """拆条机制本身没删——配置回 3 就能恢复，行为要还在。"""
    text = "第一句说点什么。第二句再补一句。第三句收个尾。"
    out = split_human_bubbles(text, min_chars=1, prefer_chars=10, max_bubbles=3)
    assert len(out) >= 2


def test_chat_and_explain_modes_differ() -> None:
    """闲聊档压到一句，解释档允许展开，两档都要带语气铁律。

    为什么拆两档：只有一套规则时「要不要展开」由模型自己判，它几乎总判「该」，
    实测最近 159 条回复中位数 63 字、77% 超 40 字（真人聊天 10~20 字）。
    """
    chat = build_brevity_hint(True)
    explain = build_brevity_hint(True, explain=True)

    assert "20 字" in chat
    assert "20 字" not in explain
    assert "400 字" in explain
    assert chat != explain
    # 语气铁律两档共用，任何一档掉了都会让回答回到"客服腔"
    assert "【别演】" in chat
    assert "【别演】" in explain
    assert build_brevity_hint(False, explain=True) == ""


def test_is_explain_request_recognizes_questions() -> None:
    """求解释的句子走解释档，闲聊走闲聊档。"""
    for text in (
        "深度学习是什么",
        "为什么我的代码报错了",
        "怎么配置 NapCat",
        "详细讲讲这个方案",
        "帮我写个正则",
    ):
        assert is_explain_request(text), text

    for text in ("在吗", "123", "晚安", "哈哈哈哈哈", "牛逼", ""):
        assert not is_explain_request(text), text
