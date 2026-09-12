from __future__ import annotations

from dl_senpai.config import PluginConfig
from dl_senpai.voice import (
    looks_like_voice_request,
    parse_voice_request,
    text_for_voice,
)


def test_parse_voice_request() -> None:
    parsed = parse_voice_request("在呢～\n<<<VOICE>>>")
    assert parsed.force is True
    assert parsed.text == "在呢～"
    assert parsed.override == ""

    parsed2 = parse_voice_request("哼\n<<<VOICE 晚安啦笨蛋>>>")
    assert parsed2.force is True
    assert parsed2.override == "晚安啦笨蛋"
    assert "VOICE" not in parsed2.text

    parsed3 = parse_voice_request(
        "お母さんが極東出身だからか、あたしの本名って～\n\n"
        "<<<VOICE お母さんが極東出身だからか、あたしの本名って】【。"
    )
    assert parsed3.force is True
    assert parsed3.override == "お母さんが極東出身だからか、あたしの本名って"
    assert parsed3.text == "お母さんが極東出身だからか、あたしの本名って～"
    assert "VOICE" not in parsed3.text
    assert "】【" not in parsed3.text


def test_looks_like_voice_request() -> None:
    assert looks_like_voice_request("@学姐 发语音")
    assert looks_like_voice_request("用语音说一下")
    assert not looks_like_voice_request("别发语音了")
    assert not looks_like_voice_request("你是谁")


def test_text_for_voice_trims_and_strips_emoji() -> None:
    spoken = text_for_voice("在呢～😤 学姐来啦", max_chars=50)
    assert spoken is not None
    assert "😤" not in spoken
    assert "学姐来啦" in spoken

    long = "学姐今天心情不错，" * 30
    spoken_long = text_for_voice(long, max_chars=40)
    assert spoken_long is not None
    assert len(spoken_long) <= 45


def test_angelina_voice_profile() -> None:
    cfg = PluginConfig(DL_SENPAI_VOICE_PROFILE="angelina")
    voice = cfg.resolved_voice_settings()
    assert voice.profile == "angelina"
    assert voice.name == "zh-CN-XiaoyiNeural"
    assert voice.rate == "+14%"
    assert voice.pitch == "+8Hz"


def test_gptsovits_autostart_defaults() -> None:
    cfg = PluginConfig()
    assert cfg.should_autostart_gptsovits() is True
    host, port = cfg.gptsovits_bind_host_port()
    assert host == "127.0.0.1"
    assert port == 9880
    assert cfg.gptsovits_home_path().name == "GPT-SoVITS-v2pro-20250604"


def test_gptsovits_autostart_disabled_for_edge() -> None:
    cfg = PluginConfig(DL_SENPAI_VOICE_PROVIDER="edge")
    assert cfg.should_autostart_gptsovits() is False


def test_parse_gptsovits_url() -> None:
    from dl_senpai.gptsovits_server import parse_gptsovits_url

    assert parse_gptsovits_url("http://127.0.0.1:9880") == ("127.0.0.1", 9880)
    assert parse_gptsovits_url("http://localhost:9999/tts") == ("localhost", 9999)


def test_extract_voice_quote_from_user() -> None:
    from dl_senpai.voice import extract_voice_quote_from_user

    text = '用日语语音念一下以下内容"お母さんが極東出身だからか、あたしの本名って"'
    assert "お母さん" in extract_voice_quote_from_user(text)
    assert extract_voice_quote_from_user("发语音：你好") == ""


def test_resolve_gptsovits_text_lang() -> None:
    from dl_senpai.voice import resolve_gptsovits_text_lang

    cfg = PluginConfig()
    assert (
        resolve_gptsovits_text_lang("你好呀", "用日语语音念一下", cfg) == "ja"
    )
    assert (
        resolve_gptsovits_text_lang(
            "お母さんが極東出身だからか", "发语音", cfg
        )
        == "ja"
    )
    assert resolve_gptsovits_text_lang("学姐来啦", "发语音", cfg) == "zh"
