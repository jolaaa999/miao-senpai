"""AgentRuntime：Agent 执行主循环。

调用链（第二节目标形态）：

    LegacyAgentBridge
            ↓
    AgentRuntime.run(context, user_message)
            ↓
    ┌──────────────────────────────────────┐
    │ 1. ToolRegistry.visible_tools(ctx)   │  ← 第一次权限过滤
    │ 2. PromptBuilder.build(...)          │
    │ 3. ModelGateway.chat(...)            │
    │ 4. MarkerToolAdapter.parse_response  │
    │ 5. ToolExecutor.execute(...)         │  ← 第二次权限过滤
    │ 6. continuation == RETURN_TO_MODEL ? │
    │      回填结果 → 回到 3                │
    │    else → 结束                        │
    └──────────────────────────────────────┘
            ↓
    AgentResult

**硬性约束**：本模块不得出现任何具体工具名（`card` / `mute` / `draw` ...）。
所有工具差异由 ToolDefinition 携带。这一点由
`test_agent_runtime_knows_no_tools` 断言保护。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..observability.logging import log_event
from ..observability.metrics import metrics
from .context import AgentContext, AgentResult, AgentState
from .guard import AgentGuard, GuardConfig, StopReason
from .model_gateway import LegacyModelGateway, ModelGateway, ModelRequest
from .prompt.builder import PromptBuilder, PromptRequest
from .tools.executor import ToolExecutor
from .tools.marker_adapter import MarkerToolAdapter
from .tools.models import Continuation, ToolResult
from .tools.registry import ToolRegistry, get_tool_registry


class AgentRuntime:
    """执行一轮 Agent 对话。

    Args:
        registry: 工具注册表。
        gateway: 模型网关。
        executor: 工具执行器。
        prompt_builder: prompt 装配器。
        adapter: 工具调用适配器。
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        gateway: ModelGateway | None = None,
        executor: ToolExecutor | None = None,
        prompt_builder: PromptBuilder | None = None,
        adapter: MarkerToolAdapter | None = None,
        guard_config: GuardConfig | None = None,
    ) -> None:
        self._registry = registry or get_tool_registry()
        self._gateway = gateway or LegacyModelGateway()
        self._adapter = adapter or MarkerToolAdapter(self._registry)
        self._executor = executor or ToolExecutor(self._registry)
        self._prompt_builder = prompt_builder or PromptBuilder()
        self._guard_config = guard_config or GuardConfig()
        # 为什么要单独记一笔"是不是外部注入的"：
        # _resolve_guard_config 需要区分「调用方显式指定了阈值」与
        # 「没指定、应当去读 .env 配置」。而 self._guard_config 为了
        # 向后兼容始终是 GuardConfig 实例（永不为 None），
        # 单看它无法区分这两种情况。
        self._guard_config_injected = guard_config is not None

    # -- 属性（供 shadow / 测试访问） --

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def adapter(self) -> MarkerToolAdapter:
        return self._adapter

    @property
    def executor(self) -> ToolExecutor:
        return self._executor

    @property
    def prompt_builder(self) -> PromptBuilder:
        return self._prompt_builder

    # -- 主循环 --

    async def run(
        self,
        context: AgentContext,
        user_message: str = "",
        *,
        dry_run: bool = False,
        guard: AgentGuard | None = None,
        journal: Any = None,
    ) -> AgentResult:
        """执行一轮 Agent。

        Args:
            context: Agent 上下文。
            user_message: 当前用户输入（不含发送者前缀）。
            dry_run: 只规划不产生副作用（shadow / 测试用）。
            guard: 外部注入的护栏（便于测试阈值）。
            journal: 副作用台账（Phase 5）。未传入时内部新建，
                     但那样调用方拿不到，因此生产路径应由 AgentEntry 注入。
        """
        from .journal import ExecutionJournal

        if journal is None:
            journal = ExecutionJournal()
            executor = getattr(self, "_executor", None)
            if executor is not None:
                executor.set_journal(journal)

        guard = guard or AgentGuard(config=self._resolve_guard_config(context))
        state = AgentState(budget=guard)

        # 0) 先填「机器人是不是群管理员」的缓存。
        # 权限层整条链是同步的（visible_tools / _check_permission 都同步），
        # 只有在这里 await 一次，才能把异步的群角色判定带进去 ——
        # 在此之前那层粗筛是死代码：调了个协程却没 await，返回值恒为真值。
        try:
            _perm = self._registry.permission_service()
            if _perm is not None:
                await _perm.prefetch_group_manage(context)
        except Exception as exc:  # noqa: BLE001 — 预取失败不该拖垮整轮
            log_event(
                "agent.group_manage_prefetch_failed",
                level="warning",
                error=type(exc).__name__,
            )

        # 1) 可见工具（第一次权限过滤）
        visible = self._registry.visible_tools(context)

        # 2) 组装 prompt
        built = self._prompt_builder.build(
            PromptRequest(
                interrupt=context.interrupt,
                # 有图片 URL 也算带图：以前只看 extra["has_images"]，
                # 而那个键根本没人写，导致视觉提示段永远不注入。
                has_images=bool(
                    context.extra.get("image_urls")
                    or context.extra.get("has_images", False)
                ),
                private_chat=context.private_chat,
                fewshot_enable=bool(
                    getattr(context.config, "fewshot_enable", False)
                )
                if context.config is not None
                else False,
                trends_brief=context.extra.get("trends_brief", ""),
                affection_brief=context.extra.get("affection_brief", ""),
                person_memory_brief=context.extra.get("person_memory_brief", ""),
                inner_state_brief=context.extra.get("inner_state_brief", ""),
                group_style_brief=context.extra.get("group_style_brief", ""),
                memory_recall_brief=context.extra.get("memory_recall_brief", ""),
                recent_chat_brief=context.extra.get("recent_chat_brief", ""),
                memory_cards_brief=context.extra.get("memory_cards_brief", ""),
                visible_tools=visible,
            )
        )

        result = AgentResult(metadata={
            "prompt_len": len(built.text),
            "visible_tools": built.tool_names,
            "dry_run": dry_run,
            "prompt_sections": built.sections,
        })

        metrics.incr("agent.runs")
        started = guard.elapsed()

        # 3) 循环
        messages: list[dict[str, Any]] = list(context.history)
        # user_text 优先：它是 gateway 侧合成好的「引用块 + 本句」。
        # 取不到才回落到原始 plain_text（保持无引用时的老行为）。
        user_text = user_message or context.user_text or context.plain_text

        while True:
            stop = guard.check_all()
            if stop is not None:
                result.stop_reason = stop
                break

            guard.begin_step()
            state.step += 1

            # 3a) 调模型
            response = await self._call_model(context, built.text, messages, user_text)
            if response.error:
                result.stop_reason = StopReason.ERROR
                result.metadata["error"] = response.error
                metrics.incr("agent.failures")
                log_event(
                    "agent.model_error",
                    level="warning",
                    error=response.error,
                    session_id=context.session_id or None,
                )
                break

            # 3b) token 统计（provider 不返回时保持 unknown）
            self._record_usage(guard, response)

            # 3c) 解析工具调用（优先用网关回传的结构化 calls）
            parsed = self._parse_model_response(response, context)

            # 3d) 无工具调用 → 先看是否需要 recovery（漏写 marker 补救）
            if not parsed.has_calls:
                recovery = await self._try_recovery(
                    context, parsed, state, guard, result, messages, user_text
                )
                if recovery is not None:
                    # 补救产生了新的解析结果，继续本轮处理
                    parsed = recovery
                    messages = state.messages
                    user_text = ""
                    # 补救若直接给出正文且无调用 → 收尾
                    if not parsed.has_calls:
                        result.text = parsed.text
                        result.stop_reason = StopReason.FINISHED
                        break
                else:
                    result.text = parsed.text
                    result.stop_reason = (
                        StopReason.NO_TOOL_CALL if state.step == 1 else StopReason.FINISHED
                    )
                    # 检索标记（保留旧 searched 语义）
                    result.metadata["searched"] = parsed.searched or (
                        response.raw is not None
                        and bool(getattr(response.raw, "searched", False))
                    )
                    break

            # 3e) 执行工具
            #
            # **并发执行**：同一条回复里的多个 marker 之间没有数据依赖
            # （参数是模型一次性写好的，不存在"B 拿 A 的输出当输入"），
            # 因此可以一起发出去。典型收益是一轮里同时
            # READ_FILE + GLOB + GREP 时，耗时从三者相加变成取最大值。
            #
            # 但护栏判定必须**按原文顺序串行推演**，且要在动手之前全部做完：
            # check_tool_call 依赖累计计数（同工具重复上限），若先并发执行
            # 再逐个记账，同批里的两个相同调用会互相看不见对方的计数，
            # 从而绕过上限。
            needs_continuation = False
            # 只收集**本轮**的工具结果。state.tool_results 是跨轮累积的，
            # 拿它去做续跑回填会把上一轮的结果又喂一遍。
            round_results: list[ToolResult] = []
            allowed = []
            for call in parsed.calls:
                reason = guard.check_tool_call(call.fingerprint())
                if reason is not None:
                    result.stop_reason = reason
                    log_event(
                        "agent.guard_blocked",
                        level="warning",
                        reason=reason.value,
                        **call.as_log_fields(),
                    )
                    break
                allowed.append(call)
                state.record_call(call)
                # 先记账再执行：Executor 保证工具异常被隔离成 ToolResult
                # （只有 CancelledError 会向外传），所以"已受理"与"已执行"
                # 在护栏计数上等价。
                guard.record_tool_call(call.fingerprint())

            if allowed:
                executed_results = await asyncio.gather(
                    *(
                        self._executor.execute(call, context, dry_run=dry_run)
                        for call in allowed
                    )
                )
            else:
                executed_results = []

            for call, tool_result in zip(allowed, executed_results):
                state.record_result(tool_result)
                round_results.append(tool_result)
                result.tool_calls.append(call)
                result.tool_results.append(tool_result)
                metrics.incr("agent.tool_calls")

                if not tool_result.success:
                    metrics.incr("agent.tool_failures")

                # 3f) continuation 由工具声明，Runtime 不判断具体工具
                definition = self._registry.get(call.tool_name)
                if (
                    definition is not None
                    and definition.continuation is Continuation.RETURN_TO_MODEL
                    and tool_result.output
                ):
                    needs_continuation = True

            if result.stop_reason not in (StopReason.FINISHED,) and not needs_continuation:
                # 被护栏拦截且不需要继续 → 用已有文本收尾
                if result.stop_reason is not StopReason.FINISHED:
                    result.text = parsed.text
                    break

            if not needs_continuation:
                result.text = parsed.text
                result.stop_reason = StopReason.FINISHED
                break

            # 3g) 回填工具结果，继续下一轮
            messages = self._append_continuation(
                messages, user_text, response.text, round_results
            )
            user_text = ""  # 后续轮次不再把用户输入当"当前输入"
            continue

        result.steps = state.step
        result.token_usage = guard.usage
        result.metadata["duration_ms"] = round((guard.elapsed() - started) * 1000, 1)
        result.metadata["journal"] = journal
        result.metadata["side_effect_committed"] = journal.side_effect_committed
        result.metadata["fallback_allowed"] = journal.fallback_allowed

        metrics.observe("agent.duration_ms", guard.elapsed() * 1000)
        metrics.incr("agent.steps", float(state.step))
        if result.token_usage.total_tokens:
            metrics.incr("agent.tokens", float(result.token_usage.total_tokens))

        log_event(
            "agent.finished",
            session_id=context.session_id or None,
            **result.as_log_fields(),
        )
        return result

    # -- Recovery（Phase 5.5）--

    async def _try_recovery(
        self,
        context: AgentContext,
        parsed: Any,
        state: AgentState,
        guard: AgentGuard,
        result: AgentResult,
        messages: list[dict[str, Any]],
        user_text: str,
    ) -> Any:
        """模型漏写 marker 时补救一轮。

        **硬性约束（第六节）**：本方法不得出现 `if tool_name == "card"`。
        所有工具差异由 `ToolDefinition.recovery_policy` 携带。

        **Guard 计入（第五节）**：补救是一次真实的 LLM 调用，必须计入
        steps / timeout / token usage / token budget / retry_count。
        重构前它游离在护栏之外，等于免费多打一次模型。

        Returns:
            补救后重新解析的结果；未补救则返回 None。
        """
        from ..observability.metrics import metrics as _metrics
        from .recovery import RecoveryPolicy

        intent_text = str(context.extra.get("intent_text", "") or "")
        if not intent_text:
            return None

        for tool in self._registry.parseable_tools():
            policy = getattr(tool, "recovery_policy", None)
            if not isinstance(policy, RecoveryPolicy):
                continue

            already = bool(parsed.calls_for(tool.name))
            if not policy.should_retry(
                intent_text=intent_text,
                already_called=already,
                interrupt=context.interrupt,
                retries_so_far=state.retry_count,
            ):
                continue

            # 护栏：补救也要花一步与一次模型调用
            blocked = guard.check_step() or guard.check_timeout() or guard.check_budget()
            if blocked is not None:
                result.stop_reason = blocked
                log_event(
                    "agent.recovery_blocked",
                    level="warning",
                    tool=tool.name,
                    reason=blocked.value,
                )
                return None

            guard.begin_step()
            state.step += 1
            state.retry_count += 1

            _metrics.incr("agent.recovery_triggered")
            log_event(
                "agent.recovery",
                tool=tool.name,
                retry=state.retry_count,
                session_id=context.session_id or None,
            )

            retry_prompt = policy.build_retry_prompt(intent_text)
            # history 构造：与 legacy hist2 形状一致
            retry_messages = policy.build_history(
                messages,
                context.actor,
                context.extra.get("chat_text", "") or context.plain_text,
                parsed.text,
            )
            state.messages = retry_messages

            request = ModelRequest(
                messages=[
                    {"role": "system", "content": context.extra.get("system_prompt", "")},
                    *retry_messages,
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=self._max_tokens(context),
                interrupt=False,
                session_id=context.session_id,
            )
            response = await self._gateway.chat(request, context)
            if response.error:
                result.stop_reason = StopReason.MODEL_ERROR
                result.metadata["error"] = response.error
                log_event(
                    "agent.recovery_model_error",
                    level="warning",
                    tool=tool.name,
                    error=response.error,
                )
                return None

            self._record_usage(guard, response)
            return self._parse_model_response(response, context)

        return None

    # -- 内部 --

    async def _call_model(
        self,
        context: AgentContext,
        system_prompt: str,
        messages: list[dict[str, Any]],
        user_text: str,
    ) -> Any:
        """组装消息并调用网关。"""
        built_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *messages,
        ]
        if user_text:
            built_messages.append(
                {"role": "user", "content": f"{context.actor}：{user_text}"}
            )
        elif not messages:
            built_messages.append({"role": "user", "content": f"{context.actor}："})

        # 状态栏（第二章 2.6）：紧贴生成位置、以 user 消息追加，而不是写进
        # system prompt。写 system 会让 KV Cache 的静态前缀每轮失效；
        # 放末尾只失效最后一段。模型会无条件相信状态栏，所以内容必须由
        # 代码确定性算出，绝不能交给模型自己统计。
        status = context.extra.get("agent_status") or ""
        if status:
            built_messages.append({"role": "user", "content": status})

        request = ModelRequest(
            messages=built_messages,
            # 结构化字段：让网关原样使用 runtime 已经拼好的 system prompt 与
            # 历史，而不是把整包 messages 再丢进 llm.chat() 让它二次组装。
            # user_text 传**原文**（不带 actor 前缀），前缀由 llm.chat 按
            # sender_name 统一拼一次，避免出现「群友：小明：你好」双前缀。
            system_prompt=system_prompt,
            history=list(messages),
            user_text=user_text,
            image_urls=list(context.extra.get("image_urls") or []),
            max_tokens=self._max_tokens(context),
            interrupt=context.interrupt,
            session_id=context.session_id,
            tools=self._native_tool_schemas(context),
        )
        return await self._gateway.chat(request, context)

    def _native_tool_schemas(self, context: AgentContext) -> list[dict[str, Any]]:
        """原生 function calling 的 tools 参数；未启用时返回空列表。

        返回空列表 ⇒ 网关不传 tools ⇒ 整条链路走 marker 协议，
        与改造前逐字节一致。这就是这个开关的"关"形态。
        """
        cfg = context.config
        if not bool(getattr(cfg, "agent_native_tools", False)):
            return []
        try:
            from .tools.marker_adapter import NativeToolAdapter

            visible = self._registry.visible_tools(context)
            return NativeToolAdapter.tool_schemas(visible)
        except Exception as exc:  # noqa: BLE001 — schema 构造失败不该拖垮整轮
            log_event("agent.native_tools_schema_error", level="warning", error=str(exc))
            return []

    def _parse_model_response(self, response: Any, context: AgentContext) -> Any:
        """把模型响应解析成 ParseOutput。

        优先使用网关回传的结构化 tool_calls，只有它为空时才回落到解析正文。

        为什么必须优先：`SenpaiLLM._finalize_result` 会把识别到的 marker 从
        正文里剥掉再交给网关，所以 runtime 拿到的 text 里**没有** marker。
        改造前这里直接 parse_response(response.text)，导致工具从未执行过。
        """
        from .tools.marker_adapter import ParseOutput

        calls = list(getattr(response, "tool_calls", None) or [])
        if calls:
            return ParseOutput(text=getattr(response, "text", "") or "", calls=calls)
        return self._adapter.parse_response(getattr(response, "text", "") or "", context)

    def _max_tokens(self, context: AgentContext) -> int:
        cfg = context.config
        if cfg is None:
            return 200 if context.interrupt else 1600
        if context.interrupt:
            return int(getattr(cfg, "interrupt_max_tokens", 200))
        return int(getattr(cfg, "max_tokens", 1600))

    def _resolve_guard_config(self, context: AgentContext) -> GuardConfig:
        """按**当前**配置构造护栏阈值。

        为什么在每次 run() 时现算，而不是构造 AgentRuntime 时算一次：
        AgentRuntime 实例是进程内长期存活的，而 .env 支持热重载
        （config_watcher 轮询）。预算改完应当立刻生效，不必重启进程。

        显式注入的 guard_config（测试/shadow）优先，保证可复现。
        """
        if self._guard_config_injected:
            return self._guard_config

        cfg = getattr(context, "config", None)
        if cfg is None:
            return GuardConfig()
        try:
            budget = int(getattr(cfg, "agent_token_budget", 0) or 0)
            return GuardConfig(
                max_steps=max(1, int(getattr(cfg, "agent_max_steps", 4) or 4)),
                max_tool_calls=max(
                    1, int(getattr(cfg, "agent_max_tool_calls", 8) or 8)
                ),
                same_tool_repeat_limit=max(
                    1,
                    int(
                        getattr(cfg, "agent_same_tool_repeat_limit", 2) or 2
                    ),
                ),
                timeout=float(getattr(cfg, "agent_timeout", 90.0) or 0.0),
                # 0 表示不设预算；护栏里用 None 表达"不限制"
                token_budget=budget if budget > 0 else None,
            )
        except (TypeError, ValueError):
            # 配置异常不该让整轮对话崩掉，退回默认阈值
            return GuardConfig()

    def _record_usage(self, guard: AgentGuard, response: Any) -> None:
        """记录 token 用量。provider 未返回时保持 None。"""
        usage = response.usage or {}
        raw = response.raw
        if not usage and raw is not None:
            usage = getattr(raw, "usage", None) or {}
            if not isinstance(usage, dict):
                usage = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                }
        guard.record_usage(
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("total_tokens"),
        )

    def _append_continuation(
        self,
        messages: list[dict[str, Any]],
        user_text: str,
        assistant_text: str,
        results: list[ToolResult],
    ) -> list[dict[str, Any]]:
        """把**本轮全部**工具结果回填进 messages，供模型继续作答。

        为什么必须全部：模型一轮里可以同时写好几个 marker
        （比如 READ_FILE + GLOB + GREP）。那些工具都已经真的执行了，
        结果却只回填最后一个的话，模型会看不到前几个的输出，
        于是重复调用、或者凭空猜测内容——两个后果都很糟。

        每条都带工具名标签：一轮多个结果堆在一起时，
        模型需要知道哪段输出对应哪次调用。
        """
        out = list(messages)
        if user_text:
            out.append({"role": "user", "content": user_text})
        out.append({"role": "assistant", "content": assistant_text})
        for result in results:
            body = (result.output or "").strip()
            if not body:
                # 没有输出也要说一声，否则模型会以为调用丢了
                body = f"（{result.tool_name} 没有返回内容）"
            out.append(
                {"role": "user", "content": f"【{result.tool_name} 的结果】\n{body}"}
            )
        return out


__all__ = ["AgentRuntime"]
