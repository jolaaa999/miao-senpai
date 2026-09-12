from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from dl_senpai.config import PluginConfig
from dl_senpai.image_gen import (
    DrawCooldown,
    build_angelina_appearance_prompt,
    build_angelina_outfit_prompt,
    classify_senpai_draw,
    extract_draw_prompt_from_user,
    extract_outfit_description,
    looks_like_appearance_request,
    looks_like_draw_request,
    looks_like_outfit_request,
    parse_draw_request,
    resolve_draw_prompt,
    wants_senpai_image,
)
from dl_senpai.llm import SenpaiLLM
from dl_senpai.persona import build_system_prompt


def test_parse_draw_request() -> None:
    parsed = parse_draw_request("好呀，学姐来画～\n<<<DRAW 一只橘猫趴在键盘上，日系插画>>>")
    assert parsed.force is True
    assert "橘猫" in parsed.prompt
    assert "DRAW" not in parsed.text


def test_looks_like_draw_request() -> None:
    assert looks_like_draw_request("@学姐 画一张猫")
    assert looks_like_draw_request("帮我生图：赛博朋克城市")
    assert not looks_like_draw_request("别画图了")
    assert not looks_like_draw_request("你是谁")


def test_looks_like_appearance_request() -> None:
    assert looks_like_appearance_request("@学姐 你长什么样")
    assert looks_like_appearance_request("发张自拍看看")
    assert not looks_like_appearance_request("别发照片了")


def test_looks_like_outfit_request() -> None:
    assert looks_like_outfit_request("@学姐 给你换一身jk制服")
    assert looks_like_outfit_request("学姐穿白色连衣裙试试")
    assert looks_like_outfit_request("我想看学姐穿绿色洛丽塔的样子")
    assert not looks_like_outfit_request("别换衣服了")
    assert not looks_like_outfit_request("大师位可制作装")
    assert not wants_senpai_image(
        "你最好去怪物猎人世界wiki里面搜索再回答"
    )


def test_parse_draw_request_multiline() -> None:
    parsed = parse_draw_request(
        "好啦～\n<<<DRAW Angelina inspired girl,\n"
        "wearing green lolita dress,\n"
        "full body anime illustration>>>"
    )
    assert parsed.force is True
    assert "lolita" in parsed.prompt.lower()
    assert "DRAW" not in parsed.text
    assert "<<<" not in parsed.text


def test_strip_draw_markers_fallback() -> None:
    from dl_senpai.image_gen import strip_draw_markers

    text = "哼～\n<<<DRAW broken marker without proper close"
    cleaned = strip_draw_markers(text)
    assert "DRAW" not in cleaned
    assert "哼" in cleaned


def test_wants_senpai_image() -> None:
    assert wants_senpai_image("画一张猫")
    assert wants_senpai_image("你长什么样")
    assert wants_senpai_image("给你换汉服")
    assert wants_senpai_image("我想看学姐穿绿色洛丽塔的样子")


def test_extract_outfit_description() -> None:
    assert "jk" in extract_outfit_description("@学姐 给你换一身jk制服").lower()
    assert "白色连衣裙" in extract_outfit_description("学姐穿白色连衣裙试试")
    assert "绿色洛丽塔" in extract_outfit_description("我想看学姐穿绿色洛丽塔的样子")


def test_normalize_outfit_green_lolita() -> None:
    from dl_senpai.image_gen import normalize_outfit_for_prompt

    assert "green" in normalize_outfit_for_prompt("绿色洛丽塔").lower()
    assert "lolita" in normalize_outfit_for_prompt("绿色洛丽塔").lower()
    assert "no red" in normalize_outfit_for_prompt("绿色洛丽塔").lower()


def test_resolve_outfit_prompt() -> None:
    prompt = resolve_draw_prompt(
        marker_prompt="",
        user_request="@学姐 给你换一身白色连衣裙",
        reply_text="行，试试这件",
        max_chars=800,
    )
    assert "Angelina" in prompt
    assert "white" in prompt.lower()
    assert "NOT" in prompt and "red" in prompt.lower()


def test_outfit_user_request_overrides_generic_marker() -> None:
    prompt = resolve_draw_prompt(
        marker_prompt=(
            "Angelina inspired girl with pink hair, fox ears, "
            "wearing her default red messenger jacket and tactical bag"
        ),
        user_request="我想看学姐穿绿色洛丽塔的样子",
        reply_text="给你看更精致的版本",
        max_chars=800,
    )
    lower = prompt.lower()
    assert "green" in lower
    assert "lolita" in lower
    assert "entirely green" in lower or "vivid green" in lower
    assert "face reference only" in lower


