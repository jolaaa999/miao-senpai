from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dl_senpai.config import PluginConfig
from dl_senpai.llm import LLMError, SenpaiLLM, truncate_reply
from dl_senpai.persona import build_system_prompt


def test_system_prompt_modes() -> None:
    mention = build_system_prompt(interrupt=False)
    interrupt = build_system_prompt(interrupt=True)
    private = build_system_prompt(private_chat=True)
    vision = build_system_prompt(has_images=True)
    card = build_system_prompt(card_enable=True)
    assert "私聊" in private
    assert "被点名" not in private
    assert "私聊" not in mention
    assert "被点名" in mention
    assert "插嘴" in interrupt
    assert "识图" in vision
    assert "群名片" in card
    assert "<<<CARD" in card
    assert "深度学习" in mention
    assert "ACM" in mention
    assert "算法" in mention
    assert "安洁莉娜" in mention
    assert "学姐" in mention
    assert "百度迎新群" in mention
    assert "锐评" in mention or "炸毛" in mention
    assert "傲娇" in mention or "炸毛" in mention
    assert "【当前时间】" in mention
    assert "身份保密" in mention or "真人" in mention
    assert "<<<STICKER" in mention
    assert "活人感" in mention or "QQ" in mention
    trends = build_system_prompt(trends_brief="【近期热梗备忘】\n- [微博] x")
    assert "近期热梗用法" in trends
    assert "[微博] x" in trends
    search = build_system_prompt(search_enable=True)
    assert "联网检索" in search
    assert "<<<SEARCH" in search
    assert "联网检索" not in build_system_prompt(search_enable=False)
    draw = build_system_prompt(draw_enable=True)
    assert "生图" in draw
    assert "<<<DRAW" in draw


def test_truncate() -> None:
    assert truncate_reply("abc", 10) == "abc"
    assert truncate_reply("abcdefghij", 5) == "abcd…"


def test_provider_ollama_uses_env_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_Key", "sk-from-env")
    cfg = PluginConfig(
        LLM_PROVIDER="ollama",
        OLLAMA_API_KEY="",
        OLLAMA_BASE_URL="http://127.0.0.1:11434/v1",
        OLLAMA_MODEL="qwen2.5:7b",
        OPENAI_API_KEY="sk-relay",
        OPENAI_BASE_URL="https://relay.example/v1",
        OPENAI_MODEL="gpt-5.4",
    )
    assert cfg.provider() == "ollama"
    assert cfg.active_api_key() == "sk-from-env"
    assert cfg.active_base_url() == "http://127.0.0.1:11434/v1"
    assert cfg.active_model() == "qwen2.5:7b"
    assert cfg.api_configured()
    # 中转配置仍原样保留
    assert cfg.openai_api_key == "sk-relay"
    assert cfg.openai_model == "gpt-5.4"


@pytest.mark.asyncio
async def test_chat_builds_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = PluginConfig(
        LLM_PROVIDER="relay",
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="gpt-5.6",
        DL_SENPAI_TEMPERATURE=0.5,
        DL_SENPAI_MAX_TOKENS=100,
        DL_SENPAI_INTERRUPT_MAX_TOKENS=50,
        DL_SENPAI_REPLY_MAX_CHARS=500,
        DL_SENPAI_REPLY_HARD_MAX_CHARS=500,
    )
    llm = SenpaiLLM(cfg)

    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        msg = MagicMock()
        msg.content = "学姐来啦～过拟合就是记太死啦。"
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    llm._client = client

    reply = await llm.chat(
        [{"role": "user", "content": "之前：你好"}, {"role": "assistant", "content": "嗨"}],
        "什么是过拟合",
        interrupt=False,
        sender_name="小明",
    )
    assert "过拟合" in reply.text
    assert "～" in reply.text
    assert captured["model"] == "gpt-5.6"
    assert captured["temperature"] == 0.5
    assert captured["max_tokens"] == 100
    assert captured["messages"][0]["role"] == "system"
    assert "被点名" in captured["messages"][0]["content"]
    assert captured["messages"][-1]["content"].startswith("小明：")


