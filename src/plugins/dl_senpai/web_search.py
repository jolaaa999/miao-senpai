"""联网检索：学姐伪指令 <<<SEARCH>>> 与 Cursor MCP 共用。

默认无需 Key：DuckDuckGo Instant Answer + Bing HTML 兜底。
可选：SearXNG / Tavily / 已安装的 ddgs。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import time
import json
import re
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .clock import beijing_now, current_beijing_year, is_asking_current_datetime
from .reply_quote import user_intent_text

try:
    # 正文抽取优先走 trafilatura：手写正则分不清正文与导航栏，
    # 实测天气站会把半屏菜单当正文塞进 prompt。缺失时静默退回正则。
    import trafilatura as _trafilatura
except Exception:  # noqa: BLE001 — 可选依赖，没装就退回内置正则
    _trafilatura = None

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
# 时效信号分两档，判据不同（2026-10-25 修）：
#   强：「最近 / 最新 / 出了吗 / 什么时候出」——问的就是「新不新」，单独命中就该搜；
#   弱：「今天 / 刚刚 / 现在」这类口语时间词——只说明「说的是眼下」，不说明要查新事实。
# 硬把弱信号当检索依据时，实测「你现在在干什么」「？你刚刚在说什么」
# 「今天好累啊，实验报告写不完」各触发 4 轮检索，把回复拖到 40 秒以上
# （于是她总在回答上一条），还把百度首页、知乎话题这类垃圾塞进 prompt。
_AUTO_SEARCH_TIME_STRONG_RE = re.compile(
    r"(最近|最新|当前|实时|出了吗|发布了吗|上线了吗|发售了吗|什么时候出|啥时候|"
    r"几号|哪天|20\d{2}年?)"
)
_AUTO_SEARCH_TIME_WEAK_RE = re.compile(
    r"(今天|今晚|昨夜|昨天|明天|今年|本月|这周|上周|刚刚|刚才|现在|目前|当下)"
)
# 兼容既有引用（looks_like_casual_banter 等按「并集」在用）：两档合起来
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
# 课本/基础概念题不必联网：模型自己就会，搜了只是浪费一轮并与无关网页对齐
# （清单与 grounding._TEXTBOOK_EXEMPT_RE 保持一致，两边判定不能互相打架）
_AUTO_SEARCH_TEXTBOOK_RE = re.compile(
    r"(过拟合|欠拟合|梯度|反向传播|链表|二叉树|复杂度|docker|linux|"
    r"python|java|算法题|leetcode|洛谷|icpc|transformer|cnn|rnn|"
    r"batch\s*norm|adam|sgd|交叉熵|损失函数|正则化)",
    re.IGNORECASE,
)
_AUTO_SEARCH_HOW_RE = re.compile(r"(怎么|怎样|咋|如何|多少|多久|多远|几天|几次|几个|几点)")
# 情绪化用法不算打听事实：「怎么这么好吃」「咋又是我」
_AUTO_SEARCH_HOW_EMO_RE = re.compile(r"(怎么这么|咋这么|怎么又|咋又|怎么才|怎么还|怎么就)")
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
    # 发布日期（YYYY-MM-DD / YYYY-MM，取不到就是空串）。
    # 为什么必须有这个字段：没有日期的检索简报，模型**无从判断内容是不是过期的**，
    # 只能默认"搜到的就是最新的" —— 实测就是这样把上赛季的旧内容当成当期答案抄了。
    date: str = ""
    # 网页正文摘录（抓取失败就是空串）。
    # 为什么要有：摘要往往只有标题级信息（"XX官网首页"），模型拿着它只能照着
    # 标题编、或者干脆说"只搜到官网"。补一段正文，它才看得到真正的数值/名单/日期。
    body: str = ""


_DIGIT_DATE_RE = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")
_YEAR_MONTH_RE = re.compile(r"(20\d{2})[-/.](\d{1,2})")


def normalize_hit_date(raw: Any) -> str:
    """把各后端五花八门的日期压成 YYYY-MM-DD，取不出来返回空串。

    后端返回的形状差别很大：Tavily 是 ISO（2026-10-12T00:00:00Z），
    SearXNG 也是 ISO，ddgs 有时只给 YYYY-MM。统一压平后渲染层不用分支。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    m = _DIGIT_DATE_RE.search(text)
    if m:
        month, day = int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return "%s-%02d-%02d" % (m.group(1), month, day)
    m2 = _YEAR_MONTH_RE.search(text)
    if m2:
        month = int(m2.group(2))
        if 1 <= month <= 12:
            return "%s-%02d" % (m2.group(1), month)
    return ""


# 「5 天前」「昨天」这类相对时间同样说明内容有新鲜度，只是没有绝对日期。
_RELATIVE_TIME_RE = re.compile(
    r"(\d+\s*(?:分钟|小时|天|周|个月)前|昨天|今天|刚刚|本周|上周|本月|上月|今日)"
)


def mentions_current_year(hit: SearchHit) -> bool:
    """标题/摘要里出现了当前年份，就算这条结果有时效线索。

    为什么不能只看结构化 date 字段：ddgs 十有八九不返回日期，
    但摘要里明明写着「2026年3月25日」——只认 date 会导致明明是新内容
    还被判成"没有时效依据"，白跑一轮补搜（实测多花 5~7 秒）。
    """
    text = f"{hit.title} {hit.snippet} {hit.body}"
    if str(current_beijing_year()) in text:
        return True
    return bool(_RELATIVE_TIME_RE.search(text))


def count_dated_hits(hits: list[SearchHit]) -> int:
    """这批结果里有几条带发布日期。"""
    return sum(1 for h in hits if (h.date or "").strip())


@dataclass
class SearchRequest:
    text: str
    queries: list[str]


def augment_queries_with_intent(
    queries: list[str],
    intent: str,
    *,
    max_total: int = 3,
) -> list[str]:
    """保证用户原话一定参与一次检索，别让模型把它压成跑偏的关键词。

    为什么需要：模型习惯把整句压成两三个词再搜，限定词一丢就换了话题
    （实测「我们学校图书馆馆长是谁」被压成「图书馆馆长」，搜回来的全是别的学校）。
    代价是多一轮检索，换来的是不跑偏 —— 比搜错方向后再补贴钱更划算。
    """
    out = _dedupe_queries([q for q in (queries or []) if (q or '').strip()])
    raw = _normalize_for_search_heuristics(intent or '')
    if not raw or len(raw) < 6:
        return out[:max_total]
    joined = ''.join(q.casefold() for q in out)
    # 模型已经基本照抄原话时不必重复，省一轮预算
    if raw.casefold() in joined:
        return out[:max_total]
    out.append(raw[:_QUERY_MAX_CHARS])
    return out[:max_total]

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


