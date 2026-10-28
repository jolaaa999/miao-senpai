from __future__ import annotations

import shutil
import time
from pathlib import Path

from dl_senpai.group_verify import (
    GroupVerifyStore,
    build_verify_pass_text,
    build_verify_prompt,
    looks_like_verify_message,
    reset_verify_store,
)

_TEST_ROOT = Path(__file__).resolve().parent / "_tmp_group_verify"


def _fresh_dir(name: str) -> Path:
    path = _TEST_ROOT / name
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_looks_like_verify_message() -> None:
    assert looks_like_verify_message("认证")
    assert looks_like_verify_message("学姐我来认证一下")
    assert looks_like_verify_message("入群认证")
    assert not looks_like_verify_message("")
    assert not looks_like_verify_message("报到")
    assert not looks_like_verify_message("hello")


def test_verify_prompt_and_pass_text() -> None:
    assert "10" in build_verify_prompt(timeout_min=10)
    assert "认证" in build_verify_prompt(timeout_min=10)
    assert "小明" in build_verify_pass_text("小明")
    assert "认证通过" in build_verify_pass_text("小明")


def test_store_register_pass_drop() -> None:
    reset_verify_store()
    store = GroupVerifyStore(_fresh_dir("register"))
    item = store.register(
        group_id=100,
        user_id=200,
        nickname="小明",
        timeout_sec=600,
    )
    assert store.is_pending(100, 200)
    assert item.deadline > time.time()
    assert abs(item.deadline - item.joined_at - 600) < 1.5

    passed = store.mark_passed(100, 200)
    assert passed is not None
    assert passed.nickname == "小明"
    assert not store.is_pending(100, 200)

    store.register(group_id=100, user_id=201, nickname="a", timeout_sec=60)
    store.drop(100, 201)
    assert not store.is_pending(100, 201)
    reset_verify_store()


def test_store_persists_across_reload() -> None:
    reset_verify_store()
    data_dir = _fresh_dir("persist")
    store = GroupVerifyStore(data_dir)
    store.register(group_id=1, user_id=2, nickname="n", timeout_sec=120)
    reload = GroupVerifyStore(data_dir)
    assert reload.is_pending(1, 2)
    got = reload.get(1, 2)
    assert got is not None
    assert got.nickname == "n"
    reset_verify_store()


def test_welcome_mentions_verify_only_when_needed() -> None:
    """默认只欢迎；need_verify（群管 + 认证开着）时才提「认证」和时限。

    为什么拆两套：学姐不是群管时踢不了人，「认证」两个字也接不住，
    写成默认话术就等于让新人做一件系统做不到的事（实测她会现场瞎编）。
    """
    import random

    from dl_senpai.welcome import build_welcome_text

    plain = build_welcome_text("小明", rng=random.Random(0))
    assert "认证" not in plain

    verify = build_welcome_text("小明", need_verify=True, rng=random.Random(0))
    assert "认证" in verify
    assert "分钟" in verify or "十分钟" in verify
