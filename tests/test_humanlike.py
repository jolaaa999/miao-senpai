"""像人一点的三处开关：私聊预算、随口阈值、沉默与抖动。

这三项都是「补漏」性质的：前面几轮把语气和长度压下来了，但压得太一刀切——
私聊也按群聊 60 字截、21 字的吐槽判不成随口一句、每轮必回且间隔规整。
"""

from __future__ import annotations

from dl_senpai.config import PluginConfig
from dl_senpai.tone import is_casual_prompt


def test_private_budget_is_looser_than_group():
    cfg = PluginConfig()
    assert cfg.private_trim_chars > cfg.casual_trim_chars, (
        "私聊预算必须比群聊宽，否则私聊会被压成客服腔"
    )


def test_typing_jitter_and_silence_defaults():
    cfg = PluginConfig()
    assert 0 < cfg.typing_jitter_ratio <= 0.5, "抖动比例要有限，否则间隔会失控"
    assert 0 <= cfg.silence_prob_group <= 0.3, "沉默概率不能高到让人觉得掉线"


def test_casual_threshold_covers_medium_complaints():
    """21 字的吐槽也要算随口一句——原先 15 字的阈值留了明摆着的缺口。"""
    text = "今天真的好累啊，作业写到两点还没写完呢"
    assert len(text) > 15
    assert is_casual_prompt(text) is True


def test_casual_threshold_still_rejects_questions():
    """放宽阈值不能把正经提问也吞进去。"""
    assert is_casual_prompt("这个 loss 不下降是为什么？") is False
    assert is_casual_prompt("帮我把这段代码改成异步的") is False


# ================================================================
# 2026-10-25 大改：把「今天真实日志里的病征」逐条钉成回归测试
# ================================================================
#
# 这一批不是凭空写的规则，全部来自线上数据的四类具体病征：
#   1. 一天 99 条发送里 32 条是「在呢～」（其中 12 条带图）
#   2. 401 字的小作文被截成 31 字的判词（「这一刀就变得很致命：…」）
#   3. 「这句我接不上」「你这条断了」——把自己的理解状态当正文发
#   4. 「说实话，我刚才的回答确实偏了，抱歉。翻回去看……」——自我诊断报告


def test_empty_reply_falls_back_once_then_goes_quiet():
    """空壳兜底：一句短反应，然后闭嘴。"""
    import dl_senpai.style as style_mod

    style_mod.reset_fallback_state()
    first = style_mod.polish_reply("", session_key="group:1")
    assert first in style_mod._FALLBACK_LINES
    assert style_mod.polish_reply("", session_key="group:1") == ""
    # 换一个会话不受影响：沉默是分桶的，不能串群
    assert style_mod.polish_reply("", session_key="group:2") in style_mod._FALLBACK_LINES


def test_empty_reply_with_media_stays_empty():
    """生图/截图轮不补文案：那时候图才是要发的东西。"""
    import dl_senpai.style as style_mod

    style_mod.reset_fallback_state()
    assert style_mod.polish_reply("", session_key="g", allow_fallback=False) == ""


def test_self_commentary_dropped():
    """「这句我接不上」是内部状态，不该出现在正文里。"""
    from dl_senpai.humanize import collapse_self_review

    out = collapse_self_review(
        "这句我接不上。洛希极限你是说天文学那个，还是哪个游戏里的角色？",
        context="咋上来的",
    )
    assert "接不上" not in out
    assert "洛希极限" in out


def test_pure_self_commentary_becomes_silence():
    """整条都在点评自己懂不懂时，宁可这一轮不说话。"""
    from dl_senpai.humanize import collapse_self_review

    assert collapse_self_review("你这条「很」后面断了，猜不出来。", context="很") == ""


def test_self_review_essay_collapsed():
    """自我诊断报告整条收掉——这是最不像人的一类。"""
    from dl_senpai.humanize import collapse_self_review

    text = (
        "说实话，我刚才的回答确实偏了，抱歉。翻回去看，那几轮我都在乱接。"
        "现在从头来：你想问什么直接说。"
    )
    out = collapse_self_review(text, context="你到底在说什么")
    # 复盘段没了；要么只剩一句正常话，要么整条放弃
    assert "抱歉" not in out
    assert "翻回去看" not in out


def test_self_review_kept_when_user_asks_why():
    """对方问的就是「你为什么答错」，复盘是**被要求的答案**，不能删。"""
    from dl_senpai.humanize import collapse_self_review

    text = "抱歉，我刚才把缓存当成了连接池，是我搞错了。"
    assert collapse_self_review(text, context="你刚才为什么答错") == text


def test_ai_flavor_flags_verdict_sentence():
    """判词腔要能被数出来，否则「有没有变好」只能靠眼看。"""
    from dl_senpai.humanize import ai_flavor_score

    score, labels = ai_flavor_score("这一刀就变得很致命：绩点是软排序，德育线是硬门槛。")
    assert score >= 1 and "判词腔" in labels
    assert ai_flavor_score("草") == (0, [])
    assert ai_flavor_score("行，我搞错了") == (0, [])


def test_trim_keeps_head_and_tail_for_long_reply():
    """超预算的长回复保住首尾：只留首句会变成断头判词。"""
    from dl_senpai.tone import trim_to_sentence_budget

    text = "啊这。" + "中间解释很长。" * 12 + "所以你到底投的哪个岗。"
    out = trim_to_sentence_budget(text, 20)
    assert out.startswith("啊这。")
    assert "所以你到底投的哪个岗。" in out


def test_fewshot_is_short_by_default():
    """样例的长度分布决定了她的默认长度：短样例必须占多数。"""
    from dl_senpai.persona import FEWSHOT_DIALOGUES

    replies = [len(a) for _, a in FEWSHOT_DIALOGUES]
    short = sum(1 for n in replies if n <= 10) / len(replies)
    assert short >= 0.6, f"10 字以内的样例只有 {short:.0%}，模型会被长样例带偏"


def test_persona_allows_silence_and_forbids_pushing_back():
    """两条最关键的措辞：允许不接、不许把问题原样推回去。"""
    from dl_senpai.persona import SYSTEM_PROMPT

    assert "允许不接" in SYSTEM_PROMPT
    assert "不许把问题推回给对方" in SYSTEM_PROMPT