@pytest.mark.asyncio
async def test_chat_with_images() -> None:
    cfg = PluginConfig(
        LLM_PROVIDER="relay",
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="gpt-5.4",
        DL_SENPAI_VISION_ENABLE=True,
    )
    llm = SenpaiLLM(cfg)
    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        msg = MagicMock()
        msg.content = "图里是一只猫哦。"
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    llm._client = client

    reply = await llm.chat(
        [],
        "（发了一张图，请看看）",
        sender_name="小明",
        image_urls=["https://example.com/cat.jpg"],
    )
    assert "猫" in reply.text
    user_content = captured["messages"][-1]["content"]
    assert isinstance(user_content, list)
    assert user_content[1]["type"] == "image_url"
    assert "识图" in captured["messages"][0]["content"]


@pytest.mark.asyncio
async def test_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_Key", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = PluginConfig(
        LLM_PROVIDER="relay",
        OPENAI_API_KEY="",
        OLLAMA_API_KEY="",
    )
    llm = SenpaiLLM(cfg)
    with pytest.raises(LLMError):
        await llm.chat([], "hi")


@pytest.mark.asyncio
async def test_503_retries_then_friendly_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from openai import APIStatusError

    monkeypatch.setattr("dl_senpai.llm.asyncio.sleep", AsyncMock())

    cfg = PluginConfig(
        LLM_PROVIDER="relay",
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="gpt-5.4",
    )
    llm = SenpaiLLM(cfg)

    err = APIStatusError(
        message="Service temporarily unavailable",
        response=MagicMock(status_code=503, headers={}),
        body={"error": {"message": "Service temporarily unavailable", "type": "api_error"}},
    )
    err.status_code = 503  # type: ignore[attr-defined]

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=err)
    llm._client = client

    with pytest.raises(LLMError) as ei:
        await llm.chat([], "hi", sender_name="小明")
    assert "有些事情要做" in str(ei.value)
    assert client.chat.completions.create.await_count == 4


@pytest.mark.asyncio
async def test_502_does_not_fall_back_to_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    from openai import APIStatusError

    monkeypatch.setattr("dl_senpai.llm.asyncio.sleep", AsyncMock())

    cfg = PluginConfig(
        LLM_PROVIDER="relay",
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="gpt-5.4",
        OLLAMA_BASE_URL="http://127.0.0.1:11434/v1",
        OLLAMA_MODEL="qwen2.5:7b",
    )
    llm = SenpaiLLM(cfg)

    err = APIStatusError(
        message="Bad gateway",
        response=MagicMock(status_code=502, headers={}),
        body={"error": {"message": "Bad gateway", "type": "api_error"}},
    )
    err.status_code = 502  # type: ignore[attr-defined]

    relay = MagicMock()
    relay.chat.completions.create = AsyncMock(side_effect=err)
    llm._client = relay

    ollama = MagicMock()
    ollama.chat.completions.create = AsyncMock()
    llm._ollama_client = ollama

    with pytest.raises(LLMError) as ei:
        await llm.chat([], "你好", sender_name="小明")
    assert "有些事情要做" in str(ei.value)
    assert relay.chat.completions.create.await_count == 4
    assert ollama.chat.completions.create.await_count == 0


@pytest.mark.asyncio
async def test_chat_parses_card_marker() -> None:
    cfg = PluginConfig(
        LLM_PROVIDER="relay",
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="gpt-5.4",
    )
    llm = SenpaiLLM(cfg)

    async def fake_create(**kwargs: Any) -> MagicMock:
        msg = MagicMock()
        msg.content = (
            "改好啦～\n"
            '<<<CARD {"user_id": 42, "card": "卷王苗苗", "kind": "request"}>>>'
        )
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    llm._client = client

    result = await llm.chat(
        [],
        "给我改个群名片",
        sender_name="小明",
        card_enable=True,
        card_candidates_prompt="【名片候选】\n- sender: 小明 user_id=42",
    )
    assert "改好啦" in result.text
    assert "<<<CARD" not in result.text
    assert len(result.card_actions) == 1
    assert result.card_actions[0].user_id == 42
    assert result.card_actions[0].card == "卷王苗苗"


@pytest.mark.asyncio
async def test_chat_parses_sticker_marker() -> None:
    cfg = PluginConfig(
        LLM_PROVIDER="relay",
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="gpt-5.4",
    )
    llm = SenpaiLLM(cfg)

    async def fake_create(**kwargs: Any) -> MagicMock:
        msg = MagicMock()
        msg.content = "你才是猫娘！\n<<<STICKER 炸毛>>>"
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    llm._client = client

    result = await llm.chat([], "臭猫娘", sender_name="小明")
    assert "猫娘" in result.text
    assert "<<<STICKER" not in result.text
    assert result.sticker_force is True
    assert "炸毛" in result.sticker_query


