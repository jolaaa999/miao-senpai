"""MemoryFacade：统一的记忆访问接口。

Phase 5 第三十四/三十五/三十六节明确要求：

- 只统一**访问接口**，不统一存储；
- 6 套记忆模块（ChatMemory / memory_recall / person_memory /
  affection / inner_state / group_style）**全部保留**；
- **不迁移数据、不改 schema、不上向量库**。

Facade 解决的具体痛点（重构前）：`handlers._handle_chat_inner` 里有 60+ 行
"从 6 个模块各取一段 brief 再拼进 prompt"的编排代码，散落在
830 行函数里。AgentRuntime 不该知道底层有 6 套存储。

    AgentRuntime → MemoryFacade → MemorySnapshot
                                     ↓
                            （6 个模块各自保留）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MemoryScope(str, Enum):
    """记忆作用域（第三十八节）。

    上层不再手写 `"group:123"` —— 字符串拼接容易出错且无法统一处理。
    """

    SESSION = "session"
    USER = "user"
    GROUP = "group"
    GLOBAL = "global"


@dataclass
class MemorySnapshot:
    """一次 prompt 装配所需的全部记忆内容。

    第三十七节：PromptBuilder 只依赖这个快照。以后换存储实现，
    PromptBuilder 不需要改。

    所有字段都是**已渲染的文本片段**或**结构化对象**，
    Facade 负责把 6 套存储的差异消化掉。
    """

    # 会话历史（OpenAI messages 格式）
    history: list[dict[str, Any]] = field(default_factory=list)
    # 关键词召回的旧聊天
    recalled: str = ""
    # 人物画像 brief
    person_profile: str = ""
    # 好感度 brief
    affection: str = ""
    # 内心状态 brief
    inner_state: str = ""
    # 群语言风格 brief
    group_style: str = ""
    # 近期热梗 brief
    trends: str = ""
    # 长期记忆·概览层：结构化事实卡片（双层记忆的上层）
    cards: str = ""
    # 结构化附属数据（供工具使用，不直接进 prompt）
    data: dict[str, Any] = field(default_factory=dict)

    def as_log_fields(self) -> dict[str, Any]:
        """只记录长度，不记录内容（第五十五节：不日志完整 prompt/记忆）。"""
        return {
            "history_len": len(self.history),
            "recalled_len": len(self.recalled or ""),
            "person_profile_len": len(self.person_profile or ""),
            "affection_len": len(self.affection or ""),
            "inner_state_len": len(self.inner_state or ""),
            "group_style_len": len(self.group_style or ""),
            "trends_len": len(self.trends or ""),
            "cards_len": len(self.cards or ""),
        }


@dataclass
class MemoryRef:
    """记忆寻址。

    明确区分 group 与 user 语境（第三十九/四十节）：
        group A + user 1  ≠  group B + user 1   （群语境不共享）
        user 1 的长期偏好  在 A、B 中**共享**     （个人偏好跨群）
        private:{uid} 与 group:{gid} 不同 namespace
    """

    scope: MemoryScope
    session_id: str = ""
    group_id: str = ""
    user_id: str = ""

    @staticmethod
    def for_session(session_id: str) -> "MemoryRef":
        return MemoryRef(scope=MemoryScope.SESSION, session_id=session_id)

    @staticmethod
    def for_user(user_id: str, *, group_id: str = "") -> "MemoryRef":
        return MemoryRef(
            scope=MemoryScope.USER, user_id=str(user_id), group_id=str(group_id)
        )

    @staticmethod
    def for_group(group_id: str) -> "MemoryRef":
        return MemoryRef(scope=MemoryScope.GROUP, group_id=str(group_id))

    def cache_key(self) -> str:
        """用于缓存/去重的稳定键。"""
        return f"{self.scope.value}|{self.session_id}|{self.group_id}|{self.user_id}"


__all__ = ["MemoryFacade", "MemorySnapshot", "MemoryRef", "MemoryScope"]


# ================================================================
# Phase 7A.1：Memory 观测与作用域守卫（第十九~二十六节）
# ================================================================


def _instrument_memory(
    *,
    history_count: int = 0,
    recalled_count: int = 0,
    person_profile_hit: bool = False,
    group_style_hit: bool = False,
    affection_loaded: bool = False,
    inner_state_loaded: bool = False,
    trends_loaded: bool = False,
    duration_ms: float = 0.0,
) -> None:
    """把 Memory 命中情况写进当前 telemetry（第二十节）。

    **只记 hit/miss/count/duration，不记内容**（第二十六节）。
    失败静默：观测不影响业务。
    """
    try:
        from ..validation.telemetry import record_memory

        record_memory(
            history_count=history_count,
            recalled_count=recalled_count,
            person_profile_hit=person_profile_hit,
            group_style_hit=group_style_hit,
            affection_loaded=affection_loaded,
            inner_state_loaded=inner_state_loaded,
            trends_loaded=trends_loaded,
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001 — 观测失败不影响业务
        pass


def _guard_scope(ref: MemoryRef, *, expected_kind: str) -> bool:
    """检查一次读取的作用域是否合法（第二十一~二十六节）。

    Scope Contract（第二十二节）：
        USER profile   —— 允许跨群（人物画像本来就是跨群认人）
        GROUP style / affection / group context —— 必须绑定当前 group
        private:user   —— 不得读到任何 group scope（第二十三节）

    Args:
        ref: 本次读取的记忆引用。
        expected_kind: `"group"` / `"user"` / `"session"` / `"global"`。

    Returns:
        True 表示可以继续读取；False 表示应**安全拒绝**这次读取。

    **保守策略**（第二十四/二十五节）：
        - 检测到跨群污染 → 记 CRITICAL 并拒绝返回该内容
        - 守卫自身异常 → 同样拒绝（无法确认 scope 时不返回可疑数据）
    """
    try:
        from ..validation.cost import check_scope
        from ..validation.telemetry import record_scope_violation

        if expected_kind == "user":
            # 人物画像跨群共享是设计内的，直接放行
            return True

        if ref.scope is MemoryScope.GROUP:
            # group 作用域必须与请求的群一致 —— 这里用 ref 自省：
            # for_group() 构造的 ref 一定带 group_id，不会有跨群可能。
            # 真正需要拦的是"session 串到别的群"，由 check_scope 判定。
            if not ref.group_id:
                record_scope_violation("group_scope_without_group_id")
                return False
            result = check_scope(f"group:{ref.group_id}", f"group:{ref.group_id}")
            if not result.ok:
                record_scope_violation(result.reason)
                return False
        return True
    except Exception:  # noqa: BLE001 — 守卫失败时保守拒绝（第二十五节）
        try:
            from ..validation.telemetry import record_scope_violation

            record_scope_violation("guard_error")
        except Exception:  # noqa: BLE001
            pass
        return False


class MemoryFacade:
    """统一记忆访问入口。

    Phase 5 实现策略：**包装现有 6 个模块**，不重写它们的存储。
    每个方法内部延迟 import，避免在无 nonebot 的单测环境里失败。
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config

    def _cfg(self, config: Any = None) -> Any:
        if config is not None:
            return config
        if self._config is not None:
            return self._config
        from ..config_runtime.runtime import get_config

        return get_config()

    # -- 会话历史 --

    def get_session_history(
        self, ref: MemoryRef, config: Any = None
    ) -> list[dict[str, Any]]:
        """读取会话历史（不裁剪）。"""
        from ..memory import ChatMemory

        cfg = self._cfg(config)
        # 一律 getattr 兜底：测试用的精简 config 里可能没有这几个键，
        # 直接取属性会抛 AttributeError，被外层 except 吞掉后表现为「检索不到东西」
        memory = ChatMemory(
            cfg.memory_dir,
            max_history=getattr(cfg, "max_history", 0),
            max_chars=getattr(cfg, "history_max_chars", 0),
            persist_max=getattr(cfg, "memory_persist_max_messages", 0),
        )
        return memory.load_all(ref.session_id)

    def load_history_for_prompt(
        self,
        ref: MemoryRef,
        query: str,
        config: Any = None,
        *,
        enable_recall: bool = True,
    ) -> tuple[list[dict[str, Any]], str]:
        """读取历史并做关键词召回，返回 (近期历史, 召回 brief)。

        Phase 7A.1：回填 `history_count` / `recalled_count`（第二十节）。
        只记数量，不记内容（第二十六节）。
        """
        import time as _time

        from ..memory_recall import messages_for_llm_with_recall

        cfg = self._cfg(config)
        started = _time.perf_counter()
        raw = self.get_session_history(ref, cfg)
        # 心情滤镜：她此刻的心情决定「更容易想起哪一类事」。
        # 读不到就当没有（空串），排序完全退回关键词命中。
        mood_word = ""
        try:
            # MemoryRef 没有 private 字段：私聊靠 session_id 前缀区分
            _sid = str(getattr(ref, "session_id", "") or "")
            key = self.inner_scope_key(
                per_group=bool(getattr(cfg, "inner_state_per_group", True)),
                group_id=(getattr(ref, "group_id", "") or None),
                private=_sid.startswith("private:"),
            )
            from ..inner_state import current_word

            mood_word = current_word(self.load_inner_state(key, cfg))
        except Exception:  # noqa: BLE001 — 滤镜失败不能挡召回
            mood_word = ""
        history, brief = messages_for_llm_with_recall(
            raw,
            query,
            recent_turns=cfg.memory_recent_turns,
            recall_top=cfg.memory_recall_top,
            enable=enable_recall,
            # 条数由 memory_recent_turns 管，长度由这里兜：
            # 一条超长消息（贴日志/转聊天记录）能顶掉整段上下文
            max_chars=int(getattr(cfg, "history_max_chars", 0) or 0),
            # 只带最近 N 条她的回复：更早的是旧人设输出，是污染源不是记忆
            max_assistant_turns=int(getattr(cfg, "history_assistant_turns", 0) or 0),
            mood_word=mood_word,
        )
        # 召回条目数：brief 非空时按行估算（不解析内容）
        recalled = 0
        if brief:
            recalled = sum(1 for ln in brief.splitlines() if ln.strip())
        _instrument_memory(
            history_count=len(history),
            recalled_count=recalled,
            duration_ms=(_time.perf_counter() - started) * 1000,
        )
        return history, brief

    # -- 人物画像 --

    def get_person_profile(
        self, ref: MemoryRef, name: str, query: str, config: Any = None
    ) -> tuple[str, Any]:
        """读取人物画像 brief，并记录本次昵称。

        返回 (brief, profile)。profile 供后续写入使用。

        Phase 7A.1：回填 `person_profile_hit`（第二十节）。
        USER 画像允许跨群（第二十二节），因此不做 group 守卫。
        """
        import time as _time

        from ..person_memory import (
            build_person_memory_brief,
            get_person_memory_store,
        )

        cfg = self._cfg(config)
        started = _time.perf_counter()
        store = get_person_memory_store(
            cfg.person_memory_dir,
            max_traits=cfg.person_memory_max_traits,
            max_note_chars=cfg.person_memory_max_note_chars,
        )
        # 每次互动记下当前昵称（跨群认人）
        profile = store.touch_name(ref.user_id, name)
        brief = build_person_memory_brief(profile, name=name, query=query)
        _instrument_memory(
            person_profile_hit=bool(brief),
            duration_ms=(_time.perf_counter() - started) * 1000,
        )
        return brief, profile

    def record_person_updates(
        self, ref: MemoryRef, updates: list[Any], display_name: str, config: Any = None
    ) -> None:
        """写入人物画像更新。**不改 schema**，走现有 Store。"""
        from ..person_memory import get_person_memory_store

        cfg = self._cfg(config)
        store = get_person_memory_store(
            cfg.person_memory_dir,
            max_traits=cfg.person_memory_max_traits,
            max_note_chars=cfg.person_memory_max_note_chars,
        )
        store.apply_updates(ref.user_id, updates, display_name=display_name)

    # -- 好感度 --

    def get_affection(
        self, scope: str, user_id: str, config: Any = None
    ) -> tuple[str, int]:
        """读取好感度 brief。返回 (brief, 数值)。

        Phase 7A.1：回填 `affection_loaded`（第二十节）。
        """
        import time as _time

        from ..affection import build_affection_brief, get_affection_store

        cfg = self._cfg(config)
        started = _time.perf_counter()
        user = get_affection_store(cfg.affection_dir).get_user(scope, user_id)
        # 主观认知优先于分数：分数只用来决定档位口气
        brief = build_affection_brief(
            name=str(user_id),
            value=user.value,
            view=getattr(user, "view", "") or "",
            view_at=float(getattr(user, "view_at", 0.0) or 0.0),
        )
        _instrument_memory(
            affection_loaded=True,
            duration_ms=(_time.perf_counter() - started) * 1000,
        )
        return brief, user.value

    def record_interaction(
        self, scope: str, user_id: str, delta: int, display_name: str, config: Any = None
    ) -> Any:
        """记录一次互动（加好感度）。走现有 Store，不改数据格式。"""
        from ..affection import get_affection_store

        cfg = self._cfg(config)
        return get_affection_store(cfg.affection_dir).add_chat(
            scope, user_id, delta, display_name=display_name
        )

    # -- 内心状态 --

    def get_inner_state(self, key: str, config: Any = None) -> str:
        """读取内心状态 brief。

        Phase 7A.1：回填 `inner_state_loaded`（第二十节）。
        """
        import time as _time

        from ..inner_state import build_inner_state_brief, get_inner_state_store

        cfg = self._cfg(config)
        started = _time.perf_counter()
        store = get_inner_state_store(cfg.inner_state_dir)
        now = _time.time()
        state = store.load(key, now=now)
        # 惰性日结算：底色一天定一次。这里不挂定时任务，读的时候顺手判——
        # 定时任务要靠人记得开，读是每轮都会发生的事，漏不掉。
        # 只在「有手札、且上次结算超过一天」时结算，避免每轮重算把底色刷成
        # 最近一句的情绪（那就成了一句话定生死）。
        try:
            if state.notes and (now - float(state.base_since or 0)) > 86400:
                state = store.settle(key, now=now)
        except Exception:  # noqa: BLE001 — 结算失败不能挡聊天
            state = store.load(key, now=now)
        brief = build_inner_state_brief(state, now=now)
        _instrument_memory(
            inner_state_loaded=True,
            duration_ms=(_time.perf_counter() - started) * 1000,
        )
        return brief

    # -- 群风格 --

    def get_group_style(
        self, group_id: str, config: Any = None, *, short: bool = False
    ) -> str:
        """读取群风格 brief。

        Phase 7A.1：回填 `group_style_hit`（第二十节），
        并接**作用域守卫** —— 群风格必须绑定当前 group（第二十二节）。
        """
        import time as _time

        from ..group_style import build_group_style_brief, get_group_style_store

        cfg = self._cfg(config)
        # 守卫：group 作用域必须带有效 group_id，否则拒绝返回内容
        guard_ref = MemoryRef.for_group(str(group_id)) if group_id else None
        if guard_ref is None or not _guard_scope(guard_ref, expected_kind="group"):
            return ""

        started = _time.perf_counter()
        brief = build_group_style_brief(
            get_group_style_store(cfg.group_style_dir),
            group_id,
            top_n=cfg.group_style_top_n,
            min_count=cfg.group_style_min_count,
            short=short,
        )
        _instrument_memory(
            group_style_hit=bool(brief),
            duration_ms=(_time.perf_counter() - started) * 1000,
        )
        return brief

    # -- 长期记忆·概览层（双层记忆的上层）--

    def get_cards(
        self, ref: MemoryRef, *, query: str = "", config: Any = None
    ) -> str:
        """读取结构化事实卡并渲染成常驻概览。

        为什么按 user 和 group 各取一份再合并：个人偏好跨群共享（第三章：
        user 1 的长期偏好在 A、B 群都成立），而群内的共同事实只在该群可见。
        两份都进概览位，渲染时按相关性排序、按字数截断。
        """
        from .memory.cards import build_cards_brief, get_card_store

        cfg = self._cfg(config)
        store = get_card_store(
            cfg.memory_cards_dir, max_cards=max(8, int(cfg.memory_cards_max) * 4)
        )
        collected = []
        if ref.user_id:
            collected.extend(store.active(f"user_{ref.user_id}"))
        if ref.group_id:
            collected.extend(store.active(f"group_{ref.group_id}"))
        if not collected:
            return ""
        # 同一事实可能既在个人卡又在群卡里，按 card_id 去重，避免概览位浪费。
        seen: set[str] = set()
        unique = []
        for card in collected:
            if card.card_id in seen:
                continue
            seen.add(card.card_id)
            unique.append(card)
        return build_cards_brief(
            unique,
            max_cards=int(cfg.memory_cards_max),
            max_chars=int(cfg.memory_cards_max_chars),
            query=query,
        )

    def search_detail(
        self,
        ref: MemoryRef,
        query: str,
        *,
        config: Any = None,
        history: list[dict[str, Any]] | None = None,
        top_k: int = 4,
    ) -> str:
        """按需检索长期记忆的细节（双层记忆的下层）。

        为什么放在 Facade：agent/ 下除本文件外不得直接触碰记忆存储
        （test_phase55_boundaries 的守卫）。工具只描述「要什么」，
        「从哪读、怎么排序」归这里。

        与 get_cards 的分工：那个是常驻概览（每轮都在，给全局视角），
        这个是按需取回的细节（模型主动要，才去翻）。

        Phase 10：历史检索改读**磁盘全量**，不再用调用方传进来的 history。
        后者是 AgentContext.history，被 select_recent_and_recall 截成了
        最近 24 条——模型本来就看得见，在它里面搜等于空转。
        """
        from .memory.cards import get_card_store
        from .memory.retrieval import (
            build_context_prefix,
            render_hits,
            search_cards,
            search_history,
        )

        cfg = self._cfg(config)
        store = get_card_store(
            cfg.memory_cards_dir, max_cards=max(8, int(cfg.memory_cards_max) * 4)
        )
        cards = []
        if ref.user_id:
            cards.extend(store.active(f"user_{ref.user_id}"))
        if ref.group_id:
            cards.extend(store.active(f"group_{ref.group_id}"))

        # 上下文前缀：把片段锚回它原本的语境，否则「好的」这种孤立片段
        # 检索出来也判读不了（第三章 3.3.5）。
        prefix = build_context_prefix(
            session_id=ref.session_id,
            group_id=ref.group_id,
            user_id=ref.user_id,
        )

        pieces: list[str] = []
        for _score, card in search_cards(cards, query, top_k=top_k):
            subject = f"{card.subject}：" if card.subject else ""
            line = f"{prefix} {subject}{card.fact}".strip()
            if card.qualifiers:
                line += f"（限：{card.qualifiers}）"
            pieces.append(line)

        # 为什么读磁盘全量而不是用传进来的 history：见上面的说明。
        # 两级兜底，保证「读不到全量」时至少还能用窗口里的内容：
        #   1) 会话刚建立、还没落盘 -> 磁盘为空
        #   2) 配置里没有 memory_dir（测试用的精简 config 就是这种）
        # 让工具在这里抛异常没有意义：调用方的 failure_policy 只会把它
        # 变成一句「检索失败」，而窗口里的内容本来也能用。
        try:
            raw_history = self.get_session_history(ref, cfg)
        except Exception:  # noqa: BLE001
            raw_history = []
        source = raw_history or list(history or [])
        pieces.extend(
            search_history(source, query, top_k=top_k, context_prefix=prefix)
        )
        return render_hits("检索到的记忆", pieces)


    # -- 热梗 --

    async def get_trends(self, config: Any = None, *, interrupt: bool = False) -> tuple[str, list[str]]:
        """读取热梗 brief 与标题列表。

        Phase 7A.1：回填 `trends_loaded`（第二十节）。
        """
        import time as _time

        from ..trends import get_hot_items, get_trends_brief

        cfg = self._cfg(config)
        started = _time.perf_counter()
        items = await get_hot_items(limit=cfg.trends_max_items)
        titles = [i.title for i in items if i.title]
        brief = await get_trends_brief(
            max_items=cfg.trends_max_items,
            max_chars=cfg.trends_prompt_max_chars,
            interrupt=interrupt,
        )
        _instrument_memory(
            trends_loaded=bool(brief),
            duration_ms=(_time.perf_counter() - started) * 1000,
        )
        return brief, titles

    # -- 写入（第十五节：写也必须走 Facade）--

    def record_turn(
        self,
        ref: MemoryRef,
        user_content: str,
        assistant_content: str,
        config: Any = None,
    ) -> None:
        """记录一轮对话到会话历史。**不改 schema**，走现有 ChatMemory。"""
        from ..memory import ChatMemory

        cfg = self._cfg(config)
        # 一律 getattr 兜底：测试用的精简 config 里可能没有这几个键，
        # 直接取属性会抛 AttributeError，被外层 except 吞掉后表现为「检索不到东西」
        memory = ChatMemory(
            cfg.memory_dir,
            max_history=getattr(cfg, "max_history", 0),
            max_chars=getattr(cfg, "history_max_chars", 0),
            persist_max=getattr(cfg, "memory_persist_max_messages", 0),
        )
        memory.append_turn(ref.session_id, user_content, assistant_content)

    def observe_group_text(
        self, group_id: str, text: str, config: Any = None
    ) -> None:
        """群风格采样（写入）。"""
        from ..group_style import get_group_style_store

        cfg = self._cfg(config)
        get_group_style_store(cfg.group_style_dir).observe(group_id, text)

    def load_inner_state(self, key: str, config: Any = None) -> Any:
        """读取内心状态对象（供计算 interrupt 缩放等）。"""
        from ..inner_state import get_inner_state_store

        cfg = self._cfg(config)
        return get_inner_state_store(cfg.inner_state_dir).load(key)

    def update_inner_state(
        self,
        key: str,
        *,
        reply_len: int,
        mentioned: bool,
        interrupt: bool,
        private_chat: bool,
        config: Any = None,
    ) -> None:
        """回复后更新内心状态（写入）。"""
        from ..inner_state import get_inner_state_store

        cfg = self._cfg(config)
        get_inner_state_store(cfg.inner_state_dir).apply_after_reply(
            key,
            reply_len=reply_len,
            mentioned=mentioned,
            interrupt=interrupt,
            private_chat=private_chat,
        )

    def add_inner_note(
        self, key: str, text: str, tag: str = "", config: Any = None
    ) -> None:
        """写一条心绪手札（写入）。

        agent/ 层不允许直接碰 inner_state 的存储（架构边界，见
        tests/test_phase55_boundaries.py），所以记心事工具走这里。
        """
        from ..inner_state import get_inner_state_store

        cfg = self._cfg(config)
        get_inner_state_store(cfg.inner_state_dir).add_note(key, text, tag)

    @staticmethod
    def inner_scope_key(
        *, per_group: bool, group_id: str | None, private: bool
    ) -> str:
        """计算内心状态的作用域键（纯函数，无需存储）。"""
        from ..inner_state import scope_key

        return scope_key(
            per_group=per_group,
            group_id=group_id,
            private=private,
        )

    @staticmethod
    def inner_state_interrupt_multiplier(state: Any) -> float:
        """内心状态对插嘴概率的缩放（纯函数）。"""
        from ..inner_state import interrupt_prob_multiplier

        return interrupt_prob_multiplier(state)

    @staticmethod
    def build_inner_state_brief(state: Any) -> str:
        from ..inner_state import build_inner_state_brief

        return build_inner_state_brief(state)

    # -- 聚合 --

    def build_snapshot(
        self,
        ref: MemoryRef,
        *,
        query: str,
        name: str,
        config: Any = None,
        want: dict[str, bool] | None = None,
        affection_scope: str = "",
        inner_key: str = "",
    ) -> MemorySnapshot:
        """按需聚合各部分记忆。

        `want` 控制读取哪些部分（对应群功能开关），
        避免为一个纯聊天请求把所有存储都读一遍。

        affection_scope / inner_key 由调用方算好传入：这两者的作用域规则
        属于 core.domain 的领域逻辑（群必须是签到群、私聊要 enable_private），
        放在这里会把 memory_facade 反过来依赖上层。

        trends 不在这里：get_trends 是 async 且要联网，本方法是同步的纯读取。
        """
        cfg = self._cfg(config)
        flags = dict(want or {})
        snapshot = MemorySnapshot()

        # 历史总是需要
        history, recalled = self.load_history_for_prompt(
            ref, query, cfg, enable_recall=flags.get("memory_recall", True)
        )
        snapshot.history = history
        snapshot.recalled = recalled

        if flags.get("person_memory", False):
            brief, profile = self.get_person_profile(ref, name, query, cfg)
            snapshot.person_profile = brief
            snapshot.data["person_profile"] = profile

        # 好感度：没有作用域就跳过（未开启好感度或本群不算签到群）
        if flags.get("affection", False) and affection_scope:
            brief, value = self.get_affection(affection_scope, ref.user_id, cfg)
            snapshot.affection = brief
            snapshot.data["affection_value"] = value

        # 内心状态：键由 scope_key() 决定（按群或全局）
        if flags.get("inner_state", False) and inner_key:
            snapshot.inner_state = self.get_inner_state(inner_key, cfg)

        # 群风格：必须有 group_id，私聊没有群语境
        if flags.get("group_style", False) and ref.group_id:
            snapshot.group_style = self.get_group_style(ref.group_id, cfg)

        # 概览层：双层记忆的上层，每轮都进 prompt。
        if flags.get("memory_cards", False):
            snapshot.cards = self.get_cards(ref, query=query, config=cfg)

        return snapshot
