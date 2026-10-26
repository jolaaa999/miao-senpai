"""反 AI 味清洗与拟人指标。

来源：
- sakurawwwxh/qq-agent-plus 的 humanize.js / humanize-eval.mjs（分档清洗 + 可数指标）
- chengzhi-c/natural-talk 的 SKILL.md（翻案腔、提示语冒号、翻译腔、首句壳）

搬过来是因为它把「像不像人」拆成了可数的东西，而不是靠眼看。
这里测的是**别误伤**：清洗本身好写，写坏的方式都是把正常话删掉。
"""

from __future__ import annotations

from dl_senpai.humanize import (
    collapse_multiline,
    find_ai_tells,
    humanize_metrics,
    humanize_text,
    keep_first_enumeration,
    strip_opening_echo,
)


# ── 元话语与自曝 ────────────────────────────────────────────


def test_strips_meta_prefix_but_keeps_the_content():
    """「回他：」是她在替你起草，不该出现；后面的内容能用。"""
    out, tells = humanize_text("回他：看人数和口味")
    assert out == "看人数和口味", out
    assert any(t["label"] == "元话语前缀" for t in tells)


def test_deletes_self_reported_retrieval():
    """「知识库里没查到」必须整句消失：人设明令禁止，模型照做。"""
    out, _ = humanize_text("天气不错。不过知识库里没查到天气信息，你在哪个城市")
    assert "知识库" not in out
    assert "天气不错" in out


def test_does_not_eat_normal_search_advice():
    """「你用搜索引擎查一下」是正常建议，不能顺手删。"""
    text = "你用搜索引擎查一下课表就行"
    out, _ = humanize_text(text)
    assert out == text


def test_ai_self_reference_removed():
    out, tells = humanize_text("作为一个 AI，我也不知道。明天再看吧。")
    assert "作为一个 AI" not in out
    assert "明天再看吧" in out
    assert any(t["severity"] == "severe" for t in tells)


def test_customer_service_closer_removed():
    out, _ = humanize_text("先看日志。希望这些建议对你有所帮助。")
    assert "希望" not in out
    assert "先看日志" in out


def test_soft_connectives_rewritten_not_deleted():
    """「其次」改成「然后」：句子的意思要留住。"""
    out, _ = humanize_text("首先，看日志。其次，别急着重跑。")
    assert "首先" not in out
    assert "然后" in out
    assert "别急着重跑" in out


def test_clean_text_untouched():
    for text in ("两点睡，活该困。", "课设重做就重做，能咋办。", "嗯。"):
        assert humanize_text(text)[0] == text


# ── 翻案腔（natural-talk B 层）────────────────────────────────
# 这是最刺眼的一条：「不是A，而是B」——没人主张过 A，先立个 A 再推翻。


def test_flip_removes_negation_keeps_subject():
    """「他在重新定义问题」要留住主语——删的只是否定面。"""
    out, _ = humanize_text("他不是在避问题，而是在重新定义问题。")
    assert out == "他在重新定义问题。", out


def test_flip_drops_demonstrative():
    """「这不是A，是B」里的「这」不能留，否则出来是「这连接池打满了」。"""
    out, _ = humanize_text("这不是缓存的问题，而是连接池打满了。")
    assert out == "连接池打满了。", out


def test_flip_warranted_when_user_asked_about_it():
    """用户问的就是「缓存为什么没生效」，这时纠错句式是信息本体。"""
    text = "这不是缓存的问题，而是连接池打满了。"
    out, _ = humanize_text(text, context="Redis 缓存为什么没生效")
    assert out == text


def test_flip_warranted_on_shared_content_word():
    """用词不必完全一致：「钱的问题」对「是不是钱不够」也算有前提。"""
    text = "这不是钱的问题，是我自己的问题。"
    out, _ = humanize_text(text, context="是不是钱不够")
    assert out == text


def test_flip_deleted_when_context_unrelated():
    out, _ = humanize_text("这不是我的错，是环境的问题。", context="今天天气真好")
    assert "不是我的错" not in out


