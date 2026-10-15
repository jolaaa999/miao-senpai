"""回复纯净性（第 9 项，2026-10-15）：三条会污染用户可见内容与历史的根因。

1. style.strip_thinking_prefix 的措辞表覆盖不到「用户问…」「检索结果里有…」，
   思考前言原样发出去（群记忆里能看到用户把它引用回来当把柄）；
2. 伪指令闭合符只认 >>>，模型写 >> / > > > / ＞＞ 时 <<<IMPRESSION {...} 漏进正文；
3. 历史里存了渲染态（【引用消息】/【我现在说的】），下一轮又喂回 prompt。

每条都取自线上真实文本，不是构造的边角案例。
"""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pytest

from dl_senpai import legacy_bridge as bridge
from dl_senpai import style as style_mod
from dl_senpai.person_memory import strip_impression_markers


# ---------------- 1. 思考前言 ----------------

# 线上原文（群 958691688，2026-10-14）：思考 + end + 真实回复
CASE_LNO = (
    "用户问的是《佐佐木与文鸟小哔》这部作品。检索结果里有清晰的信息："
    "轻小说，2018年12月起KAKUYOMU连载。\n\n可以说了。语气可以聊得随意一点。\n\n"
    "回答要口语，别太长。短一点，两三条。end"
    "知道呀，社畜佐佐木去宠物店买了只文鸟，结果小哔是异世界贤者转生过来的。"
)
CASE_OW = (
    "用户问10月14号（今天）的热补丁：路霸回调是不是真的。\n\n"
    "检索结果里有多个10月14日的热补丁视频标题，明确写着：路霸加强。\n\n"
    "我要说清方向，不给具体数字。短一点，别太长。\n\n注意别用markdown。end"
    "行，这次搜到了，是我上回嘴硬了。"
)


def test_thinking_prefix_stripped_for_production_wording_lno() -> None:
    out = style_mod.strip_thinking_prefix(CASE_LNO)
    assert out.startswith("知道呀")
    assert "检索结果里" not in out


def test_thinking_prefix_stripped_for_production_wording_owl() -> None:
    out = style_mod.strip_thinking_prefix(CASE_OW)
    assert out.startswith("行，这次搜到了")
    assert "用户问" not in out


def test_thinking_prefix_reached_through_polish_reply() -> None:
    """真实入口也要生效：polish_reply 是发送前必经的那一步。"""
    assert style_mod.polish_reply(CASE_LNO).startswith("知道呀")


def test_plain_reply_without_delimiter_is_untouched() -> None:
    """没有分隔符时一个字都不许动 —— 这是防误伤的另一半判据。"""
    text = "用户问我是不是猫娘？学姐才不是猫娘呢。"
    assert style_mod.strip_thinking_prefix(text) == text


def test_reply_with_end_but_no_plan_wording_is_untouched() -> None:
    text = "这个故事没有 end，是开放式结局。"
    assert style_mod.strip_thinking_prefix(text) == text


# ---------------- 1b. 昵称带点时伪造转录也要截断 ----------------

def test_fake_transcript_cut_when_nickname_contains_dot() -> None:
    """线上真实形态：昵称「失语.」带点，原字符集不匹配，554 字转录一条没截。"""
    text = "学姐呀，群里的，计算机这边混的。\n\n失语.：你是猫娘吗\n失语.：猫娘吗\n\n才不是呢。"
    assert style_mod.strip_fake_transcript(text) == "学姐呀，群里的，计算机这边混的。"


# ---------------- 2. 控制标记漏进正文 ----------------

@pytest.mark.parametrize(
    "close",
    [">>", ">>>", "> > >", "＞＞", "】】"],
)
def test_marker_close_pattern_covers_model_variants(close: str) -> None:
    """结构守卫：容错常量必须覆盖模型实际写过的所有闭合形态。"""
    pattern = re.compile(style_mod.MARKER_CLOSE_PATTERN)
    assert pattern.match(close), close


@pytest.mark.parametrize(
    "raw",
    [
        '正文\n\n<<<IMPRESSION {"vibe":"损友"}>>',
        '正文\n\n<<<IMPRESSION {"vibe":"损友"}>>>',
        '正文\n\n<<<IMPRESSION {"vibe":"损友"}> > >',
        "正文\n\n<<<IMPRESSION>>>",
        "正文\n\n<<<IMPRESSION",
    ],
)
def test_impression_marker_never_reaches_body(raw: str) -> None:
    cleaned = style_mod.polish_reply(raw)
    assert "<<<" not in cleaned
    assert "正文" in cleaned


def test_mute_marker_never_reaches_body() -> None:
    cleaned = style_mod.polish_reply('好的\n\n<<<MUTE {"user_id":1,"duration":60}>>')
    assert "<<<" not in cleaned
    assert "好的" in cleaned


@pytest.mark.parametrize("close", [">>", ">>>", "> > >", "＞＞", "】】"])
def test_impression_payload_still_parsed_with_loose_close(close: str) -> None:
    """画像必须还能写进去：少一个 > 不该让整条印象丢失。"""
    cleaned = strip_impression_markers(f'你好呀\n\n<<<IMPRESSION {{"vibe":"热心"}}{close}')
    assert cleaned == "你好呀"


def test_person_memory_reuses_the_shared_close_pattern() -> None:
    """结构守卫：闭合符容错只有一处定义，别再各写一份。"""
    from dl_senpai import person_memory as pm

    assert "MARKER_CLOSE_PATTERN" in inspect.getsource(pm)


# ---------------- 3. 历史存干净原话 ----------------

def test_history_user_text_prefers_clean_plain_text() -> None:
    ctx = SimpleNamespace(
        plain_text="这句话是我说的",
        user_text="【引用消息】小明：上一句说了啥\n\n【我现在说的】这句话是我说的",
    )
    assert bridge._history_user_text(ctx) == "这句话是我说的"


def test_history_user_text_falls_back_when_only_quoted() -> None:
    """只引用、自己没打字时 plain_text 是空的，那时渲染态是唯一信息来源。"""
    ctx = SimpleNamespace(
        plain_text="",
        user_text="【引用消息】小明：看这张图\n\n【我现在说的】（只引用了上面这条，自己没另打字）",
    )
    assert "【引用消息】" in bridge._history_user_text(ctx)


def test_history_user_text_tolerates_missing_fields() -> None:
    assert bridge._history_user_text(SimpleNamespace()) == ""


def test_record_turn_uses_history_helper() -> None:
    """结构守卫：落盘必须走 _history_user_text，否则脚手架又进历史。"""
    src = inspect.getsource(bridge._deliver_runtime_reply)
    assert "_history_user_text(agent_context)" in src
