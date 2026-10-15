"""转发聊天记录（合并转发）解析与渲染的回归测试。

线上背景：有人在群里转一段聊天记录给学姐看，学姐只拿到一个 CQ 码，
只能瞎猜内容。这里钉住「取出来、渲染成模型能读的文本、带上限」这几件事。
"""

from __future__ import annotations

from types import SimpleNamespace

from dl_senpai.forwards import (
    MAX_CHARS,
    MAX_NODES,
    extract_forward_segments,
    forward_id_of,
    render_forward,
    render_node_content,
    resolve_forward_text,
)


def _seg(seg_type: str, **data: object) -> SimpleNamespace:
    return SimpleNamespace(type=seg_type, data=dict(data))


# ---------------- 段提取 ----------------

def test_extract_forward_segments_only_picks_forward() -> None:
    message = [
        _seg("text", text="你看这个"),
        _seg("forward", id="abc123"),
        _seg("file", file="a.pdf"),
        _seg("forward", id="def456"),
    ]
    found = extract_forward_segments(message)
    assert [d["id"] for d in found] == ["abc123", "def456"]


def test_extract_forward_segments_handles_none_and_empty() -> None:
    assert extract_forward_segments(None) == []
    assert extract_forward_segments([]) == []
    # 段里没有 data 的不要塞进来，否则后面取 id 时白白报错
    assert extract_forward_segments([_seg("forward")]) == []


def test_forward_id_of_tries_several_field_names() -> None:
    # OneBot 标准写 message_id，NapCat 给的是 id
    assert forward_id_of({"id": "x"}) == "x"
    assert forward_id_of({"message_id": "y"}) == "y"
    assert forward_id_of({"res_id": "z"}) == "z"
    assert forward_id_of({}) == ""


# ---------------- 单条内容渲染 ----------------

def test_render_node_content_renders_text_and_at() -> None:
    content = [_seg("text", text="你好 "), _seg("at", qq="123", name="小明")]
    assert render_node_content(content) == "你好 @小明"


def test_render_node_content_uses_placeholders_for_media() -> None:
    content = [_seg("image", file="a.jpg"), _seg("face", id=1)]
    assert render_node_content(content) == "[图片][表情]"


def test_render_node_content_does_not_leak_unknown_segments() -> None:
    # 认不出的段类型给空串：宁可不显示，也不能把原始 CQ 码吐给模型
    content = [_seg("text", text="前面"), _seg("mystery", raw="[CQ:mystery,x=1]")]
    assert render_node_content(content) == "前面"


def test_render_node_content_accepts_plain_string() -> None:
    assert render_node_content("直接是文本") == "直接是文本"
    assert render_node_content(None) == ""


def test_render_node_content_caps_a_single_long_message() -> None:
    out = render_node_content([_seg("text", text="啊" * 1000)])
    assert len(out) <= 300
    assert out.endswith("…")


# ---------------- 整段渲染 ----------------

def _node(nick: str, text: str, **extra: object) -> dict:
    return {
        "user_id": extra.get("user_id", "1"),
        "nickname": nick,
        "content": [_seg("text", text=text)],
    }


def test_render_forward_writes_one_line_per_message() -> None:
    nodes = [_node("小明", "在吗"), _node("小红", "在的")]
    out = render_forward(nodes)
    assert out == "小明：在吗\n小红：在的"


def test_render_forward_uses_sender_hint_when_node_has_no_name() -> None:
    nodes = [{"content": [_seg("text", text="没名字")]}]
    assert render_forward(nodes, sender_hint="失语") == "失语：没名字"


def test_render_forward_keeps_media_only_lines() -> None:
    # 只有图片的一条也要占一行，否则前后两句会被读成连着的
    nodes = [
        _node("小明", "看这个"),
        {"nickname": "小明", "content": [_seg("image", file="a.jpg")]},
        _node("小明", "好看吧"),
    ]
    out = render_forward(nodes)
    assert out.splitlines()[1] == "小明：[图片]"


def test_render_forward_marks_truncated_records() -> None:
    nodes = [_node("小明", "第%d条" % i) for i in range(MAX_NODES + 5)]
    out = render_forward(nodes)
    assert "还有 5 条没显示" in out
    # 超出部分不能真的渲染进来
    assert "第%d条" % (MAX_NODES + 1) not in out


def test_render_forward_caps_total_length() -> None:
    nodes = [_node("小明", "长" * 290) for _ in range(60)]
    assert len(render_forward(nodes)) <= MAX_CHARS


def test_render_forward_empty_input() -> None:
    assert render_forward([]) == ""


# ---------------- 取数据（假 bot） ----------------

class _FakeBot:
    """记录调用参数的假 bot。

    响应按「参数名:值」精确匹配（例如 "id:f1"、"message_id:f2"），
    而不是只看值——否则第一轮 id= 就命中，参数名回退那条分支永远测不到。
    """

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    async def call_api(self, api: str, **kwargs: object) -> object:
        self.calls.append({"api": api, **kwargs})
        if "id" in kwargs:
            key = "id:" + str(kwargs["id"])
        else:
            key = "message_id:" + str(kwargs.get("message_id"))
        if key in self.responses:
            return self.responses[key]
        raise RuntimeError("no such forward")


async def test_resolve_forward_text_renders_nodes() -> None:
    bot = _FakeBot({"id:f1": {"messages": [_node("小明", "你好")]}})
    out = await resolve_forward_text(bot, [{"id": "f1"}])
    assert out == "小明：你好"
    assert bot.calls[0]["api"] == "get_forward_msg"
    assert bot.calls[0]["id"] == "f1"


