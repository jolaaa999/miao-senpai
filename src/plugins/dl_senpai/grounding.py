"""防写串/瞎编：游戏装备、技能、角色等专有名词必须能被检索或用户原文支撑。"""

from __future__ import annotations

import re

_STRICT_GROUNDING_RE = re.compile(
    r"(配装|装备|武器|护甲|头盔|胸甲|护腕|腰甲|鞋|护石|装饰品|宝珠|珠|"
    r"技能|天赋|被动|主动|大招|小招|干员|特工|角色|皮肤|羁绊|词条|"
    r"掉落|制作|合成|强化|升级|材料|"
    r"boss|怪物|肉质|弱点|"
    r"叫什么|名字|名单|有哪些|哪几个|哪个好|怎么配|怎么搭)",
    re.IGNORECASE,
)
_GAME_OR_MEDIA_CONTEXT_RE = re.compile(
    r"(明日方舟|怪物猎人|怪猎|原神|崩坏|碧蓝航线|少女前线|三角洲|方舟|"
    r"冰原|世界|崛起|曙光|wilds|mhw|mhr|mhwilds|genshin|arknights|干员|猎人|"
    r"乌鲁鲁|干员|卡池|副本|关卡|地图)",
    re.IGNORECASE,
)
_TEXTBOOK_EXEMPT_RE = re.compile(
    r"(过拟合|欠拟合|梯度|反向传播|链表|二叉树|复杂度|docker|linux|"
    r"python|java|算法题|leetcode|洛谷|icpc|transformer|cnn|rnn|"
    r"batch\s*norm|adam|sgd|交叉熵|损失函数|正则化)",
    re.IGNORECASE,
)
_ENTITY_SUFFIXES = (
    "羽饰",
    "头盔",
    "胸甲",
    "腕甲",
    "腰甲",
    "护石",
    "装饰品",
    "宝珠",
    "技能",
    "套装",
    "铠甲",
    "护甲",
    "干员",
    "特工",
)
_ENTITY_SHORT_SUFFIXES = ("弓", "弩", "珠")


_CONNECTOR_PREFIX_CHARS = frozenset("和与及或的在是用把将就别为为了")

def _extract_suffix_terms(raw: str, suffix: str, *, max_prefix: int = 4) -> list[str]:
    found: list[str] = []
    start = 0
    while True:
        idx = raw.find(suffix, start)
        if idx < 0:
            break
        for plen in range(2, min(max_prefix, idx) + 1):
            candidate = raw[idx - plen : idx]
            if not re.fullmatch(r"[\u4e00-\u9fff]+", candidate):
                continue
            if candidate[0] in _CONNECTOR_PREFIX_CHARS:
                continue
            found.append(candidate + suffix)
            break
        start = idx + len(suffix)
    return found
_QUOTED_TERM_RE = re.compile(r"[「『""]([^」』""]{2,24})[」』""]")
_EN_ENTITY_RE = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+){0,3}\b")
_COMMON_REPLY_WORDS = frozenset(
    {
        "学姐",
        "建议",
        "大概",
        "可以",
        "不要",
        "可能",
        "应该",
        "感觉",
        "其实",
        "不过",
        "如果",
        "因为",
        "所以",
        "这个",
        "那个",
        "一下",
        "一点",
        "比较",
        "优先",
        "暂时",
        "目前",
        "刚才",
        "刚刚",
        "怪物猎人",
        "明日方舟",
        "三角洲行动",
        "怪物猎人世界",
        "冰原",
        "大师位",
        "强弓珠",
        "耳塞",
        "体术",
        "无伤",
        "冰属性",
        "火属性",
        "水属性",
        "雷属性",
        "龙属性",
    }
)
_SHALLOW_SEARCH_MARKERS = ("官网", "首页", "official site", "home page", "官方网站")
_DETAIL_SEARCH_MARKERS = (
    "技能",
    "装备",
    "干员",
    "配装",
    "弱点",
    "属性",
    "wiki",
    "介绍",
    "效果",
    "数据",
    "掉落",
    "制作",
)


def requires_strict_grounding(text: str) -> bool:
    """游戏/媒体实体类问题：专有名词不能凭印象编。"""
    raw = (text or "").strip()
    if len(raw) < 4:
        return False
    if _TEXTBOOK_EXEMPT_RE.search(raw):
        return False
    if not _STRICT_GROUNDING_RE.search(raw):
        return False
    if _GAME_OR_MEDIA_CONTEXT_RE.search(raw):
        return True
    # 配装/装备/技能类：即便没写游戏名，也禁止凭印象编专名
    return bool(
        re.search(r"配装|装备|技能|干员|护石|头盔|宝珠|弓|弩|太刀|大剑", raw, re.IGNORECASE)
    )


