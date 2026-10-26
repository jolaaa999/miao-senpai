# -*- coding: utf-8 -*-
"""商店素材并发生成（A）与后台预热（C）的回归测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dl_senpai import shop as shop_mod


class _FakeConfig:
    shop_thumb_concurrency = 3


def _item(item_id: str):
    """构造一个只带 id 的假款式对象（缩略图逻辑只用到 id）。"""
    item = type('Item', (), {})()
    item.id = item_id
    item.name = item_id
    item.thumb_prompt = ''
    item.outfit_en = item_id
    return item


class _FakeStore:
    def __init__(self, tmp_path: Path):
        self.thumb_dir = tmp_path / 'thumbs'
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.composite_dir = tmp_path / 'composites'
        self.composite_dir.mkdir(parents=True, exist_ok=True)
        self.outfit_dir = tmp_path / 'outfits'
        self.outfit_dir.mkdir(parents=True, exist_ok=True)


def _make_thumb(item, store, config):
    """假的缩略图生成：写一个占位文件，模拟一次耗时 IO。"""
    path = store.thumb_dir / f'{item.id}.png'
    path.write_bytes(b'x')
    return path


def test_prewarm_skips_when_all_cached(monkeypatch, tmp_path):
    store = _FakeStore(tmp_path)
    items = [_item('a'), _item('b')]
    for it in items:
        (store.thumb_dir / f'{it.id}.png').write_bytes(b'x')
    (store.composite_dir / 'plain_senpai.png').write_bytes(b'x')
    calls = []

    async def _fake(item, store_, config):
        calls.append(item.id)
        return _make_thumb(item, store_, config)

    monkeypatch.setattr(shop_mod, '_thumb_path', _fake)
    made = asyncio.run(shop_mod.prewarm_shop_assets(items, store, _FakeConfig()))
    assert made == 0
    assert calls == [], '全部命中缓存时不应发起任何生成请求'


def test_prewarm_generates_only_missing(monkeypatch, tmp_path):
    store = _FakeStore(tmp_path)
    items = [_item('a'), _item('b'), _item('c')]
    (store.thumb_dir / 'a.png').write_bytes(b'x')  # a 已缓存
    (store.composite_dir / 'plain_senpai.png').write_bytes(b'x')
    calls = []

    async def _fake(item, store_, config):
        calls.append(item.id)
        return _make_thumb(item, store_, config)

    monkeypatch.setattr(shop_mod, '_thumb_path', _fake)
    made = asyncio.run(shop_mod.prewarm_shop_assets(items, store, _FakeConfig()))
    assert made == 2
    assert sorted(calls) == ['b', 'c']


def test_prewarm_runs_concurrently(monkeypatch, tmp_path):
    """4 张缺图必须并发跑：总耗时接近单张，而不是 4 倍。"""
    store = _FakeStore(tmp_path)
    items = [_item(f'i{n}') for n in range(4)]
    (store.composite_dir / 'plain_senpai.png').write_bytes(b'x')
    running = 0
    peak = 0

    async def _fake(item, store_, config):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return _make_thumb(item, store_, config)

    monkeypatch.setattr(shop_mod, '_thumb_path', _fake)
    asyncio.run(shop_mod.prewarm_shop_assets(items, store, _FakeConfig(), concurrency=4))
    assert peak > 1, f'期望并发，实际峰值 {peak}'


def test_prewarm_respects_concurrency_limit(monkeypatch, tmp_path):
    store = _FakeStore(tmp_path)
    items = [_item(f'i{n}') for n in range(6)]
    (store.composite_dir / 'plain_senpai.png').write_bytes(b'x')
    running = 0
    peak = 0

    async def _fake(item, store_, config):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return _make_thumb(item, store_, config)

    monkeypatch.setattr(shop_mod, '_thumb_path', _fake)
    asyncio.run(shop_mod.prewarm_shop_assets(items, store, _FakeConfig(), concurrency=2))
    assert peak <= 2, f'并发度应受信号量限制，实际 {peak}'


def test_prewarm_survives_single_failure(monkeypatch, tmp_path):
    """单张失败不能拖垮整批（限流时其余图仍要出）。"""
    store = _FakeStore(tmp_path)
    items = [_item('ok1'), _item('bad'), _item('ok2')]
    (store.composite_dir / 'plain_senpai.png').write_bytes(b'x')

    async def _fake(item, store_, config):
        if item.id == 'bad':
            raise RuntimeError('限流')
        return _make_thumb(item, store_, config)

    monkeypatch.setattr(shop_mod, '_thumb_path', _fake)
    made = asyncio.run(shop_mod.prewarm_shop_assets(items, store, _FakeConfig()))
    assert made == 2, f'应成功 2 张，实际 {made}'