@pytest.mark.asyncio
async def test_chat_search_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = PluginConfig(
        LLM_PROVIDER="relay",
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="gpt-5.4",
        DL_SENPAI_SEARCH_ENABLE=True,
        DL_SENPAI_SEARCH_MAX_ROUNDS=2,
    )
    llm = SenpaiLLM(cfg)
    calls = {"n": 0}

    async def fake_create(**kwargs: Any) -> MagicMock:
        calls["n"] += 1
        msg = MagicMock()
        if calls["n"] == 1:
            msg.content = "学姐去瞅一眼～\n<<<SEARCH 今日油价>>>"
        else:
            msg.content = "刚看了一眼，油价大概还在浮动，别当投资建议哦～"
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    llm._client = client

    searcher = MagicMock()
    searcher.search_many = AsyncMock(
        return_value="【联网检索结果】查询：今日油价\n1. 示例\n   今日参考价……"
    )
    monkeypatch.setattr("dl_senpai.llm.searcher_from_config", lambda _cfg: searcher)

    result = await llm.chat([], "今天油价多少", sender_name="小明")
    assert result.searched is True
    assert "油价" in result.text
    assert "<<<SEARCH" not in result.text
    assert client.chat.completions.create.await_count == 2
    searcher.search_many.assert_awaited_once()
    called_queries = searcher.search_many.await_args.kwargs.get("queries") or searcher.search_many.await_args.args[0]
    assert called_queries[0] == "今日油价"


@pytest.mark.asyncio
async def test_chat_auto_search_prefetch(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = PluginConfig(
        LLM_PROVIDER="relay",
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="gpt-5.4",
        DL_SENPAI_SEARCH_ENABLE=True,
        DL_SENPAI_SEARCH_AUTO=True,
        DL_SENPAI_SEARCH_MAX_ROUNDS=0,
    )
    llm = SenpaiLLM(cfg)

    async def fake_create(**kwargs: Any) -> MagicMock:
        msg = MagicMock()
        msg.content = "刚看了一眼，今天北京大概多云～"
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    llm._client = client

    searcher = MagicMock()
    searcher.search_many = AsyncMock(
        return_value="【联网检索结果】查询：今天北京天气\n1. 北京 多云 20℃"
    )
    monkeypatch.setattr("dl_senpai.web_search.searcher_from_config", lambda _cfg: searcher)

    result = await llm.chat([], "今天北京天气怎么样", sender_name="小明")
    assert result.searched is True
    assert "多云" in result.text
    searcher.search_many.assert_awaited_once()
    user_msg = client.chat.completions.create.await_args.kwargs["messages"][-1][
        "content"
    ]
    assert "联网检索结果" in user_msg
    assert client.chat.completions.create.await_count == 1


@pytest.mark.asyncio
async def test_chat_grounding_retry_on_fabricated_gear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = PluginConfig(
        LLM_PROVIDER="relay",
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="gpt-5.4",
        DL_SENPAI_SEARCH_ENABLE=True,
        DL_SENPAI_SEARCH_AUTO=True,
        DL_SENPAI_SEARCH_MAX_ROUNDS=0,
    )
    llm = SenpaiLLM(cfg)
    calls = {"n": 0}

    async def fake_create(**kwargs: Any) -> MagicMock:
        calls["n"] += 1
        msg = MagicMock()
        if calls["n"] == 1:
            msg.content = "前期可以先用耳塞羽饰，再补强弓珠～"
        else:
            msg.content = "搜了没查到「耳塞羽饰」这装备名，别信我瞎编的，强弓珠方向倒是对的。"
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    llm._client = client

    searcher = MagicMock()
    searcher.search_many = AsyncMock(
        return_value="【联网检索结果】\n1. 冰弓配装\n   强弓珠 耳塞技能"
    )
    monkeypatch.setattr("dl_senpai.web_search.searcher_from_config", lambda _cfg: searcher)

    result = await llm.chat([], "冰弓前期配装怎么搭", sender_name="小明")
    assert client.chat.completions.create.await_count == 2
    assert "耳塞羽饰" in result.text or "没查到" in result.text or "瞎编" in result.text