def assess_search_detail_level(brief: str) -> str:
    """none / shallow / detailed — 用于判断检索是否够支撑具体名词。"""
    text = (brief or "").strip()
    if not text or "没有搜到" in text or "检索暂时不可用" in text:
        return "none"
    lower = text.casefold()
    has_detail = any(marker.casefold() in lower for marker in _DETAIL_SEARCH_MARKERS)
    has_shallow = any(marker.casefold() in lower for marker in _SHALLOW_SEARCH_MARKERS)
    if has_shallow and not has_detail and len(text) < 320:
        return "shallow"
    if len(text) < 120 and not has_detail:
        return "shallow"
    return "detailed"


def build_grounding_user_hint(*, detail_level: str, strict: bool) -> str:
    if not strict:
        return ""
    if detail_level == "none":
        return (
            "（【强核实】对方在问游戏装备/技能/角色等具体信息，但联网没搜到可靠细节。"
            "禁止报具体装备名、技能名、干员技能效果；直说没查到，最多给泛泛方向。）"
        )
    if detail_level == "shallow":
        return (
            "（【强核实】检索结果多半只有官网/首页，没有具体装备或技能条目。"
            "禁止凭印象拼装备名、技能名，禁止把别的角色/游戏机制串过来；"
            "只能说「搜了只有官网，具体名字不敢报」。）"
        )
    return (
        "（【强核实】对方在问具体装备/技能/角色："
        "你写的每个装备名、技能名、干员名都必须能在上方检索结果或对方原文里找到；"
        "找不到就别写那个名字，禁止瞎编、禁止串位。）"
    )


def extract_entity_terms(text: str) -> list[str]:
    """从文本里抽出像游戏专有名词的片段。"""
    raw = text or ""
    found: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        t = term.strip()
        if len(t) < 2 or t in _COMMON_REPLY_WORDS:
            return
        if t[0] in _CONNECTOR_PREFIX_CHARS:
            return
        key = t.casefold()
        if key in seen:
            return
        seen.add(key)
        found.append(t)

    for suffix in _ENTITY_SUFFIXES:
        for term in _extract_suffix_terms(raw, suffix, max_prefix=4):
            add(term)
    for suffix in _ENTITY_SHORT_SUFFIXES:
        for term in _extract_suffix_terms(raw, suffix, max_prefix=3):
            add(term)
    for match in _QUOTED_TERM_RE.finditer(raw):
        add(match.group(1))
    for match in _EN_ENTITY_RE.finditer(raw):
        add(match.group())
    for match in re.finditer(
        r"[\u4e00-\u9fff]{2,10}(?:α\+?|β\+?|Ⅰ|Ⅱ|Ⅲ|IV|V)",
        raw,
        re.IGNORECASE,
    ):
        add(match.group())
    return found


def find_ungrounded_terms(reply: str, allowed_context: str) -> list[str]:
    """回复里出现、但上下文（检索+用户话）找不到的疑似编造专名。"""
    context = (allowed_context or "").casefold()
    context_compact = re.sub(r"[αβ＋+\s·•]", "", context)
    bad: list[str] = []
    for term in extract_entity_terms(reply):
        key = term.casefold()
        compact = re.sub(r"[αβ＋+\s·•]", "", key)
        if key in context or compact in context_compact:
            continue
        bad.append(term)
    return bad


def build_grounding_retry_user_message(ungrounded: list[str]) -> str:
    names = "、".join(ungrounded[:6])
    return (
        f"你刚才的回答里这些专有名词在检索结果和对方原文里都找不到：{names}。\n"
        "请重写整段回复：\n"
        "1. 删掉或改掉这些没依据的名字；\n"
        "2. 检索没写清楚的技能/装备/角色效果，一律说「搜了没查到/不敢乱报」；\n"
        "3. 禁止把别的游戏/角色机制串过来；\n"
        "4. 保持学姐口吻，简短一点。"
    )


def should_grounding_retry(
    reply: str,
    *,
    allowed_context: str,
    strict: bool,
    detail_level: str,
) -> tuple[bool, list[str]]:
    if not strict or not (reply or "").strip():
        return False, []
    ungrounded = find_ungrounded_terms(reply, allowed_context)
    if ungrounded:
        return True, ungrounded
    if detail_level in {"none", "shallow"} and (
        any(suffix in reply for suffix in _ENTITY_SUFFIXES)
        or any(suffix in reply for suffix in _ENTITY_SHORT_SUFFIXES)
    ):
        return True, extract_entity_terms(reply)[:4]
    return False, []
