from __future__ import annotations

import json
import shutil
from pathlib import Path

import dl_senpai.group_features as gf
from dl_senpai.group_features import (
    FEATURES,
    FEATURE_KEYS,
    GroupFeatureStore,
    default_group_features_path,
    is_feature_enabled,
)

_TEST_ROOT = Path(__file__).resolve().parent / "_tmp_group_features"


def _fresh_path(name: str) -> Path:
    path = _TEST_ROOT / name / "group_features.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    # 让模块单例指向本用例的数据文件（is_feature_enabled 读单例）
    gf._store = None
    gf._store_path = None
    gf.get_group_feature_store(path)
    return path


class _FakeConfig:
    """模拟 PluginConfig 的全局开关与 checkin 白名单。"""

    def __init__(self, **overrides: bool) -> None:
        self.checkin_groups = "100,200"
        for key, _label in FEATURES:
            setattr(self, f"{key}_enable", True)
        for key, value in overrides.items():
            setattr(self, f"{key}_enable", value)

    def is_checkin_group(self, group_id: int | str) -> bool:
        return str(group_id) in {"100", "200"}


def test_feature_catalog_complete() -> None:
    keys = {k for k, _ in FEATURES}
    assert keys == FEATURE_KEYS
    assert len(FEATURES) == 21
    # key 与 config 的 {key}_enable 字段一一对应
    for key, _label in FEATURES:
        assert hasattr(_FakeConfig(), f"{key}_enable")


def test_default_follows_global() -> None:
    store = GroupFeatureStore(_fresh_path("follow"))
    cfg = _FakeConfig()
    # 无覆盖：全局开 → 开
    assert is_feature_enabled(cfg, 100, "draw") is True
    # 全局关 → 关（即使无覆盖）
    cfg_off = _FakeConfig(draw=False)
    assert is_feature_enabled(cfg_off, 100, "draw") is False
    # 覆盖关不掉全局已关的
    store.set_feature(100, "draw", True)
    assert is_feature_enabled(cfg_off, 100, "draw") is False


def test_override_toggle_and_reset() -> None:
    store = GroupFeatureStore(_fresh_path("override"))
    cfg = _FakeConfig()
    assert store.override(100, "checkin") is None
    store.set_feature(100, "checkin", False)
    assert store.override(100, "checkin") is False
    assert is_feature_enabled(cfg, 100, "checkin") is False
    # 其他群不受影响
    assert is_feature_enabled(cfg, 200, "checkin") is True
    # 恢复跟随全局
    store.set_feature(100, "checkin", None)
    assert store.override(100, "checkin") is None
    assert is_feature_enabled(cfg, 100, "checkin") is True


def test_private_and_unknown_key() -> None:
    cfg = _FakeConfig()
    store = GroupFeatureStore(_fresh_path("private"))
    store.set_feature(100, "draw", False)
    # 私聊只看全局
    assert is_feature_enabled(cfg, None, "draw") is True
    # 未知 key 不拦截
    assert is_feature_enabled(cfg, 100, "nope") is True


def test_upsert_and_cross_process_visibility() -> None:
    path = _fresh_path("cross")
    store = GroupFeatureStore(path)
    assert store.upsert_group(300, "测试群") is True
    assert store.upsert_group(300, "测试群") is False
    store.upsert_group(300, "改名群")
    # 另一个实例（模拟 Go 后端/bot 重读）能看到
    other = GroupFeatureStore(path)
    groups = {g["group_id"]: g for g in other.list_groups()}
    assert groups["300"]["name"] == "改名群"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1


def test_default_path_layout() -> None:
    path = default_group_features_path()
    assert path.parent.name == "admin"
    assert path.parent.parent.name == "dl_senpai"
    assert path.name == "group_features.json"


def teardown_module(module: object) -> None:
    # 还原单例，避免影响其它用例模块
    gf._store = None
    gf._store_path = None
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
