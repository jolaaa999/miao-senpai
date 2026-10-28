"""拍一拍：主动戳人 + 被别人戳到时的反应。

为什么单独一个模块（2026-10-27）：

qq-bridge 把 qq_send_poke 作为独立动作交给模型，理由是「戳一下」在 QQ 里
等于一句不用打字的语气词——逗熟人、催回复、表达「我看见你了」都靠它。
学姐这边此前完全没有这个动作，更麻烦的是**连被戳都收不到**：拍一拍是
notice 事件，而 notice 只接了入群/退群两条旁路，from_onebot_event 对
poke 返回 None，整条事件被丢弃。群里有人戳她，她永远毫无反应。

三条约束，都是为了避免它变成新的骚扰源：

1. **有额度**：戳人比说话更烦人。同一个人 90 秒内不重复戳，全局每小时
   最多 8 次——比发消息的配额严得多。
2. **被戳可以只回拍、一个字都不打**：真人被戳最自然的反应就是回戳一下。
   但回拍有独立冷却，否则两边会来回戳个没完。
3. **不念出来**：不要在文字里写「我戳了你一下」，那是复述动作不是说话。
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .agent.tools.models import Continuation, ToolCall, ToolResult
from .agent.tools.registry import FailurePolicy, ToolDefinition
from .observability.logging import log_event

POKE_MARKER = "POKE"

# <<<POKE>>>（戳当前说话人）或 <<<POKE 123456789>>>（戳指定 QQ 号）
_POKE_RE = re.compile(r"<<<\s*POKE(?:\s+(\d{5,12}))?\s*>>>", re.IGNORECASE)

POKE_PROMPT_TEXT = """【拍一拍】
想戳一下对方、又不想为这点事单独说一句话，就写：

<<<POKE>>>

默认戳当前跟你说话的人；要戳别人就带上 QQ 号：<<<POKE 123456789>>>

什么时候戳：逗熟人、催他回话、表示「我看见了」、被夸之后不好意思回、
他隔了很久才冒头。跟表情包一样，**偶尔**才用——连着戳就是骚扰。