# 检索词的有效长度下限：低于这个数就算"被压缩成了一个词"。
# 为什么定 4：实测两三个字的词（「签到」「弓」「攻略」）在搜索引擎里
# 完全没有语境，捞回来的全是同名歌曲、课堂考勤、DNF 入口这类东西。
# ddgs 的默认 auto 后端会挨个试 mojeek/yandex/brave 这些慢源，
# 实测同一 query 要 8.8 秒然后整体超时；指名 google 只要 3 秒且中文质量更好。
_DDGS_PREFERRED_BACKEND = "google"

_QUERY_MIN_MEANINGFUL = 4
# 并回原问题时的语境上限：太长会盖过模型自己的检索意图
_QUERY_CONTEXT_MAX_CHARS = 30


def _meaningful_len(text: str) -> int:
    """检索词的有效字符数（只数汉字/字母/数字，忽略空格标点）。"""
    return len(re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text or ""))


def prepare_tool_queries(queries: list[str], user_text: str = "") -> list[str]:
    """把模型写出的检索词补上语境：过短的词并回用户原话。

    为什么需要这一层：模型常把问题压成一两个词（「签到」「弓」「攻略」），
    单独搜必然跑偏 —— 实测「你也可以找 签到」被搜成歌名，
    《孝庄秘史》主题曲排在第一条，然后被当成答案抄进回复。
    规则：有效字符不足 4 个时，把用户原话（剥掉问句套话）并进检索词，
    让它同时带上「想找什么」和「在什么语境下」。原词始终排在最前，
    模型自己的意图不会被盖掉。
    """
    ctx = refine_search_query(user_text) if user_text else ""
    if len(ctx) > _QUERY_CONTEXT_MAX_CHARS:
        ctx = ctx[:_QUERY_CONTEXT_MAX_CHARS].strip()
    out: list[str] = []
    seen: set[str] = set()
    for raw in queries or []:
        q = refine_search_query(raw)
        if not q:
            continue
        # 只在"被压缩过"时才补：写全的检索词原样放行，不擅自改写
        if _meaningful_len(q) < _QUERY_MIN_MEANINGFUL and ctx:
            if q in ctx:
                # 原话里本来就写着这个词：直接用原话当检索词，
                # 拼起来会把同一个词写两遍（「签到 你也可以找 签到」），反而更糊
                q = ctx
            elif ctx not in q:
                q = f"{q} {ctx}"
            q = q[:_QUERY_MAX_CHARS].strip()
        key = q.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


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


# 时效敏感词：单独一条正则，不复用 _AUTO_SEARCH_TIME_RE。
# 后者是"要不要自动联网"的判定，含"几号/哪天"这类问日期的词；
# 这里问的是"答案会不会随时间变"，赛季/版本/补丁/热修 同样属于会变的内容。
# 这类问题问的是"此刻的事实"，不是"最新版本/发布会"。给它们补搜
# 「X 最新」「X 2026年10月」只会换回一堆同义结果，实测白花 7 秒。
_FRESHNESS_SKIP_RE = re.compile(
    r"(天气|气温|温度|几度|下雨|下雪|空气质量|汇率|牌价|油价|限行|路况|比分|赛程|水位|潮汐)"
)

_FRESHNESS_RE = re.compile(
    r"(最新|最近|当前|目前|实时|今天|今日|昨天|明天|刚刚|刚才|今年|本月|本周|这周|上周|"
    r"赛季|版本|更新|补丁|热修|热补丁|上线|发售|开服|停服|维护|公告|"
    r"出了吗|发布了吗|上线了吗|发售了吗|什么时候出|啥时候|20\d{2}年?)",
    re.IGNORECASE,
)


def is_time_sensitive_question(text: str) -> bool:
    """问题是否在问"会随时间变"的事（最新/版本/赛季/补丁…）。

    判定为真且首轮结果没有任何日期时，才值得再补搜一轮 ——
    这条闸门避免闲聊也去做第二轮检索，白花时间和 token。
    """
    raw = _normalize_for_search_heuristics(text) or (text or "")
    if len(raw.strip()) < 2:
        return False
    return bool(_FRESHNESS_RE.search(raw))


def build_freshness_queries(text: str, *, max_total: int = 2) -> list[str]:
    """时效问题的补搜词：给核心词加上"最新"与年月，逼出当期内容。

    为什么不复用 build_fallback_search_queries：那套是按**相关度太低**触发的
    （best_hit_relevance < 阈值）。"相关但对不上当期"恰恰相关度很高，
    永远走不到那条路 —— 而它才是出错最多的一种。
    """
    base = refine_search_query(text) or _compact_query_core(text) or (text or "").strip()
    base = _WS_RE.sub(" ", base).strip()
    if not base:
        return []
    now = beijing_now()
    core = _compact_query_core(base) or base
    found: list[str] = []

    def add(q: str) -> None:
        item = _WS_RE.sub(" ", (q or "").strip())
        if item and len(item) <= _QUERY_MAX_CHARS:
            found.append(item)

    # 先试"最新"这类词（搜索引擎对时效词更敏感），再补具体年月（对当期更准）
    add("%s 最新" % core)
    add("%s %d年%d月" % (core, now.year, now.month))
    add("%s 更新公告 %d" % (core, now.year))
    return _dedupe_queries(found)[: max(1, max_total)]


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
    if _AUTO_SEARCH_HOW_RE.search(raw) and not _AUTO_SEARCH_HOW_EMO_RE.search(raw):
        return True
    # 这里曾经有一条 `_QUERY_TERM_RE.search(raw) and len(raw) >= 8`：
    # 而 _QUERY_TERM_RE 是「任意两个连续汉字」，等于「消息够 8 个字就搜」。
    # 实测把「补药再踢了，可以免打扰」「踢了吧，等我调好我再拉进来玩」
    # 「你也可以找 签到」这类纯闲聊全判成事实查询，各搜了 5 轮全是无关结果。
    # 所以只认明确的疑问式，不再看长度和实词。
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
    # 问「现在几点 / 今天几号」这类：系统时钟就能答（prompt 里有【当前时间】），
    # 联网只会翻出「北京时间校准」之类的垃圾页。判据刻意收窄 —— 必须同时满足
    # 长度短、不含英文/数字、没有新闻/事实类的锚点，否则「iPhone 17 几号发售」
    # 这种真打听会被一起吞掉（它命中「几号」，也命中 topic 的「发售」）。
    if (
        is_asking_current_datetime(raw)
        and len(raw) <= 10
        and not re.search(r"[A-Za-z0-9]", raw)
        and not _AUTO_SEARCH_TOPIC_RE.search(raw)
        and not _AUTO_SEARCH_FACTUAL_RE.search(raw)
    ):
        return False
    if _AUTO_SEARCH_TOPIC_RE.search(raw):
        return True
    # 强时效词单独成立；弱口语时间词必须同时有「新闻/事实」类的锚点才搜
    if _AUTO_SEARCH_TIME_STRONG_RE.search(raw) and len(raw) >= 4:
        return True
    if (
        _AUTO_SEARCH_TIME_WEAK_RE.search(raw)
        and len(raw) >= 6
        and (
            _AUTO_SEARCH_TOPIC_RE.search(raw)
            or _AUTO_SEARCH_FACTUAL_RE.search(raw)
        )
    ):
        return True
    if _AUTO_SEARCH_FACTUAL_RE.search(raw) and len(raw) >= 5:
        return True
    # 「怎么搭/多少钱/多久出」这类明确的打听语气也算事实查询。
    # 为什么不复用原来的「长度」判据：那条会把中文闲聊全扫进来（实测三句日常话
    # 各触发 5 轮无关检索）；疑问词能把「冰弓前期配装怎么搭」留下、
    # 把「补药再踢了，可以免打扰」挡住。
    if (
        _AUTO_SEARCH_HOW_RE.search(raw)
        and len(raw) >= 6
        and not _AUTO_SEARCH_HOW_EMO_RE.search(raw)
        and not _AUTO_SEARCH_TEXTBOOK_RE.search(raw)
    ):
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


