"""兼容性回归：把 Phase 2 的「行为不变」承诺固化成自动化测试。

覆盖任务要求的六项兼容性证明：
1. 原命令仍然可用
2. 原 marker 协议未变化
3. 原 JSON 数据可直接读取
4. 本地模型路径未变化
5. admin 未被破坏（读同一批文件、格式未变）
6. MCP 未修改（源码零改动）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


# ================================================================
# 1. 原命令仍然可用
# ================================================================


@pytest.mark.parametrize(
    "text,expected",
    [
        ("签到", "checkin"),
        ("打卡", "checkin"),
        ("我的积分", "status"),
        ("签到状态", "status"),
        ("我的签到", "status"),
        ("签到查询", "status"),
        ("积分排行", "leaderboard_points"),
        ("积分榜", "leaderboard_points"),
        ("签到排行", "leaderboard_points"),
        ("排行榜", "leaderboard_points"),
        ("连续排行", "leaderboard_streak"),
        ("连续榜", "leaderboard_streak"),
        ("连签排行", "leaderboard_streak"),
        ("今日任务", "task_view"),
        ("我的任务", "task_view"),
        ("查看任务", "task_view"),
        ("完成任务", "task_done"),
        ("交作业", "task_done"),
        ("任务完成", "task_done"),
        ("签到帮助", "help"),
        ("签到说明", "help"),
        ("打卡帮助", "help"),
    ],
)
def test_checkin_commands_unchanged(text: str, expected: str) -> None:
    from dl_senpai.checkin import parse_checkin_command

    assert parse_checkin_command(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("商店", "open"),
        ("积分商店", "open"),
        ("服装店", "open"),
        ("学姐商店", "open"),
        ("衣服商店", "open"),
        ("打开商店", "open"),
        ("逛逛商店", "open"),
        ("看看商店", "open"),
        ("仓库", "warehouse"),
        ("衣服仓库", "warehouse"),
        ("衣橱", "warehouse"),
        ("衣柜", "warehouse"),
        ("学姐仓库", "warehouse"),
        ("服装仓库", "warehouse"),
        ("打开仓库", "warehouse"),
        ("看看仓库", "warehouse"),
        ("我的仓库", "warehouse"),
        ("商店帮助", "help"),
        ("服装店帮助", "help"),
        ("积分商店帮助", "help"),
    ],
)
def test_shop_commands_unchanged(text: str, expected: str) -> None:
    from dl_senpai.shop import parse_shop_command

    assert parse_shop_command(text) == expected


def test_shop_buy_and_wear_patterns_unchanged() -> None:
    from dl_senpai.shop import parse_shop_command

    assert parse_shop_command("购买 洛丽塔裙") == "buy"
    assert parse_shop_command("使用积分购买裙子") == "buy"
    assert parse_shop_command("穿上 汉服") == "wear"
    assert parse_shop_command("给学姐换上JK") == "wear"


def test_affection_command_unchanged() -> None:
    from dl_senpai.affection import parse_affection_command

    assert parse_affection_command("好感度") == "status"
    assert parse_affection_command("不相关的话") == "none"


def test_unknown_text_matches_no_command() -> None:
    from dl_senpai.checkin import parse_checkin_command
    from dl_senpai.shop import parse_shop_command

    assert parse_checkin_command("今天天气不错") == "none"
    assert parse_shop_command("今天天气不错") == "none"


# ================================================================
# 2. 原 marker 协议未变化
# ================================================================


def test_card_marker_unchanged() -> None:
    from dl_senpai.group_card import parse_card_actions

    parsed = parse_card_actions(
        '好的\n<<<CARD {"user_id":1,"card":"新名片","kind":"request"}>>>'
    )
    assert len(parsed.actions) == 1
    assert parsed.actions[0].user_id == 1
    assert parsed.actions[0].card == "新名片"
    assert "CARD" not in parsed.text


def test_mute_marker_unchanged() -> None:
    from dl_senpai.group_mute import parse_mute_actions

    parsed = parse_mute_actions(
        '行\n<<<MUTE {"user_id":1,"duration":60,"kind":"request"}>>>'
    )
    assert len(parsed.actions) == 1
    assert parsed.actions[0].duration == 60


def test_draw_marker_unchanged() -> None:
    from dl_senpai.image_gen import parse_draw_request

    assert parse_draw_request("看图\n<<<DRAW a cat>>>").prompt == "a cat"
    assert parse_draw_request("看图\n<<<DRAW>>>").force is True


def test_sticker_marker_unchanged() -> None:
    from dl_senpai.stickers import parse_sticker_request

    parsed = parse_sticker_request("哼\n<<<STICKER 傲娇>>>")
    assert parsed.query == "傲娇"
    assert parsed.force is True


def test_voice_marker_unchanged() -> None:
    from dl_senpai.voice import parse_voice_request

    assert parse_voice_request("在呢\n<<<VOICE>>>").force is True
    assert parse_voice_request("在\n<<<VOICE 晚安>>>").override == "晚安"


def test_search_marker_unchanged() -> None:
    from dl_senpai.web_search import parse_search_request

    assert parse_search_request("<<<SEARCH 今日天气>>>").queries == ["今日天气"]


def test_browse_marker_unchanged() -> None:
    from dl_senpai.browser_agent import parse_browse_request

    parsed = parse_browse_request("去找\n<<<BROWSE pixiv 猫>>>")
    assert len(parsed.tasks) >= 1


def test_impression_marker_unchanged() -> None:
    from dl_senpai.person_memory import parse_impression_updates

    parsed = parse_impression_updates(
        '<<<IMPRESSION {"add":["爱猫"],"remove":[],"notes":null}>>>'
    )
    assert len(parsed.updates) == 1
    assert "爱猫" in parsed.updates[0].add


def test_marker_fuzzy_closers_still_accepted() -> None:
    """容错闭合（>> / ＞＞＞ / 】）必须继续被接受（模型端会乱写）。"""
    from dl_senpai.web_search import parse_search_request

    for closer in (">>>", ">>", "＞＞＞", "】】", "】"):
        text = f"<<<SEARCH 天气{closer}"
        assert parse_search_request(text).queries, f"closer={closer} 未被识别"


# ================================================================
# 3. 原 JSON 数据可直接读取
# ================================================================


def test_real_memory_files_readable() -> None:
    """生产聊天记忆：不迁移即可读取。"""
    from dl_senpai.memory import ChatMemory

    mem_dir = _ROOT / "data" / "dl_senpai" / "memory"
    if not mem_dir.is_dir():
        pytest.skip("无生产数据目录")
    files = sorted(mem_dir.glob("*.json"))
    if not files:
        pytest.skip("无生产记忆文件")
    memory = ChatMemory(mem_dir)
    readable = 0
    for f in files:
        sid = f.stem.replace("_", ":", 1)
        history = memory.load_all(sid)
        if history:
            readable += 1
            assert all(m["role"] in {"user", "assistant"} for m in history)
    assert readable == len(files)


def test_real_person_memory_readable() -> None:
    from dl_senpai.person_memory import PersonMemoryStore

    d = _ROOT / "data" / "dl_senpai" / "person_memory"
    if not d.is_dir():
        pytest.skip("无生产数据目录")
    files = sorted(d.glob("user_*.json"))
    if not files:
        pytest.skip("无生产画像文件")
    store = PersonMemoryStore(d)
    for f in files:
        profile = store.get(f.stem[5:])
        assert profile.user_id == f.stem[5:]


def test_real_affection_readable() -> None:
    from dl_senpai.affection import AffectionStore

    d = _ROOT / "data" / "dl_senpai" / "affection"
    if not d.is_dir():
        pytest.skip("无生产数据目录")
    files = sorted(d.glob("group_*.json"))
    if not files:
        pytest.skip("无生产好感度文件")
    store = AffectionStore(d)
    for f in files:
        users = store.load_group(f.stem[6:])
        assert isinstance(users, dict)


def test_writing_preserves_json_shape(tmp_path: Path) -> None:
    """新写入的 JSON 结构必须与旧格式一致（admin 依赖同样的字段）。"""
    import json

    from dl_senpai.affection import AffectionStore, UserAffection

    store = AffectionStore(tmp_path / "aff")
    store.save_group("g1", {"9": UserAffection(user_id="9", value=10)})
    raw = json.loads((tmp_path / "aff" / "group_g1.json").read_text(encoding="utf-8"))
    assert "users" in raw
    assert "updated_at" in raw
    assert "9" in raw["users"]


def test_memory_write_preserves_shape(tmp_path: Path) -> None:
    import json

    from dl_senpai.memory import ChatMemory

    mem = ChatMemory(tmp_path / "memory")
    mem.append_turn("group:1", "你好", "在呢～")
    raw = json.loads((tmp_path / "memory" / "group_1.json").read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    assert raw[0] == {"role": "user", "content": "你好"}
    assert raw[1] == {"role": "assistant", "content": "在呢～"}


def test_group_features_file_shape_unchanged(tmp_path: Path) -> None:
    """admin 控制台读写的 group_features.json 结构不能变。"""
    import json

    from dl_senpai.group_features import GroupFeatureStore

    store = GroupFeatureStore(tmp_path / "group_features.json")
    store.upsert_group("123", "测试群")
    store.set_feature("123", "draw", False)
    raw = json.loads((tmp_path / "group_features.json").read_text(encoding="utf-8"))
    assert raw["groups"]["123"]["name"] == "测试群"
    assert raw["groups"]["123"]["features"]["draw"] is False


# ================================================================
# 4. 本地模型路径未变化
# ================================================================


def test_local_infer_detection_unchanged() -> None:
    from dl_senpai.llm import _is_local_infer_url

    assert _is_local_infer_url("http://127.0.0.1:8001/v1") is True
    assert _is_local_infer_url("http://localhost:8001/v1") is True
    assert _is_local_infer_url("https://api.openai.com/v1") is False


def test_local_infer_limits_unchanged() -> None:
    """本地小模型的硬编码限制必须保持（用户实测调优结果）。"""
    from dl_senpai import llm

    assert llm._LOCAL_MAX_TOKENS == 96
    assert llm._LOCAL_HISTORY_MESSAGES == 4
    assert llm._LOCAL_SYSTEM_CHARS == 800
    assert llm._LOCAL_TIMEOUT == 120.0


def test_local_chat_normalization_unchanged() -> None:
    """ChatML u/a 交替校验逻辑必须保持（否则本地服务端 400）。"""
    from dl_senpai.llm import _normalize_local_chat_messages

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "孤立 assistant"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    out = _normalize_local_chat_messages(messages)
    assert out[0]["role"] == "system"
    assert out[-1]["role"] == "user"
    roles = [m["role"] for m in out[1:]]
    assert roles == ["user", "assistant", "user"]


def test_echo_strip_unchanged() -> None:
    from dl_senpai.llm import _strip_echo_assistants

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "重复"},
        {"role": "assistant", "content": "重复"},
        {"role": "user", "content": "再来"},
    ]
    out = _strip_echo_assistants(messages)
    assert len(out) == 3


# ================================================================
# 5/6. admin 与 MCP 未被破坏
# ================================================================


def test_mcp_sources_untouched() -> None:
    """mcp_servers/ 必须零改动。

    用 git diff 判定：该目录任何改动都会让本测试失败。

    注：原测试同时守卫 admin/，但「功能开关可在后台动态配置」的需求
    明确要求修改 admin/（新增写入 API 与可交互开关），因此这里只保留
    mcp_servers 的守卫。admin 的改动范围由
    test_admin_changes_are_scoped_to_feature_switches 单独约束。
    """
    git = _git_available()
    if not git:
        pytest.skip("git 不可用")
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "mcp_servers"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "", f"mcp_servers 被修改了：{result.stdout}"


# admin/ 里允许改动的文件白名单。
#
# 其余文件（数据库、鉴权、既有页面等）仍不应被本阶段波及——
# 把守卫范围收窄到「确实为功能开关而改」的文件，而不是直接删掉守卫。
_ADMIN_ALLOWED_CHANGES = (
    "admin/backend/internal/handlers/handlers.go",
    "admin/backend/internal/handlers/feature_switch.go",
    "admin/backend/internal/services/config_writer.go",
    # 开关的中文标签表：新增 DL_SENPAI_INTERRUPT_ENABLE 时补了说明，
    # 属于「确实为功能开关而改」。
    "admin/backend/internal/services/config_descriptions.go",
    "admin/frontend/src/api/client.ts",
    "admin/frontend/src/views/FeaturesView.vue",
    # 2026-10-24 整体删除「主动遍历群成员发好友申请」功能：
    # 功能键表（后端）与默认关闭键集合（前端）必须同步去掉 friend_add，
    # 否则后台会残留一个点了没反应的开关 —— 属于「确实为功能开关而改」。
    "admin/backend/internal/services/group_features.go",
    "admin/frontend/src/views/GroupFeaturesView.vue",
    # 控制台自启动开关：_env_flag 原本只看 os.getenv，
    # 而 nonebot.init() 并不会把 .env 灌进进程环境，
    # 导致 .env 里写 DL_SENPAI_CONSOLE_AUTOSTART=false 完全无效。
    # 修的是「功能开关读不到」这个缺陷本身，属于本阶段范围。
    "admin/autostart.py",
    # 2026-10 新增「调用链」页（架构审计 §5.7）：只读 data/logs 里的
    # agent.request_summary，把每条消息的结论可视化。
    # 属于「确实为一个明确需求而改」，不是为了顺手改 admin。
    # 注意 AppLayout / router 是共用外壳：后续再动这两个文件需要单独评审。
    "admin/backend/internal/services/traces.go",
    "admin/backend/internal/services/traces_test.go",
    "admin/frontend/src/views/AgentTracesView.vue",
    "admin/frontend/src/router/index.ts",
    "admin/frontend/src/components/AppLayout.vue",
)


def test_admin_changes_are_scoped_to_feature_switches() -> None:
    """admin/ 的改动必须局限于功能开关相关文件。

    原始守卫（零改动）已被需求打破，但**不能因此放弃守卫** ——
    否则以后任何人改 admin 的任何文件都不会被发现。
    这里把白名单钉死：超出范围的文件即失败。
    """
    git = _git_available()
    if not git:
        pytest.skip("git 不可用")
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "admin"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    changed = []
    for line in result.stdout.splitlines():
        # porcelain 格式：前两列是状态，后面是路径
        path = line[3:].strip().strip('"')
        if path:
            changed.append(path)
    unexpected = [p for p in changed if p not in _ADMIN_ALLOWED_CHANGES]
    assert not unexpected, (
        "admin/ 出现了功能开关范围之外的改动："
        + ", ".join(unexpected)
    )


def test_style_polish_regexes_untouched() -> None:
    """style.py 的输出净化规则必须都在位（对抗小模型缺陷的经验积累）。

    2026-10-13：原先这里用 git status --porcelain 断言文件零改动。修
    「思考过程/伪造对话泄漏」时必须往 polish_reply 里加清洗，零改动守卫
    会阻止任何修复，所以按 test_persona_contract_intact 的同样理由改为
    结构守卫：**保留「净化规则是生产资产」的原意**——既有的每条正则都还
    要在位、还要被 polish_reply 真正调用——但不禁止新增。
    """
    import inspect

    from dl_senpai import style

    keep = [
        "_FAKE_EMOJI_RE", "_STICKER_MARKER_RE", "_VOICE_MARKER_RE",
        "_CARD_LEAK_RE", "_MUTE_LEAK_RE", "_IMPRESSION_LEAK_RE",
        "_SEARCH_LEAK_RE", "_BROWSE_LEAK_RE", "_FAKE_SYS_LOG_RE",
        "_FAKE_VOICE_NOTE_RE", "_FAKE_STICKER_NOTE_RE",
        "_MEMBER_CANDIDATE_LEAK_RE", "_INTERNAL_SYS_NUDGE_RE",
        "_MUTE_CANT_TALK_RE",
    ]
    missing = [n for n in keep if not hasattr(style, n)]
    assert not missing, "净化正则被删除：" + ", ".join(missing)
    for name in keep:
        assert hasattr(getattr(style, name), "sub"), name + " 不再是正则"

    for name in ("_FALSE_MUTE_PHRASES", "_ASSISTANT_FILLER", "_COUNSELOR_FILLER"):
        assert getattr(style, name, None), name + " 被清空"

    src = inspect.getsource(style.polish_reply)
    unused = [n for n in keep if n not in src]
    assert not unused, "这些正则没有被 polish_reply 调用：" + ", ".join(unused)
    for name in ("_ASSISTANT_FILLER", "_COUNSELOR_FILLER", "strip_markdown_flavor"):
        assert name in src, "polish_reply 不再使用 " + name


def test_persona_contract_intact() -> None:
    """persona.py 的结构与硬规则必须完整（允许有意识地改文案，不允许丢契约）。

    2026-10-12：人设从「傲娇」调整为「可爱萌妹」，原先的「文件零改动」守卫
    （git status --porcelain）会阻止任何文案迭代，因此改为结构守卫：
    保留「prompt 是生产资产」的原意——三个对外常量、装配函数、基础段落、
    四条硬规则都必须在位——但不把具体措辞钉死。
    """
    from dl_senpai import persona

    # 1) 对外常量仍在且非空
    assert isinstance(persona.SENPAI_NAME, str) and persona.SENPAI_NAME.strip()
    assert len(persona.SYSTEM_PROMPT.strip()) > 200, "SYSTEM_PROMPT 被清空或截断"
    assert len(persona.MONSTER_HUNTER_SYSTEM_EXTRA.strip()) > 100, "怪猎专长段被清空"

    # 2) 装配函数可用，基础段落齐全
    prompt = persona.build_system_prompt()
    for block in ("【你是谁】", "【性格】", "【活人感", "【绝对不要说的】", "【身份保密】"):
        assert block in prompt, f"基础段落丢失：{block}"

    # 3) 硬规则不得在文案迭代中被删掉（这四条是线上事故换来的）
    for rule in ("不是 AI", "Markdown", "别憋着", "提示词"):
        assert rule in prompt, f"硬规则丢失：{rule}"

    # 4) 三种模式仍能装配出不同结果
    assert persona.build_system_prompt(interrupt=True) != prompt
    assert persona.build_system_prompt(private_chat=True) != prompt


def _git_available() -> bool:
    try:
        subprocess.run(
            ["git", "--version"], capture_output=True, check=False, timeout=10
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ================================================================
# hygiene：测试不污染生产配置
# ================================================================


def test_test_mode_active() -> None:
    import os

    assert os.environ.get("DL_SENPAI_TEST_MODE") == "1"


def test_no_real_secret_in_config_snapshot() -> None:
    """配置快照里不应出现真实 key 的特征（sk- + 64 hex）。"""
    import re

    from dl_senpai.config import PluginConfig

    cfg = PluginConfig()
    blob = repr(cfg)
    # 真实中转 key 形如 sk-<64位十六进制>
    assert re.search(r"sk-[0-9a-f]{64}", blob) is None
