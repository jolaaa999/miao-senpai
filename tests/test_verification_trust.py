"""Verification Trust Model：区分「模型审阅」与「人工核验」。

这一层盯的是一件事：**只有人给的核验才能变成硬约束**。

上一阶段的事故是：8 个字段 verification_method=assistant_review 但 verified=true，
于是它们进了提示词的 "Verified canonical identity constraints" 段，配上
"must be preserved / do not contradict"。数据错了，措辞越强，画错得越稳定。

现在的规则：
    assistant_reviewed → Level C（supporting guidance only，禁止 authoritative 措辞）
    human_verified     → Level A（唯一可以写 must/do not contradict 的层级）
    unverified/空      → Level D（不写入）

所有用例不联网、不调付费 API。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from dl_senpai.character_references import (
    ReferenceEntry,
    add_reference_to_library,
    count_verified_references,
    load_manifest,
)
from dl_senpai.character_registry import (
    CharacterRegistry,
    VerificationStatus,
    builtin_data_path,
    migrate_status,
    reset_character_registry,
    update_field_status,
    update_field_value,
)
from dl_senpai.identity_eval import CharacterIdentityEvaluator
from dl_senpai.image_prompt_builder import ImagePromptBuilder

REPO = Path(__file__).resolve().parent.parent
PNG = b"\x89PNG\r\n\x1a\n" + b"trust-fixture" * 8

HUMAN = VerificationStatus.HUMAN_VERIFIED
REVIEWED = VerificationStatus.ASSISTANT_REVIEWED
UNVERIFIED = VerificationStatus.UNVERIFIED


class _Cfg:
    character_enable = True
    character_min_confidence = 0.8
    character_dir = ""
    character_reference_dir = ""
    character_verify_reference = True
    character_web_reference_mode = "verify_only"
    character_web_reference_min_trust = 0.7
    identity_eval = True
    identity_min_confidence = 0.5
    draw_enable = True
    draw_verify = False
    draw_max_retry = 0
    draw_enhance = False
    draw_find_reference = True
    draw_max_prompt_chars = 1600
    draw_timeout = 30.0
    draw_size = "1024x1024"
    draw_quality = ""
    draw_async = True
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


@pytest.fixture(autouse=True)
def _reset():
    reset_character_registry()
    yield
    reset_character_registry()


@pytest.fixture
def data_file(tmp_path) -> Path:
    """内置角色数据的副本：测试绝不改真实档案。"""
    target = tmp_path / "characters.json"
    shutil.copyfile(builtin_data_path(), target)
    return target


def _profile_from(path: Path, name: str = "艾雅法拉"):
    return CharacterRegistry.from_file(path).resolve(name)


def _prompt_for(profile, *, reference_verified: bool = False):
    resolution = SimpleNamespace(is_known=True, profile=profile)
    return ImagePromptBuilder().build(
        user_request="画一张艾雅法拉站在火山前，半身构图，自然表情",
        draft_prompt="火山前，半身构图，自然表情",
        resolution=resolution,
        reference_verified=reference_verified,
    )


# ---------------- Case 1：assistant_review 不能变成 verified ----------------


def test_case1_assistant_review_is_not_verified(data_file) -> None:
    ok, _ = update_field_status(
        data_file, "艾雅法拉", "hair", status=REVIEWED, method="assistant_review"
    )
    assert ok
    fld = _profile_from(data_file).visual_identity.field("hair")
    assert fld.verification_status is REVIEWED
    # 核心：verified 是派生属性，assistant_reviewed 永远拿不到它
    assert fld.verified is False


def test_case1_verify_field_rejects_assistant_method(data_file) -> None:
    """人工核验接口不许被 assistant_review 借用。"""
    ok, message = update_field_status(
        data_file, "艾雅法拉", "hair", status=HUMAN, method="assistant_review"
    )
    assert not ok
    assert "assistant_review cannot create a verified field" in message
    # 状态没有被写成 human_verified
    assert _profile_from(data_file).visual_identity.field("hair").verified is False


# ---------------- Case 2 / 3：提示词分层 ----------------


def test_case2_reviewed_fields_never_enter_hard_constraints(data_file) -> None:
    for field in ("hair", "eyes", "horns"):
        update_field_status(data_file, "艾雅法拉", field, status=REVIEWED, method="assistant_review")
    prompt = _prompt_for(_profile_from(data_file))
    text = prompt.text
    assert "Human-verified identity constraints" not in text
    assert "must be preserved" not in text
    assert "Do not contradict them" not in text
    assert prompt.human_verified_fields == ()
    # 真实数据里这 8 个字段本来就是 assistant_reviewed，全部只能待在 Level C
    assert {"hair", "eyes", "horns"} <= set(prompt.assistant_reviewed_fields)


def test_case3_reviewed_fields_go_to_hint_section_only(data_file) -> None:
    update_field_status(data_file, "艾雅法拉", "hair", status=REVIEWED, method="assistant_review")
    text = _prompt_for(_profile_from(data_file)).text
    assert "Assistant-reviewed identity hints (not human-verified" in text
    assert "银白偏浅紫的长发" in text
    # Level C 必须自带"不许越权"的说明
    assert "These hints are not authoritative." in text
    assert "Do not let them override" in text


# ---------------- Case 4：human_review 才可以 verified ----------------


def test_case4_human_review_creates_hard_constraints(data_file) -> None:
    ok, _ = update_field_status(
        data_file, "艾雅法拉", "hair", status=HUMAN, method="human_review", at="2026-10-27"
    )
    assert ok
    fld = _profile_from(data_file).visual_identity.field("hair")
    assert fld.verified is True
    assert fld.verified_at == "2026-10-27"

    text = _prompt_for(_profile_from(data_file)).text
    assert "Human-verified identity constraints" in text
    assert "Do not contradict them." in text
    # 人工核验的那一条在硬约束段里，而不是只出现在 hints 里
    hard_part = text.split("Assistant-reviewed identity hints")[0]
    assert "银白偏浅紫的长发" in hard_part


def test_case4_reference_section_only_with_verified_reference(data_file) -> None:
    update_field_status(data_file, "艾雅法拉", "hair", status=HUMAN, method="human_review")
    profile = _profile_from(data_file)
    assert "Human-verified reference image supplied" not in _prompt_for(profile).text
    assert "Human-verified reference image supplied" in _prompt_for(
        profile, reference_verified=True
    ).text


# ---------------- Case 5：改内容 → 核验自动失效 ----------------


def test_case5_editing_verified_field_invalidates_it(data_file) -> None:
    update_field_status(data_file, "艾雅法拉", "hair", status=HUMAN, method="human_review")
    assert _profile_from(data_file).visual_identity.field("hair").verified is True

    ok, message = update_field_value(data_file, "艾雅法拉", "hair", "棕色的短发")
    assert ok
    assert "unverified" in message
    fld = _profile_from(data_file).visual_identity.field("hair")
    assert fld.value == "棕色的短发"
    # 最关键：新内容不能继承旧内容的核验结论
    assert fld.verification_status is UNVERIFIED
    assert fld.verified is False
    assert fld.verified_at == ""


# ---------------- Case 6 / 7 / 8：profile_status 推导 ----------------


def test_case6_reviewed_only_profile_is_assistant_reviewed(data_file) -> None:
    for field in ("race", "hair", "eyes", "horns", "ears", "clothing", "accessories", "palette"):
        update_field_status(data_file, "艾雅法拉", field, status=REVIEWED, method="assistant_review")
    profile = _profile_from(data_file)
    assert len(profile.assistant_reviewed_fields()) == 8
    assert profile.human_verified_fields() == ()
    assert profile.effective_status() == "assistant_reviewed"


def test_case7_some_human_verified_is_partial(data_file) -> None:
    for field in ("race", "hair", "eyes", "horns", "ears", "clothing", "accessories", "palette"):
        update_field_status(data_file, "艾雅法拉", field, status=REVIEWED, method="assistant_review")
    for field in ("hair", "clothing"):
        update_field_status(data_file, "艾雅法拉", field, status=HUMAN, method="human_review")
    profile = _profile_from(data_file)
    assert len(profile.human_verified_fields()) == 2
    assert profile.effective_status() == "partial_verified"


def test_case8_all_required_human_verified_is_verified(data_file) -> None:
    required = _profile_from(data_file).required_fields()
    assert required
    for field in required:
        update_field_status(data_file, "艾雅法拉", field, status=HUMAN, method="human_review")
    profile = _profile_from(data_file)
    assert profile.effective_status() == "verified"


def test_case8_generic_rule_no_character_special_case(data_file) -> None:
    """必核字段按角色实际数据算，不是写死给艾雅法的。"""
    kal = _profile_from(data_file, "凯尔希")
    assert "horns" not in kal.required_fields()      # 凯尔希没有角
    assert "body_features" in kal.required_fields()  # 她的族属特征槽落在这里
    # 凯尔希的族属特征槽落在 ears（她被设成有兽耳、无角），说明规则真的是按数据算的
    assert kal.signature_feature_field() in ("ears", "body_features")


# ---------------- Case 9：Evaluator 不拿审阅字段当标准答案 ----------------


def test_case9_evaluator_ignores_assistant_reviewed(data_file) -> None:
    for field in ("hair", "eyes", "horns"):
        update_field_status(data_file, "艾雅法拉", field, status=REVIEWED, method="assistant_review")
    profile = _profile_from(data_file)
    evaluator = CharacterIdentityEvaluator(profile, verified_reference_count=0)
    assert evaluator.evaluable_fields() == ()
    assert evaluator._basis() == "none"


def test_case9_evaluator_scores_human_verified_fields(data_file) -> None:
    update_field_status(data_file, "艾雅法拉", "hair", status=HUMAN, method="human_review")
    profile = _profile_from(data_file)
    evaluator = CharacterIdentityEvaluator(profile, verified_reference_count=0)
    assert evaluator.evaluable_fields() == ("hair",)
    assert evaluator._basis() == "human_verified_profile"


# ---------------- Case 10：基准闸门 ----------------


def _bench_mod():
    spec = importlib.util.spec_from_file_location(
        "bench_mod_trust", REPO / "scripts" / "character_generation_benchmark.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_case10_benchmark_gate_blocks_reviewed_profile(tmp_path) -> None:
    mod = _bench_mod()
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    # 真实内置数据里艾雅法拉就是 assistant_reviewed / 0 张核验图
    ok, reasons = mod.ab_gate(cfg)
    assert ok is False
    joined = " | ".join(reasons)
    assert "human_verified_fields = 0" in joined
    assert "human_verified_references = 0" in joined


def test_case10_gate_reports_blocked_marker(tmp_path, capsys) -> None:
    mod = _bench_mod()
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    assert mod._print_gate(cfg) is False
    out = capsys.readouterr().out
    assert "BLOCKED_BY_HUMAN_REFERENCE" in out


# ---------------- Case 11 / 12：旧数据迁移 ----------------


def test_case11_legacy_assistant_review_becomes_reviewed() -> None:
    assert migrate_status(verified=True, method="assistant_review") is REVIEWED
    assert migrate_status(verified=False, method="assistant_review") is REVIEWED


def test_case12_legacy_human_review_becomes_human_verified() -> None:
    assert migrate_status(verified=True, method="human_review") is HUMAN


def test_case12_legacy_unknown_method_is_not_human_verified() -> None:
    """verified=true 但没记来源：不能自动升级成人工核验。"""
    assert migrate_status(verified=True, method="") is UNVERIFIED
    assert migrate_status(verified=True, method="scraped_blog") is UNVERIFIED


def test_case12_legacy_json_migrates_on_load(tmp_path) -> None:
    path = tmp_path / "characters.json"
    path.write_text(json.dumps({
        "characters": [{
            "canonical_name": "Test",
            "franchise": "F",
            "localized_names": ["测试"],
            "must_preserve": [],
            "visual_identity": {
                "hair": {"value": "银发", "verified": True, "verification_method": "assistant_review"},
                "eyes": {"value": "紫瞳", "verified": True, "verification_method": "human_review"},
                "clothing": {"value": "黑制服", "verified": True},
            },
        }]
    }, ensure_ascii=False), encoding="utf-8")
    profile = CharacterRegistry.from_file(path).resolve("Test")
    assert profile.visual_identity.field("hair").verification_status is REVIEWED
    assert profile.visual_identity.field("eyes").verification_status is HUMAN
    assert profile.visual_identity.field("clothing").verification_status is UNVERIFIED
    assert profile.effective_status() == "partial_verified"


# ---------------- 真实数据验收（本阶段完成条件） ----------------


def test_real_eyjafjalla_is_assistant_reviewed_pending_human() -> None:
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    assert profile is not None
    assert profile.effective_status() == "assistant_reviewed"
    assert len(profile.assistant_reviewed_fields()) == 8
    assert profile.human_verified_fields() == ()
    assert count_verified_references(_Cfg(), profile) == 0
    # 一个字段都不能悄悄变成 verified
    for key in profile.visual_identity.keys():
        assert profile.visual_identity.field(key).verified is False


def test_real_data_has_no_legacy_verified_flag() -> None:
    raw = json.loads(builtin_data_path().read_text(encoding="utf-8"))
    for entry in raw["characters"]:
        for field, data in (entry.get("visual_identity") or {}).items():
            if isinstance(data, dict):
                assert "verified" not in data, f"{entry['canonical_name']}.{field} 还留着旧字段"
                assert data.get("verification_status") in {
                    "unverified", "assistant_reviewed", "human_verified"
                }


def test_real_other_characters_stay_draft() -> None:
    registry = CharacterRegistry.load()
    for profile in registry.all():
        if profile.canonical_name == "Eyjafjalla":
            continue
        assert profile.effective_status() == "draft", profile.canonical_name
        assert profile.human_verified_fields() == ()


# ---------------- 参考图也要走同一套状态 ----------------


def test_reference_status_is_derived_not_written(tmp_path) -> None:
    from dl_senpai.character_registry import CharacterRegistry as Reg

    profile = Reg.load().resolve("艾雅法拉")
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    ok, _ = add_reference_to_library(
        cfg, profile, PNG, original_name="a.png", role="fullbody",
        verified=True, method="human_review", at="2026-10-27",
    )
    assert ok
    manifest = load_manifest(cfg, profile)
    entry = manifest.references[0]
    assert entry.verification_status is HUMAN
    assert entry.verified is True


def test_reference_assistant_review_cannot_be_verified(tmp_path) -> None:
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    ok, message = add_reference_to_library(
        cfg, profile, PNG, original_name="a.png", verified=True, method="assistant_review",
    )
    assert not ok
    assert "human_review" in message


def test_reference_reviewed_is_not_used_for_edit(tmp_path) -> None:
    """assistant_reviewed 的库图不许被当成 images.edit 的权威参考。"""
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    add_reference_to_library(
        cfg, profile, PNG, original_name="a.png", reviewed=True, method="assistant_review",
    )
    manifest = load_manifest(cfg, profile)
    assert manifest.references[0].verification_status is REVIEWED
    assert manifest.references[0].verified is False
    assert count_verified_references(cfg, profile) == 0


def test_manifest_legacy_verified_flag_migrates() -> None:
    entry = ReferenceEntry.from_dict({
        "file": "a.png", "verified": True, "verification_method": "assistant_review"
    })
    assert entry.verification_status is REVIEWED
    entry2 = ReferenceEntry.from_dict({
        "file": "b.png", "verified": True, "verification_method": "human_review"
    })
    assert entry2.verification_status is HUMAN