def test_sublimation_keeps_subject():
    """「不仅是A，更是B」→「这是B」：补上「这是」，别留个断头句。"""
    out, _ = humanize_text("这不仅仅是升级，更是团队成熟的必经之路。")
    assert out == "这是团队成熟的必经之路。", out


# ── 翻译腔与首句壳 ──────────────────────────────────────────


def test_leading_shell_removed():
    """「先说结论：」不带信息，只是把「我要开始了」演一遍。"""
    out, _ = humanize_text("先说结论：这个方案自动处理所有边界情况。")
    assert out == "这个方案自动处理所有边界情况。"


def test_translationese_topic_shell():
    out, _ = humanize_text("对于新手来说，先看日志就行。")
    assert out == "先看日志就行。"


# ── 清单与结构 ──────────────────────────────────────────────


def test_collapse_list_into_one_line():
    text = "看想吃哪类\n- 省事：番茄鸡蛋面\n- 下饭：青椒肉丝"
    out = collapse_multiline(text)
    assert "\n" not in out
    assert out.startswith("看想吃哪类，")


def test_collapse_keeps_single_line():
    assert collapse_multiline("嗯。") == "嗯。"


def test_keep_first_enumeration_leaves_one_slot():
    """问吃什么时只留一个选项，别把整张菜单拍平成一行。"""
    text = "看你想吃哪类，省事：番茄鸡蛋面、饺子，下饭：黄焖鸡，清淡：粥，几个人吃？"
    out = keep_first_enumeration(text)
    assert "省事" in out
    assert "下饭" not in out and "清淡" not in out
    assert "几个人吃" in out


def test_keep_first_enumeration_untouched_without_labels():
    text = "先别急，慢慢来，我在这儿。"
    assert keep_first_enumeration(text) == text


# ── 指标 ────────────────────────────────────────────────────


def test_metrics_shape():
    report = humanize_metrics(["嗯。", "作为一个 AI，我不能。"])
    assert report["samples"] == 2
    assert report["aiTellRate"] == 0.5
    assert report["cjkRate"] == 1.0
    assert report["maxChars"] >= report["medianChars"] >= 0


def test_metrics_empty_safe():
    report = humanize_metrics([])
    assert report["samples"] == 0
    assert report["aiTellRate"] == 0.0


def test_metrics_has_new_mechanical_fields():
    report = humanize_metrics(["这不是A，而是B。", "嗯。"])
    assert report["negationFlipRate"] == 0.5
    assert "enumerationRate" in report
    assert "dunhaoPerSentence" in report

# ── 复述开场（natural-talk D7）────────────────────────────────


def test_echo_clause_stripped():
    """「作业写到两点」→「凌晨两点还在写作业」是复述，真人直接接后半句。"""
    text = "这确实挺惨的——凌晨两点还在写作业，基本是在硬撑了。"
    out = strip_opening_echo(text, "崩不住了，作业写到两点")
    assert "写作业" not in out
    assert "硬撑" in out
    # 破折号留下的断口要收干净
    assert "——" not in out


def test_echo_leaves_new_content_alone():
    """换了说法的回应不算复述：「换谁都得困」是新内容。"""
    text = "确实，两点睡换谁都得困，早点躺吧。"
    assert strip_opening_echo(text, "昨天两点才睡，困死了") == text


def test_echo_keeps_reply_when_too_short():
    """砍完只剩几个字就别砍：宁可带一句复述也不能让回复断掉。"""
    assert strip_opening_echo("作业写到两点。", "作业写到两点") == "作业写到两点。"

# ── 标记行是功能指令，不是文字 ────────────────────────────


def test_marker_lines_survive_every_cleaner():
    """CARD 的 JSON 里 ", " 曾命中「逗号+引号」的标点收尾规则，JSON 碎掉、名片失效。"""
    text = '改好啦～\n<<<CARD {"user_id": 42, "card": "卷王苗苗"}>>>'
    assert humanize_text(text)[0] == text
    assert collapse_multiline(text) == text
    assert keep_first_enumeration(text) == text
    assert strip_opening_echo(text, "把群名片改成卷王苗苗") == text
