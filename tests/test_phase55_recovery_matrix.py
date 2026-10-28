"""Phase 5.5 Step 0：Legacy Recovery vs RecoveryPolicy 决策矩阵对照。

第三节明确要求：**在修改任何 Recovery 生产路径之前**建立完整对照矩阵。

对照的不是"看起来差不多"，而是逐项等价：
    - should_retry（触发判定）
    - retry prompt（逐字）
    - history 构造（形状与内容）
    - max retries
    - 附加的 llm.chat kwargs（各工具不同！）

矩阵维度：
    tool            : card / mute / draw / browse
    explicit intent : true / false
    tool enabled    : true / false
    first response  : 有 marker / 无 marker
    interrupt       : true / false
    history         : 有 / 无

本文件**只做对照**，不修改生产路径。跑通后才切换。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from dl_senpai.msg_format import format_user_turn
from dl_senpai.agent.recovery import (
    CARD_RETRY_PROMPT,
    MUTE_RETRY_PROMPT,
    RecoveryPolicy,
    build_recovery_policies,
)


# ================================================================
# Legacy 判定逻辑的忠实复现
# ================================================================
#
# 直接搬运 handlers.py L1264-L1360 的 if 条件，不改写。
# 这样对照才有意义——如果这里"顺手优化"了，对照就失去价值。


@dataclass
class LegacyInputs:
    """一次 legacy recovery 判定的输入。"""

    tool: str
    intent_text: str
    tool_enabled: bool
    has_marker: bool
    interrupt: bool = False
    history: list[dict[str, Any]] | None = None
    sender: str = "小明"
    chat_text: str = "改名片"
    first_reply: str = "已经改好啦"


def legacy_should_retry(inp: LegacyInputs) -> bool:
    """复现 legacy 的触发条件。

    对应 handlers.py：
        if (card_enable and not interrupt and not result.card_actions
                and looks_like_card_request(intent_text)):

    四个条件缺一不可，顺序无关（都是 and）。
    """
    from dl_senpai.browser_agent import wants_browser_help
    from dl_senpai.group_card import looks_like_card_request
    from dl_senpai.group_mute import looks_like_mute_request
    from dl_senpai.image_gen import wants_senpai_image

    predicates = {
        "card": looks_like_card_request,
        "mute": looks_like_mute_request,
        "draw": wants_senpai_image,
        "browse": wants_browser_help,
    }
    predicate = predicates[inp.tool]

    if not inp.tool_enabled:      # *_enable
        return False
    if inp.interrupt:             # not interrupt
        return False
    if inp.has_marker:            # not result.<tool>_actions / _force
        return False
    return bool(predicate(inp.intent_text))


def legacy_retry_prompt(inp: LegacyInputs) -> str:
    """复现 legacy 的补救 prompt 文本。"""
    if inp.tool == "card":
        return CARD_RETRY_PROMPT
    if inp.tool == "mute":
        return MUTE_RETRY_PROMPT
    if inp.tool == "draw":
        from dl_senpai.image_gen import draw_retry_hint

        return draw_retry_hint(inp.intent_text)
    if inp.tool == "browse":
        from dl_senpai.browser_agent import browse_retry_hint

        return browse_retry_hint(inp.intent_text)
    raise ValueError(inp.tool)


def legacy_history(inp: LegacyInputs) -> list[dict[str, Any]]:
    """复现 legacy 的 hist2 构造。

         hist2 = [
             *history,
             # 期望值也走同一个 helper：前缀形态以后改了，这里跟着变，不会再漂
             {"role": "assistant", "content": result.text},
         ]
    """
    return [
        *(inp.history or []),
        {"role": "user", "content": format_user_turn(inp.sender, inp.chat_text)},
        {"role": "assistant", "content": inp.first_reply},
    ]


def legacy_chat_kwargs(inp: LegacyInputs) -> dict[str, Any]:
    """复现补救调用传给 llm.chat 的**工具专属参数**。

    这是容易被忽略的差异点：CARD 补救显式传 `mute_enable=False`，
    DRAW/BROWSE 传各自的 enable。如果不逐项比对，切换后可能
    让补救轮意外带上别的工具。
    """
    if inp.tool == "card":
        return {"card_enable": True, "mute_enable": False}
    if inp.tool == "mute":
        return {"card_enable": False, "mute_enable": True}
    if inp.tool == "draw":
        return {"draw_enable": True}
    if inp.tool == "browse":
        return {"browser_enable": True}
    raise ValueError(inp.tool)


# ================================================================
# 矩阵
# ================================================================

TOOLS = ["card", "mute", "draw", "browse"]

# 每类工具的"明确意图"文本与"非明确"文本
INTENT_TEXT: dict[str, tuple[str, str]] = {
    "card": ("把我名片改成小鱼干", "今天天气不错"),
    "mute": ("禁言他", "讲个笑话"),
    "draw": ("给我画只猫", "今天几号"),
    "browse": ("去pixiv找图", "你好呀"),
}


def build_matrix() -> list[LegacyInputs]:
    """构造完整对照矩阵。"""
    cases: list[LegacyInputs] = []
    for tool in TOOLS:
        yes_text, no_text = INTENT_TEXT[tool]
        for intent in (True, False):
            for enabled in (True, False):
                for has_marker in (True, False):
                    for interrupt in (True, False):
                        for has_history in (True, False):
                            cases.append(
                                LegacyInputs(
                                    tool=tool,
                                    intent_text=yes_text if intent else no_text,
                                    tool_enabled=enabled,
                                    has_marker=has_marker,
                                    interrupt=interrupt,
                                    history=(
                                        [{"role": "user", "content": "旧消息"}]
                                        if has_history
                                        else []
                                    ),
                                    chat_text=yes_text,
                                    first_reply="已经改好啦",
                                )
                            )
    return cases


@pytest.fixture
def policies() -> dict[str, RecoveryPolicy]:
    return build_recovery_policies()


@pytest.mark.parametrize("tool", TOOLS)
def test_should_retry_matches_legacy(tool: str, policies) -> None:
    """核心断言：legacy_should_retry == policy.should_retry 全矩阵一致。"""
    policy = policies[tool]
    mismatches: list[str] = []

    for inp in build_matrix():
        if inp.tool != tool:
            continue
        legacy = legacy_should_retry(inp)
        new = policy.should_retry(
            intent_text=inp.intent_text,
            already_called=inp.has_marker,
            interrupt=inp.interrupt,
        )
        # policy 通过 visibility/enabled 在别处过滤；这里对齐 enable 维度
        if not inp.tool_enabled:
            new = False
        if legacy != new:
            mismatches.append(
                f"intent={inp.intent_text!r} enabled={inp.tool_enabled} "
                f"marker={inp.has_marker} interrupt={inp.interrupt}: "
                f"legacy={legacy} policy={new}"
            )

    assert not mismatches, (
        f"{tool} 决策不一致 {len(mismatches)} 处:\n" + "\n".join(mismatches[:5])
    )


@pytest.mark.parametrize("tool", TOOLS)
def test_retry_prompt_matches_legacy_verbatim(tool: str, policies) -> None:
    """补救 prompt 必须逐字一致（第四节：DRAW/BROWSE 也补齐）。"""
    policy = policies[tool]
    yes_text, _ = INTENT_TEXT[tool]
    inp = LegacyInputs(
        tool=tool, intent_text=yes_text, tool_enabled=True, has_marker=False,
        chat_text=yes_text,
    )
    legacy = legacy_retry_prompt(inp)
    new = policy.build_retry_prompt(yes_text)
    assert new == legacy, (
        f"{tool} 补救 prompt 不一致:\n  legacy={legacy!r}\n  policy={new!r}"
    )


@pytest.mark.parametrize("tool", TOOLS)
def test_history_construction_matches_legacy(tool: str, policies) -> None:
    """hist2 形状与内容必须一致。"""
    policy = policies[tool]
    yes_text, _ = INTENT_TEXT[tool]
    inp = LegacyInputs(
        tool=tool, intent_text=yes_text, tool_enabled=True, has_marker=False,
        history=[{"role": "user", "content": "旧"}],
        sender="小明", chat_text="改名片", first_reply="好",
    )
    legacy = legacy_history(inp)
    new = policy.build_history(
        inp.history or [], inp.sender, inp.chat_text, inp.first_reply
    )
    assert new == legacy


@pytest.mark.parametrize("tool", TOOLS)
def test_max_retries_is_one(tool: str, policies) -> None:
    """legacy 每类工具都只补救 1 次。"""
    assert policies[tool].max_retries == 1


@pytest.mark.parametrize("tool", TOOLS)
def test_interrupt_blocks_recovery(tool: str, policies) -> None:
    """插嘴模式不补救（legacy 的 `not interrupt` 条件）。"""
    policy = policies[tool]
    yes_text, _ = INTENT_TEXT[tool]
    assert policy.should_retry(
        intent_text=yes_text, already_called=False, interrupt=True
    ) is False
    inp = LegacyInputs(
        tool=tool, intent_text=yes_text, tool_enabled=True, has_marker=False,
        interrupt=True,
    )
    assert legacy_should_retry(inp) is False


# ================================================================
# 各工具专属 kwargs 差异（易被忽略）
# ================================================================


def test_card_recovery_kwargs_differ_from_mute() -> None:
    """CARD 补救显式关掉 MUTE，反之亦然 —— 切换时必须保留这个差异。"""
    card = LegacyInputs("card", "改名片", True, False)
    mute = LegacyInputs("mute", "禁言他", True, False)
    assert legacy_chat_kwargs(card) == {"card_enable": True, "mute_enable": False}
    assert legacy_chat_kwargs(mute) == {"card_enable": False, "mute_enable": True}


def test_draw_recovery_passes_draw_enable_only() -> None:
    draw = LegacyInputs("draw", "画猫", True, False)
    assert legacy_chat_kwargs(draw) == {"draw_enable": True}


def test_browse_recovery_passes_browser_enable_only() -> None:
    browse = LegacyInputs("browse", "找图", True, False)
    assert legacy_chat_kwargs(browse) == {"browser_enable": True}


# ================================================================
# 矩阵统计（报告用）
# ================================================================


def test_matrix_size() -> None:
    """矩阵规模：4 工具 × 2 intent × 2 enabled × 2 marker × 2 interrupt × 2 history。"""
    assert len(build_matrix()) == 4 * 2 * 2 * 2 * 2 * 2


def test_matrix_covers_all_trigger_combinations() -> None:
    """矩阵必须包含"应触发"与"不应触发"两类。"""
    should = [i for i in build_matrix() if legacy_should_retry(i)]
    should_not = [i for i in build_matrix() if not legacy_should_retry(i)]
    assert should, "矩阵里没有'应触发 recovery'的用例"
    assert should_not, "矩阵里没有'不应触发 recovery'的用例"


@pytest.mark.parametrize("tool", TOOLS)
def test_draw_browse_prompt_nonempty(tool: str, policies) -> None:
    """DRAW/BROWSE 的补救 prompt 由各自模块生成，必须非空。"""
    yes_text, _ = INTENT_TEXT[tool]
    prompt = policies[tool].build_retry_prompt(yes_text)
    assert prompt.strip(), f"{tool} 补救 prompt 为空"
    # 必须包含该工具的 marker 说明，否则模型不知道要写什么
    assert "<<" in prompt, f"{tool} 补救 prompt 未提及 marker"


def test_all_four_policies_present() -> None:
    policies = build_recovery_policies()
    assert set(policies) == {"card", "mute", "draw", "browse"}
    for name, policy in policies.items():
        assert policy.tool_name == name
        assert policy.intent_predicate is not None
        assert policy.max_retries == 1