什么时候别戳：刚戳过同一个人（系统会有额度）、对方正在说正事、
群里有陌生人在吵架。戳完之后不要再补一句解释，别写「戳你一下」这种话。
"""


@dataclass
class PokeRequest:
    """一次拍一拍请求。target 为空表示戳当前说话人。"""

    target: str = ""
    force: bool = True


def parse_poke_request(raw_reply: str) -> tuple[str, list[ToolCall]]:
    """把文本里的 POKE marker 剥出来，返回 (干净文本, 调用列表)。

    与 sticker / voice 的 parser 同形状：文本协议解析完就从正文里删掉，
    否则模型写的 marker 会原样发到群里。
    """
    text = raw_reply or ""
    calls: list[ToolCall] = []
    for match in _POKE_RE.finditer(text):
        calls.append(
            ToolCall(
                tool_name="poke",
                arguments={"target": match.group(1) or ""},
                raw=match.group(0),
                marker=POKE_MARKER,
            )
        )
    cleaned = _POKE_RE.sub("", text)
    return cleaned, calls


class PokeBudget:
    """拍一拍的额度闸门（进程级单例）。

    为什么要独立于 send_guard：那个管的是「发消息」，这里是「骚扰同一个人」。
    两件事的合理频率差一个量级，混在一起会让配额逻辑变得谁也说不清。
    """

    def __init__(self) -> None:
        # session|target -> 上次戳的墙钟时间
        self._last_poke: dict[str, float] = {}
        # 每个 session 最近一小时的戳时间戳
        self._hourly: dict[str, list[float]] = {}
        # session -> 上次回拍时间
        self._last_back: dict[str, float] = {}
        self._max_keys = 2048

    def _key(self, session_id: str, target: str) -> str:
        return f"{session_id}|{target}"

    def _prune(self) -> None:
        # 上限只是防内存无限长；超出就整体丢掉，反正都是过期冷却
        if len(self._last_poke) > self._max_keys:
            self._last_poke.clear()
        if len(self._hourly) > self._max_keys:
            self._hourly.clear()
        if len(self._last_back) > self._max_keys:
            self._last_back.clear()

    def spend(
        self,
        session_id: str,
        target: str,
        *,
        now: float,
        cooldown: float,
        max_per_hour: int,
    ) -> tuple[bool, str]:
        """申请一次主动戳。返回 (是否放行, 拒绝原因)。"""
        self._prune()
        key = self._key(session_id, target)
        last = self._last_poke.get(key, 0.0)
        if cooldown > 0 and now - last < cooldown:
            return False, f"刚戳过他，{int(cooldown - (now - last))} 秒后再说"
        window = [t for t in self._hourly.get(session_id, []) if now - t < 3600]
        if max_per_hour > 0 and len(window) >= max_per_hour:
            self._hourly[session_id] = window
            return False, "这一小时戳得够多了"
        window.append(now)
        self._hourly[session_id] = window
        self._last_poke[key] = now
        return True, ""

    def allow_back(
        self,
        session_id: str,
        *,
        now: float,
        cooldown: float,
        probability: float,
        rng: Callable[[], float] = random.random,
    ) -> bool:
        """被戳之后要不要回拍。冷却 + 概率双闸门。"""
        self._prune()
        last = self._last_back.get(session_id, 0.0)
        if cooldown > 0 and now - last < cooldown:
            return False
        if probability <= 0:
            return False
        if rng() > probability:
            return False
        self._last_back[session_id] = now
        return True

    def reset(self) -> None:
        self._last_poke.clear()
        self._hourly.clear()
        self._last_back.clear()


_budget = PokeBudget()


def get_poke_budget() -> PokeBudget:
    return _budget


def reset_poke_budget() -> None:
    _budget.reset()


def session_of(*, group_id: Any = None, user_id: Any = None) -> str:
    """拼会话 id。与 recent_chat / 表情库用的是同一套写法（group:123）。"""
    gid = str(group_id or "")
    if gid and gid != "0":
        return f"group:{gid}"
    return f"private:{user_id or ''}"


async def _call_poke(bot: Any, *, group_id: str, user_id: str) -> tuple[bool, str]:
    """调 OneBot 的拍一拍接口。

    为什么要试多个名字：NapCat 各版本对拍一拍的实现不一致，有的只认
    send_poke，有的只有 group_poke / friend_poke。逐个试比「探一次就永久
    关闭」更划算——戳一下失败没人看得出来，但功能不该因此整体失效。
    """
    params: dict[str, Any] = {"user_id": int(user_id)}
    if group_id and group_id != "0":
        params["group_id"] = int(group_id)
        apis = ("send_poke", "group_poke")
    else:
        apis = ("friend_poke", "send_poke")
    last_error = ""
    for api in apis:
        try:
            await bot.call_api(api, **params)
            return True, api
        except Exception as exc:  # noqa: BLE001 — 换下一个接口名再试
            last_error = f"{type(exc).__name__}: {exc}"
    return False, last_error


async def execute_poke(context: Any, call: ToolCall) -> ToolResult:
    """执行主动戳人。额度不够就失败并让模型改用文字。"""
    started = time.perf_counter()
    cfg = getattr(context, "config", None)
    if cfg is not None and not bool(getattr(cfg, "poke_enable", True)):
        return ToolResult(
            tool_name="poke", tool_call_id=call.id, success=False,
            error="拍一拍功能关着", error_type="disabled",
        )

    bot = getattr(context, "bot", None)
    if bot is None:
        return ToolResult(
            tool_name="poke", tool_call_id=call.id, success=False,
            error="缺少 bot 上下文", error_type="no_bot",
        )

    session_id = str(getattr(context, "session_id", "") or "")
    group_id = str(getattr(context, "group_id", "") or "")
    speaker = str(getattr(context, "user_id", "") or "")
    target = str(call.arguments.get("target") or "").strip() or speaker
    if not target.isdigit():
        return ToolResult(
            tool_name="poke", tool_call_id=call.id, success=False,
            error="戳谁没写清楚（要 QQ 号）", error_type="bad_target",
        )

    self_id = str(getattr(bot, "self_id", "") or "")
    if self_id and target == self_id:
        # 模型偶尔会「戳自己」来占位，这动作在群里看着很傻
        return ToolResult(
            tool_name="poke", tool_call_id=call.id, success=False,
            error="别戳你自己", error_type="self_target",
        )

    allowed, reason = get_poke_budget().spend(
        session_id,
        target,
        now=time.time(),
        cooldown=float(getattr(cfg, "poke_cooldown_sec", 90) or 0),
        max_per_hour=int(getattr(cfg, "poke_max_per_hour", 8) or 0),
    )
    if not allowed:
        return ToolResult(
            tool_name="poke", tool_call_id=call.id, success=False,
            error=reason, error_type="quota",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    if bool(getattr(cfg, "dry_run", False)):
        log_event("poke.dry_run", session=session_id, target=target)
        return ToolResult(
            tool_name="poke", tool_call_id=call.id, success=True,
            output="（干跑：本来会戳一下）",
            data={"dry_run": True, "target": target},
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    ok, detail = await _call_poke(bot, group_id=group_id, user_id=target)
    log_event("poke.send", ok=ok, session=session_id, target=target, api=detail[:80])
    if not ok:
        return ToolResult(
            tool_name="poke", tool_call_id=call.id, success=False,
            error=detail or "拍一拍接口不可用", error_type="api_failed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    return ToolResult(
        tool_name="poke", tool_call_id=call.id, success=True,
        output="戳了",
        data={"target": target},
        duration_ms=(time.perf_counter() - started) * 1000,
    )


async def handle_poke_notice(bot: Any, event: Any) -> bool:
    """处理「有人拍了拍我」。返回 True 表示这条 notice 已被消费。

    为什么要接这条路：拍一拍是最轻的「叫你一下」。别人戳她，她一点反应
    都没有，比不理人还怪——真人被戳至少会回头看一眼。

    行为分三层：
      1. 被戳就算「被叫到」→ 写进在场状态，把她从潜水里捞出来；
      2. 冷却允许时**回拍**一下（不说话，只回动作）；
      3. 其余情况什么都不做。回文字的事交给下一轮对话，不在这里抢话。
    """
    try:
        from nonebot.adapters.onebot.v11 import PokeNotifyEvent
    except Exception:  # noqa: BLE001 — 不是 onebot v11 环境
        return False
    if not isinstance(event, PokeNotifyEvent):
        return False

    try:
        from .config import get_config

        cfg = get_config()
        self_id = str(getattr(event, "self_id", "") or "")
        target_id = str(getattr(event, "target_id", "") or "")
        poker_id = str(getattr(event, "user_id", "") or "")
        group_id = str(getattr(event, "group_id", "") or "")

        # 别人互相拍：与她无关，直接放行给别的观察者
        if self_id and target_id != self_id:
            return False
        # 自己戳自己（NapCat 某些版本会把回拍再回传一次）
        if poker_id and poker_id == self_id:
            return True

        session_id = session_of(group_id=group_id, user_id=poker_id)
        try:
            from .presence import get_presence_gate

            gate = get_presence_gate()
            gate.note_incoming(session_id)
            # 被戳 = 被叫到：潜水结束，下一轮她说话时是「在场」的
            gate.note_incoming(session_id, addressed=True)
        except Exception:  # noqa: BLE001 — 在场状态写不进去不影响回拍
            pass

        if not bool(getattr(cfg, "poke_back_enable", True)):
            return True
        if bool(getattr(cfg, "dry_run", False)):
            return True

        allowed = get_poke_budget().allow_back(
            session_id,
            now=time.time(),
            cooldown=float(getattr(cfg, "poke_back_cooldown_sec", 20) or 0),
            probability=float(getattr(cfg, "poke_back_probability", 0.65)),
        )
        if not allowed:
            return True

        await _call_poke(bot, group_id=group_id, user_id=poker_id)
        log_event("poke.back", session=session_id, target=poker_id)
        return True
    except Exception as exc:  # noqa: BLE001 — notice 处理失败不能影响别的观察者
        log_event("poke.notice_error", error=f"{type(exc).__name__}: {exc}"[:200])
        return True


def _visible_poke(ctx: Any) -> bool:
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return False
    return bool(getattr(cfg, "poke_enable", True))


def build_poke_tool() -> ToolDefinition:
    return ToolDefinition(
        name="poke",
        description="拍一拍（戳一下对方，代替一句多余的话）",
        marker=POKE_MARKER,
        prompt_text=POKE_PROMPT_TEXT,
        parser=parse_poke_request,
        executor=execute_poke,
        failure_policy=FailurePolicy.FALLBACK_TEXT,
        continuation=Continuation.SIDE_EFFECT_ONLY,
        permission="social.poke",
        visibility=_visible_poke,
        side_effect=True,
        owner="social_layer",
        # 比 sticker(50) 小：同一句里既戳又发图时，戳先执行更符合语序
        priority=48,
        timeout=8.0,
        parameter_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要戳的 QQ 号；留空表示戳当前说话人",
                }
            },
        },
    )


def register_poke_tool(registry: Any) -> None:
    registry.register(build_poke_tool())


__all__ = [
    "POKE_MARKER",
    "POKE_PROMPT_TEXT",
    "PokeBudget",
    "PokeRequest",
    "build_poke_tool",
    "execute_poke",
    "get_poke_budget",
    "handle_poke_notice",
    "parse_poke_request",
    "register_poke_tool",
    "reset_poke_budget",
    "session_of",
]
