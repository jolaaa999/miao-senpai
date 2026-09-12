from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from dl_senpai.persona import build_system_prompt
from dl_senpai.trends import (
    HotItem,
    TrendsSnapshot,
    TrendsStore,
    format_trends_for_prompt,
)

_WORK_TMP = Path(__file__).resolve().parents[1] / ".pytest_tmp"


@pytest.fixture
def cache_dir() -> Path:
    _WORK_TMP.mkdir(parents=True, exist_ok=True)
    d = _WORK_TMP / f"trends-{uuid.uuid4().hex}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    for p in sorted(d.rglob("*"), reverse=True):
        try:
            if p.is_file():
                p.unlink()
            else:
                p.rmdir()
        except OSError:
            pass
    try:
        d.rmdir()
    except OSError:
        pass


SAMPLE_WEIBO = {
    "code": 200,
    "data": [
        {"title": "梗A", "url": "https://example.com/a", "hot": "100万"},
        {"title": "梗B", "url": "https://example.com/b"},
        {"title": "梗A", "url": "https://example.com/a2"},
    ],
}


def test_parse_source_payload() -> None:
    items = TrendsStore.parse_source_payload("weibo", SAMPLE_WEIBO, limit=2)
    assert len(items) == 2
    assert items[0].title == "梗A"
    assert items[0].rank == 1
    assert items[1].title == "梗B"


def test_format_trends_for_prompt_truncates() -> None:
    items = [
        HotItem("weibo", f"标题{i}" + ("长" * 20), rank=i) for i in range(1, 30)
    ]
    text = format_trends_for_prompt(items, max_items=20, max_chars=120)
    assert "近期热梗备忘" in text
    assert len(text) <= 120
    assert "<<<" not in text


def test_persona_includes_trends_brief() -> None:
    brief = "【近期热梗备忘】\n- [微博] 测试梗"
    prompt = build_system_prompt(trends_brief=brief)
    assert "近期热梗用法" in prompt
    assert "测试梗" in prompt
    assert "备忘里没有" in prompt or "热梗备忘" in prompt


@pytest.mark.asyncio
async def test_ttl_hits_memory_cache(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TrendsStore(
        cache_dir=cache_dir,
        base_url="https://example.invalid",
        sources=["weibo"],
        ttl_sec=3600,
        per_source_limit=5,
    )
    store._mem = TrendsSnapshot(
        fetched_at=10_000_000_000.0,
        items=[HotItem("weibo", "缓存梗", 1)],
        base_url="https://example.invalid",
    )
    monkeypatch.setattr("dl_senpai.trends.time.time", lambda: 10_000_000_000.0 + 10)
    calls = {"n": 0}

    async def boom(*_a, **_k):  # noqa: ANN001
        calls["n"] += 1
        raise AssertionError("should not refresh")

    monkeypatch.setattr(store, "refresh", boom)
    snap = await store.get_snapshot()
    assert snap is not None
    assert snap.items[0].title == "缓存梗"
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_refresh_failure_falls_back_to_disk(cache_dir: Path) -> None:
    cache = cache_dir / "cache.json"
    cache.write_text(
        json.dumps(
            {
                "fetched_at": 1.0,
                "base_url": "https://example.invalid",
                "items": [
                    {
                        "source": "weibo",
                        "title": "旧梗",
                        "rank": 1,
                        "url": "",
                        "hot": "",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = TrendsStore(
        cache_dir=cache_dir,
        base_url="https://example.invalid",
        sources=["weibo"],
        ttl_sec=1,
        per_source_limit=5,
        timeout=0.5,
    )
    snap = await store.get_snapshot(force=True)
    assert snap is not None
    assert snap.stale is True
    assert snap.items[0].title == "旧梗"


@pytest.mark.asyncio
async def test_refresh_success(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TrendsStore(
        cache_dir=cache_dir,
        base_url="https://api.test",
        sources=["weibo", "zhihu"],
        ttl_sec=60,
        per_source_limit=3,
    )

    class FakeResp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k) -> None:  # noqa: ANN001
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a) -> None:  # noqa: ANN001
            return None

        async def get(self, url: str, params: dict | None = None) -> FakeResp:
            if url.endswith("/weibo") or (params or {}).get("type") == "weibo":
                return FakeResp(
                    {"data": [{"title": "微博热1"}, {"title": "共用标题"}]}
                )
            return FakeResp({"data": [{"title": "共用标题"}, {"title": "知乎热2"}]})

    monkeypatch.setattr("dl_senpai.trends.httpx.AsyncClient", FakeClient)
    snap = await store.refresh()
    titles = [i.title for i in snap.items]
    assert "微博热1" in titles
    assert "知乎热2" in titles
    assert titles.count("共用标题") == 1
    assert (cache_dir / "cache.json").exists()


def test_parse_uapis_list_payload() -> None:
    payload = {
        "type": "weibo",
        "list": [
            {"index": 1, "title": "热梗甲", "url": "https://x", "hot_value": "1"},
            {"index": 2, "title": "热梗乙"},
        ],
    }
    items = TrendsStore.parse_source_payload("weibo", payload, limit=10)
    assert [i.title for i in items] == ["热梗甲", "热梗乙"]
    assert items[0].hot == "1"


def test_request_for_uapis(cache_dir: Path) -> None:
    store = TrendsStore(
        cache_dir=cache_dir,
        base_url="https://uapis.cn/api/v1/misc/hotboard",
    )
    url, params = store._request_for_source("zhihu")
    assert url.endswith("/hotboard")
    assert params == {"type": "zhihu"}
