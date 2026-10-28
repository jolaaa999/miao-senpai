"""人工核验流程 + 参考图库完整性 + 基准闸门。

这一层测试盯的是「verified 到底是不是人给的」：
- 字段核验只能通过写回库改，且必须带方法与时间；
- 参考图必须能被检出「文件被换过」（sha256）；
- 同一个文件不许导入两次；
- 有本地核验图时绝不许联网搜图；
- 基准脚本默认不花钱，双闸缺一不可。

所有用例不联网、不调付费生图 API。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

import dl_senpai.image_gen as image_gen
from dl_senpai.character_references import (
    add_reference_to_library,
    count_verified_references,
    resolve_library_reference,
    set_reference_verified,
    sha256_of,
)
from dl_senpai.character_registry import (
    CharacterRegistry,
    builtin_data_path,
    reset_character_registry,
    update_field_verification,
    update_profile_status_lock,
)

REPO = Path(__file__).resolve().parent.parent
PNG = b"\x89PNG\r\n\x1a\n" + b"benchmark-fixture" * 8
PNG2 = b"\x89PNG\r\n\x1a\n" + b"another-fixture" * 8


class _Cfg:
    draw_enable = True
    draw_verify = False
    draw_max_retry = 0
    draw_enhance = False
    draw_find_reference = True
    draw_max_prompt_chars = 900
    draw_timeout = 30.0
    draw_size = "1024x1024"
    draw_quality = ""
    draw_async = True
    identity_eval = False
    image_debug = False
    image_trace_file = ""
    character_enable = True
    character_min_confidence = 0.8
    character_dir = ""
    character_verify_reference = True
    character_web_reference_mode = "verify_only"
    character_web_reference_min_trust = 0.7
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
def _clean_registry():
    reset_character_registry()
    yield
    reset_character_registry()


@pytest.fixture
def data_file(tmp_path) -> Path:
    """一份内置角色数据的副本：测试绝不改真实档案。"""
    target = tmp_path / "characters.json"
    shutil.copyfile(builtin_data_path(), target)
    return target


def _profile_from(path: Path, name: str = "艾雅法拉"):
    return CharacterRegistry.from_file(path).resolve(name)


# ---------------- Case 1：verify 一个字段 ----------------


def test_case1_verify_field_recomputes_status(data_file) -> None:
    profile = _profile_from(data_file)
    before = profile.effective_status()
    assert before in ("draft", "assistant_reviewed", "partial_verified", "verified")

    ok, message = update_field_verification(
        data_file, "艾雅法拉", "hair", verified=True, method="human_review",

    )
    assert ok, message

    profile = _profile_from(data_file)
    assert profile.visual_identity.field("hair").verified is True
    assert profile.visual_identity.field("hair").verification_method == "human_review"
    assert profile.visual_identity.field("hair").verified_at == "2026-10-27"
    assert "hair" in profile.verified_fields()
    # 至少要从 draft 往前走一步
    assert profile.effective_status() in ("partial_verified", "verified")


def test_case1_cannot_verify_empty_field(data_file) -> None:
    """空字段不能靠"验证"变成事实。"""
    ok, message = update_field_verification(data_file, "艾雅法拉", "face", verified=True, method="human_review")
    assert ok is False
    assert "没有内容" in message


def test_case1_unknown_field_rejected(data_file) -> None:
    ok, message = update_field_verification(data_file, "艾雅法拉", "hairstyle", verified=True, method="human_review")
    assert ok is False and "未知字段" in message


# ---------------- Case 2：unverify 后不再进硬约束 ----------------


def test_case2_unverify_removes_hard_constraint(data_file) -> None:
    from dl_senpai.character_resolver import resolve_character
    from dl_senpai.image_prompt_builder import ImagePromptBuilder

    update_field_verification(data_file, "艾雅法拉", "horns", verified=True, method="human_review")
    profile = _profile_from(data_file)
    built = ImagePromptBuilder().build(
        user_request="画一张艾雅法拉", draft_prompt="",
        resolution=resolve_character("画一张艾雅法拉", registry=CharacterRegistry.from_file(data_file)),
    )
    assert "horns" in built.verified_fields

    ok, _ = update_field_verification(data_file, "艾雅法拉", "horns", verified=False, note="核验错了")
    assert ok
    profile = _profile_from(data_file)
    assert profile.visual_identity.field("horns").verified is False
    # 描述保留（下次核验的起点），但不再有 verified 元数据
    assert profile.visual_identity.get("horns")
    assert profile.visual_identity.field("horns").verification_method == ""

    built = ImagePromptBuilder().build(
        user_request="画一张艾雅法拉", draft_prompt="",
        resolution=resolve_character("画一张艾雅法拉", registry=CharacterRegistry.from_file(data_file)),
    )
    # 撤销后 horns 回到 unverified：既不进硬约束，也不进"模型审阅提示"，
    # 更不该出现在提示词里（unverified = Level D，不写入）
    assert "horns" not in built.human_verified_fields
    assert "horns" not in built.assistant_reviewed_fields
    assert "一对弯曲的羊角" not in built.identity_block


def test_case2_status_derivation_is_generic(data_file) -> None:
    """状态必须由字段推导，不接受手写字符串升级。

    用凯尔希做样本：她一个字段都没核验，手写 verified 也不该生效。
    （艾雅法拉在本阶段已核验，不能拿来当"未核验"的样本。）
    """
    profile = _profile_from(data_file, "凯尔希")
    assert profile.verified_fields() == ()

    ok, _ = update_profile_status_lock(data_file, "凯尔希", "verified")
    assert ok
    profile = _profile_from(data_file, "凯尔希")
    assert profile.effective_status() == "draft", "手写 verified 不能凭空升级"

    # 显式 draft 是保守方向，允许锁定
    ok, _ = update_profile_status_lock(data_file, "凯尔希", "draft")
    assert ok
    assert _profile_from(data_file, "凯尔希").effective_status() == "draft"

    # 清掉手写值，让推导接管
    ok, _ = update_profile_status_lock(data_file, "凯尔希", "")
    assert ok
    from dl_senpai.character_registry import find_raw_entry

    raw = json.loads(data_file.read_text(encoding="utf-8"))
    entry = find_raw_entry(raw, "凯尔希")
    assert entry is not None and "profile_status" not in entry


# ---------------- Case 3：导入 verified reference ----------------


def test_case3_add_verified_reference(tmp_path) -> None:
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    profile = CharacterRegistry.load().resolve("艾雅法拉")

    ok, message = add_reference_to_library(
        cfg, profile, PNG,
        original_name="eyja_official.png", role="fullbody", priority=100,
        verified=True, method="human_review", at="2026-10-27",
        source_type="official", source_url="https://example.invalid/eyja",
    )
    assert ok, message

    manifest_path = tmp_path / "refs" / "arknights" / "eyjafjalla" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["references"][0]
    assert entry["verification_status"] == "human_verified"
    assert entry["sha256"] == sha256_of(PNG)
    assert entry["reference_role"] == "fullbody"
    assert entry["verification_method"] == "human_review"
    assert manifest["verified"] is True

    cand = resolve_library_reference(cfg, profile)
    assert cand is not None
    assert cand.verified is True
    assert cand.role == "fullbody"
    assert cand.raw == PNG
    assert count_verified_references(cfg, profile) == 1


def test_case3_role_preference_by_prompt(tmp_path) -> None:
    """头像要肖像图、全身要全身图——极简规则，但必须生效。"""
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    add_reference_to_library(cfg, profile, PNG, original_name="a.png", role="fullbody", priority=10, verified=True, method="human_review")
    add_reference_to_library(cfg, profile, PNG2, original_name="b.png", role="portrait", priority=10, verified=True, method="human_review")


    full = resolve_library_reference(cfg, profile, user_request="画一张全身立绘")
    head = resolve_library_reference(cfg, profile, user_request="画个头像")
    assert full is not None and full.role == "fullbody"
    assert head is not None and head.role == "portrait"


# ---------------- Case 4：文件被改 → sha 不符 → 拒绝 ----------------


def test_case4_tampered_reference_is_rejected(tmp_path) -> None:
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    ok, _ = add_reference_to_library(cfg, profile, PNG, original_name="a.png", verified=True, method="human_review")
    assert ok

    char_dir = tmp_path / "refs" / "arknights" / "eyjafjalla"
    target = next(char_dir.glob("*.png"))
    target.write_bytes(PNG2)          # 文件被换成另一张图

    diags: list[str] = []
    cand = resolve_library_reference(cfg, profile, diagnostics=diags)
    assert cand is None, "sha256 不符的参考图绝不能被用于 images.edit"
    assert any("reference_integrity_failed" in d for d in diags)
    assert count_verified_references(cfg, profile) == 1   # 清单没变，仍是 1 条（但要被拦住）


def test_case4_non_image_reference_is_rejected(tmp_path) -> None:
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    ok, _ = add_reference_to_library(cfg, profile, PNG, original_name="a.png", verified=True, method="human_review")
    assert ok
    char_dir = tmp_path / "refs" / "arknights" / "eyjafjalla"
    next(char_dir.glob("*.png")).write_bytes(b"not an image at all")

    diags: list[str] = []
    assert resolve_library_reference(cfg, profile, diagnostics=diags) is None
    assert any("not_an_image" in d or "reference_integrity_failed" in d for d in diags)


# ---------------- Case 5：重复参考图 ----------------


def test_case5_duplicate_reference_rejected(tmp_path) -> None:
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    ok, _ = add_reference_to_library(cfg, profile, PNG, original_name="a.png", verified=True, method="human_review")
    assert ok

    ok2, message2 = add_reference_to_library(cfg, profile, PNG, original_name="a_copy.png", verified=True, method="human_review")
    assert ok2 is False
    assert "already exists" in message2

    char_dir = tmp_path / "refs" / "arknights" / "eyjafjalla"
    assert len(list(char_dir.glob("*.png"))) == 1, "同一张图不该被编号成多份"


def test_case5_add_rejects_non_image_and_oversize(tmp_path) -> None:
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    ok, msg = add_reference_to_library(cfg, profile, b"plain text")
    assert ok is False and "不是有效图片" in msg
    ok, msg = add_reference_to_library(cfg, profile, b"\x89PNG\r\n\x1a\n" + b"x" * (9 * 1024 * 1024))
    assert ok is False and "过大" in msg


def test_case5_unverify_reference_keeps_file(tmp_path) -> None:
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    add_reference_to_library(cfg, profile, PNG, original_name="a.png", verified=True, method="human_review")
    name = next((tmp_path / "refs" / "arknights" / "eyjafjalla").glob("*.png")).name

    ok, _ = set_reference_verified(cfg, profile, name, verified=False, note="核验错了")
    assert ok
    assert count_verified_references(cfg, profile) == 0
    # 撤销核验不删文件：图还在，只是不再被信任
    assert (tmp_path / "refs" / "arknights" / "eyjafjalla" / name).is_file()
    assert resolve_library_reference(cfg, profile) is None


# ---------------- Case 6：有本地核验图 → 绝不联网 ----------------


@pytest.mark.asyncio
async def test_case6_local_verified_skips_web_search(monkeypatch, tmp_path) -> None:
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    add_reference_to_library(cfg, profile, PNG, original_name="a.png", role="fullbody", verified=True, method="human_review")

    async def _web_must_not_run(*a, **kw):
        raise AssertionError("本地已有核验参考图，不该联网搜图")

    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", _web_must_not_run)

    out = tmp_path / "out.png"
    out.write_bytes(PNG)
    calls: list[dict] = []

    async def _fake(prompt, config=None, *, reference=None, fresh=False):
        calls.append({"reference": reference})
        return out

    monkeypatch.setattr(image_gen, "generate_image_file", _fake)
    trace_file = tmp_path / "t.jsonl"

    await image_gen.build_image_segment(
        marker_prompt="",
        user_request="画一张艾雅法拉站在火山前",
        config=_Cfg(character_reference_dir=str(tmp_path / "refs"), image_debug=True,
                    image_trace_file=str(trace_file)),
        auto_reference=True,
    )
    assert calls[0]["reference"] is not None
    record = json.loads(trace_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["reference_mode"] == "local_verified"
    assert record["reference_verified"] is True
    assert record["reference_source_trust"] == 1.0
    assert record["generation_mode"] == "edit"


@pytest.mark.asyncio
async def test_case6_local_only_never_falls_back_to_web(monkeypatch, tmp_path) -> None:
    """local_only 组就算本地图被篡改，也只允许退化成 text_only，不许偷偷联网。

    这条是实测踩出来的：CRLF 文件里一次 replace 静默失败，导致「只用本地库」的
    那组回退到网图搜索，A/B 的两个变量同时变了。
    """
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    profile = CharacterRegistry.load().resolve("艾雅法拉")
    add_reference_to_library(cfg, profile, PNG, original_name="a.png", verified=True, method="human_review")
    char_dir = tmp_path / "refs" / "arknights" / "eyjafjalla"
    next(char_dir.glob("*.png")).write_bytes(PNG2)   # 篡改

    async def _web_must_not_run(*a, **kw):
        raise AssertionError("local_only 模式不许联网搜图")

    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", _web_must_not_run)

    out = tmp_path / "out.png"
    out.write_bytes(PNG)
    seen: list[dict] = []

    async def _fake(prompt, config=None, *, reference=None, fresh=False):
        seen.append({"reference": reference})
        return out

    monkeypatch.setattr(image_gen, "generate_image_file", _fake)
    trace_file = tmp_path / "t.jsonl"
    await image_gen.build_image_segment(
        marker_prompt="",
        user_request="画一张艾雅法拉站在火山前",
        config=_Cfg(character_reference_dir=str(tmp_path / "refs"), image_debug=True,
                    image_trace_file=str(trace_file)),
        auto_reference=True,
        reference_policy="local_only",
    )
    assert seen[0]["reference"] is None
    record = json.loads(trace_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["generation_mode"] == "text_only"
    assert record["reference_mode"] not in ("web_verified", "web_probe_only")
    assert "reference_integrity_failed" in (record.get("extra") or {})


# ---------------- Case 7 / 8：基准闸门 ----------------


def _bench_module():
    spec = importlib.util.spec_from_file_location(
        "bench_mod", REPO / "scripts" / "character_generation_benchmark.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_case7_dry_run_never_generates(monkeypatch) -> None:
    bench = _bench_module()

    async def _must_not_generate(*a, **kw):
        raise AssertionError("dry-run 不许调用生图")

    import dl_senpai.image_gen as ig

    monkeypatch.setattr(ig, "build_image_segment", _must_not_generate)
    monkeypatch.setattr("sys.argv", ["bench", "--strategies", "text_only,local_verified_1"])
    # dry-run 里会解析角色、构建 prompt，但绝不生成
    rc = await bench.main()
    assert rc == 0


def test_case8_paid_requires_both_gates(monkeypatch) -> None:
    bench = _bench_module()

    class Args:
        # 三处闸门：--run-paid、--allow-paid、环境变量。少一个都不许花钱。
        run_paid = True
        allow_paid = True

    monkeypatch.delenv(bench._ENV_FLAG, raising=False)
    assert bench._paid_allowed(Args()) is False, "只有 --allow-paid 不够"

    monkeypatch.setenv(bench._ENV_FLAG, "true")
    assert bench._paid_allowed(Args()) is True

    class Args2:
        run_paid = True
        allow_paid = False

    assert bench._paid_allowed(Args2()) is False, "只有环境变量也不够"


def test_case8_strategy_cap_enforced(monkeypatch) -> None:
    bench = _bench_module()

    class Args:
        strategies = "text_only,local_verified_1,text_only"

    with pytest.raises(SystemExit):
        bench._strategies_of(Args())


# ---------------- Case 9 / 10：无退化 ----------------


def test_case9_other_characters_stay_draft() -> None:
    """只处理艾雅法拉：其余角色不许被顺手升级。"""
    registry = CharacterRegistry.load()
    for name in ("凯尔希", "阿米娅", "雷电将军", "芙宁娜", "初音未来"):
        profile = registry.resolve(name)
        assert profile is not None, name
        assert profile.effective_status() == "draft", f"{name} 不该被改动"
        assert profile.verified_fields() == ()


def test_case9_unknown_character_still_unknown() -> None:
    from dl_senpai.character_resolver import resolve_character
    from dl_senpai.image_prompt_builder import ImagePromptBuilder

    res = resolve_character("画一只橘猫在窗台上")
    built = ImagePromptBuilder().build(user_request="画一只橘猫在窗台上", draft_prompt="橘猫", resolution=res)
    assert built.kind == "general"
    assert "identity preservation" not in built.text.lower()


@pytest.mark.asyncio
async def test_case10_general_image_untouched(monkeypatch, tmp_path) -> None:
    out = tmp_path / "out.png"
    out.write_bytes(PNG)
    prompts: list[str] = []

    async def _fake(prompt, config=None, *, reference=None, fresh=False):
        prompts.append(prompt)
        return out

    monkeypatch.setattr(image_gen, "generate_image_file", _fake)
    trace_file = tmp_path / "t.jsonl"
    await image_gen.build_image_segment(
        marker_prompt="",
        user_request="画一只橘猫",
        config=_Cfg(image_debug=True, image_trace_file=str(trace_file)),
        auto_reference=False,
    )
    low = prompts[0].lower()
    for banned in ("identity preservation", "supporting identity hints", "eyjafjalla"):
        assert banned not in low
    record = json.loads(trace_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["intent"] == "general_image"


# ---------------- 场景评分 ----------------


@pytest.mark.asyncio
async def test_scene_evaluator_parses_and_averages(monkeypatch) -> None:
    from dl_senpai.scene_eval import SceneComplianceEvaluator

    async def _chat(config, content, *, max_tokens=300):
        return json.dumps({
            "checks": {"火山/熔岩背景": 0.9, "半身构图（腰部以上）": 0.5, "自然表情": 0.7},
            "issues": ["构图偏全身"],
        })

    monkeypatch.setattr("dl_senpai.draw_enhance._chat", _chat)
    score = await SceneComplianceEvaluator(("火山/熔岩背景", "半身构图（腰部以上）", "自然表情")).evaluate(
        _Cfg(), PNG
    )
    assert score.overall == pytest.approx((0.9 + 0.5 + 0.7) / 3)
    assert score.verdict == "acceptable"
    assert score.issues == ("构图偏全身",)


@pytest.mark.asyncio
async def test_scene_evaluator_without_requirements_skips(monkeypatch) -> None:
    from dl_senpai.scene_eval import SceneComplianceEvaluator

    async def _chat_must_not_run(*a, **kw):
        raise AssertionError("没有场景要求就不该调用视觉模型")

    monkeypatch.setattr("dl_senpai.draw_enhance._chat", _chat_must_not_run)
    score = await SceneComplianceEvaluator(()).evaluate(_Cfg(), PNG)
    assert score.verdict == "not_evaluated"