async def grounding_prefetch(
    text: str,
    config: Any,
    *,
    max_queries: int | None = None,
    trend_titles: list[str] | None = None,
    aggressive: bool | None = None,
) -> tuple[str, list[str]]:
    """自动预判并检索；返回 (brief, queries)。无需检索时 brief 为空。

    Phase 6.6 命名澄清（第二十六节）：

        原名 `prefetch_search_brief` 容易让人误以为它是 SEARCH Tool 的
        第二个实现。实际上它是 **Model orchestration 的 grounding 预取**：

            - 在**首次模型响应之前**执行（llm.chat L408，模型调用在 L506）
            - 由 heuristic（`should_auto_search`）决定是否检索
            - 结果直接作为 context 注入 prompt，不是模型请求的 Tool 调用

        真正的 SEARCH Tool 走 `ToolRegistry` → `execute_search`；
        模型在响应里写 `<<<SEARCH ...>>>` 时触发的**多轮 grounding 循环**
        在 `llm.chat` 内，与 Tool 共享同一个底层 searcher。

        三者关系：
            grounding_prefetch   ← 本函数（响应前预取）
            multi-round grounding ← llm.chat 循环（模型写 marker 后补检索）
            SEARCH Tool           ← ToolRegistry + execute_search（统一入口）
    """
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
        # 把总预算交给 search_many 自己分摊到每个查询上。
        # 不传的话每段都按默认上限生成，拼完再整体截断，只有第一条能活下来。
        brief = await searcher.search_many(
            queries,
            intent_query=queries[0],
            max_chars=int(getattr(config, "search_prompt_max_chars", 2400)),
            # 传原始问题而不是检索词：时效词（最新/补丁/赛季）在用户话里，
            # 而 search_many 要靠它决定"整批无日期时是否再补一轮"。
            freshness_intent=text,
        )
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


def hit_domain(url: str) -> str:
    """取结果链接的域名（去掉 www.）。

    交叉验证只认域名：同一个站点的三条结果仍是「一个来源」，
    不能因为条数多就当成了多方印证。
    """
    try:
        netloc = urlparse((url or "").strip()).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def count_independent_domains(hits: list[SearchHit]) -> int:
    """这批结果来自几个互不相同的域名。"""
    return len({d for d in (hit_domain(h.url) for h in hits) if d})


def format_search_for_prompt(
    hits: list[SearchHit],
    *,
    query: str = "",
    max_chars: int = 2400,
) -> str:
    if not hits:
        q = f"「{query}」" if query else ""
        return f"【联网检索结果】{q}\n（没有搜到可用结果）"
    head = f"【联网检索结果】查询：{query}" if query else "【联网检索结果】"
    # 把来源数量和日期覆盖情况摆在最前面：前者决定能不能互相印证，
    # 后者决定**有没有资格谈时效**。一条日期都没有时必须明说，
    # 否则模型会默认"搜索返回的就是最新的"。
    dated = count_dated_hits(hits)
    head += f"（共 {len(hits)} 条 / {count_independent_domains(hits)} 个独立来源）"
    if dated:
        head += f"\n（其中 {dated} 条带发布日期，见行尾）"
    else:
        head += (
            "\n（本批结果没有任何发布日期：你无法据此判断时效。"
            "涉及版本/赛季/补丁/最新/今天的问题，先补搜再下结论，不要默认它是最新的。）"
        )
    # 没抓到正文的条目只能看到搜索页给的那段话。不点明的话，模型会把
    # 「这套很强」「配装大改」当成完整资料，然后自己把装备名/珠子/数值补上
    # ——群里看到的就是「搜了但还是不准」。
    only_snippet = sum(1 for h in hits if not (h.body or "").strip())
    # 预算太小时不塞这条提示：保住标题和 URL 比多一句告示重要
    if only_snippet and max_chars >= 600:
        head += (
            f"\n（{only_snippet} 条只有摘要、没抓到正文："
            "摘要没写的数值/装备/等级/步骤别自己补，问就说没查到）"
        )
    lines = [head]
    used = len(head) + 1
    for i, hit in enumerate(hits, start=1):
        title = (hit.title or "无标题").strip()
        snippet = (hit.snippet or "").strip()
        url = (hit.url or "").strip()
        # 标域名而不是引擎名：同一引擎的多条结果经常来自同一个站，
        # 只写引擎名会让模型误以为拿到了多个来源。
        tag = hit_domain(url) or hit.source
        src = f" · {tag}" if tag else ""
        when = f" · {hit.date}" if (hit.date or "").strip() else ""
        block = f"{i}. {title}{src}{when}"
        # 抓到正文就用正文顶替摘要：摘要通常只是正文开头的一小段，
        # 两条一起放等于同一段话占两遍预算。
        body = (hit.body or "").strip()
        if body:
            snippet = body if len(body) <= 320 else body[:319] + "…"
            block += f"\n   【网页正文】{snippet}"
        elif snippet:
            block += f"\n   （仅搜索摘要，未见正文）{snippet}"
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


