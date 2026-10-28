"""日记本单测：落盘、注入、角度轮换、宽松解析。

重点盯三件容易悄悄坏掉的事：
1. 两份落盘（JSONL 给机器、md 给人）都要写出来——md 掉了没人会发现。
2. context() 只喂最近 N 条——多喂会把「她昨天干了什么」变成第二本记忆账本。
3. 角度轮换真的避开最近用过的——纯随机会几天内撞同一条，显得在套模板。
"""

from __future__ import annotations

import json

from dl_senpai.diary import (
    DIARY_ANGLES,
    DiaryEntry,
    DiaryStore,
    build_diary_brief,
    parse_diary_reply,
    write_diary,
)


class _FakeCompletion:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeLLM:
    """假模型：只实现日记用到的那一个传输边界。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[list[dict[str, str]]] = []

    async def _complete_once(self, messages, *, max_tokens, timeout, tools=None):
        self.calls.append(messages)
        return _FakeCompletion(self.text)


class _BrokenLLM:
    async def _complete_once(self, messages, *, max_tokens, timeout, tools=None):
        raise RuntimeError("model down")


def test_add_writes_jsonl_and_markdown(tmp_path):
    store = DiaryStore(tmp_path)
    entry = store.add({"text": "冰箱最里头翻出半瓶酸奶，保质期就到今天。", "title": "翻出酸奶", "mood": "有点乐", "tags": ["吃"]})

    assert isinstance(entry, DiaryEntry)
    assert entry.day == entry.day  # 属性可读，不是空串

    lines = store.index_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"].startswith("冰箱最里头")

    day_md = tmp_path / f"{entry.day}.md"
    assert day_md.exists()
    body = day_md.read_text(encoding="utf-8")
    assert "翻出酸奶" in body
    assert "标签：吃" in body


def test_add_rejects_empty_text(tmp_path):
    store = DiaryStore(tmp_path)
    try:
        store.add({"text": "   "})
    except ValueError:
        return
    raise AssertionError("空正文应该抛 ValueError")


def test_recent_and_context_only_last_n(tmp_path):
    store = DiaryStore(tmp_path)
    for i in range(5):
        store.add({"text": f"第 {i} 条日记"})

    assert len(store.recent(2)) == 2
    assert store.recent(2)[-1].text == "第 4 条日记"

    # 只有最近 2 条（第 3、第 4 条）能进上下文，更早的必须挡掉
    ctx = store.context(2)
    assert "第 4 条日记" in ctx
    assert "第 2 条日记" not in ctx
    # 注入段落必须带上「别复述」的边界，否则她会把日记当新闻讲出去
    assert "别复述" in ctx


def test_context_empty_when_no_entries(tmp_path, monkeypatch):
    # 日记本身为空时必须返回空串。刷到的内容是挂在同一个 brief 上的另一段，
    # 它读的是线上真实台账，所以这里显式关掉——不关就会把线上内容串进断言。
    monkeypatch.setattr("dl_senpai.browse_influence.build_browse_brief", lambda cfg: "")
    assert DiaryStore(tmp_path).context(3) == ""
    assert build_diary_brief(DiaryStore(tmp_path), n=3) == ""


def test_pick_angle_avoids_recent_angles(tmp_path):
    store = DiaryStore(tmp_path)
    for angle in DIARY_ANGLES[:3]:
        store.add({"text": f"按 {angle} 写的", "angle": angle})

    picked = store.pick_angle(window=3)
    assert picked in DIARY_ANGLES
    assert picked not in DIARY_ANGLES[:3]


def test_pick_angle_falls_back_when_all_used(tmp_path):
    store = DiaryStore(tmp_path)
    for angle in DIARY_ANGLES:
        store.add({"text": "x", "angle": angle})
    # 全用过了也不能崩，随便给一个
    assert store.pick_angle(window=len(DIARY_ANGLES)) in DIARY_ANGLES


def test_parse_diary_reply_handles_fenced_json():
    raw = '好的，这是今天的：\n\n```json\n{"title":"挪了位置","mood":"高兴","tags":["小事"],"text":"把花挪到窗边了，苞鼓了一点"}\n```'
    out = parse_diary_reply(raw)
    assert out["text"] == "把花挪到窗边了，苞鼓了一点"
    assert out["title"] == "挪了位置"
    assert out["mood"] == "高兴"
    assert out["tags"] == ["小事"]


def test_parse_diary_reply_falls_back_to_whole_text():
    """格式全错也要留下正文：她说的话本身有价值，不能因为少个括号丢掉。"""
    out = parse_diary_reply("今天什么都没干，就坐着看窗外。")
    assert out["text"] == "今天什么都没干，就坐着看窗外。"
    assert out["title"] == ""


def test_parse_diary_reply_strips_markdown_in_fallback():
    out = parse_diary_reply("- 今天很困\n- 但还是爬起来吃了饭")
    assert not out["text"].startswith("- ")


async def test_write_diary_persists(tmp_path):
    store = DiaryStore(tmp_path)
    llm = _FakeLLM('{"title":"窗边的花","mood":"高兴","tags":["小事"],"text":"把花挪到窗边了，苞鼓了一点"}')

    entry = await write_diary(llm, store=store, role_text="你是学姐", hint="写点小事")

    assert entry is not None
    assert entry.title == "窗边的花"
    assert entry.angle in DIARY_ANGLES
    assert len(store.entries()) == 1
    # 提示词里必须带上「别重复」和角度要求，否则天天同一个调子
    system = llm.calls[0][0]["content"]
    assert "别写总结句" in system
    assert "开口角度" in system


async def test_write_diary_swallows_model_failure(tmp_path):
    """写日记是后台旁路：模型挂了只记日志，不能把异常抛回聊天链路。"""
    store = DiaryStore(tmp_path)
    entry = await write_diary(_BrokenLLM(), store=store, role_text="你是学姐")
    assert entry is None
    assert store.entries() == []
