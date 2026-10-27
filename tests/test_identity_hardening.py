"""身份管线加固：字段级可信度、参考图策略、身份评分。

盯的是「可信度层级」这件事本身：未验证信息不许伪装权威，网图不许悄悄进
图生图，评分只许评有依据的维度，生成结果不许反写档案。
所有用例不联网、不调付费生图 API。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dl_senpai.image_gen as image_gen
from dl_senpai.character_references import (
    SOURCE_FAN_ART,
    SOURCE_UNKNOWN,
    SOURCE_WIKI,
    candidate_from_url,
    classify_source,
    count_verified_references,
    resolve_library_reference,
    validate_character_profile,
)
from dl_senpai.character_registry import (
    CharacterProfile,
    CharacterRegistry,
    reset_character_registry,
)
from dl_senpai.character_resolver import CharacterResolution, resolve_character
from dl_senpai.identity_eval import CharacterIdentityEvaluator, IdentityScore, verdict_for
from dl_senpai.image_prompt_builder import ImagePromptBuilder

PNG = b"\x89PNG\r\n\x1a\nfake"


class _Cfg:
    draw_enable = True
    draw_verify = False
    draw_max_retry = 0
    draw_enhance = False
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
    character_verify_reference = True
    character_web_reference_mode = "verify_only"
    character_web_reference_min_trust = 0.7
    identity_eval = False          # 单测里默认关：评分另有专门用例
    image_debug = False
    image_trace_file = ""
    llm_provider = "relay"

    def __init__(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)

    def draw_model_resolved(self) -> str:
        return "gpt-image-2.5-flare"

    def draw_base_url_resolved(self) -> str:
        return "https://relay.example/v1"

    def draw_configured(self) -> bool:
        return True


def _profile(**overrides) -> CharacterProfile:
    """合成一个可控的档案（不依赖内置数据里的验证状态）。"""
    data = {
        "canonical_name": "TestChar",
        "localized_names": ["测试角色"],
        "aliases": ["testchar"],
        "franchise": "TestFranchise",
        "franchise_localized": "测试作品",
        "franchise_aliases": ["测试作品"],
        "confidence": 0.95,
        "profile_status": "partial_verified",
        "visual_identity": {
            "hair": {"value": "银色长发", "verified": True},
            "horns": {"value": "一对弯曲羊角", "verified": True},
            "clothing": {"value": "黑色外套", "verified": False},
        },
        "must_preserve": [
            "facial identity",
            "hairstyle and hair color",
            "horn shape and placement",
            "signature outfit structure",
        ],
        "negative_notes": ["不要画成泛化羊角少女"],
        "identity_salience": {"hair": 3.0, "clothing": 1.0},
    }
    data.update(overrides)
    return CharacterProfile.from_dict(data)


def _resolution(profile: CharacterProfile) -> CharacterResolution:
    return CharacterResolution(
        kind="known_character", profile=profile, confidence=0.95, matched=profile.canonical_name
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_character_registry()


@pytest.fixture
def stub_generate(monkeypatch, tmp_path):
    png = tmp_path / "out.png"
    png.write_bytes(PNG)
    calls: list[dict] = []

    async def _fake(prompt, config=None, *, reference=None):
        calls.append({"prompt": prompt, "reference": reference})
        return png

    monkeypatch.setattr(image_gen, "generate_image_file", _fake)
    return calls


# ---------------- Case 1：draft 不许产生 authoritative ----------------


def test_case1_draft_profile_never_authoritative() -> None:
    """未核验的档案不能出现权威措辞——写错时会稳定地画错。

    样本用凯尔希：她至今没有核验任何字段。艾雅法拉在本阶段已核验，
    不再适合当"未核验"的例子（这正是流程该有的样子：数据变了，测试跟着换样本）。
    """
    profile = CharacterRegistry.load().resolve("凯尔希")
    assert profile.effective_status() == "draft"
    assert profile.verified_fields() == ()
    built = ImagePromptBuilder().build(
        user_request="画一张凯尔希", draft_prompt="凯尔希", resolution=_resolution(profile)
    )
    low = built.text.lower()
    for banned in ("authoritative", "do not contradict", "canonical visual identity"):
        assert banned not in low, f"draft 档案不该出现 {banned!r}"
    # 但身份锚点和支撑性提示必须还在：不能因为降级就把信息丢了
    assert "Kal'tsit" in built.text and "Arknights" in built.text
    assert "supporting identity hints" in low
    assert built.profile_status == "draft"
    assert built.verified_fields == ()


# ---------------- Case 2：字段级 verified 分层 ----------------


def test_case2_field_level_tiers() -> None:
    built = ImagePromptBuilder().build(
        user_request="画测试角色", draft_prompt="", resolution=_resolution(_profile())
    )
    low = built.text.lower()
    assert "verified canonical identity constraints" in low
    assert "do not contradict them" in low
    # hair / horns 已验证 → 进硬约束
    assert set(built.verified_fields) == {"hair", "horns"}
    # clothing 未验证 → 只能进 hints
    assert "clothing" in built.hint_fields
    verified_part = built.identity_block.split("Supporting identity hints")[0]
    hints_part = built.identity_block.split("Supporting identity hints")[-1]
    assert "银色长发" in verified_part and "一对弯曲羊角" in verified_part
    assert "黑色外套" not in verified_part
    assert "黑色外套" in hints_part


def test_case2_must_preserve_split_by_verification() -> None:
    hard, soft = _profile().split_preserve()
    assert "hairstyle and hair color" in hard
    assert "horn shape and placement" in hard
    assert "signature outfit structure" in soft
    # facial identity 没有对应已验证字段 → 只能是提示
    assert "facial identity" in soft


# ---------------- Case 3：Unknown ≠ None ----------------


def test_case3_unknown_is_not_written_as_none() -> None:
    profile = _profile(visual_identity={"hair": {"value": "银色长发", "verified": True}})
    built = ImagePromptBuilder().build(
        user_request="画测试角色", draft_prompt="", resolution=_resolution(profile)
    )
    low = built.text.lower()
    for banned in ("horns: none", "ears: none", "accessories: none", "无角", "没有角"):
        assert banned not in low
    # 未声明的字段一个都不该出现在提示词里（"角：" 这种字段行，不是名字里的字）
    assert "角：" not in built.identity_block
    assert "配饰：" not in built.identity_block
    assert "耳朵：" not in built.identity_block


# ---------------- Case 4：本地已验证参考图优先于网图 ----------------


@pytest.mark.asyncio
async def test_case4_local_verified_reference_blocks_web(monkeypatch, stub_generate, tmp_path) -> None:
    ref_dir = tmp_path / "refs"
    char_dir = ref_dir / "arknights" / "eyjafjalla"
    char_dir.mkdir(parents=True)
    (char_dir / "ref_01.png").write_bytes(PNG)
    (char_dir / "manifest.json").write_text(
        json.dumps(
            {
                "character": "Eyjafjalla",
                "franchise": "Arknights",
                "verified": True,
                "references": [{"file": "ref_01.png", "verified": True, "priority": 100}],
            }
        ),
        encoding="utf-8",
    )

    async def _web_must_not_run(*a, **kw):
        raise AssertionError("本地已有核验参考图，不该再联网找图")

    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", _web_must_not_run)

    await image_gen.build_image_segment(
        resolved_prompt="艾雅法拉",
        user_request="画一张艾雅法拉",
        config=_Cfg(character_reference_dir=str(ref_dir)),
        auto_reference=True,
    )
    assert stub_generate[0]["reference"] is not None
    assert stub_generate[0]["reference"][0] == "ref_01.png"


def test_case4_unverified_library_entry_is_not_used(tmp_path) -> None:
    ref_dir = tmp_path / "refs"
    char_dir = ref_dir / "arknights" / "eyjafjalla"
    char_dir.mkdir(parents=True)
    (char_dir / "ref_01.png").write_bytes(PNG)
    (char_dir / "manifest.json").write_text(
        json.dumps({"character": "Eyjafjalla", "verified": False,
                    "references": [{"file": "ref_01.png", "verified": False}]}),
        encoding="utf-8",
    )
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    assert resolve_library_reference(_Cfg(character_reference_dir=str(ref_dir)), profile) is None
    assert count_verified_references(_Cfg(character_reference_dir=str(ref_dir)), profile) == 0


# ---------------- Case 5：verify_only 不送 images.edit ----------------


@pytest.mark.asyncio
async def test_case5_verify_only_never_edits(monkeypatch, stub_generate, tmp_path) -> None:
    """默认策略下，网图即使校验通过也只能当证据，不能进图生图。"""
    cand = candidate_from_url("https://prts.wiki/eyja.png", "eyja.png", PNG)
    assert cand.source_type == SOURCE_WIKI

    async def _find(*a, **kw):
        return cand

    async def _ok(config, profile, raw):
        return True, "", 0.92

    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", _find)
    monkeypatch.setattr("dl_senpai.character_references.verify_reference", _ok)

    trace_file = tmp_path / "t.jsonl"
    await image_gen.build_image_segment(
        resolved_prompt="艾雅法拉",
        user_request="画一张艾雅法拉",
        config=_Cfg(image_debug=True, image_trace_file=str(trace_file)),
        auto_reference=True,
    )
    assert stub_generate[0]["reference"] is None, "verify_only 模式下网图不许进 images.edit"
    record = json.loads(trace_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["reference_mode"] == "web_probe_only"
    assert record["generation_mode"] == "text_only"
    assert record["extra"]["web_reference"]["source_type"] == SOURCE_WIKI
    assert record["extra"]["web_reference"]["visual_score"] == 0.92


@pytest.mark.asyncio
async def test_case5_off_mode_does_not_search(monkeypatch, stub_generate) -> None:
    async def _must_not_run(*a, **kw):
        raise AssertionError("off 模式不许联网找图")

    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", _must_not_run)
    await image_gen.build_image_segment(
        resolved_prompt="艾雅法拉",
        user_request="画一张艾雅法拉",
        config=_Cfg(character_web_reference_mode="off"),
        auto_reference=True,
    )
    assert stub_generate[0]["reference"] is None


@pytest.mark.asyncio
async def test_case5_enabled_requires_trust_and_verification(monkeypatch, stub_generate) -> None:
    """enabled 模式：可信来源 + 校验通过 才放行；同人站一律不放行。"""
    wiki = candidate_from_url("https://prts.wiki/eyja.png", "eyja.png", PNG)
    fan = candidate_from_url("https://www.deviantart.com/x.png", "x.png", PNG)
    assert fan.source_type == SOURCE_FAN_ART

    async def _ok(config, profile, raw):
        return True, "", 0.9

    monkeypatch.setattr("dl_senpai.character_references.verify_reference", _ok)

    class _First:
        def __init__(self, value):
            self.value = value
            self.calls = 0

        async def __call__(self, *a, **kw):
            self.calls += 1
            return self.value

    finder = _First(wiki)
    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", finder)
    await image_gen.build_image_segment(
        resolved_prompt="艾雅法拉",
        user_request="画一张艾雅法拉",
        config=_Cfg(character_web_reference_mode="enabled"),
        auto_reference=True,
    )
    assert stub_generate[0]["reference"] is not None, "可信来源 + 校验通过应允许图生图"

    finder2 = _First(fan)
    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", finder2)
    calls_before = len(stub_generate)
    await image_gen.build_image_segment(
        resolved_prompt="艾雅法拉",
        user_request="画一张艾雅法拉",
        config=_Cfg(character_web_reference_mode="enabled"),
        auto_reference=True,
    )
    assert stub_generate[calls_before]["reference"] is None, "同人站来源不该进 images.edit"


# ---------------- Case 6：评估器只评有依据的字段 ----------------


def test_case6_evaluator_skips_unverified_fields() -> None:
    evaluator = CharacterIdentityEvaluator(_profile())
    assert set(evaluator.evaluable_fields()) == {"hair", "horns"}


@pytest.mark.asyncio
async def test_case6_no_basis_returns_not_evaluated(monkeypatch) -> None:
    """没有已验证字段也没有已验证参考图时，一次视觉调用都不许发。"""
    profile = CharacterRegistry.load().resolve("凯尔希")

    async def _chat_must_not_run(*a, **kw):
        raise AssertionError("没有依据就不该调用视觉模型")

    monkeypatch.setattr("dl_senpai.draw_enhance._chat", _chat_must_not_run)
    score = await CharacterIdentityEvaluator(profile).evaluate(_Cfg(), PNG)
    assert score.verdict == "not_evaluated"
    assert score.overall is None
    assert score.basis == "none"
    assert "hair" in score.not_evaluated


@pytest.mark.asyncio
async def test_case6_unverified_dimension_is_null(monkeypatch) -> None:
    async def _chat(config, content, *, max_tokens=500):
        # 模型擅自给出未验证字段的分数：必须被丢弃
        return json.dumps({
            "dimensions": {"hair": 0.9, "horns": 0.8, "clothing": 0.1, "face": 0.2},
            "issues": ["角略偏"],
        })

    monkeypatch.setattr("dl_senpai.draw_enhance._chat", _chat)
    score = await CharacterIdentityEvaluator(_profile()).evaluate(_Cfg(), PNG)
    assert score.dimensions["hair"] == 0.9
    assert score.dimensions["horns"] == 0.8
    assert score.dimensions["clothing"] is None, "未验证字段必须是 None，不能采信模型打分"
    assert score.dimensions.get("face") is None
    assert "clothing" in score.not_evaluated


# ---------------- Case 7：加权总分 ----------------


def test_case7_weighted_overall_not_average() -> None:
    evaluator = CharacterIdentityEvaluator(_profile())
    # hair 权重 3.0、clothing 权重 1.0（profile.identity_salience）
    dims = {"hair": 0.4, "clothing": 1.0}
    weighted = evaluator.weighted_overall(dims)
    assert weighted is not None
    assert weighted == pytest.approx((0.4 * 3 + 1.0 * 1) / 4)
    assert weighted < (0.4 + 1.0) / 2, "核心维度权重必须把总分拉低，而不是平均掉"


def test_case7_verdict_bands() -> None:
    assert verdict_for(0.95) == "excellent"
    assert verdict_for(0.84) == "good"
    assert verdict_for(0.72) == "acceptable"
    assert verdict_for(0.60) == "weak"
    assert verdict_for(0.30) == "reject"
    assert verdict_for(None) == "not_evaluated"


# ---------------- Case 8：普通图零污染 ----------------


@pytest.mark.asyncio
async def test_case8_general_image_untouched(monkeypatch, stub_generate, tmp_path) -> None:
    trace_file = tmp_path / "t.jsonl"
    await image_gen.build_image_segment(
        resolved_prompt="一只橘猫趴在窗台上",
        user_request="画一只橘猫",
        config=_Cfg(image_debug=True, image_trace_file=str(trace_file)),
        auto_reference=False,
    )
    prompt = stub_generate[0]["prompt"]
    low = prompt.lower()
    for banned in ("identity preservation", "supporting identity hints", "eyjafjalla", "character profile"):
        assert banned not in low
    record = json.loads(trace_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["intent"] == "general_image"
    assert "character" not in record


# ---------------- Case 9：生成结果不许反写档案 ----------------


@pytest.mark.asyncio
async def test_case9_evaluation_never_mutates_profile(monkeypatch) -> None:
    async def _chat(config, content, *, max_tokens=500):
        return json.dumps({"dimensions": {"hair": 0.1, "horns": 0.1}, "issues": ["完全不像"]})

    monkeypatch.setattr("dl_senpai.draw_enhance._chat", _chat)

    profile = _profile()
    before = profile.visual_identity.as_dict()
    registry = CharacterRegistry([profile])
    score = await CharacterIdentityEvaluator(profile).evaluate(_Cfg(), PNG)

    assert score.verdict == "reject"
    # 评分再差也不能改档案：档案只接受人工编辑
    assert profile.visual_identity.as_dict() == before
    assert registry.resolve("TestChar").visual_identity.as_dict() == before
    assert profile.effective_status() == "partial_verified"


# ---------------- 来源可信度与一致性检查 ----------------


def test_classify_source_ranks_fan_art_below_wiki() -> None:
    _d, wiki_type, wiki_trust = classify_source("https://prts.wiki/images/eyja.png")
    _d2, fan_type, fan_trust = classify_source("https://www.deviantart.com/eyja.png")
    _d3, unknown_type, _t3 = classify_source("https://example.invalid/a.png")
    assert wiki_type == SOURCE_WIKI and wiki_trust > 0.7
    assert fan_type == SOURCE_FAN_ART and fan_trust < 0.3
    assert unknown_type == SOURCE_UNKNOWN
    assert wiki_trust > fan_trust


def test_validate_character_profile_reports_missing_manifest(tmp_path) -> None:
    ref_dir = tmp_path / "refs"
    char_dir = ref_dir / "arknights" / "eyjafjalla"
    char_dir.mkdir(parents=True)
    (char_dir / "ref_01.png").write_bytes(PNG)
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    issues = validate_character_profile(_Cfg(character_reference_dir=str(ref_dir)), profile)
    assert any("manifest" in i for i in issues)


def test_validate_character_profile_reports_verified_images_with_draft_profile(tmp_path) -> None:
    """有已核验参考图、档案却还是 draft：结构上应该被提示出来。"""
    ref_dir = tmp_path / "refs"
    char_dir = ref_dir / "arknights" / "kal-tsit"
    char_dir.mkdir(parents=True)
    (char_dir / "ref_01.png").write_bytes(PNG)
    (char_dir / "manifest.json").write_text(
        json.dumps({"character": "Kal'tsit", "franchise": "Arknights", "verified": True,
                    "references": [{"file": "ref_01.png", "verified": True, "priority": 10}]}),
        encoding="utf-8",
    )
    profile = CharacterRegistry.load().resolve("凯尔希")
    issues = validate_character_profile(_Cfg(character_reference_dir=str(ref_dir)), profile)
    assert any("draft" in i for i in issues)


def test_manifest_priority_ordering(tmp_path) -> None:
    ref_dir = tmp_path / "refs"
    char_dir = ref_dir / "arknights" / "eyjafjalla"
    char_dir.mkdir(parents=True)
    (char_dir / "low.png").write_bytes(PNG)
    (char_dir / "high.png").write_bytes(b"\x89PNG\r\n\x1a\nhigh")
    (char_dir / "manifest.json").write_text(
        json.dumps({
            "character": "Eyjafjalla", "verified": True,
            "references": [
                {"file": "low.png", "verified": True, "priority": 10},
                {"file": "high.png", "verified": True, "priority": 99},
            ],
        }),
        encoding="utf-8",
    )
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    cand = resolve_library_reference(_Cfg(character_reference_dir=str(ref_dir)), profile)
    assert cand is not None and cand.filename == "high.png"
    assert cand.verified is True and cand.priority == 99
