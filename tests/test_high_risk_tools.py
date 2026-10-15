"""高风险工具（文件 / 命令 / 代码）的测试。

这些测试守三件事，按重要性排序：

1. **权限**：非管理员既看不到、也调不到。这是唯一真正兜住风险的东西——
   沙箱是软边界，deny list 是兜底，只有"谁能用"是硬的。
2. **沙箱**：路径越界一律拒绝，包括 `..`、绝对路径、软链。
3. **契约**：新增 7 个工具**没有**动到既有 8 个生产工具的数量与 executor。
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from dl_senpai.agent.permissions import PermissionService
from dl_senpai.agent.tools import exec_tools as X
from dl_senpai.agent.tools import file_tools as F
from dl_senpai.agent.tools.builtin import build_production_tools, register_production_tools
from dl_senpai.agent.tools.high_risk import (
    HIGH_RISK_OWNER,
    build_high_risk_tools,
    register_high_risk_tools,
)
from dl_senpai.agent.tools.models import Continuation, ToolCall
from dl_senpai.agent.tools.registry import ToolRegistry
from dl_senpai.agent.tools.sandbox import (
    SandboxViolation,
    looks_binary,
    resolve_in_sandbox,
    resolve_sandbox_root,
    truncate_output,
)

ADMIN = "1074839353"


def _cfg(root, **over):
    """构造一份最小可用配置。用 SimpleNamespace 是为了让测试只关心它读的字段。"""
    base = dict(
        agent_admin_qq=ADMIN,
        agent_tools_enable=True,
        agent_file_enable=True,
        agent_shell_enable=True,
        agent_code_enable=True,
        agent_sandbox_dir=str(root),
        agent_shell_timeout=20,
        agent_code_timeout=20,
        agent_output_max_chars=6000,
        agent_file_read_max_bytes=200000,
        agent_glob_max=60,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _ctx(root, *, actor=ADMIN, **over):
    """构造一个**真实**的 AgentContext。

    为什么不手搓 SimpleNamespace：AgentContext 的字段会随运行时演进
    （interrupt / private_chat / history / extra ...），手搓的对象每漏一个
    就在运行到那一行时炸一次 AttributeError，而且报错点离真正原因很远。
    用真对象，字段齐全，也不会再随运行时改动而悄悄失效。
    """
    from dl_senpai.agent.context import AgentContext

    message_context = types.SimpleNamespace(
        actor=actor, user_id=actor, group_id="2002", plain_text="", is_group=True
    )
    return AgentContext(
        config=_cfg(root, **over),
        message_context=message_context,
        session_id="group:2002",
        history=[],
        extra={},
    )


def _call(name, **args):
    return ToolCall(tool_name=name, arguments=args, id=name + "-1")


# ------------------------------------------------------------------ 沙箱


def test_relative_path_lands_in_sandbox(tmp_path) -> None:
    root = resolve_sandbox_root(_cfg(tmp_path))
    assert resolve_in_sandbox(root, "a/b.txt") == (tmp_path / "a" / "b.txt").resolve()


@pytest.mark.parametrize(
    "given",
    [
        "../escape.txt",
        "..\\escape.txt",
        "a/../../escape.txt",
        str(Path.cwd() / "outside.txt"),
        "C:\\Windows\\win.ini",
    ],
)
def test_escaping_paths_are_rejected(tmp_path, given) -> None:
    """`..` 与绝对路径都必须被拦。只比对字符串前缀是不够的。"""
    root = resolve_sandbox_root(_cfg(tmp_path))
    with pytest.raises(SandboxViolation):
        resolve_in_sandbox(root, given)


def test_symlink_escape_is_rejected(tmp_path) -> None:
    """软链指向外部时，resolve 之后就不在沙箱里了。

    这是「只做字符串前缀检查」最典型的翻车场景。
    """
    root = resolve_sandbox_root(_cfg(tmp_path))
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(SandboxViolation):
        resolve_in_sandbox(root, "link.txt")


def test_null_byte_path_is_rejected(tmp_path) -> None:
    root = resolve_sandbox_root(_cfg(tmp_path))
    with pytest.raises(SandboxViolation):
        resolve_in_sandbox(root, "a\x00b")


def test_must_exist_flag(tmp_path) -> None:
    root = resolve_sandbox_root(_cfg(tmp_path))
    with pytest.raises(SandboxViolation):
        resolve_in_sandbox(root, "nope.txt", must_exist=True)


def test_sibling_dir_with_same_prefix_is_rejected(tmp_path) -> None:
    """`ws-evil` 不是 `ws` 的子目录，尽管字符串前缀相同。"""
    root = tmp_path / "ws"
    root.mkdir()
    sibling = tmp_path / "ws-evil"
    sibling.mkdir()
    with pytest.raises(SandboxViolation):
        resolve_in_sandbox(root, str(sibling / "x.txt"))


# ------------------------------------------------------------ 输出裁剪


def test_truncate_keeps_head_and_tail() -> None:
    """长输出保留头尾：报错栈与测试结论都在结尾，只留头会全丢。"""
    text = "\n".join(f"line{i}" for i in range(300))
    out = truncate_output(text, max_chars=100000)
    assert "line0" in out
    assert "line299" in out
    assert "line150" not in out
    assert "省略中间" in out


def test_truncate_short_text_untouched() -> None:
    assert truncate_output("a\nb") == "a\nb"
    assert truncate_output("") == ""


def test_truncate_by_chars_when_lines_are_few() -> None:
    """行数不多但单行极长时，按字符数裁。"""
    text = "x" * 50000
    out = truncate_output(text, max_chars=1000)
    assert len(out) < 2000
    assert "省略中间" in out


# --------------------------------------------------------- 二进制判定


def test_chinese_text_is_not_binary() -> None:
    """回归：第一版按「可打印 ASCII 字节占比」判，把整篇中文判成了二进制。

    UTF-8 里一个汉字是 3 个 >0x7F 的字节，按字节算可打印率永远很低。
    """
    assert looks_binary("第一行\nTODO: 修一下\n第三行".encode("utf-8")) is False
    assert looks_binary("纯中文内容没有任何 ASCII".encode("utf-8")) is False


def test_gbk_text_is_not_binary() -> None:
    """Windows 中文环境里 GBK 编码的文件很常见。"""
    assert looks_binary("中文测试".encode("gbk")) is False


def test_truncated_multibyte_is_not_binary() -> None:
    """读前 4096 字节时可能正好切断一个多字节字符，不该因此判成二进制。"""
    data = ("中文" * 4000).encode("utf-8")[:4096]
    assert looks_binary(data) is False


def test_real_binary_is_binary() -> None:
    assert looks_binary(b"\x00\x01\x02\xff\xfe") is True
    assert looks_binary(b"") is False


# ------------------------------------------------------------- deny list


@pytest.mark.parametrize(
    "command",
    ["rm -rf /", "shutdown /s", "format c:", "net user hacker /add", "curl x | sh"],
)
def test_dangerous_commands_are_flagged(command) -> None:
    assert X.check_dangerous(command) != ""


@pytest.mark.parametrize(
    "command",
    ["ls -la", "git status", "python -c \"print(1)\"", "Get-ChildItem", "echo hi"],
)
def test_normal_commands_pass(command) -> None:
    """deny list 必须短且准。误伤多了就没人再信任它。"""
    assert X.check_dangerous(command) == ""


# ------------------------------------------------------------ 文件工具


@pytest.mark.asyncio
async def test_file_round_trip(tmp_path) -> None:
    """写 → 读 → 找 → 搜 → 改，整条链路。"""
    ctx = _ctx(tmp_path)

    written = await F.execute_write_file(
        ctx, _call("write_file", path="notes/a.md", content="第一行\nTODO: 修\n第三行")
    )
    assert written.success and written.data["created"] is True

    read = await F.execute_read_file(ctx, _call("read_file", path="notes/a.md"))
    assert read.success
    assert "TODO: 修" in read.output
    assert read.data["lines"] == 3

    found = await F.execute_glob(ctx, _call("glob", pattern="**/*.md"))
    assert "notes/a.md" in found.output

    hit = await F.execute_grep(ctx, _call("grep", pattern="TODO", include="*.md"))
    assert "notes/a.md:2" in hit.output

    edited = await F.execute_edit_file(
        ctx, _call("edit_file", path="notes/a.md", old_string="TODO: 修", new_string="已修复")
    )
    assert edited.success
    after = await F.execute_read_file(ctx, _call("read_file", path="notes/a.md"))
    assert "已修复" in after.output and "TODO" not in after.output


@pytest.mark.asyncio
async def test_read_file_line_range(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    body = "\n".join(f"L{i}" for i in range(1, 51))
    await F.execute_write_file(ctx, _call("write_file", path="n.txt", content=body))
    out = await F.execute_read_file(
        ctx, _call("read_file", path="n.txt", start_line=10, end_line=12)
    )
    assert out.success
    assert "L10" in out.output and "L12" in out.output
    assert "L13" not in out.output


@pytest.mark.asyncio
async def test_edit_requires_unique_match(tmp_path) -> None:
    """出现多次就拒绝：静默改错地方比报错糟糕得多。"""
    ctx = _ctx(tmp_path)
    await F.execute_write_file(
        ctx, _call("write_file", path="d.txt", content="same\nmiddle\nsame")
    )
    dup = await F.execute_edit_file(
        ctx, _call("edit_file", path="d.txt", old_string="same", new_string="x")
    )
    assert dup.success is False and dup.error_type == "old_string_not_unique"

    missing = await F.execute_edit_file(
        ctx, _call("edit_file", path="d.txt", old_string="not-there", new_string="x")
    )
    assert missing.success is False and missing.error_type == "old_string_not_found"


@pytest.mark.asyncio
async def test_file_tools_enforce_sandbox(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    for result in (
        await F.execute_read_file(ctx, _call("read_file", path="../x.txt")),
        await F.execute_write_file(ctx, _call("write_file", path="../x.txt", content="x")),
        await F.execute_glob(ctx, _call("glob", pattern="../*.py")),
    ):
        assert result.success is False
        assert result.error_type == "sandbox_violation"


@pytest.mark.asyncio
async def test_read_file_rejects_binary(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\xff" * 100)
    out = await F.execute_read_file(ctx, _call("read_file", path="blob.bin"))
    assert out.success is False and out.error_type == "binary_file"


# ------------------------------------------------------------ 执行工具


@pytest.mark.asyncio
async def test_shell_runs_command(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    out = await X.execute_shell(ctx, _call("shell", command="echo hello-senpai"))
    assert out.success
    assert "hello-senpai" in out.output
    assert out.data["exit_code"] == 0


@pytest.mark.asyncio
async def test_shell_runs_in_sandbox_dir(tmp_path) -> None:
    """cwd 必须是沙箱根，不能是 bot 进程的目录。"""
    ctx = _ctx(tmp_path)
    (tmp_path / "marker-here.txt").write_text("x", encoding="utf-8")
    out = await X.execute_shell(ctx, _call("shell", command="Get-ChildItem -Name"))
    assert out.success
    assert "marker-here.txt" in out.output


@pytest.mark.asyncio
async def test_shell_blocks_dangerous(tmp_path) -> None:
    out = await X.execute_shell(_ctx(tmp_path), _call("shell", command="rm -rf /"))
    assert out.success is False and out.error_type == "blocked_by_policy"


@pytest.mark.asyncio
async def test_code_runs_python(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    out = await X.execute_code(ctx, _call("code", code="print(6 * 7)", language="python"))
    assert out.success
    assert "42" in out.output
    # 脚本落在 .code/ 里，不污染工作区搜索结果
    assert out.data["script"].startswith(".code/")


@pytest.mark.asyncio
async def test_code_nonzero_exit_is_not_tool_failure(tmp_path) -> None:
    """脚本自己 raise 不等于工具坏了——输出要回给模型让它判断。"""
    ctx = _ctx(tmp_path)
    out = await X.execute_code(
        ctx, _call("code", code="raise ValueError(\"boom\")", language="python")
    )
    assert out.success is True
    assert out.data["exit_code"] != 0
    assert "boom" in out.output


@pytest.mark.asyncio
async def test_code_rejects_other_languages(tmp_path) -> None:
    out = await X.execute_code(
        _ctx(tmp_path), _call("code", code="console.log(1)", language="javascript")
    )
    assert out.success is False and out.error_type == "unsupported_language"


@pytest.mark.asyncio
async def test_code_blocks_dangerous(tmp_path) -> None:
    out = await X.execute_code(
        _ctx(tmp_path), _call("code", code="import os  # shutdown /s", language="python")
    )
    assert out.success is False and out.error_type == "blocked_by_policy"


# ------------------------------------------------------------- 权限门控


def test_admin_can_see_and_use_high_risk_tools(tmp_path) -> None:
    perms = PermissionService()
    ctx = _ctx(tmp_path, actor=ADMIN)
    for tool in build_high_risk_tools():
        assert perms.visible(tool, ctx) is True, tool.name
        decision = perms.authorize(
            tool=tool, call=_call(tool.name), context=ctx
        )
        assert decision.allowed is True, tool.name


def test_non_admin_cannot_see_or_use_high_risk_tools(tmp_path) -> None:
    """这是整套设计里唯一**硬**的那道门，必须两侧都锁死。"""
    perms = PermissionService()
    ctx = _ctx(tmp_path, actor="99999")
    for tool in build_high_risk_tools():
        assert perms.visible(tool, ctx) is False, tool.name
        decision = perms.authorize(tool=tool, call=_call(tool.name), context=ctx)
        assert decision.allowed is False, tool.name
        assert decision.policy == "admin_only"


def test_empty_admin_config_closes_everything(tmp_path) -> None:
    """回归：漏配管理员时必须**默认关闭**，不能变成全开的后门。"""
    perms = PermissionService()
    ctx = _ctx(tmp_path, actor=ADMIN, agent_admin_qq="")
    for tool in build_high_risk_tools():
        assert perms.visible(tool, ctx) is False, tool.name
        assert perms.authorize(
            tool=tool, call=_call(tool.name), context=ctx
        ).allowed is False


def test_multiple_admins_supported(tmp_path) -> None:
    perms = PermissionService()
    for actor in ("1", "2", "3"):
        ctx = _ctx(tmp_path, actor=actor, agent_admin_qq="1, 2;3")
        assert perms.visible(build_high_risk_tools()[0], ctx) is True, actor


def test_private_chat_does_not_bypass_admin_check(tmp_path) -> None:
    """被拉进陌生人的私聊不是特权——场景不改变信任级别，身份才改变。"""
    perms = PermissionService()
    ctx = _ctx(tmp_path, actor="88888")
    ctx.is_private = True
    assert perms.visible(build_high_risk_tools()[0], ctx) is False


def test_feature_switch_hides_tools(tmp_path) -> None:
    """总开关与分类开关都要生效（可见性层）。"""
    perms = PermissionService()
    tools = {t.name: t for t in build_high_risk_tools()}

    off_all = _ctx(tmp_path, agent_tools_enable=False)
    assert perms.visible(tools["shell"], off_all) is False

    off_shell = _ctx(tmp_path, agent_shell_enable=False)
    assert perms.visible(tools["shell"], off_shell) is False
    assert perms.visible(tools["code"], off_shell) is True

    off_file = _ctx(tmp_path, agent_file_enable=False)
    assert perms.visible(tools["read_file"], off_file) is False
    assert perms.visible(tools["write_file"], off_file) is False


def test_low_risk_tools_are_unaffected_by_admin_gate(tmp_path) -> None:
    """管理员门控只该管高风险工具，不能顺手把 search / draw 也锁掉。

    断言的是「管理员与非管理员看到的一样」，而不是「一定可见」——
    后者会去依赖各工具自己的功能开关（这里是假配置，没那些字段），
    把一条权限测试变成一条配置测试。
    """
    perms = PermissionService()
    registry = ToolRegistry()
    register_production_tools(registry)
    admin_ctx = _ctx(tmp_path, actor=ADMIN)
    user_ctx = _ctx(tmp_path, actor="99999")
    for name in ("search", "draw", "voice"):
        tool = registry.get(name)
        assert perms.visible(tool, admin_ctx) == perms.visible(tool, user_ctx), name


# ------------------------------------------------------------- 工具契约


def test_high_risk_tools_are_seven_and_named() -> None:
    names = [t.name for t in build_high_risk_tools()]
    assert names == [
        "read_file", "glob", "grep", "write_file", "edit_file", "shell", "code",
    ]


def test_production_tool_contract_untouched() -> None:
    """既有生产工具的数量与身份都不能因为新增工具而变化。

    2026-10-14：加了 group_read（读群公告/群文件/群相册），总数 8 → 9。
    这里断言的是**快照**：以后再加工具要显式改这个数字，
    避免"顺手多注册一个"这种隐式扩张。
    """
    assert len(build_production_tools()) == 9
    registry = ToolRegistry()
    register_production_tools(registry)
    before = set(registry.names())
    register_high_risk_tools(registry)
    assert set(registry.names()) == before | {t.name for t in build_high_risk_tools()}
    assert registry.get("search") is not None


def test_high_risk_tools_share_owner_for_bulk_removal() -> None:
    """整体摘除要一条命令搞定（owner 一致）。"""
    registry = ToolRegistry()
    register_high_risk_tools(registry)
    assert registry.unregister_owner(HIGH_RISK_OWNER) == 7
    assert registry.names() == []


def test_exec_tools_are_return_to_model_and_side_effecting() -> None:
    tools = {t.name: t for t in build_high_risk_tools()}
    for name in ("shell", "code"):
        tool = tools[name]
        assert tool.continuation is Continuation.RETURN_TO_MODEL, name
        assert tool.side_effect is True, name
        # 进程超时由 _run_process 自己管，执行器那层不能再包一层，
        # 否则模型只会收到干巴巴的「工具超时」。
        assert tool.timeout == 0.0, name


def test_read_only_file_tools_are_not_side_effecting() -> None:
    """读类工具重跑无害；若标成有副作用，会白白禁掉本轮的 fallback。"""
    tools = {t.name: t for t in build_high_risk_tools()}
    for name in ("read_file", "glob", "grep"):
        assert tools[name].side_effect is False, name
    for name in ("write_file", "edit_file"):
        assert tools[name].side_effect is True, name


def test_parser_priority_is_stable_and_unique() -> None:
    """解析顺序必须确定：多个 marker 同现时不能靠字典序碰运气。

    要点是**注册表按 priority 排序后**仍然有序、且互不相同；
    构造函数返回的列表顺序无所谓（文件类与执行类是两批拼起来的）。
    """
    priorities = [t.priority for t in build_high_risk_tools()]
    assert len(set(priorities)) == len(priorities)

    registry = ToolRegistry()
    register_high_risk_tools(registry)
    ordered = [t.priority for t in registry.parseable_tools()]
    assert ordered == sorted(ordered)


def test_markers_do_not_collide_with_existing_tools() -> None:
    existing = {t.marker for t in build_production_tools() if t.marker}
    ours = {t.marker for t in build_high_risk_tools() if t.marker}
    assert existing & ours == set()


def test_shell_marker_is_parsed_without_leaking_into_text() -> None:
    """marker 被解析后必须从正文里消失，否则模型会看到自己写的指令。"""
    tool = {t.name: t for t in build_high_risk_tools()}["shell"]
    cleaned, calls = tool.parser("前面 <<<SHELL echo hi>>> 后面")
    assert len(calls) == 1
    assert calls[0].arguments["command"] == "echo hi"
    assert "<<<" not in cleaned and "echo hi" not in cleaned


def test_write_file_marker_keeps_multiline_content() -> None:
    """块式 marker 的内容里可以有空行与 >>>，靠显式 <<<END>>> 收尾。"""
    tool = {t.name: t for t in build_high_risk_tools()}["write_file"]
    raw = "<<<WRITE_FILE a.txt>>>\nline1\n\nline3 >>>\n<<<END>>>"
    cleaned, calls = tool.parser(raw)
    assert len(calls) == 1
    assert calls[0].arguments["content"] == "line1\n\nline3 >>>"
    assert cleaned.strip() == ""


# ------------------------------------------------ 可见性过滤必须走权限层


def test_registry_visible_tools_enforces_admin_gate(tmp_path) -> None:
    """回归：visible_tools 曾经只问 ToolDefinition.is_visible。

    那条路径上 PermissionService.visible 是**死代码**——工具对非管理员
    照样出现在提示词里，模型会一次次去调、又一次次被执行期拒绝。
    门必须同时关在「看得见」与「调得动」两侧。
    """
    registry = ToolRegistry()
    register_production_tools(registry)
    register_high_risk_tools(registry)
    registry.bind_permissions(PermissionService())

    admin_names = {t.name for t in registry.visible_tools(_ctx(tmp_path, actor=ADMIN))}
    user_names = {t.name for t in registry.visible_tools(_ctx(tmp_path, actor="99999"))}

    assert {"shell", "code", "read_file", "write_file"} <= admin_names
    assert user_names & {t.name for t in build_high_risk_tools()} == set()
    # 非管理员拿到的必须是管理员的子集：门控只该做减法，
    # 不该顺带把别的工具也误伤掉。
    # （这里不断言"普通工具一定可见"——那是各工具自己功能开关的事，
    #   本用例的假配置没有那些字段。）
    assert user_names <= admin_names


def test_registry_can_disable_permission_layer(tmp_path) -> None:
    """显式关掉权限层时退回纯 is_visible，供内部/测试构造使用。"""
    registry = ToolRegistry()
    register_high_risk_tools(registry)
    registry.disable_permissions()
    names = {t.name for t in registry.visible_tools(_ctx(tmp_path, actor="99999"))}
    assert "shell" in names


# ------------------------------------------- 注册即可见必须真的进提示词


def test_registered_tool_prompt_text_reaches_system_prompt(tmp_path) -> None:
    """回归：build() 曾经只把工具翻译成 persona 的 enable 标志。

    后加的工具（knowledge / memory / 高风险）prompt_text 写好了，
    却从来没进过 system prompt——模型不知道 marker 存在，
    自然一次都不会调用。这条测试锁住「注册即可见」这件事本身。
    """
    from dl_senpai.agent.prompt.builder import PromptBuilder, PromptRequest

    registry = ToolRegistry()
    register_high_risk_tools(registry)
    registry.bind_permissions(PermissionService())
    visible = registry.visible_tools(_ctx(tmp_path, actor=ADMIN))

    built = PromptBuilder().build(PromptRequest(visible_tools=visible, private_chat=False))
    for token in ("<<<SHELL", "<<<CODE", "<<<READ_FILE", "<<<WRITE_FILE", "<<<GLOB"):
        assert token in built.text, token
    assert built.sections["extra_tool_block"] is True


def test_prompt_block_is_not_duplicated_for_persona_tools(tmp_path) -> None:
    """既有的 8 个工具说明由 persona 渲染，不能再被追加一遍。

    判重靠「原文是否已出现」而不是硬编码工具名，这条测试保护那个机制：
    构造一个 prompt_text 已经在 text 里的工具，它不该被重复追加。
    """
    from dl_senpai.agent.prompt.builder import PromptBuilder
    from dl_senpai.agent.tools.registry import ToolDefinition

    builder = PromptBuilder()
    tool = ToolDefinition(name="tl", prompt_text="已经存在的说明")
    assert builder._new_tool_block([tool], "头部\n已经存在的说明\n尾部") == ""
    assert builder._new_tool_block([tool], "其它内容") == "已经存在的说明"


def test_prompt_block_dedupes_shared_text(tmp_path) -> None:
    """5 个文件工具共用同一段说明，只能出现一次。"""
    from dl_senpai.agent.prompt.builder import PromptBuilder
    from dl_senpai.agent.tools.registry import ToolDefinition

    builder = PromptBuilder()
    tools = [
        ToolDefinition(name=f"f{i}", prompt_text="共用说明") for i in range(5)
    ]
    assert builder._new_tool_block(tools, "") == "共用说明"


def test_non_admin_prompt_has_no_high_risk_markers(tmp_path) -> None:
    """非管理员的提示词里不该出现任何高风险 marker。"""
    from dl_senpai.agent.prompt.builder import PromptBuilder, PromptRequest

    registry = ToolRegistry()
    register_high_risk_tools(registry)
    registry.bind_permissions(PermissionService())
    visible = registry.visible_tools(_ctx(tmp_path, actor="99999"))

    built = PromptBuilder().build(PromptRequest(visible_tools=visible, private_chat=False))
    for token in ("<<<SHELL", "<<<CODE", "<<<READ_FILE", "<<<GLOB", "<<<EDIT_FILE"):
        assert token not in built.text, token


def test_interrupt_turn_gets_no_tool_block(tmp_path) -> None:
    """插嘴轮不注入新工具说明——与 persona 的既有行为一致。

    插嘴是没被叫到时自己凑一句，不该在那样的轮次里握有 SHELL / CODE。
    """
    from dl_senpai.agent.prompt.builder import PromptBuilder, PromptRequest

    registry = ToolRegistry()
    register_high_risk_tools(registry)
    registry.bind_permissions(PermissionService())
    visible = registry.visible_tools(_ctx(tmp_path, actor=ADMIN))

    built = PromptBuilder().build(
        PromptRequest(visible_tools=visible, private_chat=False, interrupt=True)
    )
    assert "<<<SHELL" not in built.text
    assert "<<<CODE" not in built.text
    assert built.sections["extra_tool_block"] is False


# ------------------------------------------- 一轮多个工具的结果都要回填


@pytest.mark.asyncio
async def test_all_round_tool_results_are_fed_back(tmp_path) -> None:
    """回归：续跑回填曾经用 `results[-1:]`，一轮里多个工具只回最后一个。

    那些工具都已经真的执行了（有副作用），结果却被丢掉——
    模型看不到输出，只会重复调用或凭空瞎猜。
    """
    from dl_senpai.agent.runtime import AgentRuntime
    from dl_senpai.agent.tools.models import ToolResult
    from dl_senpai.agent.tools.registry import ToolRegistry, ToolDefinition
    from dl_senpai.agent.tools.models import Continuation

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="multi", marker="MULTI",
            parser=lambda text: (
                "",
                [
                    ToolCall(tool_name="multi", arguments={"i": i}, id=f"m{i}")
                    for i in (1, 2, 3)
                    if f"<<<MULTI {i}>>>" in text
                ],
            ),
            executor=lambda c, k: ToolResult(
                tool_name="multi", success=True,
                output="输出-" + str(k.arguments["i"]),
            ),
            continuation=Continuation.RETURN_TO_MODEL,
            priority=10,
        )
    )

    # 用项目自带的 StubModelGateway，而不是手搓一个假网关：
    # 手搓的响应对象会缺字段（error / usage / raw ...），每缺一个
    # 就在 runtime 深处炸一次 AttributeError。
    from dl_senpai.agent.model_gateway import StubModelGateway

    gateway = StubModelGateway([
        "<<<MULTI 1>>><<<MULTI 2>>><<<MULTI 3>>>",
        "好",
    ])
    runtime = AgentRuntime(registry=registry, gateway=gateway)
    await runtime.run(_ctx(tmp_path, actor=ADMIN), "跑一下")

    # 第二次请求的 messages 里必须三条输出都在
    joined = " ".join(
        str(m.get("content", "")) for m in gateway.requests[-1].messages
    )
    for i in (1, 2, 3):
        assert f"输出-{i}" in joined, f"第 {i} 条工具结果没回填"
    assert joined.count("【multi 的结果】") == 3