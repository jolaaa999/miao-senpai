"""参考图流水线：找 3 张 → 交叉核对 → 只送 1 张锚定。

这里测的都是"降级链对不对"，因为这条链唯一不能出的错是
「一个环节出错，用户一张图都拿不到」。全部 mock，零联网零付费。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

pipeline = importlib.import_module("plugins.dl_senpai.draw_pipeline")
subagent = importlib.import_module("plugins.dl_senpai.subagent")
enhance = importlib.import_module("plugins.dl_senpai.draw_enhance")
image_gen = importlib.import_module("plugins.dl_senpai.image_gen")

RefCandidate = pipeline.RefCandidate
RefAnalysis = pipeline.RefAnalysis


@pytest.fixture(autouse=True)
def _isolate_reference_blocklist(tmp_path, monkeypatch):
    """把参考图黑名单指到临时目录。

    为什么必须隔离：这份黑名单默认写在 entity_resolver 的缓存目录
    （src/plugins/dl_senpai/data/reference_blocklist.json），是真机跑出来的。
    2026-10-27 真机上 Eyjafjalla 被标过「网图与设定不符」，于是三个用例
    在 collect_references 里直接 skipped=known_unusable、拿到 0 张候选——
    报错看着像流水线逻辑坏了，实际是测试读到了真机数据。
    """
    monkeypatch.setattr(
        pipeline, "_blocklist_path", lambda: tmp_path / "reference_blocklist.json"
    )

PNG = b"\x89PNG\r\n\x1a\n" + b"ref-a" * 40
PNG_B = b"\x89PNG\r\n\x1a\n" + b"ref-b" * 40
PNG_C = b"\x89PNG\r\n\x1a\n" + b"ref-c" * 40


class _Cfg:
    character_enable = True
    character_min_confidence = 0.8
    character_dir = ""
    character_reference_dir = ""
    character_verify_reference = False
    character_web_reference_mode = "verify_only"
    character_web_reference_min_trust = 0.7
    identity_eval = False
    draw_enable = True
    draw_verify = False
    draw_max_retry = 0
    draw_enhance = False
    draw_find_reference = True
    draw_reference_pipeline = True
    draw_web_reference_for_general = False
    draw_max_prompt_chars = 1600
    draw_timeout = 180.0
    draw_size = "1024x1024"
    draw_quality = "high"
    draw_async = True
    draw_entity_dynamic_resolve = True
    draw_entity_cache_ttl = 0
    draw_vision_model = "glm-5.3"
    draw_reference_timeout = 5.0
    draw_reference_query_timeout = 5.0
    draw_reference_steps = 0
    draw_max_archive = 20
    draw_archive_dir = ""
    image_debug = False
    image_trace_file = ""
    llm_provider = "relay"
    active_api_key = "sk-test-not-real"
    active_base_url = "https://relay.example/v1"

    def __init__(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)

    def draw_model_resolved(self) -> str:
        return "gpt-image-2.5-flare"

    def draw_base_url_resolved(self) -> str:
        return "https://relay.example/v1"

    def draw_api_key_resolved(self) -> str:
        return "sk-test-not-real"

    def draw_uses_dalle_params(self) -> bool:
        return False

    def draw_configured(self) -> bool:
        return True


# ---------------- 收集顺序 ----------------

@pytest.mark.asyncio
async def test_user_image_comes_first_and_wins(monkeypatch) -> None:
    async def _no_web(*a, **kw):
        raise AssertionError("用户已经给了图，不该再去搜")

    monkeypatch.setattr(pipeline, "_web_candidates", _no_web)
    cands = await pipeline.collect_references(
        _Cfg(), user_request="照这张画画她在沙滩", user_pair=("me.jpg", PNG)
    )
    assert len(cands) == 1, "用户给了图就不再联网补，避免把主体带偏"
    assert cands[0].source == "user_image"
    assert cands[0].trust == 1.0
    anchor = pipeline.pick_anchor(cands, RefAnalysis(best_index=0))
    assert anchor is not None and anchor.source == "user_image"


@pytest.mark.asyncio
async def test_web_fills_up_to_three_and_dedupes(monkeypatch) -> None:
    hits = [{"image_url": f"https://x/{i}.png", "title": "t", "page_url": f"https://p/{i}"}
            for i in range(5)]
    # 第 2 个 URL 和第 1 个返回同样的字节 → 必须去重，不能占两个名额
    blobs = {f"https://x/{i}.png": (PNG if i in (0, 1) else (PNG_B if i == 2 else PNG_C))
             for i in range(5)}

    async def _search(args):
        return {"hits": hits}

    async def _dl(url, **kw):
        return blobs.get(url)

    monkeypatch.setattr(subagent, "_tool_image_search", _search)
    monkeypatch.setattr(pipeline, "_download", _dl)

    cands = await pipeline.collect_references(_Cfg(), user_request="画艾雅法拉", subject="Eyjafjalla")
    assert len(cands) == 3
    assert len({c.sha256 for c in cands}) == 3, "同一张图被两个 query 搜到，必须去重"


def test_is_image_bytes_rejects_html_and_accepts_real_images() -> None:
    """搜到的常常是 HTML 错误页，喂给 images.edit 必然 400。"""
    assert pipeline._is_image_bytes(b"<html>not an image</html>") is False
    assert pipeline._is_image_bytes(b"") is False
    assert pipeline._is_image_bytes(PNG) is True
    assert pipeline._is_image_bytes(b"\xff\xd8\xff" + b"jpg") is True
    assert pipeline._is_image_bytes(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x") is True


@pytest.mark.asyncio
async def test_search_failure_yields_empty_not_crash(monkeypatch) -> None:
    async def _boom(args):
        raise RuntimeError("搜索服务挂了")

    monkeypatch.setattr(subagent, "_tool_image_search", _boom)
    got = await pipeline._web_candidates("q", subject="s", target=3, timeout=1.0)
    assert got == []


# ---------------- 交叉核对 ----------------

@pytest.mark.asyncio
async def test_analysis_returns_traits_and_best_index(monkeypatch) -> None:
    async def _chat(config, content, **kw):
        assert sum(1 for c in content if c.get("type") == "image_url") == 3, "三张图要一次发过去"
        return json.dumps({"traits": ["银白长发", "紫色眼睛", "黑色制服"],
                           "best_index": 2, "reason": "第二张最清晰"})

    monkeypatch.setattr(enhance, "_chat", _chat)
    cands = [RefCandidate(f"{i}.png", PNG, "web:t") for i in range(3)]
    analysis = await pipeline.analyze_references(_Cfg(), cands, subject="Eyjafjalla")
    assert analysis.ok is True
    assert analysis.traits == ("银白长发", "紫色眼睛", "黑色制服")
    assert analysis.best_index == 1, "模型按 1-based 编号回答，内部要转成 0-based"


@pytest.mark.asyncio
async def test_analysis_out_of_range_falls_back_to_first(monkeypatch) -> None:
    async def _chat(config, content, **kw):
        return json.dumps({"traits": [], "best_index": 9, "reason": "x"})

    monkeypatch.setattr(enhance, "_chat", _chat)
    cands = [RefCandidate(f"{i}.png", PNG, "web:t") for i in range(2)]
    analysis = await pipeline.analyze_references(_Cfg(), cands)
    assert analysis.best_index == 0


@pytest.mark.asyncio
async def test_single_reference_skips_vision_call(monkeypatch) -> None:
    async def _boom(*a, **kw):
        raise AssertionError("只有一张图时不该浪费一次视觉调用")

    monkeypatch.setattr(enhance, "_chat", _boom)
    analysis = await pipeline.analyze_references(_Cfg(), [RefCandidate("a.png", PNG, "web:t")])
    assert analysis.traits == ()
    assert analysis.ok is True


@pytest.mark.asyncio
async def test_analysis_failure_degrades_gracefully(monkeypatch) -> None:
    async def _boom(*a, **kw):
        raise RuntimeError("视觉模型挂了")

    monkeypatch.setattr(enhance, "_chat", _boom)
    cands = [RefCandidate(f"{i}.png", PNG, "web:t") for i in range(3)]
    analysis = await pipeline.analyze_references(_Cfg(), cands)
    assert analysis.ok is False and analysis.traits == ()
    # 分析失败也要能选出图：退回第一张，不能因此一张图都出不来
    assert pipeline.pick_anchor(cands, analysis) is not None


@pytest.mark.asyncio
async def test_unusable_reference_is_remembered_and_skipped(monkeypatch, tmp_path) -> None:
    """网图源短期内不会变好，别每次都白搜两三分钟。

    2026-10-27 实测艾雅法拉连续三次搜到的都是棕发二创，每次都要先花
    75~248 秒找图 + 60 秒核对才放弃，然后才按档案画。
    """
    monkeypatch.setattr(pipeline, "_blocklist_path", lambda: tmp_path / "bl.json")
    assert pipeline.references_known_unusable("Eyjafjalla") is False

    pipeline.mark_references_unusable("Eyjafjalla", differences=("三张均为棕发",))
    assert pipeline.references_known_unusable("Eyjafjalla") is True
    assert pipeline.references_known_unusable("eyjafjalla") is True, "大小写不该影响"
    assert pipeline.references_known_unusable("Kiriko") is False, "不能连累别的角色"

    called = {"n": 0}

    async def _boom(*a, **kw):
        called["n"] += 1
        return []

    monkeypatch.setattr(pipeline, "_web_candidates", _boom)
    got = await pipeline.collect_references(
        _Cfg(), user_request="画艾雅法拉", subject="Eyjafjalla"
    )
    assert got == [] and called["n"] == 0, "命中黑名单就不该再联网"

    # 但用户自己发的图不受影响
    got2 = await pipeline.collect_references(
        _Cfg(), subject="Eyjafjalla", user_pair=("me.jpg", PNG)
    )
    assert len(got2) == 1 and got2[0].source == "user_image"


def test_blocklist_expires_and_tolerates_corruption(monkeypatch, tmp_path) -> None:
    path = tmp_path / "bl.json"
    monkeypatch.setattr(pipeline, "_blocklist_path", lambda: path)
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert pipeline.references_known_unusable("X") is False, "坏文件当空处理，不能炸"

    import json as _json, time as _time
    path.write_text(_json.dumps({"x": {"ts": _time.time() - 8 * 24 * 3600}}), encoding="utf-8")
    assert pipeline.references_known_unusable("X") is False, "超过 7 天要重新试"


@pytest.mark.asyncio
async def test_analysis_parses_consistency_flag(monkeypatch) -> None:
    async def _chat(config, content, **kw):
        # 设定必须真的发给模型，否则它没法判一致性
        assert "银白偏浅紫" in content[0]["text"]
        return json.dumps({"traits": ["棕色短发"], "best_index": 1, "reason": "x",
                           "consistent": False,
                           "differences": ["三张均为棕发，设定为银白偏浅紫"]})

    monkeypatch.setattr(enhance, "_chat", _chat)
    cands = [RefCandidate(f"{i}.png", PNG, "web:t") for i in range(3)]
    analysis = await pipeline.analyze_references(
        _Cfg(), cands, subject="Eyjafjalla", expectations="- 头发：银白偏浅紫的长发"
    )
    assert analysis.consistent is False
    assert analysis.differences == ("三张均为棕发，设定为银白偏浅紫",)


def test_analysis_defaults_to_consistent() -> None:
    """老模型不回这个字段时不能一律当成冲突，否则参考图永远用不上。"""
    assert RefAnalysis().consistent is True
    assert RefAnalysis().differences == ()


def test_traits_block_is_soft_wording() -> None:
    block = pipeline.traits_block(("银白长发", "紫色眼睛"))
    assert "supporting evidence" in block
    assert "not as a hard rule" in block
    assert pipeline.traits_block(()) == ""


def test_fewer_than_three_still_usable() -> None:
    """降级链：3 → 2 → 1 都要能出图。"""
    two = [RefCandidate("a.png", PNG, "web:t"), RefCandidate("b.png", PNG_B, "web:t")]
    anchor = pipeline.pick_anchor(two, RefAnalysis(best_index=1))
    assert anchor is not None and anchor.name == "b.png"
    assert pipeline.pick_anchor([], RefAnalysis()) is None


# ---------------- 主链路集成 ----------------

@pytest.fixture
def stub_gen(monkeypatch, tmp_path):
    out = tmp_path / "gen.png"
    out.write_bytes(PNG_C)
    seen: dict = {}

    async def _fake(prompt, config=None, *, reference=None, fresh=False):
        seen["prompt"] = prompt
        seen["reference"] = reference
        return out

    for name in ("dl_senpai.image_gen", "plugins.dl_senpai.image_gen"):
        try:
            monkeypatch.setattr(importlib.import_module(name), "generate_image_file", _fake, raising=True)
        except Exception:  # noqa: BLE001
            continue
    return seen


@pytest.fixture
def trace_spy(monkeypatch):
    traces = []
    mod = importlib.import_module("plugins.dl_senpai.image_trace")
    real = mod.new_trace
    monkeypatch.setattr(
        mod, "new_trace",
        lambda config, *, original_prompt: traces.append(
            real(config, original_prompt=original_prompt)
        ) or traces[-1],
    )
    return traces


@pytest.mark.asyncio
async def test_pipeline_three_refs_and_consensus_traits(monkeypatch, stub_gen, trace_spy) -> None:
    async def _search(args):
        return {"hits": [{"image_url": f"https://x/{i}.png", "title": "ark", "page_url": ""}
                         for i in range(4)]}

    blobs = {f"https://x/{i}.png": b for i, b in enumerate((PNG, PNG_B, PNG_C, PNG))}

    async def _dl(url, **kw):
        return blobs.get(url)

    async def _chat(config, content, **kw):
        return json.dumps({"traits": ["银白长发", "紫色眼睛"], "best_index": 2, "reason": "最清晰"})

    monkeypatch.setattr(subagent, "_tool_image_search", _search)
    monkeypatch.setattr(pipeline, "_download", _dl)
    monkeypatch.setattr(enhance, "_chat", _chat)

    await image_gen.build_image_segment(user_request="画一张艾雅法拉", config=_Cfg(), auto_reference=True)

    trace = trace_spy[-1]
    cands = trace.extra.get("reference_candidates") or []
    assert len(cands) == 3, "应该凑齐 3 张候选"
    assert trace.extra["reference_analysis"]["traits"] == ["银白长发", "紫色眼睛"]
    assert trace.reference_mode in ("web_verified", "local_verified")
    assert trace.extra.get("reference_anchor_sha256")
    # 锚点由视觉模型指定（best_index=2 → 0-based 1），不是盲拿 URL 顺序第一张
    import hashlib as _hl
    want = _hl.sha256(blobs["https://x/1.png"]).hexdigest()[:16]
    assert trace.extra["reference_anchor_sha256"] == want
    # 共识特征进了提示词，而且是软措辞
    assert "银白长发" in stub_gen["prompt"]
    assert "supporting evidence" in stub_gen["prompt"]
    # 只送一张进图生图
    assert stub_gen["reference"] is not None
    assert isinstance(stub_gen["reference"], tuple)
    assert trace.generation_mode == "edit"


@pytest.mark.asyncio
async def test_contradicting_references_are_not_used(monkeypatch, stub_gen, trace_spy) -> None:
    """2026-10-27 的真实事故：三张棕发二创互相"共识"一致，把档案的银白带偏了。

    代价是一张 4 分钟的错图 + 自检重画 + 超时，最后用户只收到「没画出来」。
    所以设定冲突的网图既不能当锚点，它的"共识特征"也不能信。
    """
    async def _search(args):
        return {"hits": [{"image_url": f"https://x/{i}.png", "title": "ark", "page_url": ""}
                         for i in range(4)]}

    # 三张必须是不同的字节，否则会被按 sha256 去重成一张，
    # 于是走「只有一张 → 跳过核对」分支，测不到冲突降级
    _blobs = {f"https://x/{i}.png": blob for i, blob in enumerate((PNG, PNG_B, PNG_C, PNG))}

    async def _dl(url, **kw):
        return _blobs.get(url)

    async def _chat(config, content, **kw):
        return json.dumps({"traits": ["棕色短发", "白色羊角"], "best_index": 1, "reason": "x",
                           "consistent": False,
                           "differences": ["三张均为棕发，设定为银白偏浅紫"]})

    async def _no_single(*a, **kw):
        return None

    _dr = importlib.import_module("plugins.dl_senpai.draw_reference")
    monkeypatch.setattr(_dr, "find_reference_image", _no_single, raising=False)
    monkeypatch.setattr(subagent, "_tool_image_search", _search)
    monkeypatch.setattr(pipeline, "_download", _dl)
    monkeypatch.setattr(enhance, "_chat", _chat)

    await image_gen.build_image_segment(
        user_request="画一张艾雅法拉", config=_Cfg(), auto_reference=True
    )

    trace = trace_spy[-1]
    assert stub_gen["reference"] is None, "设定对不上的网图不能当锚点"
    assert trace.generation_mode == "text_only"
    assert "棕色短发" not in stub_gen["prompt"], "冲突图的共识特征也必须丢掉"
    assert trace.extra["reference_conflict"] == ["三张均为棕发，设定为银白偏浅紫"]


@pytest.mark.asyncio
async def test_pipeline_failure_falls_back_to_single_search(monkeypatch, stub_gen, trace_spy) -> None:
    """流水线炸了不能连累出图——原来的单张网搜必须接得住。"""
    async def _boom(*a, **kw):
        raise RuntimeError("流水线内部异常")

    monkeypatch.setattr(pipeline, "collect_references", _boom)

    called = {"single": 0}

    async def _find_single(*a, **kw):
        called["single"] += 1
        return None

    draw_reference = importlib.import_module("plugins.dl_senpai.draw_reference")
    monkeypatch.setattr(draw_reference, "find_reference_image", _find_single, raising=False)

    await image_gen.build_image_segment(user_request="画一张艾雅法拉", config=_Cfg(), auto_reference=True)
    assert called["single"] == 1, "流水线失败后必须走单张兜底"
    assert stub_gen["reference"] is None, "兜底也没图时应退化成纯文生图"
    assert trace_spy[-1].generation_mode == "text_only"


@pytest.mark.asyncio
async def test_general_image_does_not_run_pipeline(monkeypatch, stub_gen, trace_spy) -> None:
    async def _boom(*a, **kw):
        raise AssertionError("普通图不该跑参考图流水线")

    monkeypatch.setattr(pipeline, "collect_references", _boom)
    await image_gen.build_image_segment(user_request="画一只橘猫", config=_Cfg(), auto_reference=True)
    assert stub_gen["reference"] is None

@pytest.mark.asyncio
async def test_fresh_generation_never_reuses_cached_image(tmp_path, monkeypatch) -> None:
    """生图主链路每次都必须是新图，不吃缓存。

    2026-10-28 实测事故：7 个不同 id 的归档文件 md5 完全相同（854df67ebfcd），
    全部是同一个缓存文件的副本（archive 用 copy2，连 mtime 都还是 03:32:19）。
    用户看到的现象就是「怎么画都是同一张」，而且换要求也没用——
    缓存键里没有时间维度，同一句话画过一次就永远是那张。
    """
    from types import SimpleNamespace

    from dl_senpai import image_gen

    cfg = SimpleNamespace(
        draw_dir=str(tmp_path),
        draw_size="1024x1024",
        draw_quality="",
        draw_timeout=30.0,
        draw_api_key_resolved=lambda: "sk-test-key",
        draw_base_url_resolved=lambda: "https://example.invalid/v1",
        draw_model_resolved=lambda: "test-model",
        draw_configured=lambda: True,
        draw_uses_dalle_params=lambda: False,
    )

    # 先造一个"上次画过"的缓存文件
    cached = image_gen._cache_path(cfg, "艾雅法拉 穿死库水")
    cached.write_bytes(b"OLD-IMAGE-BYTES")

    async def _boom(*a, **kw):
        raise AssertionError("fresh=True 时不该走到 API")

    monkeypatch.setattr(image_gen, "_draw_client", _boom)

    # fresh=False：批量补图场景允许复用
    got = await image_gen.generate_image_file("艾雅法拉 穿死库水", cfg, fresh=False)
    assert got == cached, "非 fresh 时应当命中缓存（省重复扣费）"

    # fresh=True：必须绕开缓存，真去生成（这里 client 被替换成断言，说明确实没读缓存）
    got2 = await image_gen.generate_image_file("艾雅法拉 穿死库水", cfg, fresh=True)
    assert got2 is None, "client 报错时应返回 None 而不是退回旧缓存图"


@pytest.mark.asyncio
async def test_build_segment_asks_for_fresh_images(monkeypatch, tmp_path, stub_gen) -> None:
    """build_image_segment 每次都要新的：它在调用处就必须传 fresh=True。"""
    from dl_senpai import image_gen

    seen: list[bool] = []
    real = image_gen.generate_image_file

    async def _spy(prompt, cfg, *, reference=None, fresh=False):
        seen.append(bool(fresh))
        return await real(prompt, cfg, reference=reference, fresh=fresh)

    monkeypatch.setattr(image_gen, "generate_image_file", _spy)

    await image_gen.build_image_segment(
        resolved_prompt="艾雅法拉，穿死库水站在泳池边",
        user_request="画一张穿死库水的艾雅法拉",
        config=_Cfg(image_trace_file=str(tmp_path / "t.jsonl")),
        auto_reference=True,
    )
    assert seen and all(seen), f"主链路的每次生成都必须 fresh=True，实际={seen}"