async def test_resolve_forward_text_falls_back_to_message_id() -> None:
    # 先试 id（报错），再试 message_id（成功）——两套实现的参数名都要能伺候
    bot = _FakeBot({"message_id:f2": {"messages": [_node("小红", "在的")]}})
    out = await resolve_forward_text(bot, [{"message_id": "f2"}])
    assert out == "小红：在的"
    assert [c.get("id") or c.get("message_id") for c in bot.calls] == ["f2", "f2"]


async def test_resolve_forward_text_returns_empty_on_total_failure() -> None:
    bot = _FakeBot({})
    assert await resolve_forward_text(bot, [{"id": "nope"}]) == ""


async def test_resolve_forward_text_without_bot_or_segments() -> None:
    assert await resolve_forward_text(None, [{"id": "x"}]) == ""
    assert await resolve_forward_text(_FakeBot({}), []) == ""


async def test_resolve_forward_text_accepts_message_key_and_objects() -> None:
    # 有的实现返回 message 而不是 messages，节点也可能是对象而非 dict
    node = SimpleNamespace(nickname="小明", content=[_seg("text", text="对象节点")])
    bot = _FakeBot({"id:f3": SimpleNamespace(message=[node])})
    assert await resolve_forward_text(bot, [{"id": "f3"}]) == "小明：对象节点"


# ---------------- 拼进用户输入 ----------------

def test_compose_user_text_splices_forward_before_current_sentence() -> None:
    from dl_senpai.legacy_bridge import _compose_user_text

    ctx = SimpleNamespace(raw_event=None, session_id="group:1")
    out = _compose_user_text(
        ctx, "你看这个", None, attachment_texts=None, forward_text="小明：你好"
    )
    assert out.startswith("【对方转发的聊天记录】\n小明：你好")
    assert out.endswith("【我现在说的】\n你看这个")


def test_compose_user_text_without_forward_is_unchanged() -> None:
    from dl_senpai.legacy_bridge import _compose_user_text

    ctx = SimpleNamespace(raw_event=None, session_id="group:1")
    out = _compose_user_text(
        ctx, "帮我看看", None, attachment_texts=[("a.txt", "正文")], forward_text=""
    )
    assert out == "【对方发来的文件：a.txt】\n正文\n\n【我现在说的】\n帮我看看"


# ---------------- json 卡片形态的合并转发 ----------------
#
# NapCat 在不同版本/配置下有两种投递形态：type=forward（标准）和
# type=json + com.tencent.multimsg 卡片。线上没实测出是哪一种，
# 所以两条路都要钉住——只认一种的话，另一种会让学姐看到一片空白。

def _multimsg_card(resid: str = "card-1") -> dict:
    """构造一张和 QQ 实际结构同形的合并转发卡片。"""
    import json

    payload = {
        "app": "com.tencent.multimsg",
        "meta": {"detail": {"resid": resid, "uniseq": "u-1", "source": "群聊"}},
        "prompt": "[聊天记录]",
    }
    return {"data": json.dumps(payload, ensure_ascii=False)}


def test_extract_forward_segments_accepts_multimsg_card() -> None:
    message = [_seg("text", text="看这个"), _seg("json", **_multimsg_card("res-9"))]
    found = extract_forward_segments(message)
    assert len(found) == 1
    # 归一化成带 id 的 dict，后面的取数逻辑不用关心来源形态
    assert forward_id_of(found[0]) == "res-9"


def test_extract_forward_segments_ignores_ordinary_cards() -> None:
    # 音乐、链接、小程序也是 json 段，绝不能当成聊天记录去拉
    import json

    music = json.dumps({"app": "com.tencent.music.lua", "meta": {}})
    link = json.dumps({"app": "com.tencent.structmsg", "meta": {}})
    found = extract_forward_segments([_seg("json", data=music), _seg("json", data=link)])
    assert found == []


def test_extract_forward_segments_survives_broken_card_json() -> None:
    # 载荷里带 multimsg 字样但 JSON 是坏的：不能抛异常，只能当没有
    assert extract_forward_segments([_seg("json", data="{multimsg broken")]) == []


def test_extract_forward_segments_accepts_dict_payload_and_deep_resid() -> None:
    # 有的实现给的是已经解析好的 dict，且 resid 埋得更深
    seg = _seg(
        "json",
        data={"app": "com.tencent.multimsg", "meta": {"detail": {"extra": {"resid": "deep-7"}}}},
    )
    found = extract_forward_segments([seg])
    assert [forward_id_of(d) for d in found] == ["deep-7"]


def test_multimsg_id_falls_back_to_uniseq() -> None:
    import json

    payload = json.dumps(
        {"app": "com.tencent.multimsg", "meta": {"detail": {"uniseq": "only-uni"}}}
    )
    found = extract_forward_segments([_seg("json", data=payload)])
    assert [forward_id_of(d) for d in found] == ["only-uni"]


async def test_resolve_forward_text_works_with_card_form() -> None:
    # 端到端：卡片形态 -> 取到节点 -> 渲染成文本
    bot = _FakeBot({"id:card-1": {"messages": [_node("小明", "卡片里的内容")]}})
    segments = extract_forward_segments([_seg("json", **_multimsg_card("card-1"))])
    assert await resolve_forward_text(bot, segments) == "小明：卡片里的内容"
