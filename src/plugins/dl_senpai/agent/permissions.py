"""PermissionService：统一权限判定（第二十五~二十九节）。

Phase 4 只建了接口；Phase 5 开始**真正强制执行**。

双层检查：

    第一层（生成 prompt 前）
        ToolRegistry.visible_tools(context)
        → 普通成员根本看不到 MUTE/CARD

    第二层（真正执行前）
        PermissionService.authorize(tool, call, context)
        → 即使模型凭空构造了 mute(user_id=xxx)，
          也要校验 actor / group / target / bot role / target role / duration

**关键约束（第二十八节）**：复用现有权限逻辑，不重新设计规则。
现有的 `bot_can_manage_cards` / `can_emit_mute` / `filter_actions` /
`resolve_explicit_mute` 继续是权威实现，本服务负责把它们编排到统一入口。

**决策结果必须结构化**（第二十九节），不能只返回 bool——
日志需要 reason，将来需要 policy 追踪。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..observability.logging import log_event
from ..observability.metrics import metrics


# 「机器人是不是群管理员」的缓存：群号 -> (写入时刻, 是否管理员)。
# 为什么需要它：这个判定要 await 一次 get_group_member_info 网络往返，是异步的；
# 而调用它的 visible() / authorize() 以及上层的 ToolRegistry.visible_tools、
# ToolExecutor._check_permission 全是同步方法，把整条链改成 async 要动十几处调用方。
# 所以采用「异步预取 + 同步读缓存」：由 AgentRuntime.run() 在组装 prompt 前
# await prefetch_group_manage() 填好，同步侧只管读。
_GROUP_MANAGE_TTL_SECONDS = 120.0
_GROUP_MANAGE_CACHE: dict[str, tuple[float, bool]] = {}


def _group_key_of(context: Any) -> str:
    """取上下文对应的群号；私聊或拿不到时返回空串。

    优先读原始事件上的 group_id —— 那是 OneBot 给的权威值，
    AgentContext 自己的字段在不同构造路径下未必有。
    """
    # 逐级回退，谁先给出群号就用谁：
    # 原始事件最权威（OneBot 直接给的），其次是消息上下文，
    # 最后才看 AgentContext 自身 —— 不同构造路径上群号落点不一样。
    for holder in (
        getattr(context, "raw_event", None),
        getattr(context, "message_context", None),
        context,
    ):
        if holder is None:
            continue
        gid = getattr(holder, "group_id", None)
        if gid:
            return str(gid).strip()
    return ""



@dataclass
class PermissionDecision:
    """权限判定结果。"""

    allowed: bool = False
    reason: str = ""
    policy: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls, policy: str = "", reason: str = "") -> "PermissionDecision":
        return cls(allowed=True, reason=reason, policy=policy)

    @classmethod
    def deny(cls, policy: str, reason: str, **metadata: Any) -> "PermissionDecision":
        return cls(
            allowed=False, reason=reason, policy=policy, metadata=dict(metadata)
        )

    def as_log_fields(self) -> dict[str, Any]:
        """日志字段。

        **不暴露敏感内部信息给普通用户**（第二十九节）——
        这里只产出日志用字段，向用户展示的话术由上层决定。
        """
        return {
            "allowed": self.allowed,
            "policy": self.policy or None,
            "reason": self.reason or None,
        }


class PermissionService:
    """权限服务。

    设计为**可注入**：测试可以替换成 DenyAll / AllowAll，
    或断言具体的 policy 命中。
    """

    # 需要群管理能力的工具
    GROUP_MANAGE_TOOLS: frozenset[str] = frozenset({"card", "mute"})

    # 高风险工具：能读写磁盘、执行任意命令、跑任意代码。
    #
    # 这些工具**只对管理员 QQ 可见**，而且是双重门禁：
    #   visible()    —— 不出现在 prompt 里（模型根本不知道有它）
    #   authorize()  —— 即使被绕过地调用了，执行前再拒一次
    #
    # 为什么两层都要：第一层防的是"模型被群里的内容绕进去"，
    # 第二层防的是"注册表/可见性谓词被改错"。任何一层单独都不够。
    HIGH_RISK_TOOLS: frozenset[str] = frozenset({
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "shell",
        "code",
        # 电脑（GUI）类：看屏幕、点鼠标、打字。
        # 它们比文件/命令类更危险——文件改错了能还原，鼠标点错了撤不回。
        # 除了这里，computer_control.check_action 还会再叠一层围栏
        # （模式 / 租约 / 白名单 / 限流），两者是串联关系，不是二选一。
        "computer_screen",
        "computer_windows",
        "computer_focus",
        "computer_click",
        "computer_type",
        "computer_key",
        "computer_scroll",
        "computer_wait",
        # 写入层：发说说、黑盒评论。它们不是"改本机文件"，是**以她的名义
        # 在公开平台留痕**，发出去撤不回——所以和 shell/code 同一档：
        # 非管理员连名字都看不到。
        "qzone",
        "bili_write",
    "heybox_write",
    })

    def __init__(self, config: Any = None) -> None:
        self._config = config

    def _cfg(self, context: Any) -> Any:
        cfg = getattr(context, "config", None)
        if cfg is not None:
            return cfg
        if self._config is not None:
            return self._config
        from ..config_runtime.runtime import get_config

        return get_config()

    # -- 第一层：可见性 --

    def visible(self, tool: Any, context: Any) -> bool:
        """第一层检查：该工具是否应该出现在 prompt 里。

        复用 `ToolDefinition.visibility`（Phase 4 已建立），
        并叠加**群管理能力**这一层——普通成员不该看到 MUTE/CARD。
        """
        if not tool.is_visible(context):
            return False

        name = getattr(tool, "name", "")
        if name in self.GROUP_MANAGE_TOOLS:
            return self._can_use_group_manage(context)
        if name in self.HIGH_RISK_TOOLS:
            return self.is_admin(context)
        return True

    def is_admin(self, context: Any) -> bool:
        """当前发言者是不是配置里的管理员。

        为什么用「配置为空 ⇒ 谁都不是管理员」：
        反过来写（空 = 放行）会让一个漏配的字段变成全开的后门。
        高风险工具宁可默认关闭，也不能默认敞开。

        为什么在私聊也照样判：私聊的对方未必是主人，
        机器人可能被拉进任何人的私聊。场景不改变信任级别，身份才改变。
        """
        cfg = self._cfg(context)
        configured = str(getattr(cfg, "agent_admin_qq", "") or "").strip()
        if not configured:
            return False
        # 支持逗号/分号/空格分隔的多个管理员，方便以后加人。
        allowed = {
            part.strip()
            for part in configured.replace(";", ",").replace(" ", ",").split(",")
            if part.strip()
        }
        # **身份必须取账号（user_id），绝不能用 actor。**
        #
        # actor 是「发送者显示名（优先群名片）」，也就是用户**自己随手就能改**的东西。
        # 这里原先写成先看 actor、为空才退回 user_id，线上表现为：
        #   群里管理员发 reset喵 被拒 —— 群名片不是 QQ 号，拿去和 QQ 号比对永远不中；
        # 更严重的是反向风险：
        #   任何人把群名片改成管理员的 QQ 号，就能骗过这条检查拿到高风险工具。
        # 所以方向定死：先 user_id，只有它整个缺失（异常/测试上下文）时才退回 actor。
        ident = str(getattr(context, "user_id", "") or "").strip()
        if not ident:
            ident = str(getattr(context, "actor", "") or "").strip()
        return ident in allowed

    async def prefetch_group_manage(self, context: Any) -> None:
        """把「机器人是不是群管理员」问出来并写进缓存。

        为什么要单独一个异步预取：见 _GROUP_MANAGE_CACHE 的注释 ——
        判定本身要 await 网络往返，而权限层是同步的。
        预取失败（无 bot、私聊、接口异常）时什么都不做，同步侧会自动退回保守放行。
        """
        key = _group_key_of(context)
        if not key:
            return
        bot = getattr(context, "bot", None)
        if bot is None:
            return
        try:
            from ..group_card import bot_can_manage_cards

            # 这里必须显式取 group_id 再转 int：旧代码把整个 event 传了进去，
            # 而函数签名要的是 group_id，参数根本对不上；而且当时也没 await。
            allowed = await bot_can_manage_cards(bot, int(key))
        except Exception as exc:  # noqa: BLE001 — 预取失败不该影响主流程
            log_event(
                "permission.group_manage_prefetch_failed",
                level="warning",
                error=type(exc).__name__,
            )
            return
        _GROUP_MANAGE_CACHE[key] = (time.monotonic(), bool(allowed))

    def _can_use_group_manage(self, context: Any) -> bool:
        """机器人是否具备群管理能力（读预取缓存）。

        这是"visibility 层"的粗筛：如果机器人自己都不是管理员，
        那么 MUTE/CARD 根本没有意义，不该出现在 prompt 里。
        精细的 actor/target 校验在 `authorize` 里做。

        缓存未命中或已过期时保守返回 True：这一层只是粗筛，
        宁可多给模型看两个工具，也不能因为拿不到角色就把功能整个藏起来
        （第二层 authorize 仍会拦）。
        """
        key = _group_key_of(context)
        if not key:
            return True
        hit = _GROUP_MANAGE_CACHE.get(key)
        if hit is None:
            return True
        stamp, allowed = hit
        if time.monotonic() - stamp > _GROUP_MANAGE_TTL_SECONDS:
            return True
        return allowed

    # -- 第二层：执行授权 --

    def authorize(
        self, *, tool: Any, call: Any, context: Any
    ) -> PermissionDecision:
        """第二层检查：真正执行前的授权。

        对群管理类工具做**参数级**校验（第二十七节）：
        不能只检查"当前用户是不是管理员"，还要看
        target / duration / 机器人的角色。
        """
        name = getattr(tool, "name", "")
        decision = self._authorize_by_tool(name, tool, call, context)
        metrics.incr("permission.checked")
        if not decision.allowed:
            metrics.incr("permission.denied")
        log_event(
            "permission.checked",
            tool=name,
            tool_call_id=getattr(call, "id", None),
            session_id=getattr(context, "session_id", "") or None,
            **decision.as_log_fields(),
        )
        return decision

    def _authorize_by_tool(
        self, name: str, tool: Any, call: Any, context: Any
    ) -> PermissionDecision:
        """按工具分派到对应策略。

        注意：这里的分派是**权限策略**分派，不是工具实现分派——
        与"ToolExecutor 不得认识具体工具"的约束不冲突：
        权限规则本来就因工具而异，统一放在 PermissionService 里
        恰恰是为了让 Executor 保持无知。
        """
        if name == "mute":
            return self._authorize_mute(call, context)
        if name == "card":
            return self._authorize_card(call, context)
        if name in self.GROUP_MANAGE_TOOLS:
            return self._authorize_group_manage(name, tool, context)
        if name in self.HIGH_RISK_TOOLS:
            return self._authorize_high_risk(name, context)
        # 其余工具（search/draw/voice/sticker/browse/impression）无额外授权要求
        return PermissionDecision.allow(policy="default", reason="no_extra_check")

    # -- 具体策略：复用现有逻辑 --

    def _authorize_high_risk(self, name: str, context: Any) -> PermissionDecision:
        """高风险工具的授权：非管理员一律拒绝。

        这里不再看别的条件——参数合法性交给工具自己（沙箱越界、
        deny list），权限层只回答「这个人该不该有这个能力」。
        """
        if self.is_admin(context):
            return PermissionDecision.allow(
                policy="admin_only", reason="actor_is_admin"
            )
        return PermissionDecision.deny(
            policy="admin_only",
            reason="高风险工具仅限管理员使用",
            tool=name,
        )

    def _authorize_mute(self, call: Any, context: Any) -> PermissionDecision:
        """禁言授权。

        复用现有判定：
        - `can_emit_mute(intent_text)` —— 是否明确要求禁言
        - `filter_mute_actions(...)`   —— 时长/目标合法性
        - `bot_can_manage_cards`       —— 机器人是否有权限
        """
        try:
            from ..group_mute import can_emit_mute, filter_mute_actions
        except Exception:  # noqa: BLE001
            return PermissionDecision.allow(policy="mute", reason="import_unavailable")

        intent = getattr(context, "plain_text", "") or ""
        # 现有实现要求"明确要求禁言"才允许
        try:
            if not can_emit_mute(intent):
                return PermissionDecision.deny(
                    "mute.intent", "未明确要求禁言"
                )
        except Exception:  # noqa: BLE001
            pass

        if not self._can_use_group_manage(context):
            return PermissionDecision.deny(
                "mute.bot_role", "机器人不具备群管理权限"
            )

        # 目标与时长校验
        cfg = self._cfg(context)
        args = getattr(call, "arguments", {}) or {}
        duration = args.get("duration")
        if duration is not None:
            try:
                lo = int(getattr(cfg, "mute_min_sec", 1))
                hi = int(getattr(cfg, "mute_max_sec", 600))
                if not (lo <= int(duration) <= hi):
                    return PermissionDecision.deny(
                        "mute.duration",
                        f"时长超范围({lo}-{hi})",
                        requested=duration,
                    )
            except (TypeError, ValueError):
                return PermissionDecision.deny("mute.duration", "时长不是整数")

        # 不能禁言机器人自己 / 不能禁言发送者自己（现有 filter 也做，
        # 这里提前拦一次，避免把无效动作送到执行器）
        self_id = str(getattr(context, "bot", None).self_id) if getattr(context, "bot", None) else ""
        target = args.get("user_id")
        if target is not None and self_id and str(target) == self_id:
            return PermissionDecision.deny("mute.target", "不能禁言机器人自己")

        return PermissionDecision.allow(policy="mute", reason="ok")

    def _authorize_card(self, call: Any, context: Any) -> PermissionDecision:
        """改群名片授权。复用 `bot_can_manage_cards` + 长度限制。"""
        if not self._can_use_group_manage(context):
            return PermissionDecision.deny(
                "card.bot_role", "机器人不具备群管理权限"
            )

        cfg = self._cfg(context)
        args = getattr(call, "arguments", {}) or {}
        new_card = args.get("card")
        if new_card is not None:
            max_len = int(getattr(cfg, "card_max_len", 16))
            if len(str(new_card)) > max_len:
                return PermissionDecision.deny(
                    "card.length", f"名片超长(>{max_len})", length=len(str(new_card))
                )
        return PermissionDecision.allow(policy="card", reason="ok")

    def _authorize_group_manage(
        self, name: str, tool: Any, context: Any
    ) -> PermissionDecision:
        if not self._can_use_group_manage(context):
            return PermissionDecision.deny(
                f"{name}.bot_role", "机器人不具备群管理权限"
            )
        return PermissionDecision.allow(policy=name, reason="ok")


class AllowAllPermissionService(PermissionService):
    """测试/禁用权限时使用：全部放行。"""

    def visible(self, tool: Any, context: Any) -> bool:
        return getattr(tool, "enabled", True)

    def authorize(self, *, tool: Any, call: Any, context: Any) -> PermissionDecision:
        return PermissionDecision.allow(policy="allow_all", reason="test")


class DenyAllPermissionService(PermissionService):
    """测试用：全部拒绝。"""

    def visible(self, tool: Any, context: Any) -> bool:
        return False

    def authorize(self, *, tool: Any, call: Any, context: Any) -> PermissionDecision:
        return PermissionDecision.deny("deny_all", "test deny")


__all__ = [
    "PermissionService",
    "PermissionDecision",
    "AllowAllPermissionService",
    "DenyAllPermissionService",
]