# 明显与「找资料」意图相反的页面：字典释义、磁力/种子聚合。
# 实测 ddgs 会把它们排在前面（问翁恺的课，首屏全是「翁 怎么读」），
# 靠标题/摘要特征识别比维护域名黑名单稳，也不怕换域名。
_JUNK_HIT_RE = re.compile(
    # 简体词也要列全：原来只有繁体「漢語」，问中文相关问题时首屏仍被字典站占满
    r"磁力|种子下载|torrent|bt下载|迅雷|字典|词典|拼音|笔画|部首|怎么读|读音|释义"
    r"|漢語|汉语|汉字|繁体|组词",
    re.I,
)


def _is_junk_hit(hit: SearchHit) -> bool:
    """标题或摘要带噪音特征的结果直接丢弃。"""
    blob = "%s %s" % (getattr(hit, "title", "") or "", getattr(hit, "snippet", "") or "")
    return bool(_JUNK_HIT_RE.search(blob))


# 这类问法要的是「页面里的具体数值」，不是一段概述。
# 只有命中它们才值得为一条正文去开浏览器渲染（一条 5~10 秒）。
_DETAIL_INTENT_RE = re.compile(
    r"配装|流派|打法|怎么打|攻略|思路|build|参数|规格|配置|型号|价位|价格|多少钱|对比|评测"
    r"|技能|珠子|加点|天赋|面板|数值|表格|清单|配方|步骤|教程|设置|安装|报错|代码|公式",
    re.I,
)


def needs_page_detail(query: str) -> bool:
    """这问题是否值得动用浏览器兜底去抓正文。"""
    return bool(_DETAIL_INTENT_RE.search(query or ""))


# 静态抓取拿到的东西少于这个字数，基本等于空壳（只剩站名/导航）
_MIN_STATIC_BODY_CHARS = 200

# 这几个站对无头浏览器同样动手：知乎直接回「请求存在异常，限制本次访问」，
# 贴吧弹滑块验证。实测给到 23 秒也拿不到一个字，只会白等。
# 所以它们的正文就此放弃——摘要照样留在 prompt 里（并标注「仅摘要」）。
_HARD_BLOCKED_HOSTS = (
    "zhihu.com",
    "tieba.baidu.com",
    "bbs.nga.cn",
    "ngabbs.com",
    "nga.178.com",
    "xiaohongshu.com",
    "weibo.com",
    "weibo.cn",
)


def _host_is_hard_blocked(url: str) -> bool:
    """是不是那种连浏览器都过不去的高防站点。"""
    host = (hit_domain(url) or "").lower()
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in _HARD_BLOCKED_HOSTS)

# 明显读不到正文的链接直接跳过：抓了也只会拿到二进制或者必须登录的壳
_PAGE_SKIP_EXT_RE = re.compile(
    r"\.(?:pdf|jpe?g|png|gif|webp|svg|mp4|mp3|zip|rar|7z|docx?|xlsx?|pptx?)(?:$|[?#])",
    re.I,
)
# 这些标签里的内容不是给人看的正文（脚本/CSS/图标），留着只会污染摘录
_PAGE_DEAD_TAG_RE = re.compile(
    r"<(script|style|noscript|svg|template)\b[^>]*>.*?</\1\s*>",
    re.I | re.S,
)
# 网页壳上的导航话术：与内容无关，去掉能提高摘录密度
_PAGE_CHROME_RE = re.compile(
    r"(登录|注册|扫码|下载客户端|APP下载|版权所有|ICP备|免责声明|无障碍|返回顶部)",
)


def _excerpt_from_html(raw: str, *, query: str = "", max_chars: int = 500) -> str:
    """从网页 HTML 里切一段与查询最相关的纯文本。

    为什么按查询词定位窗口、而不是直接截开头：多数页面的开头是导航和
    公告，真正的内容在中段。按首个命中词往前挪一点再切，命中的概率高得多。
    """
    body = _PAGE_DEAD_TAG_RE.sub(" ", raw or "")
    # 块级标签当断行，避免相邻段落、列表项粘成一串分不清的话
    body = re.sub(r"</?(?:br|p|div|li|tr|h[1-6]|section|article)\b[^>]*>", " ", body, flags=re.I)
    text = _strip_html(body)
    text = _PAGE_CHROME_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return ""
    return _trim_window(text, query=query, max_chars=max_chars)


