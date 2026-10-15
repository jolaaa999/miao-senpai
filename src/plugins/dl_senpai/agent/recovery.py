"""RecoveryPolicy：统一重构前 4 套重复的"漏写 marker 补救重试"逻辑。

重构前 `handlers._handle_chat_inner` 里有 4 段几乎相同的代码（L1211–L1318）：

    if card_enable and not result.card_actions and looks_like_card_request(intent):
        hist2 = [*history, {...user...}, {...assistant...}]
        result = await llm.chat(hist2, "你刚才只口头说改了名片，但没写 <<<CARD ...>>>，...", ...)
    # ... MUTE 同样一段
    # ... DRAW 同样一段
    # ... BROWSE 同样一段

每段 20 行、结构一致，只是触发谓词与补救 prompt 不同。

Phase 4 把它们抽象为 `RecoveryPolicy`（第三十一/三十二节）：

    ToolDefinition
        ↓
    recovery_policy: RecoveryPolicy | None
        ├ should_retry(intent_text, agent_result) -> bool
        ├ build_retry_prompt(intent_text) -> str
        └ max_retries: int

**关键约束**：补救 prompt 文本必须逐字保留（它们是调优过的），
因此本模块的 prompt 直接从 handlers 现有字面量搬运，不做改写。

Phase 4 的定位：机制建立 + 与旧逻辑并存（旧逻辑仍在 handlers 里跑）。
等验证等价后再切换，避免"行为一致"这一首要目标被破坏。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# 触发谓词：(intent_text) -> bool
IntentPredicate = Callable[[str], bool]


@dataclass
class RecoveryPolicy:
    """一个工具的补救策略。

    Attributes:
        tool_name: 对应工具名。
        intent_predicate: 判断用户是否"明确要求了该能力"。
        retry_prompt: 静态补救文本（CARD/MUTE 用）。
        prompt_builder: 动态补救 prompt 生成器 `(intent_text) -> str`。
            **DRAW/BROWSE 必须用它** —— 它们的提示词依赖用户意图
            （browse 会区分 pixiv / taobao / screenshot 并带上具体关键词）。
            这是 Phase 5.5 决策矩阵发现的真实差异：Phase 4 曾硬编码
            `browse_retry_hint("")`，导致补救提示词退化成通用版、
            丢失具体关键词，与 legacy 不一致。
        max_retries: 最多补救几次（现有实现都是 1 次）。
        requires_not_interrupt: 插嘴模式下不补救（与现有一致）。
    """

    tool_name: str
    intent_predicate: IntentPredicate
    retry_prompt: str = ""
    prompt_builder: Callable[[str], str] | None = None
    max_retries: int = 1
    requires_not_interrupt: bool = True

    def should_retry(
        self,
        *,
        intent_text: str,
        already_called: bool,
        interrupt: bool = False,
        retries_so_far: int = 0,
    ) -> bool:
        """是否应当发起补救。

        Args:
            intent_text: 用户意图文本（已去掉引用包装的纯意图）。
            already_called: 模型本轮是否已经给出该工具的 marker。
            interrupt: 是否插嘴模式。
            retries_so_far: 已补救次数。
        """
        if already_called:
            return False
        if self.requires_not_interrupt and interrupt:
            return False
        if retries_so_far >= self.max_retries:
            return False
        try:
            return bool(self.intent_predicate(intent_text or ""))
        except Exception:  # noqa: BLE001 — 谓词异常视为不补救
            return False

    def build_retry_prompt(self, intent_text: str = "") -> str:
        """生成补救 prompt。

        **优先 `prompt_builder`**（动态、意图相关），
        否则回退静态 `retry_prompt`（CARD/MUTE 的固定文本）。
        """
        if self.prompt_builder is not None:
            try:
                return self.prompt_builder(intent_text or "")
            except Exception:  # noqa: BLE001 — 生成失败时回退静态文本
                pass
        return self.retry_prompt

    def build_history(
        self,
        history: list[dict[str, Any]],
        sender: str,
        chat_text: str,
        first_reply: str,
    ) -> list[dict[str, Any]]:
        """构造补救用的历史（与重构前 hist2 的形状一致）。"""
        return [
            *history,
            {"role": "user", "content": f"{sender}：{chat_text}"},
            {"role": "assistant", "content": first_reply},
        ]


# ================================================================
# 4 套补救 prompt：**逐字**取自 handlers.py 现有字面量
# ================================================================
#
# 这些文本是经过真实群聊调优的（模型漏写 marker 时的最后一根稻草），
# 不允许润色或改写。

CARD_RETRY_PROMPT = (
    "你刚才只口头说改了名片，但没写 <<<CARD ...>>>，所以实际没改成。"
    "请重新简短回复，并在末尾补上指令行；user_id 必须从成员候选抄，"
    'kind 用 request。格式：<<<CARD {"user_id":数字,"card":"新名片","kind":"request"}>>>'
)

MUTE_RETRY_PROMPT = (
    "对方在要求禁言。请简短回复一两句（别问心事、别说对方发不了消息），"
    "并在末尾补上指令行；user_id 必须从成员候选抄（禁言我=sender 的 user_id）。"
    '格式：<<<MUTE {"user_id":数字,"duration":60,"kind":"request"}>>>'
)


def build_recovery_policies() -> dict[str, RecoveryPolicy]:
    """构造 4 套补救策略。

    DRAW / BROWSE 的补救 prompt 由各自模块的函数生成
    （`draw_retry_hint` / `browse_retry_hint`），因为它们已经存在且有测试覆盖，
    这里直接复用而不重新造字符串。
    """
    from ..browser_agent import browse_retry_hint, wants_browser_help
    from ..group_card import looks_like_card_request
    from ..group_mute import looks_like_mute_request
    from ..image_gen import draw_retry_hint, wants_senpai_image

    return {
        "card": RecoveryPolicy(
            tool_name="card",
            intent_predicate=looks_like_card_request,
            retry_prompt=CARD_RETRY_PROMPT,
        ),
        "mute": RecoveryPolicy(
            tool_name="mute",
            intent_predicate=looks_like_mute_request,
            retry_prompt=MUTE_RETRY_PROMPT,
        ),
        # DRAW/BROWSE 的补救提示词**依赖用户意图**，必须用 prompt_builder
        # 而不是提前固化的字符串（Phase 5.5 决策矩阵发现的差异）。
        "draw": RecoveryPolicy(
            tool_name="draw",
            intent_predicate=wants_senpai_image,
            prompt_builder=draw_retry_hint,
        ),
        "browse": RecoveryPolicy(
            tool_name="browse",
            intent_predicate=wants_browser_help,
            prompt_builder=browse_retry_hint,
        ),
    }


__all__ = [
    "RecoveryPolicy",
    "build_recovery_policies",
    "CARD_RETRY_PROMPT",
    "MUTE_RETRY_PROMPT",
    "IntentPredicate",
]
