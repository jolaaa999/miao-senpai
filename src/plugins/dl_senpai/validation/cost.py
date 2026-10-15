"""成本核算与 Memory 作用域守卫。

Phase 7A 第二十四/二十五（Memory Guard）与第二十六~二十九（Cost）节。
两个都是"小型、职责单一"的组件，因此合在一个模块里，
避免为各写一个文件而产生碎片。

**CostTracker 的关键约束（第二十七节）**：
    **不要假装知道供应商价格。**

    不同中转站/自建部署价格差异极大，写死任何数字都是错的。
    因此：未配置价格时 `cost = None`（unknown），
    而不是 0 或某个猜测值。0 会被误读成"免费"，比 unknown 更危险。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable


# ================================================================
# CostTracker
# ================================================================


@dataclass
class CostTracker:
    """Token 用量与（可选的）成本核算。

    Args:
        input_price_per_million: 输入价格（每百万 token）。
            None 表示**未配置** → cost 记为 unknown，不猜价格。
        output_price_per_million: 输出价格。
    """

    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    # 累计（本阶段只观察，不做 quota —— 第二十九节）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0

    # 分维度累计（第二十八节）
    by_user: dict[str, int] = field(default_factory=dict)
    by_group: dict[str, int] = field(default_factory=dict)
    by_model: dict[str, int] = field(default_factory=dict)
    by_day: dict[str, int] = field(default_factory=dict)

    def add(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        user_hash: str = "",
        group_hash: str = "",
        model: str = "",
        when: float | None = None,
    ) -> None:
        """记一次用量。所有维度都用匿名 id。"""
        pt = int(prompt_tokens or 0)
        ct = int(completion_tokens or 0)
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.total_tokens += pt + ct
        self.requests += 1

        if user_hash:
            self.by_user[user_hash] = self.by_user.get(user_hash, 0) + pt + ct
        if group_hash:
            self.by_group[group_hash] = (
                self.by_group.get(group_hash, 0) + pt + ct
            )
        if model:
            self.by_model[model] = self.by_model.get(model, 0) + pt + ct
        day = time.strftime("%Y-%m-%d", time.localtime(when or time.time()))
        self.by_day[day] = self.by_day.get(day, 0) + pt + ct

    @property
    def pricing_known(self) -> bool:
        """是否配置了价格。**未配置时不得输出任何成本数字。**"""
        return (
            self.input_price_per_million is not None
            and self.output_price_per_million is not None
        )

    def cost(self) -> float | None:
        """累计成本。未配置价格时返回 None（unknown）。"""
        if not self.pricing_known:
            return None
        assert self.input_price_per_million is not None
        assert self.output_price_per_million is not None
        return (
            self.prompt_tokens / 1_000_000 * self.input_price_per_million
            + self.completion_tokens / 1_000_000 * self.output_price_per_million
        )

    def summary(self) -> dict[str, Any]:
        """导出统计。**成本未知时明确写 unknown**，而不是 0。"""
        cost = self.cost()
        return {
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "tokens_per_request": (
                round(self.total_tokens / self.requests, 1)
                if self.requests else 0.0
            ),
            # 第二十七节：未配置价格 → unknown
            "cost": "unknown" if cost is None else round(cost, 6),
            "currency": "USD" if cost is not None else "",
            "pricing_configured": self.pricing_known,
            "top_users": _top(self.by_user),
            "top_groups": _top(self.by_group),
            "by_model": dict(sorted(self.by_model.items())),
            "by_day": dict(sorted(self.by_day.items())),
        }


def _top(counter: dict[str, int], n: int = 10) -> dict[str, int]:
    """取用量最高的 n 项（值已用匿名 id）。"""
    return dict(sorted(counter.items(), key=lambda kv: -kv[1])[:n])


def cost_tracker_from_config(config: Any = None) -> CostTracker:
    """从配置构造 CostTracker。

    价格配置项为可选；未配置时 tracker 报 unknown。
    """
    if config is None:
        return CostTracker()
    return CostTracker(
        input_price_per_million=getattr(
            config, "validation_input_price_per_million", None
        ),
        output_price_per_million=getattr(
            config, "validation_output_price_per_million", None
        ),
    )


# ================================================================
# Memory Scope Guard（第二十四/二十五节）
# ================================================================


@dataclass
class ScopeCheck:
    """一次 Memory 作用域检查的结果。"""

    ok: bool
    requested: str = ""
    returned: str = ""
    reason: str = ""
    severity: str = "info"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "requested": self.requested,
            "returned": self.returned,
            "reason": self.reason,
            "severity": self.severity,
        }


def group_of(scope: str) -> str:
    """从 scope 串里取出群标识（`group:123` → `123`）。"""
    if not scope:
        return ""
    if scope.startswith("group:"):
        return scope.split(":", 1)[1]
    return ""


def check_scope(requested_scope: str, returned_scope: str) -> ScopeCheck:
    """检查返回的作用域是否与请求一致（第二十四/二十五节）。

    允许的情形：
        - 完全一致
        - **user 级**数据跨群共享（`user:*` 与 `group:*` 混用时不算违规，
          因为人物画像本来就是跨群认人的 —— 第二十四节明确允许）
        - `global` 数据

    违规（CRITICAL）：
        - 请求 group A，返回了 group B 的数据
          → 跨群串线，用户会看到别的群的上下文
    """
    req_group = group_of(requested_scope)
    ret_group = group_of(returned_scope)

    if not ret_group:
        # 非群作用域（user/global）—— 不算跨群污染
        return ScopeCheck(ok=True, requested=requested_scope,
                          returned=returned_scope, reason="non_group_scope")

    if not req_group:
        # 请求方不是群语境，却拿到群数据 —— 可疑但不是跨群串线
        return ScopeCheck(
            ok=False,
            requested=requested_scope,
            returned=returned_scope,
            reason="group_data_in_non_group_request",
            severity="medium",
        )

    if req_group != ret_group:
        return ScopeCheck(
            ok=False,
            requested=requested_scope,
            returned=returned_scope,
            reason="cross_group_scope_violation",
            severity="critical",
        )

    return ScopeCheck(ok=True, requested=requested_scope,
                      returned=returned_scope, reason="match")


def memory_metrics(
    *,
    history_count: int = 0,
    recalled_count: int = 0,
    person_profile_hit: bool = False,
    group_style_hit: bool = False,
    affection_loaded: bool = False,
    inner_state_loaded: bool = False,
    scope_check: ScopeCheck | None = None,
) -> dict[str, Any]:
    """构造 Memory 观测字段（第二十三节，**不含任何内容**）。"""
    return {
        "history_count": int(history_count),
        "recalled_count": int(recalled_count),
        "person_profile_hit": bool(person_profile_hit),
        "group_style_hit": bool(group_style_hit),
        "affection_loaded": bool(affection_loaded),
        "inner_state_loaded": bool(inner_state_loaded),
        "scope_violation": bool(scope_check and not scope_check.ok),
        "scope_reason": scope_check.reason if scope_check else "",
    }


__all__ = [
    "CostTracker",
    "cost_tracker_from_config",
    "ScopeCheck",
    "check_scope",
    "group_of",
    "memory_metrics",
]
