"""生图后台作业与参考图子 agent 的测试。

盯的都是一旦坏了就「静默出错」的地方：
1. 派单必须立刻返回（否则主对话还是被卡）；
2. 成功必须把图发出去、失败必须发降级话术（否则对方等到天荒地老）；
3. 自拍/换装类不许找参考图（会把她自己的脸换成网图）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import dl_senpai.draw_worker as dw
from dl_senpai.draw_reference import (
    build_reference_goal,
    find_reference_image,
    wants_self_portrait,
)
from dl_senpai.subagent import reset_subagent_runner


def _config(**kw) -> SimpleNamespace:
    base = dict(
        draw_job_max=2,
        draw_job_timeout=5.0,
        draw_find_reference=True,
        draw_reference_steps=3,
        draw_reference_timeout=5.0,
        draw_timeout=30.0,
        draw_enable=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _context(ctx_config=None, **kw) -> SimpleNamespace:
    base = dict(
        config=ctx_config or _config(),
        extra={},
        plain_text="画一张凯尔希",
        interrupt=False,
        session_id="s1",
        actor="失语.",
        user_id=2843422418,
        bot=object(),
        raw_event=object(),
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_subagent_runner()


# ---------------- 参考图子 agent ----------------


def test_self_portrait_is_excluded() -> None:
    """画她自己时不找参考图：她的外观有固定 prompt，塞网图会把脸换掉。"""
    for text in ("来张自拍", "学姐长什么样", "给你换件JK", "看看你的样子"):
        assert wants_self_portrait(text), text
    for text in ("画一张凯尔希", "帮我画只橘猫", "生成一张初音的图"):
        assert not wants_self_portrait(text), text


def test_reference_goal_mentions_character_and_source() -> None:
    goal = build_reference_goal(prompt="x", user_request="y", character="凯尔希")
    assert "凯尔希" in goal
    assert "立绘" in goal
    assert "image_find" in goal


@pytest.mark.asyncio
async def test_find_reference_returns_bytes(monkeypatch) -> None:
    """子 agent 给出 url 后，要真的下载成 (文件名, bytes)。"""

    async def _fake_agent(config, spec):
        return SimpleNamespace(
            answer="找到一张官方立绘",
            payload={"url": "https://example.com/kaltsit.png"},
            steps=2,
        )

    async def _fake_load(sources, *, timeout):
        assert sources == ["https://example.com/kaltsit.png"]
        return ("reference.png", b"\x89PNG\r\n\x1a\nfake")

    async def _no_direct(*a, **kw):
        return None

    # 直搜这段是代码自己跑的（真联网），单测里必须掐掉，否则用例会去打搜索服务
    monkeypatch.setattr("dl_senpai.draw_reference._direct_search", _no_direct)
    monkeypatch.setattr("dl_senpai.draw_reference.run_llm_agent", _fake_agent)
    monkeypatch.setattr("dl_senpai.image_gen._load_reference_image", _fake_load)

    got = await find_reference_image(
        _config(), prompt="凯尔希 立绘", user_request="画一张凯尔希"
    )
    assert got is not None
    assert got[0] == "reference.png"
    assert got[1].startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_find_reference_swallows_failures(monkeypatch) -> None:
    """找图是增强项：子 agent 崩了也只能返回 None，不能让生图整条挂掉。"""

    async def _boom(config, spec):
        raise RuntimeError("搜索服务 500")

    async def _no_direct(*a, **kw):
        return None

    monkeypatch.setattr("dl_senpai.draw_reference._direct_search", _no_direct)
    monkeypatch.setattr("dl_senpai.draw_reference.run_llm_agent", _boom)
    assert await find_reference_image(_config(), prompt="橘猫", user_request="画只猫") is None


@pytest.mark.asyncio
async def test_find_reference_skipped_when_disabled(monkeypatch) -> None:
    called = False

    async def _should_not_run(config, spec):
        nonlocal called
        called = True
        return SimpleNamespace(answer="", payload={}, steps=1)

    async def _no_direct(*a, **kw):
        called = True  # 关掉开关时连直搜也不该跑
        return None

    monkeypatch.setattr("dl_senpai.draw_reference._direct_search", _no_direct)
    monkeypatch.setattr("dl_senpai.draw_reference.run_llm_agent", _should_not_run)
    got = await find_reference_image(
        _config(draw_find_reference=False), prompt="橘猫", user_request="画只猫"
    )
    assert got is None
    assert called is False


@pytest.mark.asyncio
async def test_direct_search_hit_skips_llm_agent(monkeypatch) -> None:
    """代码自己搜到图时，不该再花一个模型回合。

    2026-10-27 07:46 的真实日志：子 agent 拿到工具清单还说「当前没有可用的
    image_find 工具」，白白丢掉参考图。搜索本来就不需要模型。
    """
    from dl_senpai import draw_reference as dr

    async def _direct(config, *, prompt, user_request, character="", per_query_timeout=18.0, **kw):
        assert character == "" or isinstance(character, str)
        return ("direct.png", b"\x89PNG\r\n\x1a\ndirect")

    async def _should_not_run(*a, **kw):
        raise AssertionError("直搜命中了就不该再派子 agent")

    monkeypatch.setattr(dr, "_direct_search", _direct)
    monkeypatch.setattr(dr, "run_llm_agent", _should_not_run)

    got = await find_reference_image(_config(), prompt="橘猫", user_request="画只猫")
    assert got is not None and got[0] == "direct.png"


@pytest.mark.asyncio
async def test_direct_search_hit_with_meta_returns_candidate(monkeypatch) -> None:
    """with_meta=True 时直搜要返回 ReferenceCandidate，不能在下标访问上炸。

    这条是线上实测踩出来的：构造 candidate 之后调用方还在用 [1] 取字节，
    结果真实生图直接抛 TypeError。
    """
    from dl_senpai import draw_reference as dr
    from dl_senpai.character_references import ReferenceCandidate

    async def _direct(config, *, prompt, user_request, character="", **kw):
        return ReferenceCandidate(
            filename="direct.png",
            raw=b"\x89PNG\r\n\x1a\ndirect",
            source_url="https://prts.wiki/a.png",
            source_type="wiki",
            source_trust=0.8,
        )

    monkeypatch.setattr(dr, "_direct_search", _direct)

    async def _should_not_run(*a, **kw):
        raise AssertionError("直搜命中了就不该再派子 agent")

    monkeypatch.setattr(dr, "run_llm_agent", _should_not_run)
    got = await find_reference_image(_config(), prompt="艾雅法拉", user_request="画一张艾雅法拉", with_meta=True)
    assert isinstance(got, ReferenceCandidate)
    assert got.filename == "direct.png" and got.source_type == "wiki"


@pytest.mark.asyncio
async def test_direct_search_falls_back_to_agent(monkeypatch) -> None:
    """直搜空手时，仍要交给子 agent 换策略。"""
    from dl_senpai import draw_reference as dr

    async def _direct(*a, **kw):
        return None

    async def _agent(config, spec):
        return SimpleNamespace(
            answer="找到了",
            payload={"url": "https://example.com/a.png"},
            steps=1,
        )

    async def _load(sources, *, timeout):
        return ("a.png", b"\x89PNG")

    monkeypatch.setattr(dr, "_direct_search", _direct)
    monkeypatch.setattr(dr, "run_llm_agent", _agent)
    monkeypatch.setattr("dl_senpai.image_gen._load_reference_image", _load)

    got = await find_reference_image(_config(), prompt="橘猫", user_request="画只猫")
    assert got is not None and got[0] == "a.png"


def test_direct_queries_strip_request_words() -> None:
    """搜索词要剥掉「帮我画一张…图片」这种口水词，只留主体。"""
    from dl_senpai.draw_reference import _direct_queries

    queries = _direct_queries(
        prompt="凯尔希，银白发，绿色外套",
        user_request="帮我画一张凯尔希的图片",
        character="凯尔希",
    )
    assert "凯尔希 官方立绘" in queries
    # 口水词必须剥干净，否则会搜出「帮我画一张凯尔希的图片 立绘」这种没人写的词
    assert all("帮我" not in q and "图片" not in q for q in queries)


def test_direct_queries_without_character_still_useful() -> None:
    from dl_senpai.draw_reference import _direct_queries

    queries = _direct_queries(prompt="一只橘猫趴在窗台上", user_request="画只橘猫")
    assert queries, "没有角色卡时也要拼得出搜索词"
    assert all(q.strip() for q in queries)


# ---------------- 后台作业 ----------------


@pytest.mark.asyncio
async def test_submit_returns_before_image_is_ready(monkeypatch) -> None:
    """派单必须立刻返回：这是「主对话不被阻塞」的全部意义。"""
    finished: list[object] = []

    async def _slow_draw(**kwargs):
        await asyncio.sleep(0.05)
        # 参考图必须被要求：没参考图时模型会凭记忆编设定
        assert kwargs.get("auto_reference") is True
        return "SEGMENT"

    sent: list[object] = []

    async def _fake_send(bot, event, image):
        sent.append(image)
        return True

    monkeypatch.setattr(dw, "build_image_segment", _slow_draw)
    monkeypatch.setattr("dl_senpai.reply_send._send_image_with_retry", _fake_send)

    job_id = dw.submit_draw_job(_context(), "凯尔希 立绘")
    assert job_id
    assert sent == []  # 还没画完，什么都没发

    from dl_senpai.subagent import get_subagent_runner

    await get_subagent_runner().drain(timeout=5.0)
    assert sent == ["SEGMENT"]
    finished.append(1)


@pytest.mark.asyncio
async def test_failed_job_sends_fallback_line(monkeypatch) -> None:
    """出图失败要说话：否则对方等到天荒地老还以为是网络卡了。"""

    async def _boom_draw(**kwargs):
        raise RuntimeError("上游 502")

    said: list[str] = []

    async def _fake_send_text(bot, event, text):
        said.append(text)

    monkeypatch.setattr(dw, "build_image_segment", _boom_draw)
    monkeypatch.setattr(dw, "_say", _fake_send_text)

    dw.submit_draw_job(_context(), "凯尔希 立绘")
    from dl_senpai.subagent import get_subagent_runner

    await get_subagent_runner().drain(timeout=5.0)
    assert said and isinstance(said[0], str) and said[0]


@pytest.mark.asyncio
async def test_no_channel_does_not_crash(monkeypatch) -> None:
    """干跑/无 bot 场景：静默留痕即可，不能抛异常把调度器带崩。"""

    async def _draw(**kwargs):
        return "SEGMENT"

    monkeypatch.setattr(dw, "build_image_segment", _draw)
    ctx = _context(bot=None, raw_event=None)
    dw.submit_draw_job(ctx, "凯尔希 立绘")
    from dl_senpai.subagent import get_subagent_runner

    # 不抛异常就是通过
    await get_subagent_runner().drain(timeout=5.0)

