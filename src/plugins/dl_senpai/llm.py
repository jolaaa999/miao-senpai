from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from nonebot import logger
from openai import APIError, APIStatusError, AsyncOpenAI, AuthenticationError, RateLimitError

from .config import PluginConfig, get_config
from .group_card import CardAction, parse_card_actions
from .group_mute import MuteAction, parse_mute_actions
from .persona import build_system_prompt
from .stickers import parse_sticker_request
from .style import polish_reply
from .vision import build_user_content
from .voice import parse_voice_request
from .image_gen import DrawRequest, parse_draw_request, strip_draw_markers
from .browser_agent import BrowseRequest, parse_browse_request, strip_browse_markers, wants_browser_help
from .clock import is_asking_current_datetime
from .grounding import (
    assess_search_detail_level,
    build_grounding_retry_user_message,
    build_grounding_user_hint,
    requires_strict_grounding,
    should_grounding_retry,
)
from .person_memory import ImpressionUpdate, parse_impression_updates, strip_impression_markers
from .web_search import (
    find_trend_search_queries,
    parse_search_request,
    prefetch_search_brief,
    searcher_from_config,
    should_auto_search,
    should_skip_auto_search,
)

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
    def __init__(self, config: PluginConfig | None = None) -> None:
        self.config = config or get_config()
        self._client: AsyncOpenAI | None = None
        self._ollama_client: AsyncOpenAI | None = None
        self._client_fingerprint: str = ""

    def _relay_fingerprint(self) -> str:
        return (
            f"{self.config.active_base_url()}|{self.config.active_api_key()[:8]}|"
            f"{self.config.active_model()}|{self.config.active_speak_style_id()}"
        )

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
            self._client = AsyncOpenAI(
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
            self._ollama_client = AsyncOpenAI(
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
            brief, auto_queries = await prefetch_search_brief(
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
                    "并说明可能不是最新；不够可再写 <<<SEARCH 查询词>>> 补搜。）"
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
        if browse_task_hint:
            user_payload = f"{user_payload}\n\n{browse_task_hint}"

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": build_system_prompt(
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
        recent_keys = _recent_assistant_keys(messages) if local_infer else set()
        for round_idx in range(max_rounds + 1):
            content = await self._complete_once(
                messages,
                max_tokens=max_tokens,
                timeout=timeout,
            )
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
                    content = await self._complete_once(
                        messages,
                        max_tokens=max_tokens,
                        timeout=timeout,
                    )
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
            req = parse_search_request(content)
            if not req.queries:
                break
            queries = req.queries[:2]
            logger.info(
                f"dl_senpai search round={round_idx + 1} queries={queries!r}"
            )
            searcher = searcher_from_config(self.config)
            try:
                brief = await searcher.search_many(
                    queries,
                    intent_query=search_focus or queries[0],
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
                        "上面是实时联网检索结果。请先核对是否包含对方所问内容；"
                        "有则据此用学姐口吻回答，没有就说没查到、别编；"
                        "信息够了就不要再写 <<<SEARCH>>>。"
                    ),
                }
            )

        if (
            search_enable
            and strict_grounding
            and not interrupt
            and content
            and not parse_search_request(content).queries
        ):
            retry, ungrounded = should_grounding_retry(
                content,
                allowed_context=f"{search_brief_text}\n{search_focus}",
                strict=True,
                detail_level=search_detail_level,
            )
            if retry:
                logger.warning(
                    f"dl_senpai grounding retry ungrounded={ungrounded!r} "
                    f"detail={search_detail_level}"
                )
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": build_grounding_retry_user_message(ungrounded),
                    }
                )
                content = await self._complete_once(
                    messages,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )

        return self._finalize_result(
            content,
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
    ) -> str:
        last_error: Exception | None = None
        channel = self.config.provider()
        if channel == "ollama":
            client = self._ollama_client_cached()
            model = self.config.ollama_model_resolved()
        else:
            client = self.client
            model = self.config.active_model()
            channel = "relay"
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=self.config.temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
                content = (resp.choices[0].message.content or "").strip()
                if not content:
                    raise LLMError(
                        polish_reply("嗯……学姐刚才脑子空了一下，你再说一遍好不好？")
                    )
                return content
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
                    f"(status={_status_code(e)}, attempt={attempt + 1}/{_MAX_ATTEMPTS}): {e}"
                )
                if _is_transient(e) and attempt < _MAX_ATTEMPTS - 1:
                    delay = 60.0
                    logger.warning(f"dl_senpai LLM retry in {delay:.1f}s")
                    await asyncio.sleep(delay)
                    continue
                raise LLMError(polish_reply(_friendly_api_error(e))) from e
            except Exception as e:  # noqa: BLE001
                last_error = e
                logger.warning(
                    f"dl_senpai LLM unexpected error channel={channel} "
                    f"(attempt={attempt + 1}/{_MAX_ATTEMPTS}): {type(e).__name__}: {e}"
                )
                if attempt < _MAX_ATTEMPTS - 1:
                    delay = min(4.0, 0.5 * (2**attempt))
                    await asyncio.sleep(delay)
                    continue
                raise LLMError(
                    polish_reply("稍等一会啦，学姐现在有些事情要做噢～晚点再叫我吧")
                ) from e
        raise LLMError(
            polish_reply(_friendly_api_error(last_error or Exception("unknown")))
        )

    def _finalize_result(
        self,
        content: str,
        *,
        card_enable: bool,
        mute_enable: bool,
        searched: bool,
        draw_enable: bool = False,
        browser_enable: bool = False,
        person_memory_enable: bool = False,
    ) -> ChatResult:
        text = content
        actions: list[CardAction] = []
        mutes: list[MuteAction] = []
        impressions: list[ImpressionUpdate] = []
        # 末轮若仍残留 SEARCH，剥掉避免泄漏
        text = parse_search_request(text).text
        if card_enable:
            parsed = parse_card_actions(text)
            text = parsed.text
            actions = parsed.actions
        if mute_enable:
            muted = parse_mute_actions(text)
            text = muted.text
            mutes = muted.actions
        if person_memory_enable:
            impression = parse_impression_updates(text)
            text = impression.text
            impressions = impression.updates
        else:
            text = strip_impression_markers(text)
        sticker = parse_sticker_request(text)
        voice = parse_voice_request(sticker.text)
        if draw_enable:
            draw = parse_draw_request(voice.text)
        else:
            draw = DrawRequest(text=strip_draw_markers(voice.text))
        browse = (
            parse_browse_request(draw.text)
            if browser_enable
            else BrowseRequest(text=strip_browse_markers(draw.text))
        )
        polished = polish_reply(
            truncate_reply(browse.text, self.config.reply_hard_max_chars)
        )
        return ChatResult(
            text=polished,
            card_actions=actions,
            mute_actions=mutes,
            sticker_query=sticker.query,
            sticker_force=sticker.force,
            voice_force=voice.force,
            voice_override=voice.override,
            draw_prompt=draw.prompt,
            draw_force=draw.force,
            browse_tasks=list(browse.tasks),
            browse_force=browse.force,
            searched=searched,
            impression_updates=impressions,
        )


def truncate_reply(text: str, max_chars: int) -> str:
    text = text.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1].rstrip()
    return cut + "…"


_llm: SenpaiLLM | None = None


def get_llm(config: PluginConfig | None = None) -> SenpaiLLM:
    global _llm
    if config is not None:
        return SenpaiLLM(config)
    if _llm is None:
        _llm = SenpaiLLM()
    return _llm
