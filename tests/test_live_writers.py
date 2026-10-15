"""内心状态与群风格采样的「写侧复活」回归。

背景：这两块数据的**读侧**一直在 runtime 桥接层里（build_snapshot →
`inner_state_brief` / `group_style_brief` 注入 prompt），但**写侧**唯一的调用者
留在跑不起来的 `agent/legacy_orchestrator.py` 里。结果是开关开着、每一轮都在读，
喂进去的却是一个多月前冻住的状态——和记忆卡片那次是同一个「读活写死」的坑。

本文件锁住补上的两处写入点，防止它们再次被静默摘掉：
  1. `handlers.observe_recent_chat` 里的群风格采样
  2. `legacy_bridge._deliver_runtime_reply` 里的内心状态结算
"""

from __future__ import annotations

from pathlib import Path

from dl_senpai import group_style as group_style_mod
from dl_senpai import handlers
from dl_senpai import legacy_bridge as bridge
from dl_senpai import persona_filter
from dl_senpai.agent import memory_facade as facade_mod
from dl_senpai.core import domain as domain_mod
from dl_senpai.agent import response as response_mod
from dl_senpai.config import PluginConfig


def _cfg(tmp_path: Path, **kw):
    """所有目录都指向 tmp_path，否则测试会写生产数据。"""
    base = {
        "DL_SENPAI_MEMORY_DIR": str(tmp_path / "memory"),
        "DL_SENPAI_RECENT_CHAT_DIR": str(tmp_path / "recent_chat"),
        "DL_SENPAI_GROUP_STYLE_DIR": str(tmp_path / "group_style"),
        "DL_SENPAI_INNER_STATE_DIR": str(tmp_path / "inner_state"),
        "DL_SENPAI_AFFECTION_DIR": str(tmp_path / "affection"),
        # 显式钉住，免得 .env 改了它导致作用域键的断言随环境漂移。
        "DL_SENPAI_INNER_STATE_PER_GROUP": True,
    }
    base.update(kw)
    return PluginConfig(**base)


# -- 发送链路桩件（与 test_quote_and_memory 同一套形状）----------------


class _Delivered:
    def __init__(self, sent: bool, error: str = "", text: str = "") -> None:
        self.sent = sent
        self.error = error
        # 生产里 ResponseOutcome.text 是**润色后**的发送文本；缺省空串表示
        # 这个替身不关心润色，此时桥接层回退到 outcome.result.text。
        self.text = text


def _pipe(sent: bool, text: str = ""):
    class _P:
        async def deliver(self, result, context, *, dry_run=False, reply_options=None):
            return _Delivered(sent, text=text)

    return _P


class _Result:
    def __init__(self, text: str) -> None:
        self.text = text


class _Outcome:
    def __init__(self, text: str) -> None:
        self.result = _Result(text)


class _AgentCtx:
    """runtime 路径 AgentContext 替身：结算内心状态要用的字段全在这里。"""

    def __init__(self, *, cfg=None, group_id="12345", private=False,
                 reason="mention", interrupt=False, bot=None,
                 user_id="1001", raw_event=None, actor="小明") -> None:
        self.session_id = "private_1" if private else f"group_{group_id}"
        self.user_text = "用户问题"
        self.plain_text = "用户问题"
        self.config = cfg
        self.group_id = group_id
        self.private_chat = private
        self.trigger_reason = reason
        self.interrupt = interrupt
        self.bot = bot
        self.user_id = user_id
        self.actor = actor
        # 好感度作用域要靠真实事件类型区分群/私聊，所以默认给 None，由测试自己
        # monkeypatch affection_scope；随手塞个假事件反而会掩盖判定逻辑。
        self.raw_event = raw_event


# ================================================================
# 1. 内心状态：回复发出后才结算
# ================================================================


