"""人设卡：解析 / 绑定 / 解析优先级 / 顶替 SYSTEM_PROMPT / 命令权限。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dl_senpai.persona import SYSTEM_PROMPT, build_system_prompt
from dl_senpai.personas import (
    PersonaCard,
    PersonaCardStore,
    candidate_keys,
    resolve_persona_body,
)


def _write(directory, name: str, text: str):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _card_text(card_id: str, body: str, *, groups: str = "", name: str = "") -> str:
    lines = ["---", f"id: {card_id}"]
    if name:
        lines.append(f"name: {name}")
    if groups:
        lines.append(f"groups: {groups}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


class _Cfg:
    """最小配置桩：只放人设卡相关的三个字段。"""

    persona_card_enable = True
    persona_card_default = ""

    def __init__(self, directory):
        self.persona_card_dir = str(directory)


def test_parse_front_matter_and_body(tmp_path):
    path = _write(
        tmp_path,
        "a.md",
        "---\nid: xuejie2\nname: 小满\ngroups: 105, group:106\ndescription: 更嘴硬\n---\n正文第一行\n正文第二行\n",
    )
    from dl_senpai.personas import parse_card_file

    card = parse_card_file(path)
    assert card.id == "xuejie2"
    assert card.name == "小满"
    assert card.groups == ("105", "group:106")
    assert card.description == "更嘴硬"
    assert card.body == "正文第一行\n正文第二行"


def test_card_without_front_matter_uses_stem(tmp_path):
    from dl_senpai.personas import parse_card_file

    path = _write(tmp_path, "老王.md", "就这几句话，我就是老王。")
    card = parse_card_file(path)
    assert card.id == "老王"
    assert card.body == "就这几句话，我就是老王。"


def test_unclosed_front_matter_falls_back_to_body(tmp_path):
    from dl_senpai.personas import parse_card_file

    path = _write(tmp_path, "broken.md", "---\nid: whatever\n正文其实从这里开始")
    card = parse_card_file(path)
    # 没闭合就整篇当正文：宁可多几行 ---，也不能把这张卡吞空
    assert card.body.startswith("---")
    assert card.id == "broken"


def test_empty_card_is_dropped(tmp_path):
    _write(tmp_path, "empty.md", "---\nid: empty\n---\n")
    store = PersonaCardStore(tmp_path)
    assert store.get("empty") is None
    assert store.cards() == ()


def test_readme_and_underscore_files_are_not_cards(tmp_path):
    """目录说明不能被当成卡：绑上去她就开始照着使用手册说话了。"""
    _write(tmp_path, "README.md", "# 用法\n说明文档，不是人设")
    _write(tmp_path, "_临时.md", "草稿")
    _write(tmp_path, "a.md", _card_text("a", "正文"))
    assert [c.id for c in PersonaCardStore(tmp_path).cards()] == ["a"]


def test_get_is_case_insensitive_and_accepts_name(tmp_path):
    _write(tmp_path, "a.md", _card_text("Xuejie", "正文", name="学姐"))
    store = PersonaCardStore(tmp_path)
    assert store.get("xuejie").id == "Xuejie"
    assert store.get("学姐").id == "Xuejie"
    assert store.get("nope") is None


def test_hot_reload_picks_up_new_file(tmp_path):
    store = PersonaCardStore(tmp_path)
    assert store.cards() == ()
    _write(tmp_path, "later.md", _card_text("later", "新写的卡"))
    # 没有手动 reload：resolve/cards 每次都自己比 mtime
    assert [c.id for c in store.cards()] == ["later"]


def test_bind_and_unbind_persist(tmp_path):
    _write(tmp_path, "a.md", _card_text("a", "正文"))
    store = PersonaCardStore(tmp_path)
    assert store.bind("group:1", "a").id == "a"
    assert store.resolve("group:1").id == "a"
    # 落盘：换一个实例（等于重启 bot）还能读到
    assert PersonaCardStore(tmp_path).resolve("group:1").id == "a"
    assert store.unbind("group:1") is True
    assert PersonaCardStore(tmp_path).resolve("group:1") is None
    assert store.unbind("group:1") is False


def test_bind_unknown_card_returns_none(tmp_path):
    _write(tmp_path, "a.md", _card_text("a", "正文"))
    assert PersonaCardStore(tmp_path).bind("group:1", "nope") is None


def test_resolve_prefers_explicit_binding(tmp_path):
    _write(tmp_path, "a.md", _card_text("a", "A 的正文", groups="105"))
    _write(tmp_path, "b.md", _card_text("b", "B 的正文"))
    store = PersonaCardStore(tmp_path)
    store.bind("group:105", "b")
    # 卡 a 声明了 groups: 105，但显式绑定（b）优先：管理员说过的话压过文件声明
    assert store.resolve("group:105").id == "b"


def test_resolve_accepts_bare_group_number_binding(tmp_path):
    _write(tmp_path, "a.md", _card_text("a", "正文"))
    store = PersonaCardStore(tmp_path)
    store.bind("105", "a")
    assert store.resolve("group:105").id == "a"


def test_resolve_wildcard_binding(tmp_path):
    _write(tmp_path, "a.md", _card_text("a", "正文"))
    store = PersonaCardStore(tmp_path)
    store.bind("*", "a")
    assert store.resolve("group:999").id == "a"


def test_resolve_uses_declared_groups_then_default(tmp_path):
    _write(tmp_path, "a.md", _card_text("a", "声明用", groups="105"))
    _write(tmp_path, "d.md", _card_text("d", "默认用"))
    store = PersonaCardStore(tmp_path, default_id="d")
    assert store.resolve("group:105").id == "a"
    assert store.resolve("group:999").id == "d"


def test_resolve_without_any_card_is_none(tmp_path):
    _write(tmp_path, "a.md", _card_text("a", "正文"))
    store = PersonaCardStore(tmp_path)
    # 这张卡既没声明 groups、也没绑定、也没默认 → 谁都不该自动用它
    assert store.resolve("group:105") is None


def test_candidate_keys():
    assert candidate_keys("group:105") == ("group:105", "105")
    assert candidate_keys("105") == ("105",)
    assert candidate_keys("") == ()


def test_resolve_persona_body(tmp_path):
    _write(tmp_path, "a.md", _card_text("a", "这是卡里的人设"))
    cfg = _Cfg(tmp_path)
    assert resolve_persona_body("group:1", cfg) == ("", "")
    from dl_senpai.personas import get_persona_store

    get_persona_store(cfg).bind("group:1", "a")
    assert resolve_persona_body("group:1", cfg) == ("这是卡里的人设", "a")
    # 别的会话不沾光
    assert resolve_persona_body("group:2", cfg) == ("", "")


def test_resolve_persona_body_respects_disable_switch(tmp_path):
    _write(tmp_path, "a.md", _card_text("a", "正文"))
    cfg = _Cfg(tmp_path)
    from dl_senpai.personas import get_persona_store

    get_persona_store(cfg).bind("group:1", "a")
    cfg.persona_card_enable = False
    # 关掉功能 = 完全等于改造前，哪怕绑定还在
    assert resolve_persona_body("group:1", cfg) == ("", "")


def test_build_system_prompt_replaces_only_the_persona():
    prompt = build_system_prompt(persona_body="我是卡里的她。")
    assert prompt.startswith("我是卡里的她。")
    # 仿真层（格式说明/场景补充）照旧在后面
    assert len(prompt) > len("我是卡里的她。") + 100
    assert SYSTEM_PROMPT.strip()[:20] not in prompt


def test_build_system_prompt_unchanged_without_card():
    # 没有卡时必须与改造前逐字一致（这条守卫是这个功能敢开着的理由）
    assert build_system_prompt(persona_body="") == build_system_prompt()
    assert build_system_prompt().startswith(SYSTEM_PROMPT.strip()[:40])


def test_prompt_builder_passes_card_body():
    """走完整的 PromptBuilder 链路，卡正文也要能顶替人设。"""
    from dl_senpai.agent.prompt.builder import PromptBuilder, PromptRequest

    built = PromptBuilder().build(PromptRequest(persona_body="我是卡里的她。"))
    assert built.text.startswith("我是卡里的她。")
    # 空串时不传这个 kwarg：渲染器调用形状与改造前一致
    plain = PromptBuilder().build(PromptRequest())
    assert plain.text.startswith(SYSTEM_PROMPT.strip()[:40])


def test_body_hash_ignores_line_endings_and_trailing_spaces():
    from dl_senpai.personas import body_hash

    # Windows 上存一次就该报「不一致」的指纹没人会看，所以先归一化
    assert body_hash("一\r\n二  \r\n") == body_hash("一\n二")


def test_source_sync_detects_drift(tmp_path):
    from dl_senpai.persona import SYSTEM_PROMPT
    from dl_senpai.personas import body_hash, parse_card_file

    good = _write(
        tmp_path,
        "good.md",
        "---\nid: good\nsource_ref: persona.py\nsource_hash: "
        + body_hash(SYSTEM_PROMPT)
        + "\n---\n"
        + SYSTEM_PROMPT.strip(),
    )
    stale = _write(
        tmp_path,
        "stale.md",
        "---\nid: stale\nsource_ref: persona.py\nsource_hash: deadbeef\n---\n导出的副本",
    )
    plain = _write(tmp_path, "plain.md", "---\nid: plain\n---\n自己写的")
    assert parse_card_file(good).source_synced is True
    assert parse_card_file(stale).source_synced is False
    # 没声明来源的卡不谈同步：猜出来的「同步」比不检查更危险
    assert parse_card_file(plain).source_synced is None


def test_exported_card_can_be_default(tmp_path):
    """把「导出的当前人设」设成默认卡后，解析要走卡而不是回落。"""
    from dl_senpai.persona import SYSTEM_PROMPT
    from dl_senpai.personas import get_persona_store

    _write(tmp_path, "学姐.md", _card_text("学姐", SYSTEM_PROMPT.strip()))
    cfg = _Cfg(tmp_path)
    cfg.persona_card_default = "学姐"
    body, card_id = resolve_persona_body("group:105", cfg)
    assert card_id == "学姐"
    assert body == SYSTEM_PROMPT.strip()
    # 单例按 (目录, 默认卡) 缓存：换了默认卡必须重建，不能继续用旧的
    assert get_persona_store(cfg).default_id == "学姐"


def _cmd_ctx(tmp_path, *, user_id: str, admin: str = "12345"):
    cfg = _Cfg(tmp_path)
    cfg.agent_admin_qq = admin
    return SimpleNamespace(
        config=cfg,
        session_id="group:1",
        user_id=user_id,
        bot=object(),
        raw_event=object(),
    )


@pytest.mark.asyncio
async def test_command_denies_non_admin(tmp_path, monkeypatch):
    """非管理员不能切人设，而且**拒绝时不回话**。

    不回话是刻意的：回一句「只有管理员能用」等于向全群复述谁的权限最高。
    改为抛 CommandPermissionDenied，由 Router 静默跳过并让消息继续走 AI。
    """
    _write(tmp_path, "a.md", _card_text("a", "正文"))
    from dl_senpai import personas
    from dl_senpai.core.commands.models import CommandPermissionDenied

    replies: list[str] = []

    async def fake_reply(bot, event, text, *args, **kwargs):
        replies.append(text)

    monkeypatch.setattr("dl_senpai.handlers._reply", fake_reply, raising=False)
    ctx = _cmd_ctx(tmp_path, user_id="999")
    match = SimpleNamespace(text="/人设 a")
    with pytest.raises(CommandPermissionDenied):
        await personas.handle_persona_command(ctx, match)
    assert replies == []
    # 被拒时不能真的改掉绑定
    assert personas.get_persona_store(ctx.config).resolve("group:1") is None


async def test_command_switches_and_resets(tmp_path, monkeypatch):
    _write(tmp_path, "a.md", _card_text("a", "正文"))
    from dl_senpai import personas

    replies: list[str] = []

    async def fake_reply(bot, event, text, *args, **kwargs):
        replies.append(text)

    monkeypatch.setattr("dl_senpai.handlers._reply", fake_reply, raising=False)
    ctx = _cmd_ctx(tmp_path, user_id="12345")
    store = personas.get_persona_store(ctx.config)

    await personas.handle_persona_command(ctx, SimpleNamespace(text="/人设 a"))
    assert store.resolve("group:1").id == "a"

    await personas.handle_persona_command(ctx, SimpleNamespace(text="/人设 列表"))
    assert any("正在用" in r or "没绑卡" in r for r in replies)

    await personas.handle_persona_command(ctx, SimpleNamespace(text="/人设 默认"))
    assert store.resolve("group:1") is None
    assert len(replies) == 3


def test_plain_chat_is_not_a_command():
    from dl_senpai.core.commands.builtin import build_builtin_commands

    persona_cmd = next(c for c in build_builtin_commands() if c.name == "persona")
    pattern = persona_cmd.patterns[0][1]
    # 「人设崩了」这类正常聊天不能被当成命令吞掉，否则用户永远收不到回复
    assert pattern.match("人设崩了") is None
    assert pattern.match("/人设") is not None
    assert pattern.match("人设卡 a").group("arg") == "a"
