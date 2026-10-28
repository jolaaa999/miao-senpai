"""喵指令（管理员指令）的测试。

覆盖的重点都是「错了很难被发现」的地方：

1. **鉴权**：只认配置里的管理员 QQ；非管理员必须一个 API 都调不到。
   这条错了的后果是群里任何人都能禁言、撤回、踢人。
2. **目标解析**：@ 的人 / 被引用消息的作者 / 直接写的 QQ 号，三条路都要通，
   而且不该把它们混成一个（比如把时长 10 当成 QQ 号）。
3. **副作用参数**：禁言的时长、解禁的 0、全员禁言的默认开——
   这些参数写错了不会报错，只会「看起来做了但没生效」。
4. **静默**：写进去要能读出来，喵醒要能清掉，且只影响本群。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from dl_senpai.admin_commands import AdminDenied, parse_admin_args, parse_duration
from dl_senpai.core.commands.admin import register_admin_commands
from dl_senpai.core.commands.registry import CommandRegistry


# ---- 测试替身 ----


class FakeBot:
    """只记录调用、不做任何真实网络请求的 bot。"""

    self_id = "999999"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, api: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((api, kwargs))

    async def set_group_ban(self, **kwargs: Any) -> None:
        self._record("set_group_ban", kwargs)

    async def set_group_whole_ban(self, **kwargs: Any) -> None:
        self._record("set_group_whole_ban", kwargs)

    async def set_group_kick(self, **kwargs: Any) -> None:
        self._record("set_group_kick", kwargs)

    async def set_group_admin(self, **kwargs: Any) -> None:
        self._record("set_group_admin", kwargs)

    async def set_group_card(self, **kwargs: Any) -> None:
        self._record("set_group_card", kwargs)

    async def send_group_notice(self, **kwargs: Any) -> None:
        self._record("send_group_notice", kwargs)

    async def delete_msg(self, **kwargs: Any) -> None:
        self._record("delete_msg", kwargs)

    async def send(self, event: Any, message: Any) -> None:
        self._record("send", {"message": message})

    def api_calls(self, api: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if name == api]


@pytest.fixture(autouse=True)
def _patch_reply(monkeypatch):
    """拦掉回复，避免测试真的去发消息；同时把发出去的文案收集起来断言。"""
    sent: list[str] = []

    async def fake_reply(bot, event, text, **kwargs):
        sent.append(text)

    import dl_senpai.handlers as handlers

    monkeypatch.setattr(handlers, "_reply", fake_reply)
    return sent


@pytest.fixture()
def registry() -> CommandRegistry:
    reg = CommandRegistry()
    register_admin_commands(reg, owner="admin_commands")
    return reg


@pytest.fixture()
def config(tmp_path):
    """真 PluginConfig（别名注入），只把静默路径改到临时目录。

    为什么不用 SimpleNamespace 造假配置：那样 is_admin 会走权限层的
    「老配置回退分支」，测不到 admin_qq 真正生效——而这正是本次需求的核心。
    """
    from dl_senpai.config import PluginConfig

    def _make(*, admin: str = "10001,10002", allow_group_admin: bool = False):
        return PluginConfig(
            DL_SENPAI_ADMIN_QQ=admin,
            DL_SENPAI_AGENT_ADMIN_QQ="",
            DL_SENPAI_OWNER_IDS="",
            DL_SENPAI_ADMIN_ALLOW_GROUP_ADMIN="true" if allow_group_admin else "false",
            DL_SENPAI_ADMIN_SILENCE_PATH=str(tmp_path / "silence.json"),
        )

    return _make


def _ctx(
    bot: FakeBot,
    cfg: Any,
    *,
    text: str,
    user_id: str = "10001",
    group_id: str = "900001",
    at_ids: tuple[int, ...] = (),
    reply_message_id: str | None = None,
    reply_author: int | None = None,
    role: str = "member",
):
    segs: list[Any] = [SimpleNamespace(type="at", data={"qq": str(qq)}) for qq in at_ids]
    segs.append(SimpleNamespace(type="text", data={"text": text}))
    reply = None
    if reply_message_id is not None or reply_author is not None:
        reply = SimpleNamespace(
            message_id=reply_message_id or "5555",
            sender=SimpleNamespace(user_id=reply_author),
        )
    raw = SimpleNamespace(
        group_id=group_id,
        user_id=user_id,
        message=segs,
        reply=reply,
        sender=SimpleNamespace(role=role),
    )
    return SimpleNamespace(
        group_id=group_id,
        user_id=user_id,
        self_id="999999",
        session_id=f"group:{group_id}",
        plain_text=text,
        raw_event=raw,
        bot=bot,
        config=cfg,
    )


def _run(registry: CommandRegistry, ctx: Any, text: str) -> None:
    """按真实链路执行：先让注册表匹配，再调 handler。"""
    invocation = registry.match(text, ctx)
    assert invocation is not None, f"这条文本没有被任何管理指令匹配：{text}"
    asyncio.run(invocation.command.handler(ctx, invocation.match))


# ---- 1. 前缀与别名 ----


@pytest.mark.parametrize(
    "text,action",
    [
        ("喵禁言", "mute"),
        ("/silence", "mute"),
        ("#sl", "mute"),
        ("！禁言", "mute"),
        ("喵 MUTE", "mute"),
        ("喵撤回", "recall"),
        ("喵帮助", "help"),
        ("喵状态", "status"),
        ("喵闭嘴", "silence"),
        ("喵醒", "wake"),
    ],
)
def test_prefix_and_alias_match(registry, text, action):
    invocation = registry.match(text)
    assert invocation is not None, text
    assert invocation.match.action == action


@pytest.mark.parametrize(
    "text",
    [
        "今天学姐好可爱",  # 没有前缀
        "禁言 @某人",  # 有指令名但没前缀：不能误伤普通聊天
        "喵禁言语无伦次",  # 指令名只是词的前缀，不该命中
        "reset喵",
    ],
)
def test_plain_chat_is_not_a_command(registry, text):
    assert registry.match(text) is None


# ---- 2. 参数解析 ----


@pytest.mark.parametrize(
    "token,seconds",
    [("10", 10), ("10s", 10), ("10秒", 10), ("10m", 600), ("1h", 3600), ("2d", 172800), ("1w", 604800)],
)
def test_parse_duration_units(token, seconds):
    assert parse_duration(token) == seconds


def test_parse_duration_rejects_words():
    assert parse_duration("随便") is None
    assert parse_duration("") is None


def test_parse_args_splits_target_and_duration():
    args = parse_admin_args(" @小明 123456789 10m")
    assert args.targets == (123456789,)
    assert args.duration == 600
    assert args.words == ""


def test_parse_args_default_unit_is_minutes_for_silence():
    """喵闭嘴 10 = 10 分钟。裸数字的单位由指令自己定，不能一律按秒。"""
    assert parse_admin_args(" 10", default_unit_seconds=60).duration == 600


# ---- 3. 鉴权 ----


def test_admin_can_mute_by_at(registry, config, _patch_reply):
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵禁言 10m", at_ids=(222333,))
    _run(registry, ctx, "喵禁言 10m")
    calls = bot.api_calls("set_group_ban")
    assert calls and calls[0]["user_id"] == 222333
    assert calls[0]["duration"] == 600
    assert calls[0]["group_id"] == 900001


def test_second_admin_is_also_allowed(registry, config):
    """配了多个管理员，第二个也要能用（这是本次需求的硬要求）。"""
    bot = FakeBot()
    ctx = _ctx(bot, config(admin="10001,10002"), user_id="10002", text="喵禁言 222333")
    _run(registry, ctx, "喵禁言 222333")
    assert bot.api_calls("set_group_ban")


def test_non_admin_is_denied_and_touches_nothing(registry, config, _patch_reply):
    bot = FakeBot()
    ctx = _ctx(bot, config(), user_id="99999", text="喵禁言 10m", at_ids=(222333,))
    with pytest.raises(AdminDenied):
        _run(registry, ctx, "喵禁言 10m")
    assert bot.calls == [], "非管理员竟然触发了群管 API"
    assert _patch_reply == [], "拒绝时不该回话（等于告诉全群谁有权限）"


def test_no_admin_configured_means_nobody(registry, config):
    """管理员名单为空 ⇒ 谁都不是管理员。反过来写就是全开的后门。"""
    bot = FakeBot()
    ctx = _ctx(bot, config(admin=""), text="喵禁言 222333")
    with pytest.raises(AdminDenied):
        _run(registry, ctx, "喵禁言 222333")
    assert bot.calls == []


def test_group_role_admin_requires_switch(registry, config):
    """群主/群管理员默认**不**能用；打开开关后才放行。"""
    bot = FakeBot()
    off = _ctx(bot, config(), user_id="88888", role="owner", text="喵禁言 222333")
    with pytest.raises(AdminDenied):
        _run(registry, off, "喵禁言 222333")

    bot2 = FakeBot()
    on = _ctx(
        bot2, config(allow_group_admin=True), user_id="88888", role="admin", text="喵禁言 222333"
    )
    _run(registry, on, "喵禁言 222333")
    assert bot2.api_calls("set_group_ban")


# ---- 4. 目标解析 ----


def test_mute_target_from_quoted_message(registry, config):
    """引用一条消息发「喵禁言」= 禁言那条消息的作者。"""
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵禁言", reply_message_id="7777", reply_author=444555)
    _run(registry, ctx, "喵禁言")
    assert bot.api_calls("set_group_ban")[0]["user_id"] == 444555


def test_mute_target_from_qq_number(registry, config):
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵禁言 666777 5m")
    _run(registry, ctx, "喵禁言 666777 5m")
    call = bot.api_calls("set_group_ban")[0]
    assert call["user_id"] == 666777
    assert call["duration"] == 300


def test_mute_without_target_explains_how(registry, config, _patch_reply):
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵禁言")
    _run(registry, ctx, "喵禁言")
    assert bot.calls == []
    assert any("@ 那个人" in text for text in _patch_reply)


def test_mute_default_duration_is_60s(registry, config):
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵禁言", at_ids=(222333,))
    _run(registry, ctx, "喵禁言")
    assert bot.api_calls("set_group_ban")[0]["duration"] == 60


def test_mute_duration_is_clamped(registry, config):
    """超长时长要自己夹住，否则协议端直接拒绝，管理员只看到「失败」。"""
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵禁言 999d", at_ids=(222333,))
    _run(registry, ctx, "喵禁言 999d")
    assert bot.api_calls("set_group_ban")[0]["duration"] == 30 * 86400


# ---- 5. 其它群管指令 ----


def test_unmute_uses_zero_duration(registry, config):
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵解禁", at_ids=(222333,))
    _run(registry, ctx, "喵解禁")
    assert bot.api_calls("set_group_ban")[0]["duration"] == 0


def test_muteall_defaults_to_on(registry, config):
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵全员禁言")
    _run(registry, ctx, "喵全员禁言")
    assert bot.api_calls("set_group_whole_ban")[0]["enable"] is True


def test_muteall_can_be_turned_off(registry, config):
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵全员禁言 关")
    _run(registry, ctx, "喵全员禁言 关")
    assert bot.api_calls("set_group_whole_ban")[0]["enable"] is False


def test_recall_deletes_quoted_message(registry, config):
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵撤回", reply_message_id="8888", reply_author=1)
    _run(registry, ctx, "喵撤回")
    assert bot.api_calls("delete_msg")[0]["message_id"] == 8888


def test_card_uses_raw_text_so_numbers_survive(registry, config):
    """名片/公告里的数字不能被当成 QQ 号吃掉。"""
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵名片 222333 小明2024", at_ids=(222333,))
    _run(registry, ctx, "喵名片 222333 小明2024")
    call = bot.api_calls("set_group_card")[0]
    assert call["user_id"] == 222333
    assert call["card"] == "222333 小明2024"


# ---- 6. 静默 ----


def test_silence_sets_state_and_wake_clears_it(registry, config):
    from dl_senpai.admin_control import get_silence_store

    cfg = config()
    bot = FakeBot()
    ctx = _ctx(bot, cfg, text="喵闭嘴 10")
    _run(registry, ctx, "喵闭嘴 10")
    store = get_silence_store(cfg.admin_silence_path)
    assert 590 <= store.remaining("900001") <= 600

    _run(registry, _ctx(bot, cfg, text="喵醒"), "喵醒")
    assert store.remaining("900001") == 0


def test_silence_is_per_group(registry, config):
    from dl_senpai.admin_control import get_silence_store

    cfg = config()
    bot = FakeBot()
    _run(registry, _ctx(bot, cfg, text="喵闭嘴 5", group_id="111"), "喵闭嘴 5")
    store = get_silence_store(cfg.admin_silence_path)
    assert store.remaining("111") > 0
    assert store.remaining("222") == 0


# ---- 7. 帮助与状态 ----


def _help_with_forward(registry, monkeypatch, text: str, _patch_reply):
    """跑一次 help，返回合并转发的节点列表（没走卡片时返回 None）。"""
    import dl_senpai.admin_commands as ac

    captured: dict[str, Any] = {"nodes": None}

    async def fake_forward(ctx, nodes):
        captured["nodes"] = nodes
        return True

    monkeypatch.setattr(ac, "_reply_forward", fake_forward)
    _run(registry, _ctx(FakeBot(), _config_of(registry), text=text), text)
    return captured["nodes"]


def _config_of(registry):
    """帮助测试只关心文案，用一个干净的管理员配置即可。"""
    from dl_senpai.config import PluginConfig

    return PluginConfig(DL_SENPAI_ADMIN_QQ="10001")


def test_help_lists_every_command(registry, monkeypatch, _patch_reply):
    """帮助文案取自注册表，并且**一条不漏**地列出来。

    用户 2026-10-28 明确要求「喵help 弹出所有指令」，所以断言的是注册表里
    46 条全部出现，而不是抽查几个关键词。
    """
    nodes = _help_with_forward(registry, monkeypatch, "喵help", _patch_reply)
    assert nodes, "默认应当发合并转发卡片"
    joined = "\n".join(body for _, body in nodes)

    from dl_senpai.core.commands.admin import merged_tables

    _, descriptions = merged_tables()
    missing = [action for action, text in descriptions.items() if text.split(" — ")[0] not in joined]
    assert missing == [], f"帮助里漏了这些指令：{missing}"
    assert "主人专属 47 条" in joined


def test_help_sends_one_merged_card(registry, monkeypatch, _patch_reply):
    """整张表用一条合并转发发出去，而不是连着刷好几屏。

    46 条分 6 条普通消息发，群里会连着刷 6 屏还夹着别人的话；
    折叠成一张聊天记录卡片，想看的人点开就有全部。
    """
    nodes = _help_with_forward(registry, monkeypatch, "喵help", _patch_reply)
    # 一个总览节点 + 六个主人专属分组 + 一张所有人可用
    assert len(nodes) == 8, [title for title, _ in nodes]
    titles = [title for title, _ in nodes]
    assert titles[0] == "怎么用"
    for group in ("群管理", "群信息", "风控", "学姐自身", "运维", "小工具"):
        assert any(title.startswith(group) for title in titles), titles
    # 每条指令的用法单独一行，说明另起一行缩进：不靠空格对齐（QQ 是比例字体，
    # 对齐出来的列在手机上必然歪，这正是之前「格式乱」的原因）
    body = dict(nodes)["群管理 · 13 条（主人专属）"]
    assert "• kick @某人 [拉黑]\n   踢出本群" in body


def test_help_falls_back_when_forward_unsupported(registry, monkeypatch, _patch_reply):
    """协议端不支持合并转发时退回普通消息，功能不能因为发送方式缺失而消失。"""
    import dl_senpai.admin_commands as ac

    async def broken_forward(ctx, nodes):
        return False

    monkeypatch.setattr(ac, "_reply_forward", broken_forward)
    _run(registry, _ctx(FakeBot(), _config_of(registry), text="喵help"), "喵help")
    assert _patch_reply, "退回分段发时必须有内容发出去"
    assert any("kick" in segment for segment in _patch_reply)


def test_help_filters_by_group(registry, config, _patch_reply):
    """「喵help 群管」只看一组：已经知道要找什么时不用被全文刷屏。"""
    bot = FakeBot()
    _run(registry, _ctx(bot, config(), text="喵help 群管"), "喵help 群管")
    joined = "\n".join(_patch_reply)
    assert "mute @某人" in joined
    assert "dice" not in joined  # 小工具组不该出现


def test_help_filters_by_keyword(registry, config, _patch_reply):
    """组名没对上时当关键词用：记不住分组、只记得「禁言」也能搜到。"""
    bot = FakeBot()
    _run(registry, _ctx(bot, config(), text="喵help 禁言"), "喵help 禁言")
    joined = "\n".join(_patch_reply)
    assert "• mute @某人" in joined and "• mutelist" in joined
    assert "dice" not in joined



def test_help_says_so_when_nothing_matches(registry, config, _patch_reply):
    bot = FakeBot()
    _run(registry, _ctx(bot, config(), text="喵help 量化交易"), "喵help 量化交易")
    assert "没有和「量化交易」对上" in _patch_reply[-1]



def test_member_help_lists_only_playable(registry, monkeypatch, _patch_reply):
    """群友发「喵help」也有回应，但只看得到自己能用的（2026-10-28 要求）。

    「喵 = 管理员专用」这条规则对 help 开了个口子：不然群友发「喵help」什么
    都收不到，只会以为 bot 坏了。
    """
    import dl_senpai.admin_commands as ac

    captured: dict[str, Any] = {"nodes": None}

    async def fake_forward(ctx, nodes):
        captured["nodes"] = nodes
        return True

    monkeypatch.setattr(ac, "_reply_forward", fake_forward)
    ctx = _ctx(FakeBot(), _config_of(registry), text="喵help", user_id="20002")
    _run(registry, ctx, "喵help")

    nodes = captured["nodes"]
    assert nodes, "群友也要收到卡片"
    joined = "\n".join(body for _, body in nodes)
    assert "签到" in joined and "好感度" in joined and "商店" in joined
    # 主人专属的一条都不许露出来
    for owner_only in ("kick", "mute", "recall", "blacklist", "leave"):
        assert owner_only not in joined, owner_only
    # 而且要明确告诉他：带「喵」的是主人的指令
    assert "主人" in joined


def test_admin_help_marks_the_two_layers(registry, monkeypatch, _patch_reply):
    """主人看全部，但每组都标「主人专属」，末尾另附「所有人可用」。"""
    nodes = _help_with_forward(registry, monkeypatch, "喵help", _patch_reply)
    titles = [title for title, _ in nodes]
    assert sum("主人专属" in title for title in titles) == 6, titles
    assert any("所有人可用" in title for title in titles), titles
    body = "\n".join(text for _, text in nodes)
    assert "kick" in body and "签到" in body


def test_member_help_search_says_owner_only(registry, _patch_reply):
    """群友搜到主人的指令时要明说「这是主人的」，而不是「没这个东西」。

    「你权限不够」和「不存在」对群友是两种完全不同的信息，后者会让人以为
    bot 坏了从而反复试。
    """
    cfg = _config_of(registry)
    # 注意别选「禁言」：禁言名单现在是群友指令，搜它会命中另一条路径
    # 用「撤回」：它只出现在主人指令的说明里，群友搜到该被明确告知「这是主人的」
    _run(registry, _ctx(FakeBot(), cfg, text="喵help 撤回", user_id="20002"), "喵help 撤回")
    joined = "\n".join(_patch_reply)
    assert "主人" in joined
    assert "• recall" not in joined

def test_member_help_finds_playable_commands(registry, _patch_reply):
    """群友搜自己有的指令要能搜到——搜不到就等于没有。

    「禁言名单」曾经只有主人能看，2026-10-28 起开放给群友，
    这条用例盯着它别又缩回主人专属里。
    """
    cfg = _config_of(registry)
    _run(registry, _ctx(FakeBot(), cfg, text="喵help 禁言名单", user_id="20002"), "喵help 禁言名单")
    joined = "\n".join(_patch_reply)
    assert "禁言名单" in joined



def test_member_cannot_run_owner_command(registry):
    """help 放开「喵」前缀，不代表别的指令也放开。"""
    from dl_senpai.admin_commands import AdminDenied

    ctx = _ctx(FakeBot(), _config_of(registry), text="喵kick 123456", user_id="20002")
    invocation = registry.match("喵kick 123456", ctx)
    assert invocation is not None
    with pytest.raises(AdminDenied):
        asyncio.run(invocation.command.handler(ctx, invocation.match))


def test_every_playable_entry_really_works():
    """帮助里列的玩法指令必须真能用——防止静态清单和实现脱节。

    ALL_LEVEL 的触发词是手写的（正则里反推不出来人话），所以需要一条测试
    拿着它们去注册表里跑一遍：写错了、或某条玩法被改没了，这里会先红。
    """
    from dl_senpai.core.commands.admin import ALL_LEVEL
    from dl_senpai.core.commands.builtin import build_builtin_commands

    reg = CommandRegistry()
    for command in build_builtin_commands():
        reg.register(command)

    for usage, _detail in ALL_LEVEL:
        for word in usage.split(" / "):
            probe = word.replace(" X", " 裙子")
            # ctx=None：跳过 guard（「认证」平时还要求有待认证的人）
            invocation = reg.match(probe, None)
            assert invocation is not None, f"「{probe}」匹配不到任何指令"
            assert invocation.command.permission != "admin", f"「{probe}」不是所有人能用的"



def test_status_reports_admin_count(registry, config, _patch_reply):
    bot = FakeBot()
    ctx = _ctx(bot, config(admin="10001,10002"), text="喵状态")
    _run(registry, ctx, "喵状态")
    assert "管理员：2 人" in _patch_reply[-1]


# ---- 8. 配置层：多个管理员 + 三级回退 ----


def test_config_admin_ids_from_new_field():
    from dl_senpai.config import PluginConfig

    cfg = PluginConfig(DL_SENPAI_ADMIN_QQ="10001, 10002；10003")
    assert cfg.admin_id_set() == {"10001", "10002", "10003"}
    assert cfg.is_admin(10001) is True
    assert cfg.is_admin("10003") is True
    assert cfg.is_admin("99999") is False


def test_config_falls_back_to_legacy_fields():
    """没配 admin_qq 时，老配置（agent_admin_qq / owner_ids）继续有效，
    否则升级当天所有老部署会突然失去管理权限。"""
    from dl_senpai.config import PluginConfig

    legacy = PluginConfig(DL_SENPAI_AGENT_ADMIN_QQ="20001")
    assert legacy.admin_id_set() == {"20001"}

    owner_only = PluginConfig(DL_SENPAI_OWNER_IDS="30001;30002")
    assert owner_only.admin_id_set() == {"30001", "30002"}

    empty = PluginConfig()
    assert empty.admin_id_set() == set()
    assert empty.is_admin("10001") is False


# ---- 9. 功能开关 ----


def test_feature_switch_writes_group_override(registry, config, tmp_path, monkeypatch, _patch_reply):
    """喵功能 签到 关 要真的写进 group_features 的按群覆盖。"""
    import dl_senpai.group_features as gf

    store = gf.GroupFeatureStore(tmp_path / "group_features.json")
    # 重定向进程级单例：指令里走的是 get_group_feature_store() 这条全局路径
    monkeypatch.setattr(gf, "_store", store)
    monkeypatch.setattr(gf, "_store_path", str(store.path))

    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵功能 签到 关")
    _run(registry, ctx, "喵功能 签到 关")
    assert store.override("900001", "checkin") is False

    # 再说「跟随」应该把覆盖删掉（三态语义：开 / 关 / 跟随全局）
    _run(registry, _ctx(bot, config(), text="喵功能 签到 跟随"), "喵功能 签到 跟随")
    assert store.override("900001", "checkin") is None


def test_feature_unknown_name_is_rejected(registry, config, _patch_reply):
    bot = FakeBot()
    ctx = _ctx(bot, config(), text="喵功能 不存在的功能 关")
    _run(registry, ctx, "喵功能 不存在的功能 关")
    assert any("没有叫" in text for text in _patch_reply)



def test_admin_reply_skips_humanize_delay(config, monkeypatch):
    """管理回执不走拟人打字延迟。

    没有这条断言时，「喵/帮助」分 6 段、每段都被当成闲聊「正在输入」等 1-2 秒，
    管理员会以为她卡死了。管理回执是工具输出，不是她在撒娇。
    """
    import dl_senpai.handlers as handlers
    from dl_senpai.admin_commands import _reply as admin_reply

    captured: dict[str, Any] = {}

    async def fake_send(bot, event, text, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(handlers, "_reply", fake_send)
    ctx = _ctx(FakeBot(), config(), text="喵帮助")
    import asyncio

    asyncio.run(admin_reply(ctx, "测试回执"))
    assert captured.get("humanize") is False
