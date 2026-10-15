from __future__ import annotations

import asyncio
import json
import time

import httpx
from dataclasses import dataclass, field
from typing import Any

from nonebot import logger
from openai import APIError, APIStatusError, AsyncOpenAI, AuthenticationError, RateLimitError

from .config import PluginConfig, get_config
from .observability.context import current_trace
from .observability.logging import log_llm_usage
from .observability.metrics import metrics
from .observability.retry import DEFAULT_RETRY_POLICY, RetryPolicy


def trace_session_id() -> str:
    """取当前 trace 的 session_id，用于 token 统计归属。无上下文返回空串。"""
    ctx = current_trace()
    return ctx.session_id if ctx is not None else ""
from .group_card import CardAction
from .group_mute import MuteAction
from .persona import build_system_prompt
from .style import polish_reply
from .vision import build_user_content
from .image_gen import DrawRequest, strip_draw_markers
from .browser_agent import BrowseRequest, strip_browse_markers, wants_browser_help
from .clock import is_asking_current_datetime
from .grounding import (
    assess_search_detail_level,
    build_grounding_fallback_reply,
    build_grounding_retry_user_message,
    build_grounding_user_hint,
    requires_strict_grounding,
    should_grounding_retry,
)
from .person_memory import ImpressionUpdate, strip_impression_markers
from .web_search import (
    find_trend_search_queries,
    grounding_prefetch,
    searcher_from_config,
    should_auto_search,
    should_skip_auto_search,
)

# 答案被判定含「没依据的专名」时最多重写几次。
# 设 2 而不是 1：只改一次经常只是换个说法接着编，复查一遍才拦得住。
_GROUNDING_MAX_RETRIES = 2

# 瞬时故障：多试几次，退避等待
_MAX_ATTEMPTS = 4
_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
# 本地小模型（如 3B LoRA）吃不消学姐默认的超长上下文 / 1600 max_tokens
# 8GB 还要跟 SoVITS 抢显存时，再砍一刀，否则会卡死几分钟无回复
_LOCAL_MAX_TOKENS = 96
_LOCAL_HISTORY_MESSAGES = 4
_LOCAL_SYSTEM_CHARS = 800
_LOCAL_TIMEOUT = 120.0


def _is_local_infer_url(url: str) -> bool:
    u = (url or "").lower()
    return any(
        x in u
        for x in (
            "127.0.0.1",
            "localhost",
            "0.0.0.0",
            "[::1]",
        )
    )


def _build_openai_client(*, api_key: str, base_url: str, timeout: float) -> AsyncOpenAI:
    """构造 AsyncOpenAI，对本地地址显式绕开系统代理。

    为什么需要：httpx 默认 trust_env=True，会读取系统代理（本机开着
    Clash 类代理时是 127.0.0.1:7897）。Windows 的「代理例外」列表只对
    WinINET 生效，Python 读不到，于是发往 http://127.0.0.1:8001 的本地
    请求也被塞进代理，代理连不上后端就回 502 Bad Gateway。

    症状极具误导性：502 看起来像上游网关故障，实际是本地服务没启动 +
    请求被代理劫持。实测同一地址「经代理」得到 502、「直连」得到
    ConnectError——后者才是真实故障形态。

    因此对本地地址信任环境开关一并关掉（trust_env=False），让连接错误
    如实暴露成 ConnectError；外网中转站仍走代理，行为不变。
    """
    if _is_local_infer_url(base_url):
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            http_client=httpx.AsyncClient(trust_env=False, timeout=timeout),
        )
    return AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)


