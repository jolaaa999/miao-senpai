"""联网检索：学姐伪指令 <<<SEARCH>>> 与 Cursor MCP 共用。

默认无需 Key：DuckDuckGo Instant Answer + Bing HTML 兜底。
可选：SearXNG / Tavily / 已安装的 ddgs。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .clock import current_beijing_year
from .reply_quote import user_intent_text

try:
    from nonebot import logger
except Exception:  # noqa: BLE001 — MCP 进程可能无 nonebot

    class _Logger:
        def warning(self, msg: str) -> None:
            print(msg)

        def info(self, msg: str) -> None:
            print(msg)

    logger = _Logger()  # type: ignore[assignment]

_SEARCH_CLOSE = r"(?:>>>|>>|＞＞＞|＞＞|】】|】>|】)"
_SEARCH_MARKER_RE = re.compile(
    rf"<<<SEARCH\s*([^>\n]+?)\s*{_SEARCH_CLOSE}",
    re.IGNORECASE,
)
_SEARCH_MARKER_FALLBACK_RE = re.compile(
    rf"<<<SEARCH[\s\S]*?{_SEARCH_CLOSE}",
    re.IGNORECASE,
)
_SEARCH_MARKER_TAIL_RE = re.compile(r"<<<SEARCH\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)
_BING_RESULT_RE = re.compile(
    r'<li class="b_algo".*?<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
    r".*?(?:<p[^>]*>(.*?)</p>|<div class=\"b_caption\"[^>]*>.*?<p[^>]*>(.*?)</p>)?",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_AUTO_SEARCH_NEGATE_RE = re.compile(
    r"(别|不要|不用|无需|别想).{0,6}(搜|查|联网|百度)",
    re.IGNORECASE,
)
_AUTO_SEARCH_EXPLICIT_RE = re.compile(
    r"(搜一下|查一下|帮我搜|帮我查|联网|上网查|网上查|百度|谷歌|查询一下|检索)",
    re.IGNORECASE,
)
_AUTO_SEARCH_TIME_RE = re.compile(
    r"(今天|今晚|昨夜|昨天|明天|最近|最新|当前|实时|今年|本月|这周|上周|刚刚|刚才|几号|哪天|"
    r"现在|目前|当下|出了吗|发布了吗|上线了吗|发售了吗|什么时候出|啥时候|"
    r"20\d{2}年?)",
)
_AUTO_SEARCH_INSTRUCT_RE = re.compile(
    r"(去|到|上).{0,24}(wiki|百科|百度|谷歌|bing|google).{0,12}搜"
    r"|最好.{0,10}搜"
    r"|自己(去|先).{0,8}搜"
    r"|搜索再回答",
    re.IGNORECASE,
)
_AUTO_SEARCH_TOPIC_RE = re.compile(
    r"(天气|新闻|热搜|热点|时事|头条|八卦|瓜|股价|股票|汇率|油价|金价|白银|比分|赛果|赛程|"
    r"版本|更新|发售|上市|政策|公告|通知|放假|汇率|cpi|gdp|"
    r"发布会|上线|下架|停运|停服|维护|联名|代言|"
    r"谁赢了|多少钱|价格|票价|票房|上映|"
    r"deepseek|openai|gpt|claude|gemini|豆包|通义|kimi|chatgpt|"
    r"iphone|华为|小米|英伟达|amd|特斯拉|spacex)",
    re.IGNORECASE,
)
# 事实/实体/设定类：不确定就先搜，对照结果再答
_AUTO_SEARCH_FACTUAL_RE = re.compile(
    r"(是什么|什么是|啥是|什么意思|介绍一下|有没有|是否存在|真有|真的吗|靠谱吗|"
    r"谁(是|做的|开发|发明|配音|当|当选)|哪里(的|有)|哪款|哪个|哪一种|多少种|"
    r"存在吗|听说过吗|没听过吗|不了解吗|怎么回事|咋回事|发生了什么|出什么事|"
    r"wiki|百科|资料|设定|角色|技能|武器|怪物|boss|"
    r"怎么(获得|解锁|打|配)|如何(获得|解锁|配))",
    re.IGNORECASE,
)
_AUTO_SEARCH_BANTER_RE = re.compile(
    r"(大笨蛋|小笨蛋|你是笨蛋|这都不认识|认错了|傻[逼卵]?|人机|菜鸡|"
    r"谢谢学姐|晚安|早安|在吗|哈哈+|hhh+|草+$|6+$|喵+$|哼+$)",
    re.IGNORECASE,
)
# 直接问学姐本人状态/能不能说话，不是事实检索
_AUTO_SEARCH_DIRECT_BOT_RE = re.compile(
    r"^(你(被|还(?!知)|在吗|还好吗|怎么样|咋了|死|挂|崩|坏|卡|没反应|说话|理我)"
    r"|你是不是(死|挂|崩|坏|ko|挂了|死了)"
    r"|你能(说话|回|理我)"
    r")"
    r"|被\s*ko\b"
    r"|说\s*不\s*了\s*话",
    re.IGNORECASE,
)
_AUTO_SEARCH_QUESTION_RE = re.compile(
    r"([？?]|吗|呢|么)\s*$|"
    r"(谁|什么|啥|哪里|哪儿|为何|为什么|怎么回事|咋回事|怎么样|如何)",
    re.IGNORECASE,
)
_AUTO_SEARCH_SKIP_CHAT_RE = re.compile(
    r"^(在吗|你好|嗨|晚安|早安|谢谢|好的|收到|ok|嗯+|啊+|哈+|喵+|哼+)"
    r"|^(别|不要|不用).{0,8}(搜|查)"
    r"|随便聊|闲聊一下",
    re.IGNORECASE,
)
_QUERY_PREFIX_NOISE_RE = re.compile(
    r"^(?:请问|问一下|帮我|能不能|可以|告诉我|说说|我想知道|我想问)+",
    re.IGNORECASE,
)
_QUERY_SUFFIX_NOISE_RE = re.compile(
    r"(?:是什么|什么是|啥是|什么意思|有没有|是否存在|存在吗|真的吗|靠谱吗|"
    r"吗|呢|啊|呀|嘛|[？?])+$",
    re.IGNORECASE,
)
_YEAR_WORD_RE = re.compile(r"今年|本年|这年")
_QUERY_TERM_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{2,}|\d{4}")
_SEARCH_STOP_TERMS = frozenset(
    {
        "今年",
        "本年",
        "什么",
        "哪些",
        "有没有",
        "告诉",
        "名字",
        "名单",
        "怎么样",
        "如何",
        "可以",
        "请问",
        "一下",
        "发送",
        "发来",
        "哪些",
        "哪个",
        "多少",
        "几个",
        "学姐",
        "帮我",
        "说说",
    }
)
_IRRELEVANT_NOISE_RE = re.compile(
    r"(世界杯|日历|放假安排|国务院|政府工作报告|元旦|中秋|国庆|高考|中考|"
    r"天气预报|汇率|股票|彩票)",
    re.IGNORECASE,
)
_GAME_SEARCH_HINTS: dict[str, list[str]] = {
    "明日方舟": [
        "prts.wiki 限定干员",
        "明日方舟 春节限定干员",
        "明日方舟 wiki 卡池",
    ],
    "怪物猎人": [
        "怪猎wiki",
        "怪物猎人 wiki",
        "mhw wiki",
        "MHW伙伴 配装",
        "怪物猎人 肉质 弱点",
        "mhwilds 配装",
    ],
    "三角洲": ["三角洲行动 wiki", "三角洲行动 干员技能", "三角洲 乌鲁鲁 技能"],
    "原神": ["原神 wiki 角色", "原神 bilibili 攻略"],
    "崩坏": ["崩坏 wiki"],
    "碧蓝航线": ["碧蓝航线 wiki"],
    "少女前线": ["少女前线 wiki"],
}
_SENPAI_CALL_RE = re.compile(r"^学姐[,，:\s]*", re.IGNORECASE)
_BRACKET_TAG_RE = re.compile(r"【[^】]*】")
_AT_RE = re.compile(r"@\S+")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str = ""
    snippet: str = ""
    source: str = ""


@dataclass
class SearchRequest:
    text: str
    queries: list[str]


def parse_search_request(raw_reply: str) -> SearchRequest:
    """剥掉 <<<SEARCH ...>>>，收集查询词。"""
    text = raw_reply or ""
    queries: list[str] = []
    seen: set[str] = set()
    for match in _SEARCH_MARKER_RE.finditer(text):
        q = (match.group(1) or "").strip()
        if not q:
            continue
        key = q.casefold()
        if key in seen:
            continue
        seen.add(key)
        queries.append(q)
    cleaned = _SEARCH_MARKER_RE.sub("", text)
    cleaned = _SEARCH_MARKER_FALLBACK_RE.sub("", cleaned)
    cleaned = _SEARCH_MARKER_TAIL_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return SearchRequest(text=cleaned, queries=queries)


def _normalize_for_search_heuristics(text: str) -> str:
    cleaned = _BRACKET_TAG_RE.sub("", text or "")
    return _WS_RE.sub(" ", cleaned).strip()


def refine_search_query(text: str) -> str:
    """去掉问句套话，留下适合检索的关键词。"""
    q = (text or "").strip()
    if not q:
        return ""
    for _ in range(4):
        old = q
        q = _SENPAI_CALL_RE.sub("", q).strip()
        q = _AT_RE.sub("", q).strip()
        q = _QUERY_PREFIX_NOISE_RE.sub("", q).strip()
        q = _QUERY_SUFFIX_NOISE_RE.sub("", q).strip()
        q = re.sub(r"[？?！!。．…~～]+$", "", q).strip()
        if q == old:
            break
    return q or (text or "").strip()


def _query_terms(query: str) -> set[str]:
    terms: set[str] = set()
    q = _compact_query_core(query) or (query or "").strip()
    for match in re.finditer(r"[a-zA-Z]{2,}|\d{4}", q):
        terms.add(match.group().casefold())
    for game in _GAME_SEARCH_HINTS:
        if game in q:
            terms.add(game.casefold())
    for match in re.finditer(
        r"明日方舟|春节限定|限定干员|怪物猎人|原神|崩坏|碧蓝航线|少女前线|"
        r"春节|限定|干员|卡池|wiki",
        q,
        re.IGNORECASE,
    ):
        terms.add(match.group().casefold())
    cleaned = q
    for noise in _SEARCH_STOP_TERMS:
        cleaned = cleaned.replace(noise, " ")
    for part in re.split(r"[\s,，、/]+", cleaned):
        part = part.strip()
        if len(part) >= 2 and part not in _SEARCH_STOP_TERMS:
            terms.add(part.casefold())
        if len(part) > 4:
            for idx in range(len(part) - 1):
                bigram = part[idx : idx + 2]
                if bigram not in _SEARCH_STOP_TERMS:
                    terms.add(bigram.casefold())
    return {term for term in terms if term and term not in _SEARCH_STOP_TERMS}


def score_hit_relevance(query: str, hit: SearchHit) -> float:
    """粗略相关性：用于多引擎合并与弱结果重试。"""
    terms = _query_terms(query)
    if not terms:
        return 0.5
    blob = f"{hit.title} {hit.snippet} {hit.url}".casefold()
    matched = sum(1 for term in terms if term in blob)
    ratio = matched / len(terms)
    if _IRRELEVANT_NOISE_RE.search(blob) and ratio < 0.45:
        return ratio * 0.15
    return ratio


def best_hit_relevance(query: str, hits: list[SearchHit]) -> float:
    if not hits:
        return 0.0
    return max(score_hit_relevance(query, hit) for hit in hits)


def _dedupe_queries(queries: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for q in queries:
        s = _WS_RE.sub(" ", (q or "").strip())
        if not s:
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _compact_query_core(text: str) -> str:
    core = (text or "").strip()
    for noise in (
        "有哪些",
        "是什么",
        "什么是",
        "有没有",
        "哪些",
        "名单",
        "名字",
        "叫什么",
        "发一下",
        "发我",
        "告诉我",
        "说说",
    ):
        core = core.replace(noise, "")
    core = _AT_RE.sub("", core)
    core = _SENPAI_CALL_RE.sub("", core)
    return _WS_RE.sub(" ", core).strip()


def expand_search_queries(
    text: str,
    *,
    max_total: int = 4,
    source_text: str = "",
) -> list[str]:
    """从用户话里扩展多条检索词（原句、补年份、wiki/bilibili、游戏专题站）。"""
    origin = (source_text or text or "").strip()
    base = refine_search_query(text) or _normalize_for_search_heuristics(text)
    base = _WS_RE.sub(" ", (base or "").strip())
    if not base and not origin:
        return []

    queries: list[str] = []
    year = current_beijing_year()

    def add(q: str) -> None:
        s = _WS_RE.sub(" ", (q or "").strip())
        if not s or len(s) > _QUERY_MAX_CHARS:
            return
        if _INSTRUCTION_QUERY_RE.search(s):
            return
        queries.append(s)

    if base and len(base) <= _QUERY_MAX_CHARS and not _INSTRUCTION_QUERY_RE.search(base):
        add(base)
        if _YEAR_WORD_RE.search(base):
            add(_YEAR_WORD_RE.sub(str(year), base))

    core = _compact_query_core(base or origin) or base or origin
    if core and core != base and len(core) <= _QUERY_MAX_CHARS:
        add(core)

    scan = origin or base
    for game, hints in _GAME_SEARCH_HINTS.items():
        if game not in scan:
            continue
        for hint in hints:
            add(hint)
        if re.search(r"限定|春节|卡池", scan):
            add(f"{game} {year} 春节限定")
            add(f"{game} 限定干员 名单")

    if re.search(r"限定|干员|角色|技能|配装|攻略|卡池|boss|怪物|武器|装备", scan, re.I):
        if core:
            add(f"{core} wiki")
            add(f"{core} bilibili")
    elif core and len(core) >= 4:
        add(f"{core} wiki")

    if _AUTO_SEARCH_TOPIC_RE.search(scan) or _AUTO_SEARCH_TIME_RE.search(scan):
        if core:
            add(f"{core} {year}")
            add(f"{core} 新闻")

    return _dedupe_queries(queries)[: max(1, max_total)]


def build_fallback_search_queries(query: str, *, max_total: int = 3) -> list[str]:
    """首轮结果太差时，换更聚焦的兜底检索词。"""
    base = refine_search_query(query) or (query or "").strip()
    if not base:
        return []
    year = current_beijing_year()
    core = _compact_query_core(base) or base
    fallbacks: list[str] = []

    def add(q: str) -> None:
        s = _WS_RE.sub(" ", (q or "").strip())
        if s:
            fallbacks.append(s)

    add(f"{core} site:prts.wiki" if "明日方舟" in base else f"{core} site:bilibili.com")
    add(f"{core} 攻略")
    add(f"{core} {year}")
    if "wiki" not in base.casefold():
        add(f"{core} wiki 中文")

    expanded = expand_search_queries(base, max_total=max_total + 2)
    for q in expanded:
        add(q)
    return _dedupe_queries(fallbacks)[: max(1, max_total)]


def should_skip_auto_search(text: str) -> bool:
    """逛网站 / 搜图 / 提取提示词类任务：不走文字联网检索。"""
    raw = (text or "").strip()
    if not raw:
        return False
    try:
        from .browser_agent import wants_browser_help

        if wants_browser_help(raw):
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(
        re.search(
            r"(提取|抽取).{0,12}(提示词|prompt)"
            r"|生图提示词"
            r"|在.{0,16}(原本|原来|原).{0,12}提示词"
            r"|优化.{0,12}提示词",
            raw,
            re.I,
        )
    )


def looks_like_casual_banter(text: str) -> bool:
    """互损/寒暄/纯情绪，不含实质信息问题。"""
    raw = _normalize_for_search_heuristics(text)
    if len(raw) < 4:
        return True
    if _AUTO_SEARCH_SKIP_CHAT_RE.search(raw):
        return True
    if _AUTO_SEARCH_DIRECT_BOT_RE.search(raw) and not _AUTO_SEARCH_TOPIC_RE.search(raw):
        return True
    if _AUTO_SEARCH_BANTER_RE.search(raw) and not _AUTO_SEARCH_TOPIC_RE.search(raw):
        if not _AUTO_SEARCH_TIME_RE.search(raw) and not _AUTO_SEARCH_FACTUAL_RE.search(raw):
            return True
    return False


def looks_like_factual_question(text: str) -> bool:
    """像在向人打听事实/背景，而非纯闲聊。"""
    raw = _normalize_for_search_heuristics(text)
    if len(raw) < 5:
        return False
    if looks_like_casual_banter(raw):
        return False
    if _AUTO_SEARCH_QUESTION_RE.search(raw):
        return True
    if _QUERY_TERM_RE.search(raw) and len(raw) >= 8:
        return True
    return False


def find_trend_search_queries(text: str, trend_titles: list[str], *, max_queries: int = 2) -> list[str]:
    """用户话与热榜标题有重叠时，用热榜词条补检索。"""
    raw = _normalize_for_search_heuristics(text)
    if not raw or not trend_titles:
        return []
    year = current_beijing_year()
    queries: list[str] = []
    seen: set[str] = set()
    raw_cf = raw.casefold()

    def add(q: str) -> None:
        s = _WS_RE.sub(" ", (q or "").strip())
        if not s or len(s) > _QUERY_MAX_CHARS:
            return
        key = s.casefold()
        if key in seen:
            return
        seen.add(key)
        queries.append(s)

    for title in trend_titles:
        t = (title or "").strip()
        if len(t) < 4:
            continue
        overlap = False
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{2,}", t):
            if len(chunk) >= 2 and chunk.casefold() in raw_cf:
                overlap = True
                break
        if not overlap:
            for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", raw):
                if len(chunk) >= 2 and chunk in t:
                    overlap = True
                    break
        if overlap:
            add(t)
            add(f"{t} {year} 最新")
        if len(queries) >= max_queries:
            break
    return queries[: max(1, max_queries)]


def should_auto_search(text: str, *, aggressive: bool = False) -> bool:
    """判断用户消息是否应自动联网（无需模型写 <<<SEARCH>>>）。"""
    raw = user_intent_text("", text) if "【" in (text or "") else (text or "").strip()
    raw = _normalize_for_search_heuristics(raw)
    if len(raw) < 4:
        return False
    if should_skip_auto_search(raw):
        return False
    if _AUTO_SEARCH_NEGATE_RE.search(raw):
        return False
    if _AUTO_SEARCH_INSTRUCT_RE.search(raw):
        return False
    if looks_like_casual_banter(raw):
        return False
    if _AUTO_SEARCH_EXPLICIT_RE.search(raw):
        return True
    if _AUTO_SEARCH_TOPIC_RE.search(raw):
        return True
    if _AUTO_SEARCH_TIME_RE.search(raw) and len(raw) >= 6:
        return True
    if _AUTO_SEARCH_FACTUAL_RE.search(raw) and len(raw) >= 5:
        return True
    if aggressive and looks_like_factual_question(raw):
        return True
    return False


_QUERY_MAX_CHARS = 48
_INSTRUCTION_QUERY_RE = re.compile(
    r"去.{0,16}(pixiv|淘宝|百度|谷歌|bing)|"
    r"能不能|可不可以|提取|生图提示词|优化|保存|对应",
    re.I,
)


def build_auto_search_queries(text: str, *, max_queries: int = 4) -> list[str]:
    """从用户话里提炼多条搜索词。"""
    raw = user_intent_text("", text) if "【" in (text or "") else (text or "").strip()
    raw = _normalize_for_search_heuristics(raw)
    if not raw or should_skip_auto_search(raw):
        return []
    focus = _AT_RE.sub("", raw)
    focus = refine_search_query(focus) or _WS_RE.sub(" ", raw).strip()
    if not focus:
        return []
    if len(focus) > _QUERY_MAX_CHARS or _INSTRUCTION_QUERY_RE.search(focus):
        focus = _compact_query_core(focus)
        if not focus or len(focus) > _QUERY_MAX_CHARS or _INSTRUCTION_QUERY_RE.search(focus):
            return expand_search_queries("", max_total=max(1, max_queries), source_text=raw)
    return expand_search_queries(focus, max_total=max(1, max_queries), source_text=raw)


async def prefetch_search_brief(
    text: str,
    config: Any,
    *,
    max_queries: int | None = None,
    trend_titles: list[str] | None = None,
    aggressive: bool | None = None,
) -> tuple[str, list[str]]:
    """自动预判并检索；返回 (brief, queries)。无需检索时 brief 为空。"""
    if aggressive is None:
        aggressive = bool(getattr(config, "search_auto_aggressive", True))
    trend_titles = trend_titles or []
    auto = should_auto_search(text, aggressive=aggressive)
    trend_queries = find_trend_search_queries(text, trend_titles) if trend_titles else []
    if not auto and not trend_queries:
        return "", []
    if max_queries is None:
        max_queries = int(getattr(config, "search_max_auto_queries", 4))
    queries = build_auto_search_queries(text, max_queries=max_queries) if auto else []
    for tq in trend_queries:
        if tq.casefold() not in {q.casefold() for q in queries}:
            queries.append(tq)
    queries = _dedupe_queries(queries)[: max(1, max_queries + len(trend_queries))]
    if not queries:
        return "", []
    searcher = searcher_from_config(config)
    try:
        brief = await searcher.search_many(queries, intent_query=queries[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai auto search failed: {exc}")
        brief = "【联网检索结果】\n（检索暂时不可用）"
    max_chars = max(400, int(getattr(config, "search_prompt_max_chars", 2400)))
    if len(brief) > max_chars:
        brief = brief[: max_chars - 1].rstrip() + "…"
    return brief, queries


def _strip_html(raw: str) -> str:
    text = html_lib.unescape(_TAG_RE.sub(" ", raw or ""))
    return _WS_RE.sub(" ", text).strip()


def _clean_bing_url(url: str) -> str:
    """解开 Bing 跳转链。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        if "bing.com" in (parsed.netloc or "") and parsed.path.startswith("/ck/"):
            qs = parse_qs(parsed.query)
            for key in ("u", "r"):
                vals = qs.get(key) or []
                if vals:
                    return unquote(vals[0])
    except Exception:  # noqa: BLE001
        pass
    return raw


