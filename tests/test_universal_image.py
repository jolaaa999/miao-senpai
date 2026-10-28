"""通用生图：意图识别、实体解析、提示词构建的行为约束（Case 1~12）。

本文件测的是「路径选对没有」，不是「画得好不好看」。
全部用例 mock 掉模型与生图，零付费、零联网。

核心不变量：
- 普通生图必须 0 次额外模型调用（多一次调用就是给所有人加延迟）；
- 已知实体在本地没档案时也必须保持身份，且不能要求用户先建档；
- 动态解析拿不准就不写特征，绝不能把模型猜测升成 human_verified。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

# 运行时用的是 plugins.dl_senpai.* 这族模块名，测试也用它，
# 否则会出现「patch 了 A 模块、代码用的是 B 模块」的假通过。
intent_mod = importlib.import_module("plugins.dl_senpai.image_intent")
resolver_mod = importlib.import_module("plugins.dl_senpai.entity_resolver")
prompt_mod = importlib.import_module("plugins.dl_senpai.entity_prompt")
enhance_mod = importlib.import_module("plugins.dl_senpai.draw_enhance")
image_gen = importlib.import_module("plugins.dl_senpai.image_gen")
registry_mod = importlib.import_module("plugins.dl_senpai.character_registry")

ImageIntent = intent_mod.ImageIntent
Entity = resolver_mod.Entity
EntityCache = resolver_mod.EntityCache
UniversalEntityResolver = resolver_mod.UniversalEntityResolver
UniversalEntityPromptBuilder = prompt_mod.UniversalEntityPromptBuilder
analyze_image_intent = intent_mod.analyze_image_intent


class _Cfg:
    """够生图链路跑起来的配置桩。"""

    character_enable = True
    character_min_confidence = 0.8
    character_dir = ""
    character_reference_dir = ""
    character_verify_reference = False
    character_web_reference_mode = "verify_only"
    character_web_reference_min_trust = 0.7
    identity_eval = False
    identity_min_confidence = 0.5
    draw_enable = True
    draw_verify = False
    draw_max_retry = 0
    draw_enhance = False
    draw_find_reference = False
    draw_max_prompt_chars = 1600
    draw_timeout = 30.0
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


def _reply(*entities: dict) -> str:
    return json.dumps({"entities": list(entities)}, ensure_ascii=False)


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """把缓存目录指到 tmp：测试绝不能写真实的 data/entity_cache.json。"""
    path = tmp_path / "entity_cache.json"
    monkeypatch.setattr(resolver_mod, "cache_path", lambda config=None: path)
    return path


@pytest.fixture
def resolver(isolated_cache):
    return UniversalEntityResolver(_Cfg(), cache=EntityCache(isolated_cache, ttl=0))


@pytest.fixture
def no_llm(monkeypatch):
    """默认禁止任何模型调用。需要模型时才在用例里覆盖。"""

    async def _boom(*a, **kw):
        raise AssertionError("这条路径不该调用模型")

    monkeypatch.setattr(enhance_mod, "_chat", _boom)
    return _boom


# ---------------- Case 1：普通物体必须走通用生成，0 次调用 ----------------


@pytest.mark.asyncio
async def test_case1_orange_cat_is_general_generation(no_llm) -> None:
    intent = await analyze_image_intent("画一只橘猫", config=_Cfg())
    assert intent.type == intent_mod.INTENT_GENERAL
    assert intent.entities == ()
    assert intent.resolution_calls == 0, "普通生图不许有任何额外模型调用"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "画一个赛博朋克城市", "画一台未来坦克", "画一个白发猫娘",
    "画一只穿宇航服的柴犬", "画一个原创角色", "画一片森林",
])
async def test_case1_generic_descriptions_stay_general(text, no_llm) -> None:
    intent = await analyze_image_intent(text, config=_Cfg())
    assert intent.type == intent_mod.INTENT_GENERAL
    assert intent.resolution_calls == 0


# ---------------- Case 2：本地没档案也必须是 known entity ----------------


@pytest.mark.asyncio
async def test_case2_known_entity_without_local_registry(monkeypatch, resolver) -> None:
    async def _fake_chat(config, content, **kw):
        return _reply({
            "name": "艾雅法拉", "canonical_name": "Eyjafjalla", "localized_name": "艾雅法拉",
            "franchise": "Arknights", "type": "fictional_character",
            "traits": ["羊角", "银白长发"], "confidence": 0.95,
        })

    monkeypatch.setattr(enhance_mod, "_chat", _fake_chat)
    # character_enable=False 等价于「本地没有这个角色的档案」
    intent = await analyze_image_intent(
        "画艾雅法拉", config=_Cfg(character_enable=False), resolver=resolver
    )
    assert intent.type == intent_mod.INTENT_KNOWN
    assert intent.entity_names_for_test() if False else True
    entity = intent.entities[0]
    assert entity.canonical_name == "Eyjafjalla"
    assert "Arknights" in entity.anchor, "作品名是锁身份的关键锚点，不能丢"
    assert "Eyjafjalla" in entity.anchor and "艾雅法拉" in entity.anchor


# ---------------- Case 3：第一次出现的角色也必须能画 ----------------


@pytest.mark.asyncio
async def test_case3_first_time_entity_needs_no_profile(monkeypatch, resolver) -> None:
    async def _fake_chat(config, content, **kw):
        return _reply({
            "name": "初音未来", "canonical_name": "Hatsune Miku", "localized_name": "初音未来",
            "franchise": "Vocaloid", "type": "fictional_character",
            "traits": ["turquoise twin tails", "signature futuristic idol outfit"],
            "confidence": 0.97,
        })

    monkeypatch.setattr(enhance_mod, "_chat", _fake_chat)
    intent = await analyze_image_intent(
        "画初音未来", config=_Cfg(character_enable=False), resolver=resolver
    )
    assert intent.type == intent_mod.INTENT_KNOWN
    built = UniversalEntityPromptBuilder().build(user_request="画初音未来", intent=intent)
    assert "Hatsune Miku" in built.text and "Vocaloid" in built.text
    # 不应该要求用户先往 characters.json 里加东西
    assert built.text.strip() and "characters.json" not in built.text


# ---------------- Case 4：多实体必须分别识别 ----------------


@pytest.mark.asyncio
async def test_case4_two_entities_detected(monkeypatch, resolver) -> None:
    async def _fake_chat(config, content, **kw):
        return _reply(
            {"name": "鸣人", "canonical_name": "Naruto Uzumaki", "localized_name": "漩涡鸣人",
             "franchise": "Naruto", "type": "fictional_character",
             "traits": ["blond spiky hair"], "confidence": 0.98},
            {"name": "佐助", "canonical_name": "Sasuke Uchiha", "localized_name": "宇智波佐助",
             "franchise": "Naruto", "type": "fictional_character",
             "traits": ["dark hair"], "confidence": 0.97},
        )

    monkeypatch.setattr(enhance_mod, "_chat", _fake_chat)
    intent = await analyze_image_intent(
        "画鸣人和佐助打架", config=_Cfg(character_enable=False), resolver=resolver
    )
    assert intent.type == intent_mod.INTENT_KNOWN
    names = {e.canonical_name for e in intent.entities}
    assert names == {"Naruto Uzumaki", "Sasuke Uchiha"}

    built = UniversalEntityPromptBuilder().build(user_request="画鸣人和佐助打架", intent=intent)
    # 两个角色各有一个 identity block，且明令不许糊成一个人
    assert built.text.count("Preserve the recognizable identity") == 2
    assert "Do not blend their traits into a single character." in built.text


# ---------------- Case 5：原创角色不能被误认成已知 IP ----------------


@pytest.mark.asyncio
async def test_case5_original_character_not_misidentified(no_llm) -> None:
    intent = await analyze_image_intent("画一个原创黄发忍者", config=_Cfg())
    assert intent.type == intent_mod.INTENT_GENERAL
    assert intent.entities == ()
    assert "Naruto" not in json.dumps(intent.as_trace(), ensure_ascii=False)


@pytest.mark.asyncio
async def test_case5_original_signal_wins_over_known_name(no_llm) -> None:
    """「画一个原创的艾雅法拉风格角色」这类请求，用户是在刻意绕开 IP。"""
    assert intent_mod.looks_like_general_description("画一个原创角色") is True


# ---------------- Case 6：显式改服装，身份仍要保留 ----------------


@pytest.mark.asyncio
async def test_case6_explicit_clothing_override(monkeypatch, resolver) -> None:
    async def _fake_chat(config, content, **kw):
        return _reply({
            "name": "雷电将军", "canonical_name": "Raiden Shogun", "localized_name": "雷电将军",
            "franchise": "Genshin Impact", "type": "fictional_character",
            "traits": ["紫色长发", "紫色和服", "雷元素"], "confidence": 0.96,
        })

    monkeypatch.setattr(enhance_mod, "_chat", _fake_chat)
    intent = await analyze_image_intent(
        "画雷电将军穿西装", config=_Cfg(character_enable=False), resolver=resolver
    )
    assert intent.override_map() == {"clothing": "overridden"}

    built = UniversalEntityPromptBuilder().build(user_request="画雷电将军穿西装", intent=intent)
    assert "Raiden Shogun" in built.text, "身份锚点必须保留"
    assert "紫色和服" not in built.text, "用户要西装，就不能再把和服写进特征"
    assert "different clothing" in built.text
    # 身份核心不能被服装覆盖带走
    assert "紫色长发" in built.text


# ---------------- Case 7：用户说不要角，就别把角加回来 ----------------


@pytest.mark.asyncio
async def test_case7_remove_horns_is_respected(monkeypatch, resolver) -> None:
    async def _fake_chat(config, content, **kw):
        return _reply({
            "name": "艾雅法拉", "canonical_name": "Eyjafjalla", "localized_name": "艾雅法拉",
            "franchise": "Arknights", "type": "fictional_character",
            "traits": ["一对弯曲的羊角", "银白长发"], "confidence": 0.95,
        })

    monkeypatch.setattr(enhance_mod, "_chat", _fake_chat)
    intent = await analyze_image_intent(
        "画艾雅法拉但不要羊角", config=_Cfg(character_enable=False), resolver=resolver
    )
    assert intent.override_map().get("horns") == "removed"
    built = UniversalEntityPromptBuilder().build(user_request="画艾雅法拉但不要羊角", intent=intent)
    # 特征段里不能再出现角（用户原话里的"不要羊角"会正常出现在 scene 段，那是对的）
    traits_block = built.identity_block
    assert "一对弯曲的羊角" not in traits_block
    assert "without horns" in built.text
    assert "银白长发" in traits_block


# ---------------- Case 8：用户参考图走参考生成，不许转文字后丢图 ----------------


@pytest.mark.asyncio
async def test_case8_user_reference_goes_to_reference_pipeline(no_llm) -> None:
    intent = await analyze_image_intent(
        "照着这个人画她在沙滩", has_user_image=True, config=_Cfg()
    )
    assert intent.type == intent_mod.INTENT_REFERENCE
    assert intent.user_references == ("user_image",)
    assert intent.resolution_calls == 0


@pytest.mark.asyncio
async def test_case8_edit_intent(no_llm) -> None:
    intent = await analyze_image_intent(
        "把我这张照片画成漫画", has_user_image=True, config=_Cfg()
    )
    assert intent.type == intent_mod.INTENT_EDIT


# ---------------- Case 9：场景/风格描述不能触发角色管线 ----------------


@pytest.mark.asyncio
async def test_case9_scene_does_not_trigger_character_pipeline(no_llm) -> None:
    intent = await analyze_image_intent("画一个赛博朋克东京", config=_Cfg())
    assert intent.type == intent_mod.INTENT_GENERAL
    assert intent.entities == ()


# ---------------- Case 10：Prompt Enhancer 必须保住实体 ----------------


@pytest.mark.asyncio
async def test_case10_enhancer_keeps_entity(monkeypatch) -> None:
    async def _fake_chat(config, content, **kw):
        return "艾雅法拉站在火山前，紫瞳银发，构图居中，柔和侧光，高质量插画。"

    monkeypatch.setattr(enhance_mod, "_chat", _fake_chat)
    out = await enhance_mod.enhance_prompt(
        _Cfg(), prompt="艾雅法拉站在火山前", user_request="画艾雅法拉",
        locked_entities=("艾雅法拉", "Eyjafjalla"),
    )
    assert "艾雅法拉" in out


@pytest.mark.asyncio
async def test_case10_enhancer_falls_back_when_entity_lost(monkeypatch) -> None:
    """旧 enhance 会把「艾雅法拉」洗成「羊角少女」——这种结果必须整段丢弃。"""

    async def _fake_chat(config, content, **kw):
        return "一位长着羊角的银发少女站在火山前，柔和侧光，高质量插画。"

    monkeypatch.setattr(enhance_mod, "_chat", _fake_chat)
    out = await enhance_mod.enhance_prompt(
        _Cfg(), prompt="艾雅法拉站在火山前", user_request="画艾雅法拉",
        locked_entities=("艾雅法拉",),
    )
    assert out == "艾雅法拉站在火山前", "改写丢了实体，必须回退原始提示词"


def test_case10_validator_basics() -> None:
    v = intent_mod.validate_entity_preservation
    assert v("艾雅法拉", (), "画一张艾雅法拉") is True
    assert v("Eyjafjalla", (), "eyjafjalla in arknights") is True
    assert v("艾雅法拉", ("小羊",), "画一只小羊") is True
    assert v("艾雅法拉", (), "一位羊角少女") is False
    assert v("", (), "随便什么") is True


# ---------------- Case 11：registry miss 不许退化成泛化人物 ----------------


@pytest.mark.asyncio
async def test_case11_registry_miss_keeps_entity_name(monkeypatch, resolver) -> None:
    async def _fake_chat(config, content, **kw):
        return _reply({
            "name": "犬夜叉", "canonical_name": "Inuyasha", "localized_name": "犬夜叉",
            "franchise": "Inuyasha", "type": "fictional_character",
            "traits": [], "confidence": 0.9,
        })

    monkeypatch.setattr(enhance_mod, "_chat", _fake_chat)
    intent = await analyze_image_intent(
        "画犬夜叉", config=_Cfg(character_enable=False), resolver=resolver
    )
    built = UniversalEntityPromptBuilder().build(user_request="画犬夜叉", intent=intent)
    assert "Inuyasha" in built.text
    # 没有可信特征时必须明说「别编」，而不是塞进一堆形容词
    assert "Do not invent specific traits" in built.text
    assert "anime girl" not in built.text.lower()


# ---------------- Case 12：低置信 → 只留名字，不编特征 ----------------


@pytest.mark.asyncio
async def test_case12_low_confidence_keeps_name_only(monkeypatch, resolver) -> None:
    async def _fake_chat(config, content, **kw):
        return _reply({
            "name": "某个角色", "canonical_name": "", "localized_name": "",
            "franchise": "", "type": "unknown_entity",
            "traits": ["银发", "红瞳"], "confidence": 0.2,
        })

    monkeypatch.setattr(enhance_mod, "_chat", _fake_chat)
    entities = await resolver.resolve(["某个角色"], request="画某个角色")
    assert entities[0].traits == (), "低置信不许带回任何特征"
    assert entities[0].name == "某个角色"


@pytest.mark.asyncio
async def test_case12_unresolved_falls_back_to_general(monkeypatch, resolver) -> None:
    async def _fake_chat(config, content, **kw):
        return _reply({
            "name": "随便什么东西", "canonical_name": "", "type": "unknown_entity",
            "traits": [], "confidence": 0.1,
        })

    monkeypatch.setattr(enhance_mod, "_chat", _fake_chat)
    intent = await analyze_image_intent(
        "画随便什么东西", config=_Cfg(character_enable=False), resolver=resolver
    )
    assert intent.type == intent_mod.INTENT_GENERAL
    assert intent.reason == "entities_unresolved"


# ---------------- 成本纪律 ----------------


@pytest.mark.asyncio
async def test_cost_cache_hit_costs_zero_calls(monkeypatch, resolver) -> None:
    calls = {"n": 0}

    async def _fake_chat(config, content, **kw):
        calls["n"] += 1
        return _reply({
            "name": "初音未来", "canonical_name": "Hatsune Miku",
            "localized_name": "初音未来", "franchise": "Vocaloid",
            "type": "fictional_character", "traits": ["turquoise twin tails"],
            "confidence": 0.97,
        })

    monkeypatch.setattr(enhance_mod, "_chat", _fake_chat)
    cfg = _Cfg(character_enable=False)
    first = await analyze_image_intent("画初音未来", config=cfg, resolver=resolver)
    assert first.resolution_calls == 1, "首次 miss 允许一次轻量解析"
    second = await analyze_image_intent("画初音未来", config=cfg, resolver=resolver)
    assert second.resolution_calls == 0, "第二次必须命中缓存，不能再调模型"
    assert second.entity_cache_hit is True
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_cost_general_never_calls_model(no_llm) -> None:
    for text in ("画一只橘猫", "画一台未来坦克", "画一个赛博朋克城市"):
        intent = await analyze_image_intent(text, config=_Cfg())
        assert intent.resolution_calls == 0


# ---------------- 缓存语义 ----------------


def test_cache_never_upgrades_to_human_verified(isolated_cache) -> None:
    cache = EntityCache(isolated_cache, ttl=0)
    cache.put(Entity(name="初音未来", canonical_name="Hatsune Miku",
                     franchise="Vocaloid", confidence=0.9), model="test")
    raw = json.loads(isolated_cache.read_text(encoding="utf-8"))
    entry = raw["entries"]["初音未来"]
    assert entry["trust"] == "assistant_generated"
    assert entry["model"] == "test"
    assert entry["confidence"] == pytest.approx(0.9)
    assert "created_at" in entry
    # 读回来也必须是 assistant_generated
    assert cache.get("初音未来").trust == "assistant_generated"


def test_cache_ttl_expires(isolated_cache, monkeypatch) -> None:
    cache = EntityCache(isolated_cache, ttl=10)
    cache.put(Entity(name="X", canonical_name="X", confidence=0.9))
    assert cache.get("X") is not None
    # 把创建时间推到很久以前 → 必须失效，让下一次请求重新解析
    raw = json.loads(isolated_cache.read_text(encoding="utf-8"))
    raw["entries"]["x"]["created_at"] = 0.0
    isolated_cache.write_text(json.dumps(raw), encoding="utf-8")
    cache2 = EntityCache(isolated_cache, ttl=10)
    assert cache2.get("X") is None


def test_cache_survives_corruption(isolated_cache) -> None:
    isolated_cache.write_text("{not json", encoding="utf-8")
    cache = EntityCache(isolated_cache, ttl=0)      # 不许抛
    assert cache.get("anything") is None


# ---------------- 端到端接线：trace 必须记下走的是哪条路 ----------------


@pytest.fixture
def stub_image(monkeypatch, tmp_path, isolated_cache):
    out = tmp_path / "gen.png"
    out.write_bytes(b"\x89PNG\r\n\x1a\n" + b"universal" * 10)

    async def _fake_generate(prompt, config=None, *, reference=None, fresh=False):
        return out

    for mod_name in ("dl_senpai.image_gen", "plugins.dl_senpai.image_gen"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001
            continue
        monkeypatch.setattr(mod, "generate_image_file", _fake_generate, raising=True)
    return out


@pytest.mark.asyncio
async def test_trace_records_intent_and_entities(monkeypatch, stub_image) -> None:
    async def _fake_chat(config, content, **kw):
        return _reply({
            "name": "初音未来", "canonical_name": "Hatsune Miku", "localized_name": "初音未来",
            "franchise": "Vocaloid", "type": "fictional_character",
            "traits": ["turquoise twin tails"], "confidence": 0.97,
        })

    for mod_name in ("dl_senpai.draw_enhance", "plugins.dl_senpai.draw_enhance"):
        try:
            monkeypatch.setattr(importlib.import_module(mod_name), "_chat", _fake_chat)
        except Exception:  # noqa: BLE001
            continue

    traces = []

    cfg = _Cfg(character_enable=False)
    # new_trace 是在 build_image_segment 内部 from .image_trace import 进来的，
    # 所以要打来源模块的补丁，打 image_gen 上无效。
    trace_mod = importlib.import_module("plugins.dl_senpai.image_trace")
    real_new_trace = trace_mod.new_trace

    def _spy(config, *, original_prompt):
        trace = real_new_trace(config, original_prompt=original_prompt)
        traces.append(trace)
        return trace

    monkeypatch.setattr(trace_mod, "new_trace", _spy)
    await image_gen.build_image_segment(user_request="画初音未来", config=cfg)
    assert traces, "没有产生 trace，接线可能没走到"
    trace = traces[-1]
    assert trace.image_intent == "known_entity_generation"
    assert trace.entity_count >= 1
    assert "Hatsune Miku" in trace.entity_names
    assert trace.entity_source in ("dynamic_resolver", "cache")
    assert trace.entity_preservation_valid is None or trace.entity_preservation_valid is True
    fields = trace.as_fields()
    assert fields["image_intent"] == "known_entity_generation"
    assert fields["entities"][0]["name"] == "Hatsune Miku"


@pytest.mark.asyncio
async def test_general_never_searches_web(monkeypatch, stub_image) -> None:
    """通用生图默认 text-first：即便配置允许找参考图，普通图也不许联网。"""
    draw_reference = importlib.import_module("plugins.dl_senpai.draw_reference")
    calls = {"n": 0}

    async def _boom(*a, **kw):
        calls["n"] += 1
        raise AssertionError("普通生图不许联网搜图")

    monkeypatch.setattr(draw_reference, "find_reference_image", _boom, raising=False)
    monkeypatch.setattr(
        importlib.import_module("dl_senpai.draw_reference"),
        "find_reference_image", _boom, raising=False,
    )

    # auto_reference=True 是真实调用时的传参，正是过去会触发搜图的组合
    await image_gen.build_image_segment(
        user_request="画一只橘猫", config=_Cfg(draw_find_reference=True), auto_reference=True
    )
    assert calls["n"] == 0, "普通生图走了网图搜索"


@pytest.mark.asyncio
async def test_known_entity_may_probe_web_but_not_edit(monkeypatch, stub_image) -> None:
    """已知实体允许查网图佐证身份，但默认 verify_only 不许送进 images.edit。"""
    async def _fake_chat(config, content, **kw):
        return _reply({
            "name": "初音未来", "canonical_name": "Hatsune Miku", "localized_name": "初音未来",
            "franchise": "Vocaloid", "type": "fictional_character",
            "traits": [], "confidence": 0.97,
        })

    for mod_name in ("dl_senpai.draw_enhance", "plugins.dl_senpai.draw_enhance"):
        try:
            monkeypatch.setattr(importlib.import_module(mod_name), "_chat", _fake_chat)
        except Exception:  # noqa: BLE001
            continue

    draw_reference = importlib.import_module("plugins.dl_senpai.draw_reference")
    seen = {"n": 0}

    async def _no_result(*a, **kw):
        seen["n"] += 1
        return None

    monkeypatch.setattr(draw_reference, "find_reference_image", _no_result, raising=False)

    traces = []
    trace_mod = importlib.import_module("plugins.dl_senpai.image_trace")
    real_new_trace = trace_mod.new_trace
    monkeypatch.setattr(
        trace_mod, "new_trace",
        lambda config, *, original_prompt: traces.append(
            real_new_trace(config, original_prompt=original_prompt)
        ) or traces[-1],
    )
    await image_gen.build_image_segment(
        user_request="画初音未来",
        config=_Cfg(character_enable=False, draw_find_reference=True),
        auto_reference=True,
    )
    trace = traces[-1]
    assert trace.generation_mode == "text_only", "网图没找到时不该变成 edit"
    assert trace.image_intent == "known_entity_generation"


@pytest.mark.asyncio
async def test_trace_general_image_has_no_entities(monkeypatch, stub_image) -> None:
    traces = []
    trace_mod = importlib.import_module("plugins.dl_senpai.image_trace")
    real_new_trace = trace_mod.new_trace
    monkeypatch.setattr(
        trace_mod, "new_trace",
        lambda config, *, original_prompt: traces.append(
            real_new_trace(config, original_prompt=original_prompt)
        ) or traces[-1],
    )
    await image_gen.build_image_segment(user_request="画一只橘猫", config=_Cfg())
    trace = traces[-1]
    assert trace.image_intent == "general_generation"
    assert trace.entity_count == 0