def _trim_window(text: str, *, query: str = "", max_chars: int = 500) -> str:
    """按查询词定位后截取 max_chars 的一段。

    为什么按查询词定位、而不是直接截开头：多数页面的开头是导航和公告，
    真正的内容在中段。按首个命中词往前挪一点再切，命中的概率高得多。
    """
    if len(text) <= max_chars:
        return text
    start = 0
    for term in sorted(_query_terms(query), key=len, reverse=True)[:6]:
        idx = text.casefold().find(term)
        if idx > 0:
            start = max(0, idx - max_chars // 4)
            break
    window = text[start : start + max_chars].strip()
    if start > 0:
        window = "…" + window
    if start + max_chars < len(text):
        window = window + "…"
    return window


def _extract_main_text(raw: str, *, query: str = "", max_chars: int = 500) -> str:
    """抽网页正文：优先 trafilatura，抽空则退回正则窗口。"""
    if _trafilatura is not None:
        try:
            text = _trafilatura.extract(
                raw or "",
                include_comments=False,
                include_tables=True,
                # favor_recall 偏保守（宁可多给）更合适：正文短一点没关系，
                # 漏掉关键段落会让模型只能照标题编。
                favor_recall=True,
            )
        except Exception:  # noqa: BLE001 — 抽取失败等同"没抽到"，不阻塞检索
            text = None
        if text:
            flat = _WS_RE.sub(" ", text).strip()
            if flat:
                return _trim_window(flat, query=query, max_chars=max_chars)
    return _excerpt_from_html(raw, query=query, max_chars=max_chars)


def _title_shingles(title: str, n: int = 3) -> set[str]:
    """标题的字符 n-gram 集合，用来认出同一篇稿子在不同站点的转载。"""
    norm = re.sub(r"[\s\W_]+", "", (title or "").casefold())
    if not norm:
        return set()
    if len(norm) < n:
        return {norm}
    return {norm[i : i + n] for i in range(len(norm) - n + 1)}


def is_reprint(
    a: SearchHit,
    b: SearchHit,
    *,
    threshold: float = 0.8,
    min_common: int = 8,
) -> bool:
    """两条结果的标题几乎一致时判为转载（URL 不同、内容同源）。"""
    sa, sb = _title_shingles(a.title), _title_shingles(b.title)
    if not sa or not sb:
        return False
    common = len(sa & sb)
    # 共用的字太少就只是"都提到了同一个热点"，不是同一篇稿。
    if common < min_common:
        return False
    smaller = min(len(sa), len(sb))
    # 长度差超过一倍说明只是共享了开头几个词，不能算转载。
    if max(len(sa), len(sb)) > smaller * 2:
        return False
    # 用重叠系数而不是 Jaccard：转载标题常在尾部挂不同站点名（"- 新浪游戏" / "- 网易"），
    # Jaccard 会被这点差异拖到 0.70 以下而漏判，重叠系数更贴近"是不是同一稿"。
    return common / smaller >= threshold


async def _browser_page_excerpt(
    url: str,
    *,
    query: str,
    max_chars: int,
    timeout: float,
    cfg: Any = None,
) -> str:
    """浏览器兜底：真开一次页面，把渲染后的正文拿回来。

    为什么要兜底：中文圈的干货基本都在知乎/贴吧/B站专栏/NGA 上，
    而这些站要么 403 挡爬虫，要么首屏靠 XHR（静态 HTML 只有壳）。
    实测不兜底时这批站点的正文成功率约三成，模型只能拿搜索摘要去编细节。

    为什么复用 link_reader 而不是自己写 Playwright：那边已经踩过坑了
    （等正文节点而不是 sleep、只剩 <title> 时不算读到、超长按头尾截断）。
    """
    if _host_is_hard_blocked(url):
        return ""
    budget = max(8.0, timeout + 5.0)
    # 先直接渲染：read_link 在 HTTP 层吃 403 就直接认输返回，
    # 而 403（知乎/贴吧/NGA）恰恰是最该上浏览器的那一类。
    try:
        from .link_reader import _browser_render

        _title, body = await asyncio.wait_for(_browser_render(url, cfg), timeout=budget)
    except asyncio.TimeoutError:
        logger.info(f"dl_senpai web_search browser render timeout url={url!r} budget={budget:.1f}s")
        body = ""
    except Exception as exc:  # noqa: BLE001 — 没装 playwright 就安静退化
        logger.info(f"dl_senpai web_search browser render failed url={url!r}: {type(exc).__name__}: {exc}")
        body = ""
    body = (body or "").strip()
    if len(body) >= _MIN_STATIC_BODY_CHARS:
        logger.info(f"dl_senpai web_search browser render ok url={url!r} chars={len(body)}")
        trimmed = _trim_window(body, query=query, max_chars=max_chars)
        return trimmed or body[:max_chars]
    # 渲染也空手，再退回 read_link：它带磁盘缓存，PDF/纯文本链接也认
    try:
        from .link_reader import read_link

        doc = await asyncio.wait_for(read_link(url, cfg=cfg, focus=query), timeout=budget)
    except Exception as exc:  # noqa: BLE001
        logger.info(f"dl_senpai web_search browser fallback failed url={url!r}: {exc}")
        return ""
    if not doc.ok:
        logger.info(f"dl_senpai web_search browser fallback empty url={url!r} err={doc.error}")
        return ""
    logger.info(
        f"dl_senpai web_search browser fallback ok url={url!r} "
        f"src={doc.source} chars={doc.total_chars}"
    )
    trimmed = _trim_window(doc.text, query=query, max_chars=max_chars)
    return trimmed or (doc.text or "")[:max_chars]


async def fetch_page_excerpt(
    url: str,
    *,
    query: str = "",
    timeout: float = 6.0,
    max_chars: int = 500,
    browser_fallback: bool = False,
    cfg: Any = None,
) -> str:
    """抓页面正文里的一段。失败一律返回空串。

    为什么容错到这里为止：抓正文是**加分项**，失败只意味着少一份证据，
    不能让整次检索跟着失败（有些站会挡爬虫、会超时、会返回登录页）。
    """
    target = (url or "").strip()
    if not target.startswith(("http://", "https://")):
        return ""
    if _PAGE_SKIP_EXT_RE.search(target):
        return ""
    text = ""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(target)
            resp.raise_for_status()
            ctype = (resp.headers.get("content-type") or "").lower()
            # 非文本响应（图片、压缩包、PDF）没有可读正文，直接放弃
            if ctype and "html" not in ctype and "text" not in ctype:
                return ""
            raw = resp.text
    except Exception as exc:  # noqa: BLE001
        logger.info(f"dl_senpai web_search fetch_page failed url={target!r}: {exc}")
        raw = ""
    if raw:
        text = _extract_main_text(raw, query=query, max_chars=max_chars)
    # 静态抓取拿到的东西还够用，就别开浏览器（贵一个数量级）
    if len(text) >= _MIN_STATIC_BODY_CHARS or not browser_fallback:
        return text
    return await _browser_page_excerpt(
        target, query=query, max_chars=max_chars, timeout=timeout, cfg=cfg
    )


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
        min_independent_domains: int = 1,
        fetch_body: bool = False,
        body_timeout: float = 6.0,
        body_max_chars: int = 500,
        body_max_sections: int = 3,
        deadline: float = 12.0,
        browser_fallback: bool = False,
        config: Any = None,
    ) -> None:
        # 抓正文默认关：测试与 MCP 场景里结果 URL 常常是假的，
        # 默认开会白等超时。装配层（searcher_from_config）按配置打开。
        # 整次检索的总预算。调用方（llm.py / 工具执行器）没有外层超时，
        # 不设预算时首轮+换词重试+时效补搜三波串行，实测能拖到 25 秒，
        # QQ 群里等到这个程度的回复已经没意义了。
        self.deadline = float(deadline) if deadline and float(deadline) > 0 else 0.0
        self._deadline_at = 0.0
        self.fetch_body = bool(fetch_body)
        self.body_timeout = max(2.0, float(body_timeout))
        self.body_max_chars = max(200, int(body_max_chars))
        self.body_max_sections = max(1, int(body_max_sections))
        self.provider = (provider or "auto").strip().lower() or "auto"
        self.max_results = max(1, min(10, int(max_results)))
        # 少于这个数量的独立来源时，会在结果里明确标注「未获多方印证」
        self.min_independent_domains = max(1, int(min_independent_domains))
        self.timeout = max(3.0, float(timeout))
        self.region = (region or "zh-cn").strip() or "zh-cn"
        self.searxng_url = (searxng_url or "").strip().rstrip("/")
        self.tavily_api_key = (tavily_api_key or "").strip()
        # 静态抓不到正文时是否开浏览器兜底（403/SPA 站点）；
        # 只有详情类问题才会真的动用，见 needs_page_detail。
        self.browser_fallback = bool(browser_fallback)
        # link_reader 需要 cfg 才能读到自己的配置和缓存目录，透传过去
        self.config = config

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
        # 单个 provider 的超时既受 self.timeout 约束，也受本轮剩余预算约束。
        # 为什么外层的 asyncio.wait 不够：它的定时器跑在 event loop 上，
        # 正文解析（trafilatura）这类同步 CPU 活会把定时器推迟，
        # 内层再兜一道 wait_for 才能真正封住尾部延迟。
        limit = self._budget()
        timeout = self.timeout if limit is None else max(1.0, min(self.timeout, limit))
        try:
            return await asyncio.wait_for(self._dispatch(name, query), timeout=timeout)
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
        # 转载去重：同一篇稿子在新浪/网易/百家号各有一条，URL 不同、标题几乎一致。
        # 不去掉它们会占满 max_results 的名额，模型看到的是"一家之言"被复制了三遍。
        deduped: list[tuple[float, SearchHit]] = []
        for score, hit in scored:
            if any(is_reprint(hit, kept) for _, kept in deduped):
                continue
            deduped.append((score, hit))
        scored = deduped
        good = [hit for score, hit in scored if score >= 0.2]
        # 先剔噪音：字典释义、磁力/种子聚合这类页面与「找资料」意图相反，
        # 实测 ddgs 会把它们排到最前（问翁恺的课，首屏全是「翁 怎么读」）。
        good = [hit for hit in good if not _is_junk_hit(hit)]
        clean = [hit for _, hit in scored if not _is_junk_hit(hit)]
        # 一条都不达标时仍退回全量兜底（单后端的结果照样能进），
        # 但退回的是「非噪音」全量，不再把字典站/磁力站塞进 prompt。
        pool = good or clean
        if not pool:
            return []
        # 域名先行：默认只取 5 条，若让同一站点占满名额，模型看到的就只是
        # 一家之言，无从交叉印证。先每个域名取一条，铺满不同来源后再回填同域名的高分结果。
        picked: list[SearchHit] = []
        picked_idx: set[int] = set()
        used_domains: set[str] = set()
        for idx, hit in enumerate(pool):
            dom = hit_domain(hit.url)
            if dom and dom in used_domains:
                continue
            picked.append(hit)
            picked_idx.add(idx)
            if dom:
                used_domains.add(dom)
            if len(picked) >= self.max_results:
                return picked
        for idx, hit in enumerate(pool):
            if idx in picked_idx:
                continue
            picked.append(hit)
            if len(picked) >= self.max_results:
                break
        return picked

    async def _enrich_bodies(self, intent: str, hits: list[SearchHit]) -> list[SearchHit]:
        """给最靠前的几条结果补网页正文摘录。

        只抓前两条：正文是"够用就好"的信息，抓满五条既慢又挤占 prompt。
        详情由模型自己写 <<<SEARCH>>> 再搜，这里只保证它**看得到内容**，
        不至于拿着一个标题就去编。
        """
        if not self.fetch_body or not hits:
            return hits
        # 浏览器兜底一条要 5~10 秒，并行开多个容易互相拖死，
        # 所以只在它生效时退化成「只抓最相关的一条」。
        use_browser = self.browser_fallback and needs_page_detail(intent)
        # 风控站（知乎/贴吧/NGA）连浏览器都过不去，别把抓正文的名额浪费在它们身上
        targets = [
            i for i, h in enumerate(hits)
            if (h.url or "").startswith(("http://", "https://"))
            and not _host_is_hard_blocked(h.url)
        ][:2]
        if not targets:
            return hits

        # 只有第一条允许动用浏览器：它最相关，也最值这几秒。
        # 第二条走便宜路线，纯静态能拿到就算赚。
        async def _one(idx: int, allow_browser: bool) -> tuple[int, str]:
            excerpt = await fetch_page_excerpt(
                hits[idx].url,
                query=intent,
                timeout=self.body_timeout,
                max_chars=self.body_max_chars,
                browser_fallback=allow_browser,
                cfg=self.config,
            )
            return idx, excerpt

        # 谁先抓到算谁的，慢的走人。
        # 之前用 wait_for(gather(...)) 整体兜底，实测一个 SPA 页面（B 站专栏）
        # 慢 5 秒就把同一批里已经抓到的正文一起丢掉，等于白抓。
        tasks = [
            asyncio.create_task(_one(idx, use_browser and pos == 0))
            for pos, idx in enumerate(targets)
        ]
        done, pending = await asyncio.wait(tasks, timeout=self.body_timeout + 1.0)
        for task in pending:
            task.cancel()
        out = list(hits)
        got = 0
        for task in done:
            if task.cancelled() or task.exception() is not None:
                continue
            idx, excerpt = task.result()
            if excerpt:
                out[idx] = replace(out[idx], body=excerpt)
                got += 1
        logger.info(f"dl_senpai web_search body enrich got={got}/{len(targets)}")
        return out

    async def _search_auto_multi(self, query: str) -> list[SearchHit]:
        order = self._provider_order()
        # 有正经主源（Tavily / 自建 SearXNG）时先只问主源：
        # 实测并抓 Bing HTML 只会用拆词结果稀释好结果，还平白多一份延迟。
        # 主源够用就直接收工，空手才退回兜底链。
        primary = [name for name in order if name in {"tavily", "searxng"}]
        if primary:
            primary_batches = await asyncio.gather(
                *(self._safe_dispatch(name, query) for name in primary)
            )
            primary_hits = self._merge_hits(query, list(primary_batches))
            if len(primary_hits) >= min(3, self.max_results):
                providers = sorted({hit.source for hit in primary_hits if hit.source})
                logger.info(
                    f"dl_senpai web_search primary q={query!r} "
                    f"n={len(primary_hits)} providers={providers!r}"
                )
                return primary_hits
        rest = [name for name in order if name not in primary]
        batches = await asyncio.gather(*(self._safe_dispatch(name, query) for name in rest))
        if primary:
            batches = list(batches) + list(primary_batches)  # type: ignore[possibly-undefined]
        merged = self._merge_hits(query, list(batches))
        if merged:
            providers = sorted({hit.source for hit in merged if hit.source})
            logger.info(
                f"dl_senpai web_search auto_multi q={query!r} "
                f"n={len(merged)} providers={providers!r}"
            )
        return merged

    def _has_primary_source(self) -> bool:
        """是否配了正经搜索 API 主源（Tavily / SearXNG）。"""
        return bool(self.tavily_api_key or self.searxng_url)

    def _budget(self, share: float = 1.0) -> float | None:
        """按剩余预算分配本轮超时；没设预算就返回 None（不限）。"""
        if not self._deadline_at:
            return None
        left = self._deadline_at - time.monotonic()
        if left <= 0.5:
            # 兜底给半秒：宁可让这一轮只拿到快 provider 的结果，也别直接砍空。
            return 0.5
        return left * share

    async def _bounded(
        self,
        coros: list[Any],
        timeout: float | None,
    ) -> list[Any]:
        """并发跑一批协程，超时就用已完成的部分。

        为什么不用 gather：gather 一旦超时会把整轮的已完成结果一起丢掉，
        而慢的那个恰好常常是「有结果但爬得慢」的源，砍掉它等于白搜。
        """
        pending_coros = list(coros)
        if not pending_coros:
            return []
        tasks = [asyncio.create_task(c) for c in pending_coros]
        done, pending = await asyncio.wait(
            tasks, timeout=timeout if timeout and timeout > 0 else None
        )
        for task in pending:
            task.cancel()
        results: list[Any] = []
        for task in tasks:
            if task not in done or task.cancelled():
                continue
            if task.exception() is not None:
                continue
            results.append(task.result())
        return results

    async def search_many(
        self,
        queries: list[str],
        *,
        intent_query: str = "",
        min_relevance: float = 0.35,
        max_chars: int = 2400,
        freshness_intent: str = "",
    ) -> str:
        """并行检索多个 query；弱结果时自动换词重试。

        freshness_intent 传原始问题：它是**时效补搜**的判定依据。
        弱结果重试看的是相关度，管不到"相关但对不上当期"，
        所以那类问题需要单独一条触发路径。
        """
        cleaned = _dedupe_queries(queries)
        if not cleaned:
            return "【联网检索结果】\n（查询为空）"
        self._deadline_at = time.monotonic() + self.deadline if self.deadline else 0.0

        intent = (intent_query or cleaned[0]).strip()
        sections: list[str] = []
        # 先把每轮拿到的结果攒起来、最后一并渲染。
        # 为什么不在每轮里就渲染：预算要按**总段数**摊，提前算会把补搜那几段挤出去。
        pending: list[tuple[str, list[SearchHit]]] = []
        weak_queries: list[str] = []
        attempted: list[str] = []
        domains: set[str] = set()
        dated_hits = 0

        def _collect(hits: list[SearchHit]) -> None:
            nonlocal dated_hits
            for hit in hits:
                d = hit_domain(hit.url)
                if d:
                    domains.add(d)
                if (hit.date or "").strip() or mentions_current_year(hit):
                    dated_hits += 1

        async def _one(q: str) -> tuple[str, list[SearchHit]]:
            hits = await self.search(q)
            return q, hits

        # 首轮拿八成预算：后面还有换词重试和时效补搜要花时间，
        # 但首轮空手而归等于整次检索作废（模型没料就会照着标题编），
        # 所以宁可挤掉补搜，也要让首轮把时间用足。
        first_pass = await self._bounded([_one(q) for q in cleaned], self._budget(0.8))
        for q, hits in first_pass:
            _collect(hits)
            pending.append((q, hits))
            # 有主源时放宽这道闸：SearXNG/Tavily 自己的相关性排序比我们的
            # 词面打分靠谱。实测「武科大 图书馆 几点关门」已经搜到了正确的
            # 校内页面，只是字面重合低就被判成弱结果，白花 10 秒去搜
            # 「site:bilibili.com」「攻略」这种没用的话。
            weak_threshold = min_relevance * 0.5 if self._has_primary_source() else min_relevance
            if best_hit_relevance(intent, hits) < weak_threshold:
                weak_queries.append(q)

        # 只有首轮至少捞到一条结果，才值得换词再试。
        # 为什么：一条都没有说明这个词本身在这个语境里不存在（闲聊里提到
        # 「签到」就是例子），再拿 site:bilibili.com / 攻略 / 2026 去套，
        # 只会捞回课堂考勤、DNF 签到入口这类八竿子打不着的东西，还白花 3 轮。
        has_any_hit = any(hits for _q, hits in first_pass)
        if weak_queries and has_any_hit:
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
                attempted.extend(fallback_queries)
                retry_pass = await self._bounded(
                    [_one(q) for q in fallback_queries], self._budget()
                )
                for q, hits in retry_pass:
                    if not hits:
                        continue
                    _collect(hits)
                    if best_hit_relevance(intent, hits) >= min_relevance * 0.7:
                        pending.append((f"{q}（换词重试）", hits))
                    elif best_hit_relevance(q, hits) >= min_relevance:
                        pending.append((f"{q}（换词重试）", hits))

        # 时效补搜：问题在问"最新/版本/赛季/补丁"，而这一整批结果**一条日期都没有**。
        # 这时模型没有依据判断内容是否过期，只能默认它是最新的 —— 实测就是这样
        # 把上赛季的旧内容当成当期答案。这里换成带年月/"最新"的词再搜一轮。
        # 一条日期都没有才补，既避免多花一次检索，也保证补搜是"有的放矢"。
        if (
            freshness_intent
            # 问「今天几号/现在几点」由系统时钟回答，不需要为时效补搜 ——
            # 这条正则很窄（要求"几"紧跟"今天/现在"），不会误伤"今天有什么新闻"。
            and not is_asking_current_datetime(freshness_intent)
            and is_time_sensitive_question(freshness_intent)
            and not _FRESHNESS_SKIP_RE.search(freshness_intent)
            and not dated_hits
        ):
            known = {q.casefold() for q in cleaned} | {q.casefold() for q in attempted}
            fresh_queries = [
                q
                for q in build_freshness_queries(freshness_intent, max_total=2)
                if q.casefold() not in known
            ]
            if fresh_queries:
                logger.info(
                    f"dl_senpai web_search freshness retry queries={fresh_queries!r}"
                )
                fresh_pass = await self._bounded(
                    [_one(q) for q in fresh_queries], self._budget()
                )
                for q, hits in fresh_pass:
                    if not hits:
                        continue
                    _collect(hits)
                    pending.append((f"{q}（时效补搜）", hits))

        # 渲染前补正文：只补最前面的几段（多轮补搜那几段排在后面，
        # 通常已经有足够证据，不值得再多花一轮网络时间）。
        # 这里整体加超时，慢站点不会拖住整次检索。
        if self.fetch_body and pending:
            head = pending[: self.body_max_sections]
            # 正文至少要留一脚：模型手里只有标题时必然照着标题编，
            # 所以宁可让整次检索略微超预算，也不能把正文饿死。
            # 详情类问题走浏览器兜底，得给它留够时间（一次页面 5~10 秒）；
            # 其余问题还是 4.5 秒，不值得为一句概述多等。
            if self.browser_fallback and needs_page_detail(intent):
                body_floor, body_cap = 9.0, 14.0
            else:
                body_floor, body_cap = 4.5, self.body_timeout + 1.5
            budget = max(body_floor, min(self._budget() or body_cap, body_cap))
            # 同样不用 gather + wait_for：整批超时会连已经抓到的正文一起丢。
            # 超时的那一段退回「只有摘要」的原始结果，其余照常用。
            tasks = [
                asyncio.create_task(self._enrich_bodies(intent, hits)) for _l, hits in head
            ]
            done, pending_tasks = await asyncio.wait(tasks, timeout=budget)
            for task in pending_tasks:
                task.cancel()
            merged_head: list[list[SearchHit]] = []
            for (_label, raw_hits), task in zip(head, tasks):
                if task in done and not task.cancelled() and task.exception() is None:
                    merged_head.append(task.result())
                else:
                    merged_head.append(raw_hits)
            pending = list(zip([label for label, _h in head], merged_head)) + pending[len(head):]

        # 预算在这里按**总段数**摊，而不是按第一轮的查询数：
        # 补搜段如果按第一轮的份额生成，拼起来会被调用方从尾部整体截断，
        # 而那段恰恰是最该让模型看到的当期证据。
        per_section = max(400, int(max_chars) // max(1, len(pending)))
        sections = [
            format_search_for_prompt(hits, query=label, max_chars=per_section)
            for label, hits in pending
        ]

        # 来源不足时明说，而不是把一条结果当成已证实的定论丢给模型
        need = self.min_independent_domains
        if need > 1 and len(domains) < need:
            sections.append(
                "【来源不足】本次只检索到 %d 个独立来源，未达到交叉印证要求（需 %d 个）。"
                "请把它当作单一来源的说法：明确告诉对方只查到这一处，不要讲成已证实的事实。"
                % (len(domains), need)
            )

        return "\n\n".join(sections)

    def _provider_order(self) -> list[str]:
        p = self.provider
        if p == "auto":
            order: list[str] = []
            if self.tavily_api_key:
                order.append("tavily")
            if self.searxng_url:
                order.append("searxng")
            # bing 放末位：实测它对中文长词会拆词（搜「原神 最新版本」
            # 会返回「原」的百度百科条目和汉字词典），结果混进来只会稀释 prompt。
            order.extend(["ddgs", "duckduckgo", "bing"])
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
                    # 摘要放宽到 400：知乎/贴吧/B 站这类站点正文基本抓不到，
                    # 摘要就是模型能看到的全部证据，280 字经常刚好卡在关键信息前。
                    snippet=snippet[:400],
                    source="searxng",
                    date=normalize_hit_date(
                        item.get("publishedDate") or item.get("published_date")
                    ),
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
                    date=normalize_hit_date(item.get("published_date") or item.get("date")),
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

        def _run(backend: str | None) -> list[SearchHit]:
            hits: list[SearchHit] = []
            kwargs: dict[str, Any] = {
                "region": self.region if self.region != "zh-cn" else "cn-zh",
                "max_results": self.max_results,
            }
            if backend:
                kwargs["backend"] = backend
            with DDGS() as ddgs:
                rows = ddgs.text(query, **kwargs)
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
                            date=normalize_hit_date(item.get("date")),
                        )
                    )
                    if len(hits) >= self.max_results:
                        break
            return hits

        # 先指名快后端，失败再退回 ddgs 的 auto（它会自己兜底更多源，但慢）。
        # to_thread 不受 httpx 超时保护，线程一旦挂死会拖垮整轮预算，外面再套 wait_for。
        for attempt, backend in enumerate((_DDGS_PREFERRED_BACKEND, None)):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(_run, backend), timeout=self.timeout
                )
            except Exception as exc:  # noqa: BLE001
                if attempt == 1:
                    raise
                logger.info(
                    f"dl_senpai web_search ddgs backend={backend!r} failed: {exc}"
                )
                await asyncio.sleep(0.3)
        return []