def format_search_for_prompt(
    hits: list[SearchHit],
    *,
    query: str = "",
    max_chars: int = 2400,
) -> str:
    if not hits:
        q = f"「{query}」" if query else ""
        return f"【联网检索结果】{q}\n（没有搜到可用结果）"
    lines = [f"【联网检索结果】查询：{query}" if query else "【联网检索结果】"]
    used = len(lines[0]) + 1
    for i, hit in enumerate(hits, start=1):
        title = (hit.title or "无标题").strip()
        snippet = (hit.snippet or "").strip()
        url = (hit.url or "").strip()
        src = f" · {hit.source}" if hit.source else ""
        block = f"{i}. {title}{src}"
        if snippet:
            block += f"\n   {snippet}"
        if url:
            block += f"\n   {url}"
        if used + len(block) + 1 > max_chars and i > 1:
            break
        lines.append(block)
        used += len(block) + 1
    text = "\n".join(lines)
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


class WebSearcher:
    """多后端联网检索。"""

    def __init__(
        self,
        *,
        provider: str = "auto",
        max_results: int = 5,
        timeout: float = 12.0,
        region: str = "zh-cn",
        searxng_url: str = "",
        tavily_api_key: str = "",
    ) -> None:
        self.provider = (provider or "auto").strip().lower() or "auto"
        self.max_results = max(1, min(10, int(max_results)))
        self.timeout = max(3.0, float(timeout))
        self.region = (region or "zh-cn").strip() or "zh-cn"
        self.searxng_url = (searxng_url or "").strip().rstrip("/")
        self.tavily_api_key = (tavily_api_key or "").strip()

    async def search(self, query: str) -> list[SearchHit]:
        q = (query or "").strip()
        if not q:
            return []
        if self.provider == "auto":
            return await self._search_auto_multi(q)
        order = self._provider_order()
        last_err: Exception | None = None
        for name in order:
            try:
                hits = await self._dispatch(name, q)
                if hits:
                    ranked = self._rank_hits(q, hits)
                    logger.info(
                        f"dl_senpai web_search ok provider={name} "
                        f"q={q!r} n={len(ranked)}"
                    )
                    return ranked[: self.max_results]
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning(f"dl_senpai web_search provider={name} failed: {e}")
        if last_err is not None:
            logger.warning(f"dl_senpai web_search all failed for {q!r}: {last_err}")
        return []

    async def _safe_dispatch(self, name: str, query: str) -> list[SearchHit]:
        try:
            return await self._dispatch(name, query)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"dl_senpai web_search provider={name} failed: {e}")
            return []

    def _rank_hits(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        scored = [(score_hit_relevance(query, hit), hit) for hit in hits]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [hit for _, hit in scored]

    def _merge_hits(self, query: str, batches: list[list[SearchHit]]) -> list[SearchHit]:
        seen: set[str] = set()
        scored: list[tuple[float, SearchHit]] = []
        for hits in batches:
            for hit in hits:
                key = (hit.url or hit.title or "").casefold()
                if not key or key in seen:
                    continue
                seen.add(key)
                scored.append((score_hit_relevance(query, hit), hit))
        scored.sort(key=lambda item: item[0], reverse=True)
        good = [hit for score, hit in scored if score >= 0.2]
        if good:
            return good[: self.max_results]
        return [hit for _, hit in scored[: self.max_results]]

    async def _search_auto_multi(self, query: str) -> list[SearchHit]:
        order = self._provider_order()
        batches = await asyncio.gather(*(self._safe_dispatch(name, query) for name in order))
        merged = self._merge_hits(query, list(batches))
        if merged:
            providers = sorted({hit.source for hit in merged if hit.source})
            logger.info(
                f"dl_senpai web_search auto_multi q={query!r} "
                f"n={len(merged)} providers={providers!r}"
            )
        return merged

    async def search_many(
        self,
        queries: list[str],
        *,
        intent_query: str = "",
        min_relevance: float = 0.35,
    ) -> str:
        """并行检索多个 query；弱结果时自动换词重试。"""
        cleaned = _dedupe_queries(queries)
        if not cleaned:
            return "【联网检索结果】\n（查询为空）"

        intent = (intent_query or cleaned[0]).strip()
        sections: list[str] = []
        weak_queries: list[str] = []

        async def _one(q: str) -> tuple[str, list[SearchHit]]:
            hits = await self.search(q)
            return q, hits

        first_pass = await asyncio.gather(*(_one(q) for q in cleaned))
        for q, hits in first_pass:
            sections.append(format_search_for_prompt(hits, query=q))
            if best_hit_relevance(intent, hits) < min_relevance:
                weak_queries.append(q)

        if weak_queries:
            fallback_queries = build_fallback_search_queries(
                intent or weak_queries[0],
                max_total=3,
            )
            fallback_queries = [
                q for q in fallback_queries if q.casefold() not in {c.casefold() for c in cleaned}
            ]
            if fallback_queries:
                logger.info(
                    f"dl_senpai web_search retry weak={weak_queries!r} "
                    f"fallback={fallback_queries!r}"
                )
                retry_pass = await asyncio.gather(*(_one(q) for q in fallback_queries))
                for q, hits in retry_pass:
                    if not hits:
                        continue
                    if best_hit_relevance(intent, hits) >= min_relevance * 0.7:
                        sections.append(format_search_for_prompt(hits, query=f"{q}（换词重试）"))
                    elif best_hit_relevance(q, hits) >= min_relevance:
                        sections.append(format_search_for_prompt(hits, query=f"{q}（换词重试）"))

        return "\n\n".join(sections)

    def _provider_order(self) -> list[str]:
        p = self.provider
        if p == "auto":
            order: list[str] = []
            if self.tavily_api_key:
                order.append("tavily")
            if self.searxng_url:
                order.append("searxng")
            order.extend(["ddgs", "bing", "duckduckgo"])
            return order
        if p in {"ddg", "duckduckgo_instant"}:
            return ["duckduckgo"]
        if p == "duckduckgo":
            return ["ddgs", "duckduckgo", "bing"]
        return [p]

    async def _dispatch(self, name: str, query: str) -> list[SearchHit]:
        if name == "tavily":
            return await self._search_tavily(query)
        if name == "searxng":
            return await self._search_searxng(query)
        if name == "ddgs":
            return await self._search_ddgs(query)
        if name == "bing":
            return await self._search_bing(query)
        if name == "duckduckgo":
            return await self._search_duckduckgo_instant(query)
        raise ValueError(f"unknown search provider: {name}")

    async def _search_duckduckgo_instant(self, query: str) -> list[SearchHit]:
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": _UA},
            follow_redirects=True,
        ) as client:
            resp = await client.get("https://api.duckduckgo.com/", params=params)
            resp.raise_for_status()
            data = resp.json()
        hits: list[SearchHit] = []
        abstract = str(data.get("AbstractText") or "").strip()
        abstract_url = str(data.get("AbstractURL") or "").strip()
        heading = str(data.get("Heading") or "").strip() or query
        if abstract:
            hits.append(
                SearchHit(
                    title=heading,
                    url=abstract_url,
                    snippet=abstract,
                    source="ddg-instant",
                )
            )
        for item in data.get("Results") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("Text") or "").strip()
            url = str(item.get("FirstURL") or "").strip()
            if title or url:
                hits.append(
                    SearchHit(title=title or url, url=url, snippet="", source="ddg")
                )
            if len(hits) >= self.max_results:
                break
        for topic in data.get("RelatedTopics") or []:
            if len(hits) >= self.max_results:
                break
            if isinstance(topic, dict) and "Topics" in topic:
                for sub in topic.get("Topics") or []:
                    if not isinstance(sub, dict):
                        continue
                    title = str(sub.get("Text") or "").strip()
                    url = str(sub.get("FirstURL") or "").strip()
                    if title or url:
                        hits.append(
                            SearchHit(
                                title=title or url,
                                url=url,
                                snippet="",
                                source="ddg",
                            )
                        )
                    if len(hits) >= self.max_results:
                        break
            elif isinstance(topic, dict):
                title = str(topic.get("Text") or "").strip()
                url = str(topic.get("FirstURL") or "").strip()
                if title or url:
                    hits.append(
                        SearchHit(title=title or url, url=url, snippet="", source="ddg")
                    )
        return hits[: self.max_results]

    async def _search_bing(self, query: str) -> list[SearchHit]:
        params = {"q": query, "setlang": "zh-hans", "mkt": "zh-CN"}
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={
                "User-Agent": _UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            follow_redirects=True,
        ) as client:
            resp = await client.get("https://www.bing.com/search", params=params)
            resp.raise_for_status()
            body = resp.text
        hits: list[SearchHit] = []
        for match in _BING_RESULT_RE.finditer(body):
            url = _clean_bing_url(match.group(1) or "")
            title = _strip_html(match.group(2) or "")
            snippet = _strip_html(match.group(3) or match.group(4) or "")
            if not title and not url:
                continue
            hits.append(
                SearchHit(
                    title=title or url,
                    url=url,
                    snippet=snippet[:280],
                    source="bing",
                )
            )
            if len(hits) >= self.max_results:
                break
        return hits

    async def _search_searxng(self, query: str) -> list[SearchHit]:
        if not self.searxng_url:
            return []
        params = {
            "q": query,
            "format": "json",
            "language": self.region,
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": _UA},
            follow_redirects=True,
        ) as client:
            resp = await client.get(f"{self.searxng_url}/search", params=params)
            resp.raise_for_status()
            data = resp.json()
        hits: list[SearchHit] = []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            snippet = str(item.get("content") or item.get("snippet") or "").strip()
            if not title and not url:
                continue
            hits.append(
                SearchHit(
                    title=title or url,
                    url=url,
                    snippet=snippet[:280],
                    source="searxng",
                )
            )
            if len(hits) >= self.max_results:
                break
        return hits

    async def _search_tavily(self, query: str) -> list[SearchHit]:
        if not self.tavily_api_key:
            return []
        payload: dict[str, Any] = {
            "api_key": self.tavily_api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": self.max_results,
            "include_answer": False,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        hits: list[SearchHit] = []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            snippet = str(item.get("content") or "").strip()
            if not title and not url:
                continue
            hits.append(
                SearchHit(
                    title=title or url,
                    url=url,
                    snippet=snippet[:280],
                    source="tavily",
                )
            )
            if len(hits) >= self.max_results:
                break
        return hits

    async def _search_ddgs(self, query: str) -> list[SearchHit]:
        try:
            from ddgs import DDGS  # type: ignore[import-untyped]
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # type: ignore[import-untyped]
            except ImportError:
                return []

        def _run() -> list[SearchHit]:
            hits: list[SearchHit] = []
            with DDGS() as ddgs:
                rows = ddgs.text(
                    query,
                    region=self.region if self.region != "zh-cn" else "cn-zh",
                    max_results=self.max_results,
                )
                for item in rows or []:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    url = str(item.get("href") or item.get("link") or "").strip()
                    snippet = str(item.get("body") or item.get("snippet") or "").strip()
                    if not title and not url:
                        continue
                    hits.append(
                        SearchHit(
                            title=title or url,
                            url=url,
                            snippet=snippet[:280],
                            source="ddgs",
                        )
                    )
                    if len(hits) >= self.max_results:
                        break
            return hits

        return await asyncio.to_thread(_run)


def searcher_from_config(config: Any) -> WebSearcher:
    return WebSearcher(
        provider=getattr(config, "search_provider", "auto"),
        max_results=getattr(config, "search_max_results", 5),
        timeout=float(getattr(config, "search_timeout", 12.0)),
        region=getattr(config, "search_region", "zh-cn"),
        searxng_url=getattr(config, "search_searxng_url", ""),
        tavily_api_key=getattr(config, "search_tavily_api_key", ""),
    )


def dumps_hits(hits: list[SearchHit]) -> str:
    payload = [
        {
            "title": h.title,
            "url": h.url,
            "snippet": h.snippet,
            "source": h.source,
        }
        for h in hits
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)
