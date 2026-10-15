from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import dl_senpai.social_proactive as sp
from dl_senpai.social_proactive import SocialStateStore

_TEST_ROOT = Path(__file__).resolve().parent / "_tmp_social_proactive"


def _fresh_store(name: str) -> SocialStateStore:
    path = _TEST_ROOT / name
    shutil.rmtree(path, ignore_errors=True)
    return SocialStateStore(path)


class _FakeConfig:
    friend_add_enable = True
    friend_add_mode = "onebot"  # 单测走 OneBot 假 API，不打真实 Cookie CGI
    friend_add_daily_limit = 2
    friend_add_verify_msg = "我是群里的学姐~"
    friend_add_exclude = "99999"


class _FakeBot:
    def __init__(self, members: dict[int, list[dict]], friends: set[int]) -> None:
        self.self_id = "10000"
        self._members = members
        self._friends = friends
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, api: str, **kwargs):
        self.calls.append((api, kwargs))
        if api == "get_friend_list":
            return [{"user_id": u} for u in self._friends]
        if api == "get_group_member_list":
            return self._members.get(kwargs["group_id"], [])
        if api == "get_group_list":
            return [{"group_id": g} for g in self._members]
        if api in ("set_buddy_request", "add_friend", "create_friend_request", "friend_request"):
            return {}
        if api == "get_cookies":
            return {
                "cookies": "uin=o10000; skey=abc; p_skey=pskey123",
                "bkn": "1",
            }
        raise RuntimeError(f"unexpected api {api}")


class _FakeRegistry:
    def __init__(self, gids: list[str]) -> None:
        self._gids = gids

    def list_groups(self):
        return [
            {"group_id": g, "name": "", "features": {}, "updated_at": ""}
            for g in self._gids
        ]

    def override(self, group_id, feature):
        for g in self.list_groups():
            if g["group_id"] == str(group_id):
                v = g["features"].get(feature)
                return v if isinstance(v, bool) else None
        return None


def test_request_dedup_and_day_reset() -> None:
    store = _fresh_store("req")
    store.record_request(111, 100)
    store.record_request(111, 100)
    assert store.count_sent_today() == 1
    raw = {"day": "2000-01-01", "sent": {"222": {"group": "100"}}}
    (store.dir / "friend_requests.json").write_text(json.dumps(raw), encoding="utf-8")
    assert store.count_sent_today() == 0



def test_friend_add_tick_quota_and_filters(monkeypatch) -> None:
    store = _fresh_store("quota")
    monkeypatch.setattr(sp, "get_social_store", lambda: store)
    monkeypatch.setattr(sp, "_FRIEND_ADD_GAP", (0, 0))
    monkeypatch.setattr(sp, "_friend_add_onebot_dead", False)
    monkeypatch.setattr(sp, "_locked_add_api", None)
    registry = _FakeRegistry(["100", "200"])
    monkeypatch.setattr("dl_senpai.group_features.get_group_feature_store", lambda: registry)

    bot = _FakeBot(
        members={
            100: [
                {"user_id": 111},
                {"user_id": 99999},
                {"user_id": 10000},
                {"user_id": 112},
                {"user_id": 113},
            ],
            200: [{"user_id": 121}],
        },
        friends={113},
    )
    config = _FakeConfig()
    config.friend_add_daily_limit = 2

    sent = asyncio.run(sp.friend_add_tick(bot, config))
    assert sent == 2
    uids = {
        c[1]["user_id"]
        for c in bot.calls
        if c[0] in ("set_buddy_request", "add_friend", "create_friend_request", "friend_request")
    }
    assert len(uids) == 2
    assert 113 not in uids and 99999 not in uids and 10000 not in uids
    assert store.count_sent_today() == 2
    assert asyncio.run(sp.friend_add_tick(bot, config)) == 0


def test_friend_add_per_group_override(monkeypatch) -> None:
    store = _fresh_store("override")
    monkeypatch.setattr(sp, "get_social_store", lambda: store)
    monkeypatch.setattr(sp, "_FRIEND_ADD_GAP", (0, 0))
    monkeypatch.setattr(sp, "_friend_add_onebot_dead", False)
    monkeypatch.setattr(sp, "_locked_add_api", None)
    registry = _FakeRegistry(["100"])
    monkeypatch.setattr("dl_senpai.group_features.get_group_feature_store", lambda: registry)

    def list_with_override():
        return [
            {
                "group_id": "100",
                "name": "",
                "features": {"friend_add": False},
                "updated_at": "",
            }
        ]

    monkeypatch.setattr(registry, "list_groups", list_with_override)

    bot = _FakeBot(members={100: [{"user_id": 111}]}, friends=set())
    assert asyncio.run(sp.friend_add_tick(bot, _FakeConfig())) == 0
    assert not any(
        c[0]
        in ("set_buddy_request", "add_friend", "create_friend_request", "friend_request")
        for c in bot.calls
    )

    monkeypatch.setattr(
        registry,
        "list_groups",
        lambda: [{"group_id": "100", "name": "", "features": {}, "updated_at": ""}],
    )
    assert asyncio.run(sp.friend_add_tick(bot, _FakeConfig())) == 1


def test_friend_add_api_failure_tolerated(monkeypatch) -> None:
    store = _fresh_store("fail")
    monkeypatch.setattr(sp, "get_social_store", lambda: store)
    monkeypatch.setattr(sp, "_FRIEND_ADD_GAP", (0, 0))
    monkeypatch.setattr(sp, "_friend_add_onebot_dead", False)
    monkeypatch.setattr(sp, "_locked_add_api", None)
    registry = _FakeRegistry(["100"])
    monkeypatch.setattr("dl_senpai.group_features.get_group_feature_store", lambda: registry)

    class _FailBot(_FakeBot):
        async def call_api(self, api: str, **kwargs):
            if api in (
                "set_buddy_request",
                "add_friend",
                "create_friend_request",
                "friend_request",
            ):
                raise RuntimeError("napcat error")
            return await super().call_api(api, **kwargs)

    bot = _FailBot(members={100: [{"user_id": 111}]}, friends={111})
    assert asyncio.run(sp.friend_add_tick(bot, _FakeConfig())) == 0
    assert store.count_sent_today() == 0


def teardown_module(module: object) -> None:
    sp._locked_add_api = None
    sp._friend_add_onebot_dead = False
    sp._onebot_unsupported_logged = False
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