async def test_inner_state_updated_after_success(monkeypatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    seen: list[dict] = []
    monkeypatch.setattr(
        facade_mod.MemoryFacade,
        "update_inner_state",
        lambda self, key, **kw: seen.append({"key": key, **kw}),
    )
    # 故意让「发出去的」和「模型原文」不一样：reply_len 必须量前者。
    monkeypatch.setattr(response_mod, "ResponsePipeline", _pipe(True, "回复正文"))
    monkeypatch.setattr(bridge, "_record_turn", lambda *a, **k: None)
    await bridge._deliver_runtime_reply(_AgentCtx(cfg=cfg), _Outcome("回复**正文**"))
    assert len(seen) == 1, "发送成功后必须结算内心状态"
    assert seen[0]["key"] == "group_12345"
    assert seen[0]["mentioned"] is True
    assert seen[0]["reply_len"] == len("回复正文")
    assert seen[0]["private_chat"] is False


async def test_inner_state_private_scope(monkeypatch, tmp_path: Path) -> None:
    """私聊要落到 private 作用域，否则会去动群状态。"""
    cfg = _cfg(tmp_path)
    seen: list[dict] = []
    monkeypatch.setattr(
        facade_mod.MemoryFacade,
        "update_inner_state",
        lambda self, key, **kw: seen.append({"key": key, **kw}),
    )
    monkeypatch.setattr(response_mod, "ResponsePipeline", _pipe(True))
    monkeypatch.setattr(bridge, "_record_turn", lambda *a, **k: None)
    ctx = _AgentCtx(cfg=cfg, private=True, reason="private")
    await bridge._deliver_runtime_reply(ctx, _Outcome("回复"))
    assert [s["key"] for s in seen] == ["private"]


async def test_inner_state_not_updated_when_send_failed(monkeypatch, tmp_path: Path) -> None:
    """没送出去的回复不该扣她精力——和 _record_turn 同一个理由。"""
    cfg = _cfg(tmp_path)
    seen: list[dict] = []
    monkeypatch.setattr(
        facade_mod.MemoryFacade,
        "update_inner_state",
        lambda self, key, **kw: seen.append({"key": key, **kw}),
    )
    monkeypatch.setattr(response_mod, "ResponsePipeline", _pipe(False))
    monkeypatch.setattr(bridge, "_record_turn", lambda *a, **k: None)
    await bridge._deliver_runtime_reply(_AgentCtx(cfg=cfg), _Outcome("回复正文"))
    assert seen == []


async def test_inner_state_really_written_to_disk(monkeypatch, tmp_path: Path) -> None:
    """不 mock 写入：确认真实 store 会落盘，而不是只调了个空壳。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(response_mod, "ResponsePipeline", _pipe(True))
    monkeypatch.setattr(bridge, "_record_turn", lambda *a, **k: None)
    await bridge._deliver_runtime_reply(_AgentCtx(cfg=cfg), _Outcome("一段挺长的回复" * 4))
    files = list((tmp_path / "inner_state").glob("*.json"))
    assert files, "内心状态没有落盘"


# ================================================================
# 2. 群风格采样：挂在永不短路的观察者上
# ================================================================


class _Ev:
    """OneBot 事件替身：观察者只读这几个字段。"""

    group_id = 100
    user_id = "1"
    self_id = "999"
    message_id = "5"


def _arm_observer(monkeypatch, cfg, *, text: str, is_group: bool = True):
    """把观察者的外围依赖全部换成桩，只留被测的那段采样逻辑。"""
    seen: list[tuple] = []

    class _Spy:
        def observe(self, gid, body):
            seen.append((gid, body))

    monkeypatch.setattr(group_style_mod, "get_group_style_store", lambda d: _Spy())
    monkeypatch.setattr(handlers, "get_config", lambda: cfg)
    monkeypatch.setattr(handlers, "_ctx_bot_event", lambda ctx: (object(), _Ev()))
    monkeypatch.setattr(
        handlers, "_resolve_session",
        lambda e, c: ("group_100" if is_group else "private_1", is_group),
    )
    monkeypatch.setattr(handlers, "_plain_text", lambda e: text)
    monkeypatch.setattr(handlers, "_sender_name", lambda e: "某人")
    return seen


async def test_group_text_is_sampled(monkeypatch, tmp_path: Path) -> None:
    seen = _arm_observer(monkeypatch, _cfg(tmp_path), text="这波操作真的抽象")
    await handlers.observe_recent_chat(object())
    assert seen == [("100", "这波操作真的抽象")]


async def test_role_setting_is_not_sampled(monkeypatch, tmp_path: Path) -> None:
    """别人贴的角色设定不能被当成「本群说话风格」学进去。"""
    text = "角色设定：你是一只猫娘，说话要自称本喵，语气要软一点"
    _, hit = persona_filter.neutralize(text)
    assert hit, "测试文本没被判成角色设定，这条用例会失效"
    seen = _arm_observer(monkeypatch, _cfg(tmp_path), text=text)
    await handlers.observe_recent_chat(object())
    assert seen == []


async def test_private_chat_is_not_sampled(monkeypatch, tmp_path: Path) -> None:
    """群风格只对群生效，私聊没有「本群」可言。"""
    seen = _arm_observer(
        monkeypatch, _cfg(tmp_path), text="今天天气不错", is_group=False
    )
    await handlers.observe_recent_chat(object())
    assert seen == []


async def test_group_style_disabled_switch(monkeypatch, tmp_path: Path) -> None:
    """开关关掉时不许写——这是唯一能防止「配置说关、实际照写」的断言。"""
    cfg = _cfg(tmp_path, DL_SENPAI_GROUP_STYLE_ENABLE=False)
    seen = _arm_observer(monkeypatch, cfg, text="这波操作真的抽象")
    await handlers.observe_recent_chat(object())
    assert seen == []


# ================================================================
# 1.5 好感度：runtime 模式下曾经完全没人写
# ================================================================


def _fake_finalize(returns: str):
    """替掉 finalize_affection_gain：真实现要 bot 去改群头衔，测试只关心文本。"""

    async def _f(bot, *, scope, user_id, text, result, config,
                 always_show=True, group_id=None):
        assert always_show is False, "聊天加好感不该每次都弹脚注"
        return returns

    return _f


async def test_chat_affection_granted_and_footnote_prepended(
    monkeypatch, tmp_path: Path
) -> None:
    """被点名的一次对话必须加好感，且升级脚注要拼进**待发**正文。

    为什么断言的是 pipeline 收到的 text：脚注跟着这条回复一起发出去，
    发送之后再也改不了，所以只能在这里验。
    """
    from dl_senpai.affection import get_affection_store

    cfg = _cfg(tmp_path, DL_SENPAI_AFFECTION_GAIN_CHAT=7)
    monkeypatch.setattr(domain_mod, "affection_scope", lambda c, e: "private_1074839353")
    monkeypatch.setattr(
        domain_mod, "finalize_affection_gain", _fake_finalize("回复正文\n（好感 +7）")
    )
    sent: list[str] = []

    class _P:
        async def deliver(self, result, context, *, dry_run=False, reply_options=None):
            sent.append(result.text)
            return _Delivered(True, text=result.text)

    monkeypatch.setattr(response_mod, "ResponsePipeline", _P)
    monkeypatch.setattr(bridge, "_record_turn", lambda *a, **k: None)

    await bridge._deliver_runtime_reply(
        # user_id 必须和 mock 出来的 scope 对上：好感是按 user_id 记名的，
        # 用桩件默认的 1001 会记到别人名下，断言就只能读回 0。
        _AgentCtx(
            cfg=cfg,
            private=True,
            raw_event=object(),
            bot=object(),
            user_id="1074839353",
        ),
        _Outcome("回复正文"),
    )

    assert sent == ["回复正文\n（好感 +7）"], "脚注必须在发送前就拼进待发正文"
    store = get_affection_store(str(tmp_path / "affection"))
    assert store.get_user("private_1074839353", "1074839353").value == 7


async def test_chat_affection_skipped_when_not_mentioned(
    monkeypatch, tmp_path: Path
) -> None:
    """没被点名（只是群里闲聊）不加好感——与 legacy 的 is_mentioned 条件一致。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(domain_mod, "affection_scope", lambda c, e: "private_1074839353")
    monkeypatch.setattr(response_mod, "ResponsePipeline", _pipe(True))
    monkeypatch.setattr(bridge, "_record_turn", lambda *a, **k: None)

    await bridge._deliver_runtime_reply(
        _AgentCtx(cfg=cfg, private=True, reason="reply", raw_event=object(), bot=object()),
        _Outcome("回复"),
    )

    assert not list((tmp_path / "affection").glob("*.json"))


async def test_chat_affection_skipped_without_scope(
    monkeypatch, tmp_path: Path
) -> None:
    """不在签到群、又没开私聊时 scope 为 None，不该写任何好感。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(domain_mod, "affection_scope", lambda c, e: None)
    monkeypatch.setattr(response_mod, "ResponsePipeline", _pipe(True))
    monkeypatch.setattr(bridge, "_record_turn", lambda *a, **k: None)

    await bridge._deliver_runtime_reply(
        _AgentCtx(cfg=cfg, raw_event=object(), bot=object()),
        _Outcome("回复"),
    )

    assert not list((tmp_path / "affection").glob("*.json"))


async def test_affection_failure_does_not_suppress_reply(
    monkeypatch, tmp_path: Path
) -> None:
    """好感度写入炸了也必须照常发送——否则一次磁盘抖动会让用户收不到回复。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(domain_mod, "affection_scope", lambda c, e: "private_1074839353")
    recorded: list[tuple] = []

    def _boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(facade_mod.MemoryFacade, "record_interaction", _boom)
    monkeypatch.setattr(response_mod, "ResponsePipeline", _pipe(True, "回复正文"))
    monkeypatch.setattr(
        bridge, "_record_turn", lambda c, u, a: recorded.append((u, a))
    )

    await bridge._deliver_runtime_reply(
        _AgentCtx(cfg=cfg, private=True, raw_event=object(), bot=object()),
        _Outcome("回复正文"),
    )

    assert recorded == [("用户问题", "回复正文")], "好感度失败不该影响回复落盘"