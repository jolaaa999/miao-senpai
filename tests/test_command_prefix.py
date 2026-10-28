"""指令分层与前缀规范。

**分层（2026-10-28 定）**
- 带「喵」字的是**管理员指令**：喵/禁言、喵/撤回、喵/人设、喵/reset…
- 不带喵的是**群友玩法**：签到、积分商店、好感度（再加上新人认证）。
  这些谁都能用，但**不认喵前缀**——「喵/签到」不会被当成签到，
  这样「带喵字 = 给管理员用」这条规则在结构上永远成立。

中文指令统一写「喵/指令」。

2026-10-28 用户定的规则：中文指令用「喵/指令」（如 喵/人设 学姐），
英文指令保持 /silence、#sl 这类市面写法。旧的「喵禁言」无斜杠写法保留兼容。

这里覆盖三件容易说错的事：
1. 「喵/」能被剥掉，并且**所有**命令都享受同一套前缀规则（不是逐个命令加）；
2. 全角斜杠 ／ 与半角 / 等价（中文输入法默认出全角）；
3. 既有的 reset / 人设 命令认得新前缀，而且参数照样能解析出来。
"""

from __future__ import annotations

import pytest

from dl_senpai.core.commands.admin import register_admin_commands
from dl_senpai.core.commands.builtin import build_builtin_commands
from dl_senpai.core.commands.registry import CommandRegistry, normalize_command_text


def _registry() -> CommandRegistry:
    reg = CommandRegistry()
    for cmd in build_builtin_commands():
        reg.register(cmd)
    register_admin_commands(reg)
    return reg


# ---- 归一化 ----

@pytest.mark.parametrize(
    "text,expected",
    [
        ("喵/人设 学姐", "/人设 学姐"),
        ("喵／人设 学姐", "/人设 学姐"),  # 全角斜杠
        ("喵 / 人设", "/人设"),          # 输入法多打的空格
        ("喵#帮助", "#帮助"),
        ("喵！状态", "!状态"),
        ("／人设", "/人设"),             # 没写喵但用了全角斜杠
        ("喵禁言", "喵禁言"),            # 没有分隔符：不剥，交给命令自己决定
        ("今天天气不错", "今天天气不错"),
        ("", ""),
    ],
)
def test_normalize_command_text(text, expected):
    assert normalize_command_text(text) == expected


# ---- 管理指令的各种前缀 ----

@pytest.mark.parametrize(
    "text",
    [
        "喵/禁言 10m",   # 本次规定的标准格式
        "喵禁言 10m",    # 旧写法，继续兼容
        "/禁言 10m",
        "#禁言 10m",
        "！禁言 10m",
        "喵 / 禁言 10m",
        "喵／禁言 10m",
        "/silence 10m",  # 市面写法
        "#sl 10m",
    ],
)
def test_mute_accepts_every_prefix(text):
    inv = _registry().match(text)
    assert inv is not None, text
    assert inv.match.action == "mute"


@pytest.mark.parametrize(
    "text,action",
    [
        ("喵/撤回", "recall"),
        ("喵/帮助", "help"),
        ("喵/状态", "status"),
        ("喵/闭嘴 10", "silence"),
        ("喵/功能 签到 关", "feature"),
        ("喵/全员禁言 关", "muteall"),
    ],
)
def test_chinese_commands_use_slash_prefix(text, action):
    inv = _registry().match(text)
    assert inv is not None, text
    assert inv.match.action == action


# ---- 既有命令也认新前缀 ----

def test_persona_accepts_mao_slash():
    """喵/人设 学姐 —— 用户点名的例子。"""
    inv = _registry().match("喵/人设 学姐")
    assert inv is not None
    assert inv.name == "persona"
    assert inv.match.arg(0) == "学姐"


@pytest.mark.parametrize(
    "text",
    ["喵/reset", "喵/reset 卡片", "喵reset", "/reset", "reset喵", "reset喵 全部"],
)
def test_reset_accepts_all_forms(text):
    inv = _registry().match(text)
    assert inv is not None, text
    assert inv.name == "reset"


def test_reset_scope_still_parsed():
    inv = _registry().match("喵/reset 卡片")
    assert inv is not None
    assert inv.match.arg(0) == "卡片"


def test_persona_handler_can_reparse_normalized_text():
    """命令匹配只是第一步：personas 内部还会拿 match.text 再解析一次参数。

    这条用例钉住那个隐性契约——归一化后的文本必须是「/人设 学姐」这种
    仍能被 personas._PERSONA_CMD_RE 认出的形状，否则会出现
    「指令命中了但参数丢了，默默切回默认」。
    """
    from dl_senpai.personas import _PERSONA_CMD_RE

    inv = _registry().match("喵/人设 学姐")
    assert inv is not None
    found = _PERSONA_CMD_RE.match(inv.match.text)
    assert found is not None, inv.match.text
    assert found.group("arg") == "学姐"


# ---- 不该被前缀规则误伤 ----

@pytest.mark.parametrize(
    "text",
    ["今天学姐好可爱", "学姐人设崩了", "喵", "喵喵喵", "禁言 @某人"],
)
def test_plain_chat_still_not_a_command(text):
    assert _registry().match(text) is None


# ---- 指令分层：带喵的只给管理员，玩法谁都能用但不认喵 ----

def test_admin_commands_are_tagged():
    """所有管理指令都带 permission=admin —— registry 靠它挡住「喵/签到」。"""
    from dl_senpai.core.commands.registry import PERMISSION_ADMIN

    for cmd in _registry().list(namespace="admin"):
        assert cmd.permission == PERMISSION_ADMIN, cmd.name


def test_play_commands_are_not_admin_only():
    from dl_senpai.core.commands.registry import PERMISSION_ADMIN

    reg = _registry()
    by_name = {cmd.name: cmd for cmd in reg.list()}
    for name in ("checkin", "shop", "affection", "verify"):
        assert by_name[name].permission != PERMISSION_ADMIN, name


@pytest.mark.parametrize("text", ["喵/签到", "喵签到", "喵 签到", "喵／签到"])
def test_play_commands_reject_mao_prefix(text):
    """群友玩法不认喵前缀：带喵的一律当管理员指令处理，匹配不到就交给 AI。"""
    assert _registry().match(text) is None


def test_play_commands_still_work_without_prefix():
    reg = _registry()
    for text in ("签到", "打卡"):
        inv = reg.match(text)
        assert inv is not None, text
        assert inv.name == "checkin"


def test_admin_commands_reject_non_admin_at_handler_or_router():
    """管理指令必须能被识别成「权限不足」而不是「没这条命令」。

    handler 里抛 CommandPermissionDenied，Router 才会静默跳过（不 consume）。
    这里只验证异常类型和它的传递方向，实际拒绝行为在 test_admin_commands 里。
    """
    from dl_senpai.admin_commands import AdminDenied
    from dl_senpai.core.commands.models import CommandPermissionDenied

    assert AdminDenied is CommandPermissionDenied