def test_resolve_appearance_prompt() -> None:
    prompt = resolve_draw_prompt(
        marker_prompt="",
        user_request="@学姐 你长什么样，发张图看看",
        reply_text="哼，给你看啦",
        max_chars=500,
    )
    assert "Angelina" in prompt
    assert "pink hair" in prompt


def test_resolve_draw_prompt_prefers_marker() -> None:
    prompt = resolve_draw_prompt(
        marker_prompt="warm sunset beach",
        user_request="画一张海边",
        reply_text="画好啦",
        max_chars=200,
    )
    assert prompt == "warm sunset beach"


def test_build_angelina_prompts() -> None:
    appearance = build_angelina_appearance_prompt()
    assert "Angelina" in appearance
    outfit = build_angelina_outfit_prompt("green lolita dress")
    assert "green lolita dress" in outfit
    assert "face reference only" in outfit
    assert "NOT Arknights default costume" in outfit


def test_extract_draw_prompt_from_user() -> None:
    assert "赛博朋克城市夜景" in extract_draw_prompt_from_user(
        "@学姐 帮我画一张赛博朋克城市夜景"
    )


def test_classify_senpai_draw() -> None:
    assert classify_senpai_draw("你长什么样") == "appearance"
    assert classify_senpai_draw("给你换jk制服") == "outfit"
    assert classify_senpai_draw("我想看学姐穿绿色洛丽塔的样子") == "outfit"
    assert classify_senpai_draw("画一只猫") == "general"


def test_draw_cooldown() -> None:
    cd = DrawCooldown()
    assert cd.ready("s1", 30, now=100.0) is True
    cd.mark("s1", now=100.0)
    assert cd.ready("s1", 30, now=120.0) is False
    assert cd.ready("s1", 30, now=131.0) is True
    # 0 / 负数：关闭冷却，始终可画（便于多并发生图）
    assert cd.ready("s1", 0, now=120.0) is True
    assert cd.ready("s1", -1, now=120.0) is True


def test_draw_config_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DL_SENPAI_DRAW_API_KEY", raising=False)
    cfg = PluginConfig(
        OPENAI_API_KEY="sk-relay",
        OPENAI_BASE_URL="https://relay.example/v1",
        DL_SENPAI_DRAW_MODEL="flux-dev",
        DL_SENPAI_DRAW_API_KEY="",
    )
    assert cfg.draw_api_key_resolved() == "sk-relay"
    assert cfg.draw_base_url_resolved() == "https://relay.example/v1"
    assert cfg.draw_model_resolved() == "flux-dev"
    assert cfg.draw_configured() is True


def test_system_prompt_draw_mode() -> None:
    draw = build_system_prompt(draw_enable=True)
    assert "生图" in draw
    assert "<<<DRAW" in draw
    assert "安洁莉娜" in build_system_prompt(draw_enable=True)
    interrupt = build_system_prompt(draw_enable=True, interrupt=True)
    assert "插嘴模式下不要生图" in interrupt


@pytest.mark.asyncio
async def test_chat_parses_draw_marker() -> None:
    cfg = PluginConfig(
        LLM_PROVIDER="relay",
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="gpt-5.4",
        DL_SENPAI_DRAW_ENABLE=True,
    )
    llm = SenpaiLLM(cfg)

    async def fake_create(**kwargs):
        msg = MagicMock()
        msg.content = "来啦～\n<<<DRAW 一只趴在键盘上的橘猫，暖色插画>>>"
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
        "画一张猫",
        sender_name="小明",
        draw_enable=True,
    )
    assert "来啦" in result.text
    assert "<<<DRAW" not in result.text
    assert result.draw_force is True
    assert "橘猫" in result.draw_prompt
    assert "生图" in client.chat.completions.create.await_args.kwargs["messages"][0]["content"]


@pytest.mark.asyncio
async def test_generate_image_file_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from dl_senpai import image_gen

    cache_dir = Path("data/dl_senpai/draw_test_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cfg = PluginConfig(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        DL_SENPAI_DRAW_DIR=str(cache_dir),
        DL_SENPAI_DRAW_MODEL="dall-e-3",
    )
    prompt = "pytest-cache-cat-on-keyboard"
    cached = image_gen._cache_path(cfg, prompt)
    cached.unlink(missing_ok=True)

    image = MagicMock()
    image.url = None
    image.b64_json = "aGVsbG8="
    resp = MagicMock()
    resp.data = [image]

    client = MagicMock()
    client.images.generate = AsyncMock(return_value=resp)
    monkeypatch.setattr(image_gen, "_draw_client", lambda _cfg: client)

    path = await image_gen.generate_image_file(prompt, cfg)
    assert path is not None
    assert path.is_file()
    assert client.images.generate.await_count == 1

    path2 = await image_gen.generate_image_file(prompt, cfg)
    assert path2 == path
    assert client.images.generate.await_count == 1
    cached.unlink(missing_ok=True)