def searcher_from_config(config: Any) -> WebSearcher:
    return WebSearcher(
        provider=getattr(config, "search_provider", "auto"),
        max_results=getattr(config, "search_max_results", 5),
        timeout=float(getattr(config, "search_timeout", 12.0)),
        region=getattr(config, "search_region", "zh-cn"),
        searxng_url=getattr(config, "search_searxng_url", ""),
        tavily_api_key=getattr(config, "search_tavily_api_key", ""),
        min_independent_domains=int(getattr(config, "search_min_independent_domains", 2)),
        # 抓正文默认开：模型手里只有标题时必然照着标题编，
        # 或者把【防写串】那套规则全用上，回答就只剩空话。
        fetch_body=bool(getattr(config, "search_fetch_page_body", True)),
        body_timeout=float(getattr(config, "search_page_body_timeout", 6.0)),
        body_max_chars=int(getattr(config, "search_page_body_max_chars", 500)),
        deadline=float(getattr(config, "search_deadline", 12.0)),
        # 静态抓不到就开浏览器：知乎/贴吧/B站这类站挡爬虫或是 SPA
        browser_fallback=bool(getattr(config, "search_browser_fallback", True)),
        config=config,
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


# 向后兼容别名（Phase 6.6 重命名）。
# 保留一段时间，方便外部脚本/旧笔记继续引用；
# 新代码请用 grounding_prefetch —— 名字才表达了它的真实归属。
prefetch_search_brief = grounding_prefetch
