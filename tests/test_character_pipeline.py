"""角色身份管线：识别 → 档案 → 提示词 → 参考图 → trace。

盯的是「已知 IP 角色被画成泛化路人」这条线上每一处会静默出错的环节。
所有用例都不联网、不调付费生图 API：生成器与参考图全部打桩。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dl_senpai.image_gen as image_gen
from dl_senpai.character_references import find_library_reference
from dl_senpai.character_registry import (
    CharacterRegistry,
    get_character_registry,
    reset_character_registry,
)
from dl_senpai.character_resolver import resolve_character
from dl_senpai.image_prompt_builder import ImagePromptBuilder
from dl_senpai.image_trace import ImageGenerationTrace, redact


class _Cfg:
    """最小配置替身：只放生图链路真正会读到的字段。"""

    draw_enable = True
    draw_verify = False          # 自检要调视觉模型，单测里关掉
    draw_max_retry = 0
    draw_enhance = False         # 增强也要调模型，单测里关掉
    draw_find_reference = True
    draw_max_prompt_chars = 800
    draw_timeout = 30.0
    draw_size = "1024x1024"
    draw_quality = ""
    draw_async = True
    character_enable = True
    character_min_confidence = 0.8
    character_dir = ""
    character_reference_dir = ""
    image_debug = False
    image_trace_file = ""
    llm_provider = "relay"

    def __init__(self, **kw) -> None:
        for key, value in kw.items():
            setattr(self, key, value)

    def draw_model_resolved(self) -> str:
        return "gpt-image-2.5-flare"

    def draw_base_url_resolved(self) -> str:
        return "https://airelay.vip/v1"

    def draw_configured(self) -> bool:
        return True


def _write_manifest(directory: Path, *, verified: bool = True, priority: int = 100) -> None:
    """写一个最小可用的参考图 manifest（参考图库 V1 的入口）。

    为什么测试也必须有 manifest：没有清单的图来源不可追踪，按新策略不算
    「已验证参考图」，不会进 images.edit——这正是上一阶段事故的教训。
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "character": "Eyjafjalla",
                "localized_name": "艾雅法拉",
                "franchise": "Arknights",
                "verified": verified,
                "references": [
                    {"file": "ref_01.png", "type": "verified_reference",
                     "verified": verified, "priority": priority},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    # 注册表是单例：不清掉的话，前一个用例改的用户目录会漏到下一个
    reset_character_registry()


# ---------------- Case 1-4：识别 ----------------


def test_registry_loads_builtin_characters() -> None:
    registry = CharacterRegistry.load()
    names = {p.canonical_name for p in registry.all()}
    assert "Eyjafjalla" in names, "艾雅法拉必须在册：这是本次事故的角色"
    eyja = registry.resolve("艾雅法拉")
    assert eyja is not None
    assert eyja.franchise == "Arknights"
    assert eyja.slug == "arknights/eyjafjalla"
    # 身份特征不能是空的，否则提示词里没有任何可比对的硬信息
    assert eyja.visual_identity.get("horns")
    assert "facial identity" in eyja.must_preserve


def test_case1_identifies_eyjafjalla() -> None:
    res = resolve_character("画一张艾雅法拉", config=_Cfg())
    assert res.kind == "known_character"
    assert res.profile is not None
    assert res.profile.canonical_name == "Eyjafjalla"
    assert res.profile.franchise == "Arknights"
    assert res.confidence >= 0.9


def test_case3_original_request_is_not_hijacked() -> None:
    """「画一个原创羊角少女」不许变成艾雅法拉。"""
    res = resolve_character("画一个原创羊角少女", config=_Cfg())
    assert res.kind == "unknown"
    assert res.profile is None


def test_case4_weak_alias_needs_franchise() -> None:
    """「小羊」单独出现不认；带上「明日方舟」才认。"""
    assert resolve_character("画小羊", config=_Cfg()).kind == "unknown"

    res = resolve_character("画小羊 明日方舟", config=_Cfg())
    assert res.kind == "known_character"
    assert res.profile.canonical_name == "Eyjafjalla"
    # 弱别名的置信度必须低于强名，避免和明确说名字一样被当作确凿
    assert res.confidence <= 0.8


def test_unknown_name_returns_unknown_without_guessing() -> None:
    """认不出来就是认不出来——不许猜发型猜服装。"""
    res = resolve_character("画一个叫阿伟的原创角色", config=_Cfg())
    assert res.kind == "unknown"
    assert res.profile is None


# ---------------- Case 1-2：提示词 ----------------


def test_case1_prompt_keeps_identity_anchors() -> None:
    res = resolve_character("画一张艾雅法拉", config=_Cfg())
    built = ImagePromptBuilder().build(
        user_request="画一张艾雅法拉", draft_prompt="艾雅法拉", resolution=res
    )
    text = built.text
    assert "Eyjafjalla" in text
    assert "Arknights" in text
    assert "identity preservation" in text.lower()
    assert "艾雅法拉" in text
    # 退化形态必须被显式禁止，而不是靠模型自觉
    assert "generic" in text.lower()
    assert built.kind == "known_character"
    assert built.identity_priority == "HIGH"


def test_case2_scene_kept_identity_kept() -> None:
    """「在咖啡店喝咖啡」是场景，不是重新设计角色的理由。"""
    res = resolve_character("画艾雅法拉在咖啡店喝咖啡", config=_Cfg())
    built = ImagePromptBuilder().build(
        user_request="画艾雅法拉在咖啡店喝咖啡",
        draft_prompt="艾雅法拉 在咖啡店",
        resolution=res,
    )
    assert "咖啡店" in built.scene_block
    assert "咖啡店" in built.text
    # 身份段落里不该出现场景词：两者必须分开
    assert "咖啡店" not in built.identity_block
    assert "Eyjafjalla" in built.identity_block


def test_general_image_prompt_is_untouched() -> None:
    """普通生图一个字都不许加（不能为了修角色把猫也拖进角色管线）。"""
    res = resolve_character("画一只橘猫", config=_Cfg())
    built = ImagePromptBuilder().build(
        user_request="画一只橘猫", draft_prompt="一只橘猫趴在窗台上", resolution=res
    )
    assert built.kind == "general"
    assert built.text == "一只橘猫趴在窗台上"
    assert built.identity_block == ""


def test_known_character_prompt_not_truncated_before_scene() -> None:
    """上限放宽后场景不该被截掉：身份写全了、场景丢了同样是失败。"""
    res = resolve_character("画艾雅法拉在咖啡店喝咖啡", config=_Cfg())
    built = ImagePromptBuilder().build(
        user_request="画艾雅法拉在咖啡店喝咖啡",
        draft_prompt="艾雅法拉 在咖啡店喝咖啡，暖光",
        resolution=res,
    )
    limit = max(_Cfg().draw_max_prompt_chars, 1800)
    assert len(built.text) <= limit
    assert "Eyjafjalla" in built.text[:600], "身份必须排在最前面，截断也先截场景"


# ---------------- Case 5：参考图优先级 ----------------


@pytest.fixture
def stub_generate(monkeypatch, tmp_path):
    """把真正的生图 API 换掉，只记录传进去的提示词与参考图。"""
    png = tmp_path / "out.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    calls: list[dict] = []

    async def _fake_generate(prompt, config=None, *, reference=None):
        calls.append({"prompt": prompt, "reference": reference})
        return png

    monkeypatch.setattr(image_gen, "generate_image_file", _fake_generate)
    return calls


@pytest.mark.asyncio
async def test_case5_user_reference_wins(monkeypatch, stub_generate) -> None:
    """用户明确说「照这个人物画」时，必须把原图直接当参考图，不许绕成文字。"""

    async def _fake_load(sources, *, timeout):
        return ("user.png", b"\x89PNG\r\n\x1a\nuser")

    async def _library_must_not_run(*a, **kw):
        raise AssertionError("用户已经给了图，不该再去本地库找")

    async def _web_must_not_run(*a, **kw):
        raise AssertionError("用户已经给了图，不该再上网搜")

    monkeypatch.setattr(image_gen, "_load_reference_image", _fake_load)
    monkeypatch.setattr("dl_senpai.character_references.resolve_library_reference", _library_must_not_run)
    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", _web_must_not_run)

    await image_gen.build_image_segment(
        resolved_prompt="照这个人物画她坐在沙发上",
        user_request="照这个人物画她坐在沙发上",
        config=_Cfg(),
        reference_images=["https://example.com/ref.png"],
        auto_reference=True,
    )
    assert stub_generate, "应该走到生图"
    assert stub_generate[0]["reference"] is not None
    assert stub_generate[0]["reference"][0] == "user.png", "参考图必须是用户那张原图"


@pytest.mark.asyncio
async def test_known_character_uses_library_before_web(monkeypatch, stub_generate, tmp_path) -> None:
    """本地角色库排在联网搜图之前：库里的图是自己确认过的，网图可能是同人。"""
    ref_dir = tmp_path / "refs"
    char_dir = ref_dir / "arknights" / "eyjafjalla"
    char_dir.mkdir(parents=True)
    (char_dir / "ref_01.png").write_bytes(b"\x89PNG\r\n\x1a\nlib")
    _write_manifest(char_dir)

    async def _web_must_not_run(*a, **kw):
        raise AssertionError("本地库有图时不该上网搜")

    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", _web_must_not_run)

    await image_gen.build_image_segment(
        resolved_prompt="艾雅法拉",
        user_request="画一张艾雅法拉",
        config=_Cfg(character_reference_dir=str(ref_dir)),
        auto_reference=True,
    )
    assert stub_generate[0]["reference"] is not None
    assert stub_generate[0]["reference"][0] == "ref_01.png"
    prompt = stub_generate[0]["prompt"]
    assert "Eyjafjalla" in prompt and "Arknights" in prompt


@pytest.mark.asyncio
async def test_general_image_flow_has_no_identity_block(monkeypatch, stub_generate) -> None:
    """普通图走的仍是老链路：提示词里不许出现任何角色条款。"""
    await image_gen.build_image_segment(
        resolved_prompt="一只橘猫趴在窗台上",
        user_request="画一只橘猫",
        config=_Cfg(),
        auto_reference=False,
    )
    prompt = stub_generate[0]["prompt"]
    assert "identity preservation" not in prompt.lower()
    assert "Eyjafjalla" not in prompt


@pytest.mark.asyncio
async def test_untrusted_web_reference_is_rejected(monkeypatch, stub_generate, tmp_path) -> None:
    """网图设定不一致时必须丢掉。

    这是本次唯一的「负面证据」修正：实测同人参考图把艾雅法拉画成棕发 + 精灵耳，
    图生图照搬后比纯文本还差。宁可退回档案生图，也不要错设定。
    """

    async def _fake_find(*args, **kwargs):
        return ("web.png", b"\x89PNG\r\n\x1a\nweb")

    async def _reject(config, profile, raw):
        assert profile.canonical_name == "Eyjafjalla"
        # verify_reference 现在返回 (ok, issue, score)
        return False, "头发呈棕色，出现官方设定里没有的尖长精灵耳", 0.2

    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", _fake_find)
    monkeypatch.setattr("dl_senpai.character_references.verify_reference", _reject)

    trace_file = tmp_path / "t.jsonl"
    await image_gen.build_image_segment(
        resolved_prompt="艾雅法拉",
        user_request="画一张艾雅法拉",
        config=_Cfg(image_debug=True, image_trace_file=str(trace_file)),
        auto_reference=True,
    )
    assert stub_generate[0]["reference"] is None, "被拒的网图不能再传给图片模型"
    record = json.loads(trace_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["reference_mode"] == "web_rejected"
    assert record["generation_mode"] == "text_only"
    assert record["extra"]["reference_issue"]


@pytest.mark.asyncio
async def test_library_reference_is_not_verified(monkeypatch, stub_generate, tmp_path) -> None:
    """本地库的图是人工确认过的，不该再去问视觉模型（省一次调用、省一份抖动）。"""
    ref_dir = tmp_path / "refs"
    char_dir = ref_dir / "arknights" / "eyjafjalla"
    char_dir.mkdir(parents=True)
    (char_dir / "ref_01.png").write_bytes(b"\x89PNG\r\n\x1a\nlib")
    _write_manifest(char_dir)

    async def _verify_must_not_run(*args, **kwargs):
        raise AssertionError("本地库参考图不该被校验")

    monkeypatch.setattr("dl_senpai.character_references.verify_reference", _verify_must_not_run)

    await image_gen.build_image_segment(
        resolved_prompt="艾雅法拉",
        user_request="画一张艾雅法拉",
        config=_Cfg(character_reference_dir=str(ref_dir)),
        auto_reference=True,
    )
    assert stub_generate[0]["reference"] is not None


# ---------------- trace ----------------


def test_trace_redacts_secrets() -> None:
    trace = ImageGenerationTrace(
        original_prompt="画一张艾雅法拉 sk-abc123456789 Bearer eyJhbGciOi.verylongtoken",
        final_prompt="prompt with key=supersecretvalue and ?api_key=abcdef123456",
    )
    fields = trace.as_fields()
    blob = json.dumps(fields, ensure_ascii=False)
    assert "sk-abc123456789" not in blob
    assert "verylongtoken" not in blob
    assert "supersecretvalue" not in blob
    assert "abcdef123456" not in blob
    assert "***" in blob


def test_redact_keeps_normal_text() -> None:
    assert redact("画一张艾雅法拉") == "画一张艾雅法拉"


@pytest.mark.asyncio
async def test_trace_written_to_disk_in_debug(monkeypatch, stub_generate, tmp_path) -> None:
    """debug 打开时要落盘：以后「为什么画得不像」才有据可查。"""
    trace_file = tmp_path / "traces.jsonl"
    await image_gen.build_image_segment(
        resolved_prompt="艾雅法拉",
        user_request="画一张艾雅法拉",
        config=_Cfg(image_debug=True, image_trace_file=str(trace_file)),
        auto_reference=False,
    )
    assert trace_file.is_file()
    record = json.loads(trace_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["intent"] == "known_character_generation"
    assert record["character"] == "Eyjafjalla"
    assert record["franchise"] == "Arknights"
    assert record["result"] == "success"
    assert record["configured_model"] == "gpt-image-2.5-flare"
    assert record["resolved_model"] == "unknown", "中转站 alias 不允许被当成真实后端"
    assert record["base_url_host"] == "airelay.vip"


# ---------------- 参考图库 ----------------


def test_library_reference_ignores_non_images(tmp_path) -> None:
    """后缀对、内容不是图的文件不许被当成参考图。"""
    from dl_senpai.character_registry import CharacterRegistry

    ref_dir = tmp_path / "refs"
    target = ref_dir / "arknights" / "eyjafjalla"
    target.mkdir(parents=True)
    (target / "ref_01.png").write_bytes(b"not really a png")

    profile = CharacterRegistry.load().resolve("艾雅法拉")
    # 没有 manifest：来源不可追踪，一律不采信（哪怕是真图）
    assert find_library_reference(_Cfg(character_reference_dir=str(ref_dir)), profile) is None


def test_registry_singleton_and_reset() -> None:
    first = get_character_registry(_Cfg())
    second = get_character_registry(_Cfg())
    assert first is second
    reset_character_registry()
    assert get_character_registry(_Cfg()) is not first