def _trim_history_for_local(history: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or len(history) <= limit:
        return history
    return history[-limit:]


def _normalize_local_chat_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """LLaMA-Factory ChatML：可选 system 后必须严格 u/a/u/a…，并以 user 结尾。

    本地裁剪历史时容易从 assistant 半截开刀，服务端会直接 400：
    ``Only supports u/a/u/a/u...``。
    """
    if not messages:
        return messages
    out: list[dict[str, Any]] = []
    i = 0
    if messages[0].get("role") == "system":
        out.append(messages[0])
        i = 1
    while i < len(messages) and messages[i].get("role") == "assistant":
        i += 1
    expect = "user"
    for m in messages[i:]:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        if role != expect:
            if role == "user" and expect == "assistant" and out and out[-1].get("role") == "user":
                prev = out[-1]
                c1, c2 = prev.get("content"), m.get("content")
                if isinstance(c1, str) and isinstance(c2, str):
                    out[-1] = {**prev, "content": f"{c1}\n{c2}".strip()}
                continue
            # 多余的 assistant 或无法合并的乱序：跳过
            continue
        out.append(m)
        expect = "assistant" if expect == "user" else "user"
    while out and out[-1].get("role") == "assistant":
        out.pop()
    # 至少保留 system + 当前 user
    if len(out) >= 1 and out[-1].get("role") != "user":
        return messages  # 兜底：别把整轮弄没了
    return out


def _compact_local_system_prompt() -> str:
    """本地微调模型用人设长文会又慢又偏；改用短风格提示。"""
    try:
        from .speak_styles import get_speak_style_store

        s = get_speak_style_store().active()
        name = (s.name or "当前风格").strip()
        overlay = (s.system_overlay or "").strip() or "语气自然、偏口语，别客服腔。"
    except Exception:  # noqa: BLE001
        name, overlay = "当前风格", "语气自然、偏口语，别客服腔。"
    return (
        f"你正在用「{name}」的说话习惯回复。\n"
        f"{overlay}\n"
        "只回一两句口语，紧扣对方刚说的话。"
        "禁止复读自己上一句，禁止说「别忘了回复我」这类催回。"
        "不要列表、不要 Markdown、不要助手腔。"
    )


def _norm_reply_key(text: str) -> str:
    t = (text or "").strip().lower()
    for ch in " \t\r\n。.!！？?…~～、，,":
        t = t.replace(ch, "")
    # 去掉表情包/语音旁注
    if "[" in t:
        t = t.split("[", 1)[0]
    return t


def _recent_assistant_keys(messages: list[dict[str, Any]], limit: int = 4) -> set[str]:
    keys: set[str] = set()
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        k = _norm_reply_key(str(m.get("content") or ""))
        if k:
            keys.add(k)
        if len(keys) >= limit:
            break
    return keys


def _strip_echo_assistants(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉历史里连续复读的 assistant，减轻本地小模型鹦鹉效应。"""
    out: list[dict[str, Any]] = []
    prev_key = ""
    for m in messages:
        if m.get("role") == "assistant":
            k = _norm_reply_key(str(m.get("content") or ""))
            if k and k == prev_key:
                continue
            prev_key = k
        else:
            prev_key = ""
        out.append(m)
    return out


class LLMError(Exception):
    """LLM 调用失败。"""


@dataclass
class ChatResult:
    text: str
    card_actions: list[CardAction] = field(default_factory=list)
    mute_actions: list[MuteAction] = field(default_factory=list)
    sticker_query: str = ""
    sticker_force: bool = False
    voice_force: bool = False
    voice_override: str = ""
    draw_prompt: str = ""
    draw_force: bool = False
    browse_tasks: list = field(default_factory=list)
    browse_force: bool = False
    searched: bool = False
    impression_updates: list[ImpressionUpdate] = field(default_factory=list)
    # 本次回复解析出的工具调用（统一形状：ToolCall）。
    # 为什么必须回传：AgentRuntime 拿的是 ModelResponse.text，而 _finalize_result
    # 已经把 marker 从正文里剥掉了 —— 只回传 text 会导致 runtime 一个工具都解析
    # 不到（marker 模式下工具从未真正执行过）。这里把 calls 一并带出去，
    # runtime 便无需再从被清洗过的正文里"重新猜"。
    tool_calls: list[Any] = field(default_factory=list)


# 已知「非流式被上游破坏、必须走流式」的模型名片段。
# 命中就走 stream=True，否则维持原有的非流式单次请求。
#
# 现状（2026-10-11）：airelay 已下架全部 deepseek 模型，因此这个列表
# **当前不会命中任何在售模型**，下面的分支处于闲置状态。
#
# 为什么保留而不是删掉：
#   1. 这是一个真实存在的上游怪癖（stream=False 仍返回 SSE 文本），
#      中转站随时可能恢复或在别的模型上重现；
#   2. 即使不命中，`_coerce_sse_response` 也能在非流式路径上就地解析
#      裸 SSE 文本、避免 AttributeError，两者是「主动规避 + 被动兜底」
#      的关系，删掉前者只是少一层保险，不改变正确性。
_PREFER_STREAM_MODELS = ("deepseek-v4.1", "deepseek-v4-flash")

# SSE 数据行前缀，用于回退解析裸流文本
_SSE_DATA_PREFIX = "data:"


def _prefer_stream(model: str) -> bool:
    """该模型是否需要走流式才能正常对话。

    实测 airelay 的 deepseek-v4.1-flash / deepseek-v4-flash 在 stream=False 时
    仍返回 SSE 文本，SDK 解析成裸 str；只有 stream=True 才有正常 content。
    用子串匹配而非精确相等，是为了覆盖 -0731 这类日期后缀变体。
    """
    name = (model or "").lower()
    return any(x in name for x in _PREFER_STREAM_MODELS)


@dataclass
class _Completion:
    """一次模型补全的结果。

    为什么要单独建类型而不是继续返回 str：原生 function calling 的返回值是
    「content + tool_calls」两个字段，且合法响应里 content 常常是空串。只返回
    str 会丢掉 tool_calls，也会让"空回复"的判断失去依据。
    """

    text: str = ""
    # OpenAI 形状的 ChatCompletionMessageToolCall 列表，由上层转成 ToolCall
    tool_calls: list[Any] = field(default_factory=list)


@dataclass
class _StreamedResponse:
    """把流式 chunk 聚合后伪装成非流式响应。

    下游只用到两个字段：choices[0].message.content 与 usage。
    这里用属性而非真实 OpenAI 类型，是为了不引入对 SDK 内部构造函数的依赖
    （那些构造函数签名随版本变化）。
    """

    text: str
    usage: Any = None

    @property
    def choices(self) -> list[Any]:
        message = type("_Msg", (), {"content": self.text})()
        return [type("_Choice", (), {"message": message})()]


def _coerce_sse_response(resp: Any) -> Any:
    """把「本应 JSON 却返回了 SSE 文本」的响应就地解析成可用对象。

    中转站偶发 bug：stream=False 也可能吐 SSE。此时 OpenAI SDK 不报错，
    而是把整段文本当字符串返回，调用方取 .choices 会抛 AttributeError，
    于是白白跑满 4 次指数退避重试。这里提前识别并聚合，避免无谓等待。

    非字符串、或文本里解析不出任何 content 时原样返回，交由后续逻辑报错。
    """
    if not isinstance(resp, str):
        return resp
    parts: list[str] = []
    usage: Any = None
    for line in resp.splitlines():
        line = line.strip()
        if not line.startswith(_SSE_DATA_PREFIX):
            continue
        payload = line[len(_SSE_DATA_PREFIX):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("usage"):
            usage = data["usage"]
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("content") or (choice.get("message") or {}).get("content")
            if piece:
                parts.append(piece)
    text = "".join(parts).strip()
    if not text and usage is None:
        return resp
    logger.warning(
        "dl_senpai upstream returned SSE for a non-stream request; parsed locally"
    )
    return _StreamedResponse(text, usage)


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return int(code) if isinstance(code, int) else None


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    code = _status_code(exc)
    return code in _TRANSIENT_STATUS if code is not None else False


def _friendly_api_error(exc: BaseException) -> str:
    """面向群友的话术：不暴露故障/中转站/模型细节。"""
    code = _status_code(exc)
    msg = str(exc).lower()
    if isinstance(exc, RateLimitError) or code == 429:
        return "稍等一会啦，找学姐的人有点多，学姐先忙完手头这点事～"
    if code == 404 or "not found" in msg:
        return "嗯……学姐这边模型没对上，晚点再叫我一下哦～"
    if code in {500, 502, 503, 504}:
        return "稍等一会啦，学姐现在有些事情要做噢～晚点再叫我吧"
    if code == 408:
        return "嗯……学姐刚才走神了一下，你再跟我说一遍好不好？"
    return "稍等一会啦，学姐现在有点事，晚点再聊哦～"


class SenpaiLLM:
    def __init__(
        self,
        config: PluginConfig | None = None,
        *,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.config = config or get_config()
        self._client: AsyncOpenAI | None = None
        self._ollama_client: AsyncOpenAI | None = None
        self._client_fingerprint: str = ""
        # 重试策略可注入：生产用默认（60s×4，与重构前一致），
        # 测试注入 NoWaitRetryPolicy 避免真实等待
        self._retry: RetryPolicy = retry or DEFAULT_RETRY_POLICY
        # 显式注入的客户端（测试/多租户场景）。非 None 时优先生效。
        self._injected_client: Any = None

    def use_client(self, client: Any) -> None:
        """注入自定义客户端（测试专用入口）。

        比直接赋值 `_client` 更明确：不走 fingerprint 重建逻辑，
        保证被测代码不会偷偷换回真实客户端。
        """
        self._injected_client = client

    def _relay_fingerprint(self) -> str:
        return (
            f"{self.config.active_base_url()}|{self.config.active_api_key()[:8]}|"
            f"{self.config.active_model()}|{self.config.active_speak_style_id()}"
        )

    def _resolve_client(self) -> tuple[AsyncOpenAI, str]:
        """取当前生效的 (client, channel)。

        **测试注入优先**：如果 `_client` 已被外部赋值（单测直接塞 mock），
        就原样返回，不再按 fingerprint 重建。

        修复的真实缺陷：重构前 `client` property 会拿
        `_relay_fingerprint()` 与 `_client_fingerprint` 比较——单测只设了
        `_client` 没设 `_client_fingerprint`（初始为空串），于是 property
        判定"客户端过期"，**丢弃 mock 并新建了一个真实的 AsyncOpenAI**，
        接着真的发出 HTTP 请求。
        这正是 Phase 1 审计里"单测访问真实网络"的直接原因。
        """
        if self._injected_client is not None:
            channel = self.config.provider()
            model = (
                self.config.ollama_model_resolved()
                if channel == "ollama"
                else self.config.active_model()
            )
            return self._injected_client, channel, model
        if self._client is not None and self._client_fingerprint == "":
            # 兼容既有单测写法：llm._client = MagicMock()
            channel = self.config.provider()
            model = (
                self.config.ollama_model_resolved()
                if channel == "ollama"
                else self.config.active_model()
            )
            return self._client, channel, model

    @property
    def client(self) -> AsyncOpenAI:
        fp = self._relay_fingerprint()
        if self._client is None or fp != self._client_fingerprint:
            if not self.config.api_configured():
                raise LLMError(
                    polish_reply("稍等一会啦，学姐这边还没收拾好，晚点再来找我哦～")
                )
            timeout = (
                180.0
                if self.config.provider() == "ollama"
                or _is_local_infer_url(self.config.active_base_url())
                else 60.0
            )
            # 走工厂而非直接构造：本地地址需要绕开系统代理（见
            # _build_openai_client 的说明），否则本地服务没启动时
            # 会被代理变成误导性的 502。
            self._client = _build_openai_client(
                api_key=self.config.active_api_key(),
                base_url=self.config.active_base_url(),
                timeout=timeout,
            )
            self._client_fingerprint = fp
        return self._client

    def _ollama_client_cached(self) -> AsyncOpenAI:
        if self._ollama_client is None:
            if not self.config.ollama_ready():
                raise LLMError(
                    polish_reply("稍等一会啦，学姐这边还没收拾好，晚点再来找我哦～")
                )
            self._ollama_client = _build_openai_client(
                api_key=self.config.ollama_api_key_resolved(),
                base_url=self.config.ollama_base_url_resolved(),
                timeout=180.0,
            )
        return self._ollama_client

    async def chat(
        self,
        history: list[dict[str, str]],
        user_text: str,
        *,
        interrupt: bool = False,
        sender_name: str = "群友",
        image_urls: list[str] | None = None,
        card_enable: bool = False,
        card_candidates_prompt: str = "",
        mute_enable: bool = False,
        trends_brief: str = "",
        affection_brief: str = "",
        person_memory_brief: str = "",
        person_memory_enable: bool | None = None,
        trend_titles: list[str] | None = None,
        search_enable: bool | None = None,
        draw_enable: bool | None = None,
        browser_enable: bool | None = None,
        private_chat: bool = False,
        plain_user_text: str = "",
        fewshot_enable: bool | None = None,
        inner_state_brief: str = "",
        group_style_brief: str = "",
        memory_recall_brief: str = "",
        # AgentRuntime 已经把人格、工具说明、各段简报全部拼进 system prompt。
        # 传入后直接用它，不再走 build_system_prompt()——否则 runtime 的提示词
        # 会被降级成一条普通历史消息，真正生效的反而是在这里重新拼的那份
        # （而网关调它时所有简报都没传，全是空串）。
        system_prompt_override: str = "",
        # 原生 function calling 的工具 schema（OpenAI 形状）。
        # 传空 = 整条链路退回 marker 协议，请求体与改造前完全一致。
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        has_images = bool(image_urls)
        if search_enable is None:
            search_enable = bool(self.config.search_enable)
            if interrupt and not self.config.search_allow_on_interrupt:
                search_enable = False
        if draw_enable is None:
            draw_enable = bool(self.config.draw_enable)
            if interrupt and not self.config.draw_allow_on_interrupt:
                draw_enable = False
        if browser_enable is None:
            browser_enable = bool(self.config.browser_enable)
            if interrupt and not self.config.browser_allow_on_interrupt:
                browser_enable = False
        if person_memory_enable is None:
            person_memory_enable = bool(self.config.person_memory_enable)
        if fewshot_enable is None:
            fewshot_enable = bool(self.config.fewshot_enable)
        local_infer = _is_local_infer_url(self.config.active_base_url())
        if local_infer:
            # 必须在联网预取之前关掉，否则 user 里会塞一堆 junk 搜索结果
            search_enable = False
            fewshot_enable = False
            browser_enable = False
        # 插嘴默认仍注入既有印象；是否允许写入由 write_person_memory 控制
        write_person_memory = bool(person_memory_enable) and (
            not interrupt or bool(self.config.person_memory_allow_on_interrupt)
        )

        user_payload = f"{sender_name}：{user_text}"
        if (card_enable or mute_enable) and card_candidates_prompt:
            user_payload = f"{user_payload}\n\n{card_candidates_prompt}"

        searched = False
        search_focus = (plain_user_text or user_text or "").strip()
        skip_auto_search = should_skip_auto_search(search_focus)
        strict_grounding = requires_strict_grounding(search_focus) and not skip_auto_search
        browse_task_hint = ""
        if browser_enable and wants_browser_help(search_focus):
            browse_task_hint = (
                "（对方要你去网站搜图/截图：请用 <<<BROWSE pixiv/taobao/image ...>>> 执行，"
                "不要用 <<<SEARCH>>> 做文字联网搜索。）"
            )
        search_brief_text = ""
        search_detail_level = "none"
        if is_asking_current_datetime(search_focus):
            user_payload = (
                f"{user_payload}\n\n"
                "（对方在问当前日期/时间：请严格按系统提示里的【当前时间】回答，"
                "不要用你记忆里的旧日期。）"
            )
        trend_queries = (
            find_trend_search_queries(search_focus, trend_titles or [])
            if trend_titles
            else []
        )
        auto_search = should_auto_search(
            search_focus,
            aggressive=bool(self.config.search_auto_aggressive),
        )
        if search_enable and self.config.search_auto and not skip_auto_search and (
            auto_search or strict_grounding or bool(trend_queries)
        ):
            # trending 之外的时效补搜在 search_many 内部判定（问题含"最新/补丁/赛季"
            # 且整批结果无日期时，自动换词补一轮）—— 这里把原话传下去当依据。
            brief, auto_queries = await grounding_prefetch(
                search_focus,
                self.config,
                trend_titles=trend_titles or [],
                aggressive=bool(self.config.search_auto_aggressive),
            )
            if brief and auto_queries:
                logger.info(
                    f"dl_senpai auto search prefetch queries={auto_queries!r} "
                    f"strict={strict_grounding}"
                )
                searched = True
                search_brief_text = brief
                search_detail_level = assess_search_detail_level(brief)
                user_payload = (
                    f"{user_payload}\n\n{brief}\n\n"
                    "（以上为系统自动联网检索结果。请先核对里面是否有对方问的人/事/物/设定；"
                    "有则据此回答。游戏专名检索没有的禁止编；时事/常识类检索没有时可结合已有知识答，"
                    "并说明可能不是最新；不够可再写 <<<SEARCH 查询词>>> 补搜。"
                    "**注意时效**：涉及最新/版本/赛季/补丁/更新的问题，"
                    "若结果一条日期都没有、或写的是旧赛季旧版本，"
                    "必须换词再搜一轮（加年份或换「补丁说明/更新公告/官网」角度）再回答，"
                    "不要把搜到的第一条当最新。）"
                )
                grounding_hint = build_grounding_user_hint(
                    detail_level=search_detail_level,
                    strict=strict_grounding,
                )
                if grounding_hint:
                    user_payload = f"{user_payload}\n\n{grounding_hint}"
        elif strict_grounding and search_enable and not skip_auto_search:
            user_payload = (
                f"{user_payload}\n\n"
                f"{build_grounding_user_hint(detail_level='none', strict=True)}"
            )
        # 混合检索：知识库结果与联网结果放进同一次提问。
        # 与联网预取并列而不是二选一 —— 用户要的是「两种结果混合」，
        # 而 knowledge 工具要靠模型自己想起来调，指望不上。
        knowledge_brief_text = ""
        if (
            bool(getattr(self.config, "knowledge_auto", False))
            and not skip_auto_search
            and (auto_search or strict_grounding)
        ):
            from .knowledge_setup import knowledge_prefetch

            kb_brief, kb_err = await knowledge_prefetch(search_focus, self.config)
            if kb_brief:
                knowledge_brief_text = kb_brief
                user_payload = f"{user_payload}\n\n{kb_brief}"
            elif kb_err not in ("empty_query", "not_configured", "no_service"):
                logger.info(f"dl_senpai knowledge prefetch skipped: {kb_err}")

        if search_brief_text and knowledge_brief_text:
            # 两份证据可能互相矛盾。必须要求模型显式处理，
            # 否则它会挑顺手的那个讲，冲突就被静默吞掉了。
            user_payload = (
                f"{user_payload}\n\n"
                "（上面同时给了知识库结果和联网结果。两者若有冲突，以更具体、更新的为准，"
                "并把冲突如实讲出来，不要只挑一个。）"
            )
        if browse_task_hint:
            user_payload = f"{user_payload}\n\n{browse_task_hint}"

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                # 有现成的就用现成的：runtime 侧 PromptBuilder 的输出比这里
                # 重新拼的更完整（含记忆简报与注册表驱动的工具说明）。
                "content": system_prompt_override or build_system_prompt(
                    interrupt=interrupt,
                    has_images=has_images,
                    card_enable=card_enable,
                    mute_enable=mute_enable,
                    trends_brief=trends_brief,
                    affection_brief=affection_brief,
                    person_memory_brief=person_memory_brief,
                    # 功能开着就注入「如何用印象」；插嘴时额外禁止写入
                    person_memory_enable=bool(person_memory_enable),
                    search_enable=bool(search_enable),
                    draw_enable=bool(draw_enable),
                    browser_enable=bool(browser_enable),
                    private_chat=bool(private_chat),
                    fewshot_enable=bool(fewshot_enable),
                    inner_state_brief=inner_state_brief,
                    group_style_brief=group_style_brief,
                    memory_recall_brief=memory_recall_brief,
                ),
            },
            *history,
            {
                "role": "user",
                "content": build_user_content(
                    user_payload,
                    image_urls if self.config.vision_enable else None,
                ),
            },
        ]
        local_infer = _is_local_infer_url(self.config.active_base_url())
        if local_infer:
            # 本地微调：砍历史、短风格、关检索；并去掉历史里的复读 assistant
            sys_msg = messages[0]
            rest = _strip_echo_assistants(messages[1:])
            rest = _trim_history_for_local(rest, _LOCAL_HISTORY_MESSAGES)
            messages = _normalize_local_chat_messages([sys_msg, *rest])
            if messages and messages[0].get("role") == "system":
                messages[0] = {
                    **messages[0],
                    "content": _compact_local_system_prompt(),
                }
            search_enable = False
            fewshot_enable = False
            logger.info(
                f"dl_senpai local infer trim msgs={len(messages)} "
                f"max_tokens<={_LOCAL_MAX_TOKENS}"
            )
        max_tokens = (
            self.config.interrupt_max_tokens if interrupt else self.config.max_tokens
        )
        if local_infer:
            max_tokens = min(int(max_tokens), _LOCAL_MAX_TOKENS)
        if self.config.provider() == "ollama" or local_infer:
            timeout = 240.0 if has_images else _LOCAL_TIMEOUT
        else:
            timeout = 90.0 if has_images else 60.0

        max_rounds = max(0, int(self.config.search_max_rounds)) if search_enable else 0
        content = ""
        # 原生工具调用只取**最后一轮**：检索结果回填后还会再问一次模型，
        # 那一轮的 tool_calls 才是最终要执行的。
        native_tool_calls: list[Any] = []
        recent_keys = _recent_assistant_keys(messages) if local_infer else set()
        for round_idx in range(max_rounds + 1):
            completion = await self._complete_once(
                messages,
                max_tokens=max_tokens,
                timeout=timeout,
                tools=tools,
            )
            content = completion.text
            native_tool_calls = list(completion.tool_calls)
            if local_infer and content:
                key = _norm_reply_key(content)
                leaked = (
                    "成员候选" in content
                    or "（系统）" in content
                    or "(系统)" in content
                    or content.strip().startswith("【成员候选】")
                )
                if leaked or (key and key in recent_keys):
                    logger.warning(
                        f"dl_senpai local bad/echo reply blocked: {content!r}"
                    )
                    # 不要再塞「（系统）…」进 messages，小模型会原样发出去
                    if messages and messages[0].get("role") == "system":
                        messages = list(messages)
                        messages[0] = {
                            **messages[0],
                            "content": (
                                str(messages[0].get("content") or "")
                                + "\n刚才那句作废，换一句新的短口语回复，禁止复读、禁止输出内部说明。"
                            ),
                        }
                    completion = await self._complete_once(
                        messages,
                        max_tokens=max_tokens,
                        timeout=timeout,
                        tools=tools,
                    )
                    content = completion.text
                    native_tool_calls = list(completion.tool_calls)
                    key2 = _norm_reply_key(content)
                    still_bad = (
                        "成员候选" in content
                        or "（系统）" in content
                        or "(系统)" in content
                        or (key2 and key2 in recent_keys)
                    )
                    if still_bad:
                        raise LLMError(
                            polish_reply("嗯……学姐卡壳了，你再说一遍好不好？")
                        )
            if not search_enable or round_idx >= max_rounds:
                break
            # Phase 6A：检索判定也走共享解析层（不再直接调 parse_search_request）。
            # 这里只需要"有没有 SEARCH 调用、查询词是什么"，
            # 由 MarkerToolAdapter 统一给出，保证与最终解析同一套正则。
            queries = _detect_search_queries(content)
            if not queries:
                break
            queries = queries[:2]
            logger.info(
                f"dl_senpai search round={round_idx + 1} queries={queries!r}"
            )
            searcher = searcher_from_config(self.config)
            try:
                brief = await searcher.search_many(
                    queries,
                    intent_query=search_focus or queries[0],
                    max_chars=max(400, int(self.config.search_prompt_max_chars)),
                    freshness_intent=search_focus or queries[0],
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"dl_senpai search failed: {e}")
                brief = "【联网检索结果】\n（检索暂时不可用）"
            max_chars = max(400, int(self.config.search_prompt_max_chars))
            if len(brief) > max_chars:
                brief = brief[: max_chars - 1].rstrip() + "…"
            searched = True
            search_brief_text = (
                f"{search_brief_text}\n\n{brief}".strip()
                if search_brief_text
                else brief
            )
            search_detail_level = assess_search_detail_level(search_brief_text)
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"{brief}\n\n"
                        "上面是实时联网检索结果。开口前先做两件事："
                        "一是核对它是不是**回答了对方问的那件事**（只是沾边不算）；"
                        "二是看**日期和版本号** —— 涉及最新/版本/赛季/补丁/更新 的问题，"
                        "若结果没有日期或写的是旧赛季旧版本，**必须换词再搜一次**再下结论。"
                        "换词要加年份/版本号或换成「补丁说明/更新公告/官网」角度，别原样重问。"
                        "确认对得上、时效也没问题再回答；确实没有就说没查到、别编。"
                    ),
                }
            )

        if (
            search_enable
            and strict_grounding
            and not interrupt
            and content
            and not _detect_search_queries(content)
        ):
            allowed_context = f"{search_brief_text}\n{search_focus}"
            # 最多重写 _GROUNDING_MAX_RETRIES 次，每次重写后**再查一遍**。
            # 旧实现只重写一次且不复查：模型第二遍换个说法接着编，照样直接发出去
            # —— 没有复查就等于没有闸门。
            for attempt in range(_GROUNDING_MAX_RETRIES + 1):
                retry, ungrounded = should_grounding_retry(
                    content,
                    allowed_context=allowed_context,
                    strict=True,
                    detail_level=search_detail_level,
                )
                if not retry:
                    break
                if attempt >= _GROUNDING_MAX_RETRIES:
                    # 改到上限仍不达标，说明它手里确实没有依据。再问只会得到
                    # 换个说法的编造，所以用确定性文案收场 —— 宁可少说。
                    logger.warning(
                        f"dl_senpai grounding fallback ungrounded={ungrounded!r} "
                        f"detail={search_detail_level}"
                    )
                    content = build_grounding_fallback_reply(ungrounded)
                    native_tool_calls = []
                    break
                logger.warning(
                    f"dl_senpai grounding retry attempt={attempt + 1} "
                    f"ungrounded={ungrounded!r} detail={search_detail_level}"
                )
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": build_grounding_retry_user_message(ungrounded),
                    }
                )
                try:
                    completion = await self._complete_once(
                        messages,
                        max_tokens=max_tokens,
                        timeout=timeout,
                        tools=tools,
                    )
                except Exception as exc:  # noqa: BLE001
                    # 旧实现这里没有 try：重写那次失败会把异常直接抛出去，
                    # 连上一版答案都发不出去。宁可留着旧答案。
                    logger.warning(f"dl_senpai grounding retry failed: {exc}")
                    break
                content = completion.text
                native_tool_calls = list(completion.tool_calls)

        return self._finalize_result(
            content,
            sender_name=sender_name,
            native_calls=native_tool_calls,
            card_enable=card_enable,
            mute_enable=mute_enable,
            searched=searched,
            draw_enable=bool(draw_enable),
            browser_enable=bool(browser_enable),
            person_memory_enable=bool(write_person_memory),
        )

    async def _complete_once(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        timeout: float,
        tools: list[dict[str, Any]] | None = None,
    ) -> _Completion:
        last_error: Exception | None = None
        channel = self.config.provider()
        injected = self._resolve_client()
        if injected is not None:
            client, channel, model = injected
        elif channel == "ollama":
            client = self._ollama_client_cached()
            model = self.config.ollama_model_resolved()
        else:
            client = self.client
            model = self.config.active_model()
            # 之前这里硬编码 "relay"，但 active_base_url() 可能被说话风格
            # 指向本地 LoRA 服务（如 http://127.0.0.1:8001/v1）。此时日志写
            # channel=relay 会把「本地服务没启动」误导成「中转站故障」，
            # 排查方向完全跑偏。按目标地址区分，保留真实信息。
            channel = (
                "local"
                if _is_local_infer_url(self.config.active_base_url())
                else "relay"
            )
        # 流式偏好：某些中转站（如 airelay 的 DeepSeek 系列）无论 stream 传什么值
        # 都返回 SSE 文本，SDK 解析出来是裸 str，取 .choices 会 AttributeError。
        # 这些模型走真正的 stream=True 才正常，因此按模型名决定是否主动用流式。
        # 带 tools 时强制非流式：_stream_once 只聚合 delta.content，
        # 不聚合分片的 delta.tool_calls，流式下会静静丢掉工具调用。
        # 实测 airelay 的 deepseek-v4.1-flash 在非流式下能正常返回 tool_calls。
        use_stream = _prefer_stream(model) and injected is None and not tools
        for attempt in range(self._retry.max_attempts):
            try:
                started = time.perf_counter()
                if use_stream:
                    resp = await self._stream_once(
                        client,
                        model=model,
                        messages=messages,
                        max_tokens=max_tokens,
                        timeout=timeout,
                    )
                else:
                    create_kwargs: dict[str, Any] = {
                        "model": model,
                        "messages": messages,
                        "temperature": self.config.temperature,
                        "max_tokens": max_tokens,
                        "timeout": timeout,
                    }
                    # 只有显式要求原生工具调用时才加这两个字段，
                    # 保证未启用时请求体与改造前逐字节一致。
                    if tools:
                        create_kwargs["tools"] = tools
                        create_kwargs["tool_choice"] = "auto"
                    resp = await client.chat.completions.create(**create_kwargs)
                    # 上游无视 stream=False 直接吐 SSE 时，SDK 会把它当 str 返回。
                    # 这里就地解析，避免抛 AttributeError 白跑 4 次重试。
                    resp = _coerce_sse_response(resp)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                message = resp.choices[0].message
                content = (message.content or "").strip()
                # 原生 function calling 的合法形态就是 content 为空 + tool_calls
                # 非空，所以"空回复"必须判两者都空，否则会把工具调用误判成空响应。
                tool_calls = list(getattr(message, "tool_calls", None) or [])
                # token 用量：部分中转站不返回 usage，缺失时记 null，绝不让
                # 它影响主流程（Phase 1 审计：resp.usage 从未被读取）
                self._record_usage(resp, model=model, duration_ms=elapsed_ms)
                # Phase 7A.1：唯一 accounting boundary（第二十八节）。
                # 这里是**真正发 HTTP 请求**的地方；ModelGateway.chat()
                # 只是转发到 SenpaiLLM.chat()，不得再记一次，否则翻倍。
                _instrument_model_call(
                    resp, model=model, provider=channel,
                    latency_ms=elapsed_ms, retry_count=attempt,
                )
                if not content and not tool_calls:
                    raise LLMError(
                        polish_reply("嗯……学姐刚才脑子空了一下，你再说一遍好不好？")
                    )
                return _Completion(text=content, tool_calls=tool_calls)
            except LLMError:
                raise
            except AuthenticationError as e:
                raise LLMError(
                    polish_reply("稍等一会啦，学姐这边还没收拾好，晚点再来找我哦～")
                ) from e
            except (RateLimitError, APIStatusError, APIError) as e:
                last_error = e
                logger.warning(
                    f"dl_senpai LLM api error channel={channel} "
                    f"(status={_status_code(e)}, attempt={attempt + 1}/{self._retry.max_attempts}): {e}"
                )
                if _is_transient(e) and self._retry.should_retry_transient(attempt):
                    delay = self._retry.delay_for_transient(attempt)
                    logger.warning(f"dl_senpai LLM retry in {delay:.1f}s")
                    await self._retry.sleep(delay)
                    continue
                raise LLMError(polish_reply(_friendly_api_error(e))) from e
            except Exception as e:  # noqa: BLE001
                last_error = e
                logger.warning(
                    f"dl_senpai LLM unexpected error channel={channel} "
                    f"(attempt={attempt + 1}/{self._retry.max_attempts}): {type(e).__name__}: {e}"
                )
                if self._retry.should_retry_unexpected(attempt):
                    delay = self._retry.delay_for_unexpected(attempt)
                    await self._retry.sleep(delay)
                    continue
                raise LLMError(
                    polish_reply("稍等一会啦，学姐现在有些事情要做噢～晚点再叫我吧")
                ) from e
        raise LLMError(
            polish_reply(_friendly_api_error(last_error or Exception("unknown")))
        )

    async def _stream_once(
        self,
        client: Any,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> _StreamedResponse:
        """流式调用并把 chunk 聚合成一个「像非流式响应」的对象。

        为什么需要它：airelay 的 DeepSeek 系列在 stream=False 时依然返回 SSE
        文本，SDK 拿到裸 str 无法解析；只有真正 stream=True 才能正常对话。
        聚合出来的对象保持 .choices[0].message.content 与 .usage 两个下游要
        用到的形状，这样 _record_usage / _instrument_model_call / 调用方
        都不需要知道走的是流式。

        注意：中转站的 usage 出现在**最后一个**带 usage 的 chunk 上（实测
        无需 stream_options.include_usage 也会带），因此循环里持续覆盖、
        不能只取首个。
        """
        parts: list[str] = []
        usage: Any = None
        fully_consumed = False
        # 用 try/finally 而非裸 async for，是为了在提前退出（相邻异常）时
        # 关闭底层响应体，避免连接泄漏。正常耗尽时不关，见 finally 里的说明。
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=self.config.temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            stream=True,
        )
        try:
            async for chunk in stream:
                usage = getattr(chunk, "usage", None) or usage
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                piece = getattr(delta, "content", None)
                if piece:
                    parts.append(piece)
            fully_consumed = True
        finally:
            # 只有**提前退出**（消费途中抛异常）才需要手动关闭，避免连接泄漏。
            # 正常耗尽时 SDK 会自行关闭，不能再关一次：httpcore2 的响应体
            # 生成器在耗尽后再收到 athrow() 会抛
            # "RuntimeError: generator didn't stop after athrow()"。
            # 注意方法名是 close() 而非 aclose()——AsyncStream 只有前者。
            if not fully_consumed:
                close = getattr(stream, "close", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001 — 关闭失败不该覆盖真实结果/异常
                        pass
        return _StreamedResponse("".join(parts).strip(), usage)

    def _record_usage(
        self,
        resp: Any,
        *,
        model: str,
        duration_ms: float,
    ) -> None:
        """读取并记录 token 用量。

        防御性实现：`resp.usage` 可能不存在（部分 OpenAI 兼容中转站不返回），
        字段也可能是 None。任何异常都被吞掉——统计失败绝不能影响回复。
        """
        try:
            usage = getattr(resp, "usage", None)
            prompt_tokens = getattr(usage, "prompt_tokens", None)
            completion_tokens = getattr(usage, "completion_tokens", None)
            total_tokens = getattr(usage, "total_tokens", None)
            if isinstance(usage, dict):
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
                total_tokens = usage.get("total_tokens")
            log_llm_usage(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                duration_ms=duration_ms,
                session_id=trace_session_id(),
            )
            metrics.incr("llm.request")
            metrics.observe("llm.latency_ms", duration_ms)
            if total_tokens is not None:
                metrics.incr("llm.tokens.total", float(total_tokens))
            if prompt_tokens is not None:
                metrics.incr("llm.tokens.prompt", float(prompt_tokens))
            if completion_tokens is not None:
                metrics.incr("llm.tokens.completion", float(completion_tokens))
        except Exception:  # noqa: BLE001 — 统计失败不影响业务
            pass

    def _finalize_result(
        self,
        content: str,
        *,
        sender_name: str = "",
        card_enable: bool,
        mute_enable: bool,
        searched: bool,
        draw_enable: bool = False,
        browser_enable: bool = False,
        person_memory_enable: bool = False,
        native_calls: list[Any] | None = None,
    ) -> ChatResult:
        """把模型原始输出整理成 `ChatResult`。

        **Phase 6A：marker parsing 已收敛到共享层。**

        重构前这个函数自己链式调用 8 个 `parse_*`，构成第二套 parser。
        现在只做三件事：

        1. **委托解析**：把"哪些工具可见"告诉 `MarkerToolAdapter`，
           由它统一解析（容错差异全部由 ToolDefinition.parser 驱动）；
        2. **文本清理**：剥掉未启用工具的 marker、截断、`polish_reply`；
        3. **形状转换**：`LegacyChatResultAdapter` 转成旧 orchestration
           消费的 8 个字段。

        为什么保留这个函数而不删：`handlers` 依赖它的返回值形状，
        且文本清理需要 config 上下文（adapter 不该知道 config）。
        """
        from .agent.legacy_adapter import LegacyChatResultAdapter
        from .agent.tools.builtin import register_production_tools
        from .agent.tools.creator_tools import register_creator_tools
        from .agent.tools.high_risk import register_high_risk_tools
        from .agent.tools.marker_adapter import MarkerToolAdapter
        from .agent.tools.memory_tool import register_memory_tool
        from .agent.tools.registry import get_tool_registry
        from .knowledge_setup import register_knowledge_tool

        # -- 1) 委托共享解析层 --
        registry = get_tool_registry()
        # 注册表由 Runtime.build() 填充；但 _finalize_result 可能在
        # Runtime 尚未构建时被调用（单测、legacy 早期路径），
        # 因此这里做一次幂等填充，避免"解析层拿到了空注册表"。
        if len(registry) == 0:
            register_production_tools(registry)
        # 知识工具默认不可见（要上下文带 knowledge_service 才暴露），
        # 所以注册它不会改变「未配置知识库时」的提示词内容。
        register_knowledge_tool(registry)
        register_memory_tool(registry)
        register_high_risk_tools(registry)
        # 生成层（Word/PPT）对所有群友可见，单独注册见 creator_tools 的说明。
        register_creator_tools(registry)
        adapter = MarkerToolAdapter(registry)

        # 只解析启用的工具：把未启用的临时禁用，解析后再恢复。
        # 这样"未启用工具的 marker"会留在正文里，由下面的清理步骤剥离，
        # 行为与重构前的 `if card_enable: parse else: pass` 一致。
        enabled_map = {
            "card": card_enable,
            "mute": mute_enable,
            "impression": person_memory_enable,
            "draw": draw_enable,
            "browse": browser_enable,
            # search/sticker/voice 在重构前**无条件解析**。但 2026-10-12 确认
            # sticker/voice 的开关是失效的：关掉之后模型仍被教着写
            # <<<STICKER>>>，这里也照旧执行，结果把该生图的一轮浪费在空的
            # 表情包库上。改为跟随配置；关闭时遗留的 marker 由 polish_reply
            # 兜底剥离，不会泄漏到群里。search 保持原样。
            "search": True,
            "sticker": bool(self.config.sticker_enable),
            "voice": bool(self.config.voice_enable),
        }
        toggled: list[Any] = []
        for name, want in enabled_map.items():
            tool = registry.get(name)
            if tool is None:
                continue
            if tool.enabled != want:
                toggled.append((tool, tool.enabled))
                tool.enabled = want
        try:
            parsed = adapter.parse_response(content)
        finally:
            for tool, original in toggled:
                tool.enabled = original

        # -- 原生 function calling 与 marker 合并 --
        # native 模式下正文里本来就没有 marker，parsed.calls 通常为空；
        # 但两种协议可能混用（模型既发 marker 又发 tool_calls），
        # 因此这里做并集而不是二选一，避免任一侧静默丢失。
        all_calls = list(parsed.calls)
        if native_calls:
            from .agent.tools.marker_adapter import NativeToolAdapter

            all_calls.extend(NativeToolAdapter(registry).parse_native_calls(native_calls))

        text = parsed.text
        if not person_memory_enable:
            # 重构前：未启用画像时用 strip_impression_markers 兜底
            text = strip_impression_markers(text)
        if not draw_enable:
            text = strip_draw_markers(text)
        if not browser_enable:
            text = strip_browse_markers(text)
        # 注意：SEARCH 的剥离由共享 adapter 完成（它注册了 search 工具，
        # 且 SEARCH 始终启用），这里**不再**直接调用 parse_search_request
        # —— 那是 Phase 6A 之前的第二套解析入口。

        # -- 2) 文本清理 --
        polished = polish_reply(
            truncate_reply(text, self.config.reply_hard_max_chars),
            speaker=sender_name,
        )

        # -- 3) 形状转换 --
        legacy = LegacyChatResultAdapter(adapter).convert(
            all_calls, text=polished, searched=searched
        )

        return ChatResult(
            text=legacy.text,
            # 关键：把调用一并带出。AgentRuntime 只看 ModelResponse.text，
            # 而 marker 已在上面的解析里被剥掉 —— 不再回传就等于工具从未触发。
            tool_calls=all_calls,
            card_actions=legacy.card_actions,
            mute_actions=legacy.mute_actions,
            sticker_query=legacy.sticker_query,
            sticker_force=legacy.sticker_force,
            voice_force=legacy.voice_force,
            voice_override=legacy.voice_override,
            draw_prompt=legacy.draw_prompt,
            draw_force=legacy.draw_force,
            browse_tasks=legacy.browse_tasks,
            browse_force=legacy.browse_force,
            searched=legacy.searched,
            impression_updates=legacy.impression_updates,
        )


def _instrument_model_call(
    resp: Any,
    *,
    model: str,
    provider: str,
    latency_ms: float,
    retry_count: int = 0,
) -> None:
    """记录一次模型调用（Phase 7A.1 第二十七~三十节）。

    **唯一 accounting boundary**（第二十八节）：
        这里位于 `_complete_once` —— 真正发 HTTP 请求的地方。
        `ModelGateway.chat()` 只是转发到 `SenpaiLLM.chat()`，
        因此**不得**再记一次，否则同一次调用被统计两遍。

    第三十节：provider 未返回 usage 时记 None（unknown），
    **不用字符数估算冒充 API token**。

    失败静默：观测绝不影响模型调用。
    """
    try:
        from .validation.telemetry import ModelCallTelemetry, record_model_call

        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            total_tokens = usage.get("total_tokens")

        # 仅当 provider 给了 prompt/completion 但没给 total 时才补算总量；
        # 三者都缺 → 保持 None，不猜。
        if (
            total_tokens is None
            and prompt_tokens is not None
            and completion_tokens is not None
        ):
            total_tokens = prompt_tokens + completion_tokens

        record_model_call(
            ModelCallTelemetry(
                model=str(model or ""),
                provider=str(provider or ""),
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated=False,  # provider-reported，不是估算
                status="ok",
                retry_count=int(retry_count or 0),
            )
        )
    except Exception:  # noqa: BLE001 — 观测失败绝不影响模型调用
        pass


def truncate_reply(text: str, max_chars: int) -> str:
    text = text.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1].rstrip()
    return cut + "…"


def _detect_search_queries(content: str) -> list[str]:
    """从模型输出里探测 SEARCH 查询词（Phase 6A：走共享解析层）。

    为什么需要这个函数而不是直接调 `parse_search_request`：
    Phase 6A 要求 marker 解析只有一套实现。检索**判定**与最终**解析**
    必须用同一套正则，否则两边会漂移（例如一边接受 `】` 闭合、
    另一边不接受）。

    这里用共享的 `MarkerToolAdapter` 解析，然后只取 search 的调用。
    解析失败时返回空列表（等价于"没有检索请求"）。
    """
    try:
        from .agent.tools.builtin import register_production_tools
        from .agent.tools.creator_tools import register_creator_tools
        from .agent.tools.high_risk import register_high_risk_tools
        from .agent.tools.marker_adapter import MarkerToolAdapter
        from .agent.tools.memory_tool import register_memory_tool
        from .agent.tools.registry import get_tool_registry
        from .knowledge_setup import register_knowledge_tool

        registry = get_tool_registry()
        if len(registry) == 0:
            register_production_tools(registry)
        register_knowledge_tool(registry)
        register_memory_tool(registry)
        register_high_risk_tools(registry)
        register_creator_tools(registry)
        parsed = MarkerToolAdapter(registry).parse_response(content or "")
        for call in parsed.calls:
            if call.tool_name == "search":
                return list(call.arguments.get("queries") or [])
    except Exception:  # noqa: BLE001 — 探测失败等价于"没有检索请求"
        pass
    return []


_llm: SenpaiLLM | None = None


def get_llm(config: PluginConfig | None = None) -> SenpaiLLM:
    global _llm
    if config is not None:
        return SenpaiLLM(config)
    if _llm is None:
        _llm = SenpaiLLM()
    return _llm


def refresh_llm_config() -> None:
    """让单例持有最新配置（配置热重载后调用）。

    为什么需要：`SenpaiLLM.__init__` 把 config 存成 `self.config`，
    而 `_complete_once` 等方法都读 `self.config.xxx`。热重载只换了
    runtime 里的配置实例，**已经构造好的单例仍指向旧对象**，于是开关
    改了却不生效。这里就地换掉引用，避免重建客户端（省掉一次连接池重建）。

    未初始化时不做任何事：让下次 get_llm() 自然构造新的即可。
    """
    if _llm is None:
        return
    _llm.config = get_config()
