"""长文合并转发的切分守卫。

盯的是「切点要落在语义边界上」这件事：小标题不能和它的正文分开，
一句话不能被劈成两截，每一节都要能单独读懂——因为 QQ 的聊天记录卡片
不展开全文，切错了就是真的丢了上下文。
"""

from __future__ import annotations

from dl_senpai.reply_send import split_forward_nodes


def test_short_text_single_node():
    """短文本不切，交给调用方的阈值逻辑判断。"""
    assert split_forward_nodes("就一句话。", target_chars=600) == ["就一句话。"]


def test_empty_returns_empty():
    assert split_forward_nodes("") == []
    assert split_forward_nodes("   \n\n  ") == []


def test_heading_kept_with_its_body():
    """小标题和紧跟的正文必须在同一节里。"""
    text = "\n\n".join([
        "一、结论" + "。这里是对结论的解释，" * 20,
        "二、理由" + "。这是第二条的解释，" * 20,
    ])
    # 目标字数调小才能逼出两节：默认 600 时这两段本来就该合并
    nodes = split_forward_nodes(text, target_chars=300)
    assert len(nodes) == 2
    assert nodes[0].startswith("一、结论")
    assert nodes[1].startswith("二、理由")


def test_each_node_within_limit():
    text = "\n\n".join(f"{i}. 第{i}条要点。" + "补充说明，" * 30 for i in range(1, 8))
    nodes = split_forward_nodes(text, target_chars=600)
    assert nodes
    for node in nodes:
        # 允许单块本身超限（一句极长的话没法再切），但正常段落必须守住
        assert len(node) <= 700


def test_never_cuts_sentence_in_half():
    """每节都以句子结束符收尾，不出现半句话。"""
    text = "。".join(f"第{i}句话说完了" for i in range(1, 60)) + "。"
    nodes = split_forward_nodes(text, target_chars=300)
    for node in nodes:
        assert node.rstrip().endswith("。"), node[-20:]


def test_nodes_merged_to_max():
    """节点数有上限，太碎会往后合并。"""
    text = "\n\n".join(f"{i}. 短要点。" for i in range(1, 40))
    nodes = split_forward_nodes(text, target_chars=60, max_nodes=8)
    assert len(nodes) <= 8


def test_paragraphs_combined_when_they_fit():
    """两个短段落能塞进一节就别拆开，少点一次折叠。"""
    text = "第一段很短。\n\n第二段也很短。"
    nodes = split_forward_nodes(text, target_chars=600)
    assert nodes == ["第一段很短。\n\n第二段也很短。"]


async def test_long_text_sends_forward_card(monkeypatch):
    """超过阈值的长文必须走合并转发，而不是连发气泡。"""
    from dl_senpai import reply_send

    captured: dict[str, object] = {}

    async def fake_forward(bot, event, nodes):
        captured["nodes"] = nodes
        return {"ok": True}

    async def fake_send(bot, event, message):
        captured.setdefault("plain", []).append(message)
        return True

    monkeypatch.setattr(reply_send, "_send_forward", fake_forward)
    monkeypatch.setattr(reply_send, "_guarded_send", fake_send)

    bot = type("FakeBot", (), {"self_id": "2480465844"})()
    text = "\n\n".join(
        [f"{i}. 第{i}条要点。" + "补充说明，" * 40 for i in range(1, 4)]
    )
    assert len(text) > 500

    await reply_send._send_text_reply_once(
        bot,
        None,
        text,
        sticker=None,
        voice=None,
        image=None,
        reply_max_chars=1500,
        reply_chunk_chars=900,
        nickname="学姐",
        forward_min_chars=500,
        forward_node_chars=600,
    )

    assert "nodes" in captured, "长文没有走合并转发"
    assert len(captured["nodes"]) >= 2
    assert "plain" not in captured


async def test_short_text_stays_plain(monkeypatch):
    """没到阈值就走普通消息，别为了两句话发张卡片。"""
    from dl_senpai import reply_send

    captured: dict[str, object] = {}

    async def fake_forward(bot, event, nodes):
        captured["nodes"] = nodes
        return {"ok": True}

    async def fake_send(bot, event, message):
        captured["plain"] = message
        return True

    monkeypatch.setattr(reply_send, "_send_forward", fake_forward)
    monkeypatch.setattr(reply_send, "_guarded_send", fake_send)

    bot = type("FakeBot", (), {"self_id": "2480465844"})()
    await reply_send._send_text_reply_once(
        bot,
        None,
        "学姐在呢，这句很短。",
        sticker=None,
        voice=None,
        image=None,
        reply_max_chars=1500,
        reply_chunk_chars=900,
        nickname="学姐",
        forward_min_chars=500,
        forward_node_chars=600,
    )

    assert "plain" in captured
    assert "nodes" not in captured


async def test_forward_failure_falls_back_to_sequential(monkeypatch):
    """卡片发不出去时必须退回逐条发送，不能把回复弄丢。"""
    from dl_senpai import reply_send

    async def boom(bot, event, nodes):
        raise RuntimeError("forward api unavailable")

    sent: list[object] = []

    async def fake_send(bot, event, message):
        sent.append(message)
        return True

    monkeypatch.setattr(reply_send, "_send_forward", boom)
    monkeypatch.setattr(reply_send, "_guarded_send", fake_send)

    bot = type("FakeBot", (), {"self_id": "2480465844"})()
    text = "\n\n".join([f"{i}. 要点。" + "补充，" * 60 for i in range(1, 4)])

    await reply_send._send_text_reply_once(
        bot,
        None,
        text,
        sticker=None,
        voice=None,
        image=None,
        reply_max_chars=1500,
        reply_chunk_chars=900,
        nickname="学姐",
        forward_min_chars=500,
        forward_node_chars=600,
    )

    assert sent, "卡片失败后什么都没发出去"



async def test_mid_length_split_into_two_nodes(monkeypatch):
    """400~600 字之间按 600 只能切出一节，要自动对半切成两节。"""
    from dl_senpai import reply_send

    captured = {}

    async def fake_forward(bot, event, nodes):
        captured["nodes"] = nodes
        return {"ok": True}

    async def fake_send(bot, event, message):
        return True

    monkeypatch.setattr(reply_send, "_send_forward", fake_forward)
    monkeypatch.setattr(reply_send, "_guarded_send", fake_send)

    bot = type("FakeBot", (), {"self_id": "2480465844"})()
    text = "\n\n".join(["第一段。" + "补充说明，" * 45, "第二段。" + "解释清楚，" * 45])
    assert 400 < len(text) < 600, len(text)

    await reply_send._send_text_reply_once(
        bot, None, text, sticker=None, voice=None, image=None,
        reply_max_chars=1500, reply_chunk_chars=900, nickname="学姐",
        forward_min_chars=400, forward_node_chars=600,
    )

    assert "nodes" in captured
    assert len(captured["nodes"]) >= 2

