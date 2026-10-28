"""A/B 实验装置的行为验证（Case 1~12）。

这里测的**不是**"参考图有没有用"——那是实验结论，需要真实图片和有依据的身份档案。
这里测的是**装置本身**：

- 闸门会不会在数据不足时真的拦住（Case 1~4）；
- A 组会不会偷偷读参考图、B 组会不会读错图（Case 5~6）；
- 两组会不会偷跑联网（Case 7）；
- 两组的生成参数是不是真的完全一致（Case 8）；
- 评测器有没有被 strategy / prompt 污染（Case 9）；
- 泄漏指标是不是只挂在 B 组（Case 10）；
- 付费策略数会不会超限（Case 11）；
- 产物里有没有图片哈希（Case 12）。

全部用例不联网、不调付费生图、不写真实参考图库。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import dl_senpai.image_gen as image_gen
from dl_senpai.character_references import (
    add_reference_to_library,
    count_verified_references,
    set_reference_verified,
)
from dl_senpai.character_registry import (
    CharacterRegistry,
    VerificationStatus,
    builtin_data_path,
    reset_character_registry,
    update_field_status,
)
from dl_senpai import reference_eval

REPO = Path(__file__).resolve().parent.parent
PNG = b"\x89PNG\r\n\x1a\n" + b"ab-fixture" * 12
PNG_ALT = b"\x89PNG\r\n\x1a\n" + b"ab-fixture-alt" * 12

HUMAN = VerificationStatus.HUMAN_VERIFIED
REVIEWED = VerificationStatus.ASSISTANT_REVIEWED


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
    draw_size = "1536x2048"
    draw_quality = "high"
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

    def draw_api_key_resolved(self) -> str:
        return "sk-test-not-real"

    # image_gen / draw_* 会读到的其余配置项，测试里给固定假值
    active_api_key = "sk-test-not-real"
    active_base_url = "https://relay.example/v1"
    draw_dir = ""
    def draw_uses_dalle_params(self) -> bool:
        return False
    draw_vision_model = "glm-5.3"
    draw_reference_timeout = 5.0
    draw_reference_query_timeout = 5.0
    draw_reference_steps = 0
    draw_max_archive = 20
    draw_archive_dir = ""

    def draw_configured(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _reset():
    reset_character_registry()
    yield
    reset_character_registry()


@pytest.fixture
def bench():
    spec = importlib.util.spec_from_file_location(
        "bench_ab", REPO / "scripts" / "character_generation_benchmark.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def data_file(tmp_path) -> Path:
    target = tmp_path / "characters.json"
    shutil.copyfile(builtin_data_path(), target)
    return target


def _verify_fields(path: Path, fields, *, character: str = "艾雅法拉") -> None:
    for field in fields:
        ok, message = update_field_status(
            path, character, field, status=HUMAN, method="human_review", at="2026-10-27"
        )
        assert ok, message


def _profile_from(path: Path, name: str = "艾雅法拉"):
    return CharacterRegistry.from_file(path).resolve(name)


# ---------------- Case 1：human fields < 4 → BLOCKED ----------------


def test_case1_gate_blocks_insufficient_human_fields(data_file, tmp_path, bench, monkeypatch) -> None:
    _verify_fields(data_file, ("hair", "clothing"))          # 只有 2 个
    profile = _profile_from(data_file)
    monkeypatch.setattr(bench, "_profile", lambda cfg: profile)
    ok, reasons = bench.ab_gate(_Cfg(character_reference_dir=str(tmp_path / "refs")))
    assert ok is False
    assert any("human_verified_fields = 2 < 4" in r for r in reasons)


def test_case1_gate_blocks_missing_coverage(data_file, tmp_path, bench, monkeypatch) -> None:
    # 数量够 4 个，但没覆盖 palette 和标志性特征 horns
    _verify_fields(data_file, ("hair", "clothing", "eyes", "race"))
    profile = _profile_from(data_file)
    monkeypatch.setattr(bench, "_profile", lambda cfg: profile)
    ok, reasons = bench.ab_gate(_Cfg(character_reference_dir=str(tmp_path / "refs")))
    assert ok is False
    joined = " | ".join(reasons)
    assert "palette" in joined and "horns" in joined


# ---------------- Case 2：reference = 0 → BLOCKED ----------------


def test_case2_gate_blocks_without_verified_reference(data_file, tmp_path, bench, monkeypatch) -> None:
    _verify_fields(data_file, ("hair", "clothing", "palette", "horns", "ears"))
    profile = _profile_from(data_file)
    monkeypatch.setattr(bench, "_profile", lambda cfg: profile)
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    assert count_verified_references(cfg, profile) == 0
    ok, reasons = bench.ab_gate(cfg)
    assert ok is False
    assert any("human_verified_references = 0" in r for r in reasons)


# ---------------- Case 3：assistant_reviewed 不满足 gate ----------------


def test_case3_assistant_reviewed_never_satisfies_gate(bench, tmp_path) -> None:
    """真实数据里艾雅法拉就是 8 个 assistant_reviewed + 0 张图 → 必须 BLOCKED。"""
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    profile = bench._profile(cfg)
    assert profile.effective_status() == "assistant_reviewed"
    assert profile.human_verified_fields() == ()
    ok, reasons = bench.ab_gate(cfg)
    assert ok is False
    assert any("human_verified_fields = 0" in r for r in reasons)


# ---------------- Case 4：sha256 不符 → REFERENCE_INTEGRITY_FAILED ----------------


def test_case4_tampered_reference_blocks_run(data_file, tmp_path, bench, monkeypatch) -> None:
    _verify_fields(data_file, ("hair", "clothing", "palette", "horns", "ears"))
    profile = _profile_from(data_file)
    monkeypatch.setattr(bench, "_profile", lambda cfg: profile)
    refs = tmp_path / "refs"
    cfg = _Cfg(character_reference_dir=str(refs))
    ok, message = add_reference_to_library(
        cfg, profile, PNG, original_name="a.png", role="fullbody",
        verified=True, method="human_review",
    )
    assert ok, message
    # 图被换掉，但清单还记着旧 hash
    next((refs / "arknights" / "eyjafjalla").glob("*.png")).write_bytes(PNG_ALT)

    raw, note = bench._load_verified_reference(cfg, profile)
    assert raw is None
    assert "reference_integrity_failed" in note


# ---------------- Case 5 / 6：A 不读参考图，B 读指定那一张 ----------------


@pytest.fixture
def stub_run(tmp_path, monkeypatch, bench):
    """把生成与三项评测全部 stub 掉：验证数据流，不花钱、不联网。"""
    out = tmp_path / "gen.png"
    out.write_bytes(PNG)
    calls: dict = {"generate": [], "evaluate": [], "reference_side": []}

    async def _fake_generate(prompt, config=None, *, reference=None, fresh=False):
        calls["generate"].append({"reference": reference})
        return out

    async def _fake_eval(cfg, profile, image, scene_checks):
        calls["evaluate"].append(len(image))
        return {"identity_score": 0.5, "scene_score": 0.5, "visual_quality": 0.5,
                "identity_dimensions": {}, "issues": []}

    async def _fake_ref_side(cfg, target, reference):
        calls["reference_side"].append(len(reference))
        return {"reference_identity_similarity": 0.8,
                "reference_leakage": {"pose_leakage": 0.1}, "issues": []}

    # 关键：dl_senpai 与 plugins.dl_senpai 是**两个模块对象**（同一文件导入两次）。
    # 只 patch 一个的话，另一个照旧发真实网络请求——实测白等 113 秒才失败。
    # 所以两处都打，并且下面断言 stub 真的被调用过。
    import importlib
    import sys as _sys

    if str(REPO / "src") not in _sys.path:
        _sys.path.insert(0, str(REPO / "src"))
    # benchmark 用 "plugins.dl_senpai.*" 这名字导入，和测试里的 "dl_senpai.*"
    # 是两个不同的模块对象（同一文件被导入两次）。两个都要打补丁。
    image_gen_p = importlib.import_module("plugins.dl_senpai.image_gen")
    for mod in (image_gen, image_gen_p):
        monkeypatch.setattr(mod, "generate_image_file", _fake_generate, raising=True)
    monkeypatch.setattr(bench, "_evaluate_image", _fake_eval)
    monkeypatch.setattr(bench, "_evaluate_reference_side", _fake_ref_side)
    # 任何视觉调用都不许漏网
    async def _no_chat(*a, **kw):
        raise AssertionError("测试里不该调用视觉模型")
    monkeypatch.setattr("dl_senpai.draw_enhance._chat", _no_chat, raising=False)
    monkeypatch.setattr("plugins.dl_senpai.draw_enhance._chat", _no_chat, raising=False)
    return SimpleNamespace(calls=calls, out=out)


@pytest.mark.asyncio
async def test_case5_text_only_never_reads_reference(bench, stub_run, tmp_path, monkeypatch) -> None:
    async def _web_must_not_run(*a, **kw):
        raise AssertionError("A 组不许联网")

    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", _web_must_not_run)
    row = await bench._generate_one(
        _Cfg(), "text_only", bench.STRATEGIES["text_only"],
        bench.SCENARIO["prompt"], bench.SCENARIO["scene_checks"], tmp_path, PNG,
    )
    assert row["success"] is True
    assert stub_run.calls["generate"], "stub 没被调用说明 patch 落空，测试会真的联网"
    assert stub_run.calls["generate"][0]["reference"] is None
    assert row["generation_mode"] == "generate"
    # A 组不该跑"像不像原图"这一票
    assert stub_run.calls["reference_side"] == []
    assert row.get("reference_identity_similarity") is None


@pytest.mark.asyncio
async def test_case6_local_verified_uses_the_verified_reference(
    data_file, tmp_path, bench, monkeypatch, stub_run
) -> None:
    _verify_fields(data_file, ("hair", "clothing", "palette", "horns", "ears"))
    profile = _profile_from(data_file)
    monkeypatch.setattr(bench, "_profile", lambda cfg: profile)
    calls = {"find": 0}

    async def _web_must_not_run(*a, **kw):
        calls["find"] += 1
        raise AssertionError("B 组也不许联网")

    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", _web_must_not_run)

    # 走真实路径：图先真进库（human_verified），B 组再从库取
    refs = tmp_path / "refs"
    cfg = _Cfg(character_reference_dir=str(refs))
    ok, message = add_reference_to_library(
        cfg, profile, PNG, original_name="eyja.png", role="fullbody",
        verified=True, method="human_review", at="2026-10-27",
    )
    assert ok, message

    row = await bench._generate_one(
        cfg, "local_verified_1",
        bench.STRATEGIES["local_verified_1"], bench.SCENARIO["prompt"],
        bench.SCENARIO["scene_checks"], tmp_path, PNG_ALT,
    )
    assert row["success"] is True
    # B 组必须带参考图进生成
    assert stub_run.calls["generate"][0]["reference"] is not None
    assert row["generation_mode"] == "edit"
    # 而且泄漏/相似度这一票必须真的跑了
    assert stub_run.calls["reference_side"] == [len(PNG_ALT)]
    assert calls["find"] == 0


# ---------------- Case 7：A/B 都不许联网 ----------------


@pytest.mark.asyncio
async def test_case7_neither_group_searches_web(bench, stub_run, tmp_path, monkeypatch) -> None:
    def _boom(*a, **kw):
        raise AssertionError("基准实验里不许出现网图搜索")

    monkeypatch.setattr("dl_senpai.draw_reference.find_reference_image", _boom)
    for name in ("text_only", "local_verified_1"):
        row = await bench._generate_one(
            _Cfg(), name, bench.STRATEGIES[name], bench.SCENARIO["prompt"],
            bench.SCENARIO["scene_checks"], tmp_path, PNG,
        )
        assert row["success"] is True


def test_case7_policies_are_local_only_or_text_only(bench) -> None:
    for name, spec in bench.STRATEGIES.items():
        assert spec["reference_policy"] in ("text_only", "local_only"), name
        assert len([name]) <= bench.MAX_PAID_CALLS


# ---------------- Case 8：生成参数完全一致 ----------------


@pytest.mark.asyncio
async def test_case8_generation_params_identical(bench, stub_run, tmp_path) -> None:
    cfg = _Cfg()
    rows = []
    for name in ("text_only", "local_verified_1"):
        rows.append(await bench._generate_one(
            cfg, name, bench.STRATEGIES[name], bench.SCENARIO["prompt"],
            bench.SCENARIO["scene_checks"], tmp_path, PNG,
        ))
    a, b = rows
    assert a["generation_params"] == b["generation_params"]
    assert a["generation_params"]["model"] == "gpt-image-2.5-flare"
    assert a["generation_params"]["size"] == b["generation_params"]["size"] == "1536x2048"
    assert a["generation_params"]["quality"] == b["generation_params"]["quality"]


def test_case8_prompt_is_shared(bench) -> None:
    """两组必须用同一句 prompt：它只存在于 SCENARIO 里，没有 per-strategy 覆盖。"""
    assert bench.SCENARIO["prompt"].startswith("画一张艾雅法拉站在火山前的半身插画")
    for spec in bench.STRATEGIES.values():
        assert "prompt" not in spec


# ---------------- Case 9：Evaluator 收不到 strategy metadata ----------------


def test_case9_reference_evaluators_take_images_only() -> None:
    for fn in (reference_eval.evaluate_reference_similarity,
               reference_eval.evaluate_reference_leakage,
               reference_eval.evaluate_visual_quality):
        params = list(inspect.signature(fn).parameters)
        assert params[:1] == ["config"]
        for banned in ("strategy", "prompt", "model", "generation_mode", "reference_mode"):
            assert banned not in params, f"{fn.__name__} 不该接收 {banned}"


def test_case9_identity_evaluator_takes_images_only() -> None:
    from dl_senpai.identity_eval import CharacterIdentityEvaluator

    params = list(inspect.signature(CharacterIdentityEvaluator.evaluate).parameters)
    assert params[:2] == ["self", "config"]
    for banned in ("strategy", "prompt", "model", "generation_mode"):
        assert banned not in params


# ---------------- Case 10：只有 B 算 leakage ----------------


@pytest.mark.asyncio
async def test_case10_only_group_b_reports_leakage(bench, stub_run, tmp_path) -> None:
    a = await bench._generate_one(
        _Cfg(), "text_only", bench.STRATEGIES["text_only"],
        bench.SCENARIO["prompt"], bench.SCENARIO["scene_checks"], tmp_path, PNG,
    )
    b = await bench._generate_one(
        _Cfg(), "local_verified_1", bench.STRATEGIES["local_verified_1"],
        bench.SCENARIO["prompt"], bench.SCENARIO["scene_checks"], tmp_path, PNG,
    )
    assert "reference_leakage" not in a and "reference_identity_similarity" not in a
    assert "reference_leakage" in b
    assert "reference_identity_similarity" in b


def test_case10_leakage_score_shape() -> None:
    score = reference_eval.LeakageScore(
        dimensions={"pose_leakage": 0.1, "composition_leakage": 0.2,
                    "background_leakage": 0.0, "style_leakage": 0.3},
        overall=0.15, verdict="acceptable",
    )
    data = score.as_dict()
    assert set(data["reference_leakage"]) == {
        "pose_leakage", "composition_leakage", "background_leakage", "style_leakage"
    }
    assert data["reference_leakage_verdict"] == "acceptable"


def test_case10_verdict_thresholds() -> None:
    assert reference_eval._leakage_verdict(0.1) == "acceptable"
    assert reference_eval._leakage_verdict(0.3) == "mild_leakage"
    assert reference_eval._leakage_verdict(0.5) == "noticeable_leakage"
    assert reference_eval._leakage_verdict(0.9) == "heavy_leakage"


# ---------------- Case 11：付费最多两个策略 ----------------


def test_case11_strategy_cap(bench) -> None:
    args = SimpleNamespace(strategies="text_only,local_verified_1")
    assert bench._strategies_of(args) == ["text_only", "local_verified_1"]
    with pytest.raises(SystemExit):
        bench._strategies_of(SimpleNamespace(strategies="text_only,local_verified_1,text_only"))
    with pytest.raises(SystemExit):
        bench._strategies_of(SimpleNamespace(strategies="nope"))


def test_case11_paid_needs_both_gates(bench, monkeypatch) -> None:
    monkeypatch.delenv(bench._ENV_FLAG, raising=False)
    args = SimpleNamespace(run_paid=True, allow_paid=True)
    assert bench._paid_allowed(args) is False
    monkeypatch.setenv(bench._ENV_FLAG, "true")
    assert bench._paid_allowed(args) is True
    assert bench._paid_allowed(SimpleNamespace(run_paid=True, allow_paid=False)) is False


# ---------------- Case 12：产物含图片 SHA256 ----------------


@pytest.mark.asyncio
async def test_case12_artifacts_include_image_sha256(bench, stub_run, tmp_path) -> None:
    outdir = tmp_path / "run"
    rows = []
    for name in ("text_only", "local_verified_1"):
        rows.append(await bench._generate_one(
            _Cfg(), name, bench.STRATEGIES[name], bench.SCENARIO["prompt"],
            bench.SCENARIO["scene_checks"], outdir, PNG,
        ))
    bench._write_artifacts(outdir, rows, {"character": "Eyjafjalla"})

    result = json.loads((outdir / "result.json").read_text(encoding="utf-8"))
    assert result["sample_size"] == "1 pair"
    shas = {r["strategy"]: r["image_sha256"] for r in result["rows"]}
    assert shas["text_only"] and shas["local_verified_1"]
    assert all(len(v) == 64 for v in shas.values())
    # 落盘的图能对上哈希，防止图片被换掉后旧结论还在引用
    for row in result["rows"]:
        raw = (outdir / row["image"]).read_bytes()
        assert bench._sha256(raw) == row["image_sha256"]
    assert (outdir / "human_review_report.md").is_file()


def test_case12_human_review_report_is_blind(bench, tmp_path) -> None:
    rows = [
        {"strategy": "text_only", "image": "A_text_only.png", "image_sha256": "b" * 64,
         "identity_score": 0.4, "scene_score": 0.9, "generation_mode": "generate"},
        {"strategy": "local_verified_1", "image": "B_local_verified.png", "image_sha256": "a" * 64,
         "identity_score": 0.7, "scene_score": 0.8, "generation_mode": "edit"},
    ]
    bench._write_artifacts(tmp_path / "run", rows, {})
    text = (tmp_path / "run" / "human_review_report.md").read_text(encoding="utf-8")
    head = text.split("<details>")[0]
    # 正文前半段不能出现"A 是参考图"这种提示
    assert "A_text_only" not in head
    assert "B_local_verified" not in head
    assert "Image 1" in head and "Image 2" in head
    assert "更像艾雅法拉的是" in head


# ---------------- 决策分类 ----------------


def test_classify_verdicts(bench) -> None:
    def rows(ai, bi, asc=0.8, bsc=0.8, leak=0.1):
        return [
            {"strategy": "text_only", "success": True, "identity_score": ai, "scene_score": asc},
            {"strategy": "local_verified_1", "success": True, "identity_score": bi,
             "scene_score": bsc, "reference_leakage_overall": leak},
        ]

    assert bench._classify(rows(0.5, 0.7))["verdict"] == "STRONG_WIN"
    assert bench._classify(rows(0.5, 0.55))["verdict"] == "WEAK_WIN"
    assert bench._classify(rows(0.5, 0.7, bsc=0.5))["verdict"] == "TRADEOFF"
    assert bench._classify(rows(0.5, 0.7, leak=0.8))["verdict"] == "TRADEOFF"
    assert bench._classify(rows(0.7, 0.5))["verdict"] == "NO_BENEFIT"
    assert bench._classify(rows(None, 0.5))["verdict"] == "NOT_EVALUATED"


# ---------------- 当前真实状态必须是 BLOCKED ----------------


def test_real_state_is_blocked(bench, tmp_path, capsys) -> None:
    cfg = _Cfg(character_reference_dir=str(tmp_path / "refs"))
    assert bench._print_gate(cfg) is False
    assert "BLOCKED_BY_HUMAN_REFERENCE" in capsys.readouterr().out
