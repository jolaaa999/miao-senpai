"""反 AI 味：把「一眼像助手」的固定说法从出站文本里去掉。

来源：sakurawwwxh/qq-agent-plus 的 src/llm/humanize.js 与 docs/HUMANIZE.md。
它那套分两档，这里照搬这个分法，因为分法本身是对的：

    SEVERE  独立成句的自指/免责/客服收尾 —— 删掉整句，改词救不回来
    SOFT    首先/其次/综上所述 这类连接词 —— 只删词，句子还是她的话

另外补了我们自己踩到的两类（原仓库没有，因为我们这个模型特有）：

    自曝内部    「知识库里没查到」「我搜了一下」「翻出来的全是别的」
    元话语      「给你几个小招」「下面几条」「可以这样回」

为什么走确定性清洗而不是再问一次模型：
    实测这个模型对「别像助手」这类约束服从度极低（见 llm.chat 里的注释），
    让它自己改只会输出「温柔版：…」这种点评。既然改不动嘴，就出站剪。

保守原则：没命中就一个字都不动。普通消息里的空格、标点、缩进不该被顺手改掉。
"""

from __future__ import annotations

import re

# 点评自己说的话：她在做阅读理解，不是在聊天。
# 线上原话（2026-10-25，群 1072442840）：
#   「这句我接不上。洛希极限你是说天文学那个……还是哪个游戏角色」
#   「你这条「很」后面断了，是「很好」还是「很烂」——猜不出来。」
#   「这句接得上——滑索的本质是玩家自己搭路……」
# 这是把「我有没有听懂」当正文发出来了。真人听不懂就直接问，或者猜一个往下聊，
# 不会先播报自己的理解状态。
_SELF_COMMENT_RE = re.compile(
    r"[^。！？!?\n]*(?:这(?:一)?句|你这条|这条消息)"
    r"[^。！？!?\n]{0,12}(?:接不上|接得上|没有前文|没前文|信息不全|看不全|断了)"
    r"[^。！？!?\n]*[。！？!?]?"
)

# ── 严重档：整句都别留 ────────────────────────────────────────
# 只吃独立从句。「作为一个 AI 产品经理」不是自指，不能误删——
# 这条正是原仓库踩过的坑，注释照抄。
_SEVERE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "AI 自指",
        re.compile(
            r"(?:作为|身为)(?:一个)?\s*(?:AI|人工智能|语言模型|智能助手)"
            r"(?:(?:语言模型|助手|机器人)?[，,。！？!?]|$)",
            re.IGNORECASE,
        ),
    ),
    (
        "AI 免责",
        re.compile(
            r"我(?:只)?是(?:一个)?\s*(?:AI|人工智能|语言模型|智能助手)"
            r"(?:(?:语言模型|助手|机器人)?[，,。！？!?]|$)",
            re.IGNORECASE,
        ),
    ),
    (
        "客服收尾",
        re.compile(r"希望(?:这|以上)?(?:些|个|段)?(?:回答|内容|信息|建议)?(?:能)?对你(?:有所|有)?帮助[。！!]?"),
    ),
    ("英文 AI 自指", re.compile(r"as an? (?:AI|artificial intelligence|language model)[^.!?\n]*[.!?]?", re.IGNORECASE)),
    # ── 我们这个模型特有的：把内部过程倒给用户 ──
    # 人设里明确禁止，但它照做（实测「天气是不错…不过知识库里没查到跟天气相关的信息」）。
    # 只吃「我/我的知识库」这种自指，不吃裸词：
    # 「你用搜索引擎查一下」是正常建议，不能顺手删掉。
    ("自曝内部", re.compile(r"[^。！？!?\n]*(?:我(?:的)?知识库|知识库里|我的数据库|数据库里|我的知识范围)[^。！？!?\n]*[。！？!?]?")),
    ("自曝内部", re.compile(r"[^。！？!?\n]*我(?:刚|去|又)?(?:搜|查)了?(?:一下|下|几轮)[^。！？!?\n]*[。！？!?]?")),
    ("自曝内部", re.compile(r"[^。！？!?\n]*(?:没搜到|搜不到|查不到|没有查到|未获多方印证)[^。！？!?\n]*[。！？!?]?")),
    # ── 第三人称元话语：她在讲「该怎么处理这个群友」 ──
    # 实测输出过「另外，只写内存溢出还不够，先让群友补充三点」——
    # 这不是在说话，是在写操作说明。
    ("第三人称元话语", re.compile(r"[^。！？!?\n]*(?:让群友|先让|建议群友|让用户|建议对方|让对方|引导对方)[^。！？!?\n]*[。！？!?]?")),
    # ── 元话语：她在替你起草回复，不是在说话 ──
    ("元话语", re.compile(r"[^。！？!?\n]*(?:几个小招|几个建议|下面几条|以下几点|可以这样回|给你几个|挑一个)[^。！？!?\n]*[。！？!?]?")),
    # ── 元话语变体：实测新冒出来的三种 ──
    # 「帮你回几句，看场合挑，*关心型，昨晚又熬到几点啦」
    # 「共情型："兄弟受苦了"」——她在给你列回复模板
    ("回复模板", re.compile(r"[^。！？!?\n]*(?:帮你回|回几句|看场合挑|关心型|共情型|安慰型|吐槽型|回复型)[^。！？!?\n]*[。！？!?]?")),
    # 「这条消息没说要你干嘛——大概是想让你帮接个话」——在分析消息，不是在回话
    ("分析消息", re.compile(r"[^。！？!?\n]*(?:这(?:条|句)消息没|没说要你|大概是想让你|想让你帮)[^。！？!?\n]*[。！？!?]?")),
    # 「几种回法，共情+追问：「哪个食堂？」」「你可以顺着接话」——同一族，换个说法
    ("回复模板", re.compile(r"[^。！？!?\n]*(?:几种回法|这种回法|顺着接话|跟着夸一句|像(?:是)?群友在)[^。！？!?\n]*[。！？!?]?")),
    # 「想回的话，最自然的一句就是：…」「你是想回他一句吧？」——同一族
    ("回复模板", re.compile(r"[^。！？!?\n]*(?:想回的话|最自然的一句|这样既接住|你是想回|帮你回他)[^。！？!?\n]*[。！？!?]?")),
    # ── 翻案腔：没人主张过 A，却先立个 A 再推翻（natural-talk B1/B2 层）──
    # 这是最刺眼的一条。「不是缓存的问题，而是连接池打满了」→ 删掉否定面，
    # 保留「连接池打满了」：肯定面才是信息本体，否定面是给自己搭的台阶。
    # 放行条件在调用方：如果用户自己先说了 A（「Redis 缓存为什么没生效」），
    # 纠错句式是信息本身——所以这条只在**没有前置主张**时用，见 flip_is_warranted。
    # 指示代词一起吃掉，否则删完剩「这连接池打满了」这种残句
    ("翻案腔", re.compile(r"(?:这|那|它|其)?不是[^，。！？!?\n]{1,22}[，,]\s*(?:而是|是)\s*")),
    ("翻案腔升华", re.compile(r"(?:这|那|它|其)?(?:不仅|不只|不但)仅?是[^，。！？!?\n]{1,22}[，,]\s*(?:更|还)是\s*")),
    # ── 翻译腔：复述前句的「这意味着」──
    ("翻译腔复述", re.compile(r"(?:这意味着|这表明|这说明|也就是说)[，,]?\s*")),
    # ── 翻译腔：前置话题壳，后面的句子本来就以这个对象为主语 ──
    ("前置话题壳", re.compile(r"(?:对于|就)[^，。！？!?\n]{1,12}(?:来说|而言)[，,]\s*")),
    # ── 点评自己说的话（正则见 _SELF_COMMENT_RE）──
    ("自我点评", _SELF_COMMENT_RE),
    # ── 结尾升华：把一件具体的事抬成大命题 ──
    ("结尾升华", re.compile(r"[，,]?\s*(?:这(?:背后|不仅|本质上)|从某种(?:意义|角度)上)[^。！？!?\n]*[。！？!?]?\s*$")),
)

# ── 轻微档：只删连接词，句子留着 ─────────────────────────────
# grounded（自然可靠）档要保留这些结构词，但闲聊里它们就是「书面语」的味道。
_SOFT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"首先[，,]\s*"), ""),
    (re.compile(r"其次[，,]\s*"), "然后，"),
    (re.compile(r"最后[，,]\s*"), ""),
    (re.compile(r"总之[，,]\s*"), "反正，"),
    (re.compile(r"综上所述[，,]\s*"), ""),
    (re.compile(r"总的来说[，,]\s*"), ""),
    (re.compile(r"需要注意的是[，,]\s*"), ""),
    (re.compile(r"值得一提的是[，,]\s*"), ""),
    (re.compile(r"需要指出的是[，,]\s*"), ""),
    # 客服腔的固定问答：「在的，有什么可以帮你？」→「在的，怎么了？」
    # 前面那个逗号要留着，别把「在的，」吃掉
    (re.compile(r"有什么(?:可以|能|需要)(?:帮|为)你(?:做)?的?"), "怎么了"),
    (re.compile(r"还有什么(?:其他)?(?:问题|需要)"), "还有事吗"),
    # 句尾的「～」卖萌尾巴在正经回答里很出戏，但它也是人设的一部分，
    # 这里不动——卖萌是设定，不该被「反 AI 味」清洗掉。
)

# ── 前缀：她在写「该怎么回」，不是在说话 ─────────────────────
# 只删开头那个引导词，后面的内容留着——
# 「回他：看人数和口味」里的「看人数和口味」是能用的。
_PREFIX_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:回[他她]|回(?:复)?(?:对方)?|这样回|可以这样回|答|可以答)[：:]\s*"),
    re.compile(r"^\s*(?:回复|回答)\s*[：:]\s*"),
    # 「可以回他，"…」「你可以这样回，…」——实测她有时用逗号+引号而不是冒号，
    # 只认冒号的话前缀会漏，发出去就是「可以回他，"两点还没崩…」。
    re.compile(r"^\s*(?:你)?可以(?:这样)?回[^，。：:\n]{0,6}[：:，,]\s*[“\"'「]?\s*"),
    # 首句壳：「先说结论：」「说白了，」「说到底，」——按 natural-talk 的说法，
    # 这些词不带任何信息，只是把「我要开始了」演一遍。
    re.compile(
        r"^\s*(?:先说结论|先给结论|先说结果|简单来说|简单点说|说白了|说到底|"
        r"归根结底|总而言之|一句话|直接说|直接给)[：:，,]\s*"
    ),
)

_CJK = re.compile(r"[\u4e00-\u9fff]")


# 标记行（<<<CARD {...}>>> / <<<STICKER ...>>>）原样保护。
# 踩过：CARD 的 JSON 里 ", " 正好命中「逗号 + 引号」的标点收尾规则，
# 引号被删掉后 JSON 碎掉，card_actions 变空、名片功能整个失效。
# 这类标记是**功能指令**，不是文字，任何清洗都不许碰。
_MARKER_LINE = re.compile(r"^.*<<<.*$", re.M)


def _protect_markers(text: str) -> tuple[str, list[str]]:
    """把标记行换成占位符，清洗完再换回来。"""
    store: list[str] = []

    def _swap(match: re.Match[str]) -> str:
        store.append(match.group(0))
        return f"\x00{len(store) - 1}\x00"

    return _MARKER_LINE.sub(_swap, text), store


def _restore_markers(text: str, store: list[str]) -> str:
    for index, original in enumerate(store):
        text = text.replace(f"\x00{index}\x00", original)
    return text


# 「她在交稿」的探针。清洗只能捞回一半，这里用来量模型本身的倾向：
# 概率降下来才算提示词改对了，靠正则删干净不算。
_TEMPLATE_PROBE = re.compile(
    r"可以这样回|这样回|共情型|关心型|安慰型|吐槽型|回复型|几种回法|回法|"
    r"想回的话|最自然的一句|帮你回|回他一句|你是想回|这条消息像"
)

# 从「共情型："…"」这类模板里捞出可以直接说的那句
_SALVAGE_QUOTED = re.compile(r"[“\"「]([^”\"」]{4,120})[”\"」]")

# 判「纠错 vs 翻案」时不算数的虚词
_FUNC_CHARS = set("的了吧呢啊么你我他她它们这不就是也有在和与一个")

_FLIP_PATTERN = re.compile(r"(?:这|那|它|其)?不是([^，。！？!?\n]{1,22})[，,]\s*(?:而是|是)\s*")


def _flip_warranted(negated: str, context: str) -> bool:
    """用户自己先提过这个对象时，「不是 A，是 B」是纠错，是信息本体。

    来自 natural-talk 的放行条款：被否定的观点真实存在过（用户前提里说过），
    这句式就不是翻案腔。判据放宽到「连续两个字重合」，
    因为群里说话用词不会完全一致（「Redis 缓存」vs「缓存」）。
    """
    source = re.sub(r"\s+", "", str(context or ""))
    core = re.sub(r"[\s，。！？、,.!?~～]", "", str(negated or ""))
    if not source or len(core) < 2:
        return False
    # 先看连续两字：用词一致时最可靠
    if any(core[i : i + 2] in source for i in range(len(core) - 1)):
        return True
    # 再退到实词重合：群里说法不会完全一致（「钱的问题」vs「是不是钱不够」），
    # 只留实词再比，避免「的/了/是」这种高频字把判据冲垮。
    significant = {ch for ch in core if ch not in _FUNC_CHARS}
    if not significant or len(significant) > 4:
        return False
    return bool(significant & set(source))


def _salvage_quoted(text: str) -> str:
    """从被删空的回复里捞出引号中的原话。

    她写模板时是这种形状：「共情型："兄弟受苦了，晚上整点好的"」——
    模板壳该删，但引号里那句是她真写好的话，删完整条不发反而浪费。
    """
    match = _SALVAGE_QUOTED.search(str(text or ""))
    if not match:
        return ""
    quoted = match.group(1).strip()
    return quoted if len(quoted) >= 4 else ""


def find_ai_tells(text: str) -> list[dict[str, str]]:
    """返回命中的 AI 味片段。给评估脚本统计 aiTellRate 用。"""
    source = str(text or "")
    out: list[dict[str, str]] = []
    for pattern in _PREFIX_RULES:
        match = pattern.match(source)
        if match:
            out.append(
                {"severity": "severe", "label": "元话语前缀", "match": match.group(0).strip()}
            )
    for label, pattern in _SEVERE_RULES:
        for match in pattern.findall(source) or []:
            hit = match if isinstance(match, str) else str(match)
            if hit.strip():
                out.append({"severity": "severe", "label": label, "match": hit.strip()})
    for pattern, _ in _SOFT_RULES:
        for match in pattern.findall(source) or []:
            out.append({"severity": "soft", "label": "助手连接词", "match": str(match)})
    return out


def humanize_text(
    text: str,
    *,
    enabled: bool = True,
    soft: bool = True,
    context: str = "",
    casual: bool = False,
) -> tuple[str, list[dict[str, str]]]:
    """去掉固定 AI 味说法。返回 (新文本, 命中的片段)。

    删完只剩空串就返回空串，由调用方决定要不要用原句兜底——
    这个函数不做「删空了怎么办」的判断，那是业务层的决定。
    """
    source = str(text or "")
    if not enabled:
        return source, []
    # 标记行先摘出去，最后再放回原位
    protected, marker_store = _protect_markers(source)
    if marker_store:
        source = protected
    tells = find_ai_tells(source)
    # 没命中就原样返回：不碰空格、标点、缩进
    if not tells:
        return _restore_markers(source, marker_store), []

    # 整条都在讲「该怎么回」时，直接把引号里的台词捞出来。
    # 为什么要在删之前判：这类回复的引号里常带问号
    # （「最自然的一句就是："哪家？避个雷。"」），
    # 按句删到问号就断，删出来的半截还会被括号不成对守卫判成碎片而退回原文。
    # 判据是「这条回复是不是从模板腔开头」，不按覆盖率算：
    # 引号里的问号会把匹配截断，覆盖率算出来偏低，但开头就是模板腔已经很明确了。
    template_hits = [t for t in tells if t["label"] in {"回复模板", "元话语"}]
    opens_with_template = bool(
        template_hits and template_hits[0]["match"][:6] in source.strip()[:14]
    )
    if opens_with_template:
        salvaged = _salvage_quoted(source)
        if salvaged:
            return _restore_markers(salvaged, marker_store), tells

    out = source
    # 前缀先削掉：「回他：」后面的内容留着，只是不该以这句话开头
    for pattern in _PREFIX_RULES:
        out = pattern.sub("", out)
    for label, pattern in _SEVERE_RULES:
        if label == "翻案腔":
            # 有前置主张就放行：用户自己说了 A，这就是纠错不是翻案
            out = _FLIP_PATTERN.sub(
                lambda m: m.group(0) if _flip_warranted(m.group(1), context) else "",
                out,
            )
            continue
        if label == "回复模板":
            # 模板壳里冒号后面通常是她写好的那句台词，留住它。
            # 「那就跟着夸一句：天气好，心情也跟着好。」→ 直接留后半句，
            # 全删的话整条会空掉，而空掉之后只能退回原文（照发模板）。
            out = pattern.sub(
                lambda m: (
                    m.group(0)[
                        max(m.group(0).rfind("："), m.group(0).rfind(":")) + 1 :
                    ].strip()
                    if max(m.group(0).rfind("："), m.group(0).rfind(":")) >= 0
                    else ""
                ),
                out,
            )
            continue
        if label == "翻案腔升华":
            # 「这不仅是升级，更是必经之路」→「这是必经之路」：
            # 补一个「这是」，不然只剩「必经之路」断了主语
            out = pattern.sub("这是", out)
            continue
        out = pattern.sub("", out)
    if soft:
        for pattern, replacement in _SOFT_RULES:
            out = pattern.sub(replacement, out)
    if casual:
        # 闲聊才做口语替换：答疑长回复里「因此/以及」是正常书面语，
        # 换成「所以/和」反而会让技术表述变含糊。
        out = speak_like_chat(out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+([，。！？!?,.;；：:])", r"\1", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()

    # 结构性碎片就别改了，整条退回原文。
    #
    # 实测踩过：问「这个报错怎么解决（内存溢出）」，她回了一整段
    # 「首先…先让群友补充三点」+ 一张 markdown 表。那段被当成第三人称元话语删掉后，
    # 只剩「麻烦补充一下」和一行 |---|---|---| 的表头——
    # 表格残行比原话更不能发，那种「删一半」比不删更难看。
    #
    # 注意**不用长度**做判据：正常删除也会让文本明显变短
    # （「天气不错。不过知识库里没查到…」→「天气不错。」），那是想要的效果。
    # 只看结构：表格分隔行、以冒号或竖线收尾，说明删出了断口。
    stripped = out.strip()
    # 标点收尾：删掉整段后常在标点边上留渣，比如「…？☀️，）」
    stripped = re.sub(r"[，,、；;]+\s*[）)】」\"”]?", "", stripped) if re.search(
        r"[，,、；;]\s*[）)】」\"”]", stripped
    ) else stripped
    stripped = re.sub(r"[，,]{2,}", "，", stripped).strip("，,、；; ")
    # markdown 残留：「*顺着吐槽（最安全）」
    stripped = re.sub(r"[*＊#]+\s*", "", stripped)
    stripped = re.sub(r"[（(](?:最安全|推荐|可选|补充|注)[）)]", "", stripped)
    stripped = stripped.strip()
    # 括号/引号不成对 = 模板只删了一半，那种半句比原文更难看，整条退回
    unbalanced = any(
        stripped.count(opener) != stripped.count(closer)
        for opener, closer in (("「", "」"), ("“", "”"), ("（", "）"), ("(", ")"))
    )
    out = stripped
    # 整条都是模板时，先把引号里那句真话捞出来。
    # 「共情型："兄弟受苦了，晚上整点好的"」——删完是空的，
    # 但引号里那半句其实是她写好的台词，能直接说。
    if len(stripped) < 4:
        salvaged = _salvage_quoted(source)
        if salvaged:
            return _restore_markers(salvaged, marker_store), tells
        return _restore_markers(source, marker_store), tells
    # 前缀削掉后常常剩孤零零的引号：左引号只有一半就剪掉，
    # 右引号没有配对也剪掉（「火锅。走"」这种）。
    if stripped[:1] in {"“", "\"", "「"} and stripped.count(stripped[0]) == 1:
        stripped = stripped[1:].lstrip()
    for opener, closer in (("“", "”"), ("\"", "\""), ("「", "」")):
        if stripped.count(opener) != stripped.count(closer):
            stripped = stripped.rstrip(closer).rstrip()
    out = stripped
    if (
        len(stripped) < 4
        or "|---" in stripped
        or unbalanced
        or stripped.endswith(("|", "：", ":"))
    ):
        return _restore_markers(source, marker_store), tells
    return _restore_markers(out, marker_store), tells


def strip_opening_echo(
    text: str, user_text: str, *, min_chars: int = 5, threshold: float = 0.6
) -> str:
    """去掉把对方的话复述一遍的开场。

    实测：「崩不住了，作业写到两点」→ 她回「这确实挺惨的——凌晨两点还在写作业，
    基本是在硬撑了。」前半句把人家的话重说一遍才接话。真人不这么聊，
    真人直接接「基本是在硬撑了」。

    natural-talk 把这条列在多轮规则里（D7：不复述用户上一轮原话开场）。
    判据用实词重合度，不用字面相等：群里复述从来都是换个说法。
    """
    raw = str(text or "").strip()
    user = str(user_text or "")
    if not raw or not user or "<<<" in raw:
        return raw
    # 破折号也要切开：「这确实挺惨的——凌晨两点还在写作业，…」里复述在后半段，
    # 只按逗号切的话它和评价粘在一起，重合度被稀释，判不出来。
    clauses = [c for c in re.split(r"(?<=[，。！？!?~—])", raw) if c.strip()]
    if len(clauses) < 2:
        return raw
    kept: list[str] = []
    dropped = 0
    for index, clause in enumerate(clauses):
        # 只看前三个小句、最多删两个：复述只会出现在开头那几句里
        core = [ch for ch in clause if ch not in _FUNC_CHARS and _CJK.match(ch)]
        echo = (
            index < 3
            and dropped < 2
            and len(core) >= min_chars
            and sum(1 for ch in core if ch in user) / len(core) >= threshold
        )
        if echo:
            dropped += 1
            continue
        kept.append(clause)
    if not dropped:
        return raw
    result = "".join(kept).strip()
    # 砍完太短就别砍了：宁可带一句复述，也不能让回复断掉
    if len(result) < 8:
        return raw
    # 删掉的小句留下的破折号要收干净，否则是「这确实挺惨的——基本是…」
    result = re.sub(r"[—\-]{2,}(?=$|[。！？!?])", "", result)
    result = re.sub(r"[—\-]{2,}", "，", result)
    return result.strip()


_LABEL_ITEM = re.compile(r"^[^，。！？：:、]{1,8}[：:]")


def keep_first_enumeration(text: str) -> str:
    """「省事：A、B，下饭：C，清淡：D」只留第一项。

    为什么这是「不像人」的重灾区：群里问吃什么，真人回「火锅」或者「随便你定」，
    不会给你一张菜单。原来只做换行压平，结果变成一句 80 字的带冒号长句，
    看着更像「把表格拍平成一行」——那反而更假。

    natural-talk 把这类叫「冒号提示语滥用」（一条分句里用冒号替自己说话）。
    群聊短句里判据简化成：出现 ≥2 个「短标签：」就说明在列清单，只留第一个。
    """
    raw = str(text or "")
    if "<<<" in raw:
        return raw
    items = re.split(r"[，,]", raw)
    if len(items) < 3:
        return raw
    label_index = [i for i, item in enumerate(items) if _LABEL_ITEM.match(item.strip())]
    if len(label_index) < 2:
        return raw
    first = label_index[0]
    kept = [
        item for i, item in enumerate(items) if not (i in label_index and i != first)
    ]
    return "，".join(kept).strip()


def collapse_multiline(text: str) -> str:
    """把「分类清单」那种多行回复合成一句。

    为什么这是「不像人」的重灾区：群里没人这样说话——

        看你想吃哪类
        省事：番茄鸡蛋面 / 饺子
        下饭：青椒肉丝 + 米饭

    真人会挑一个说出来，不会给你列菜单。所以随口一句的场景下把换行改成逗号，
    并去掉行首的列表符号。

    只在**随口一句**时调用（调用方用 is_casual_prompt 判断）：
    答疑本来就需要分点，把那里也压平就成了帮倒忙。
    """
    # 带标记行的内容整段不动：把 <<<CARD …>>> 拼进正文会把指令弄坏
    if "<<<" in str(text or ""):
        return str(text or "").strip()
    lines = [line.strip() for line in str(text or "").splitlines()]
    kept = [line for line in lines if line and not line.startswith("~~~")]
    if len(kept) <= 1:
        return str(text or "").strip()
    cleaned = [re.sub(r"^(?:[-*+•]|\d+[.、)])\s*", "", line) for line in kept]
    # 去掉行尾的「：」「——」，否则拼起来会是「看人数和口味：，一个人…」
    cleaned = [re.sub(r"[：:—\-]+$", "", line) for line in cleaned]
    joined = "，".join(part for part in cleaned if part)
    return re.sub(r"，{2,}", "，", joined).strip()


def humanize_metrics(samples: list[str]) -> dict[str, float | int]:
    """评价一组回复「像不像人」的一组客观指标。

    照抄原仓库的观察线（不是硬门禁）：单看某一项都能造假，
    但它能把你从「我感觉不像」拉到「哪一项变差了」。

    - aiTellRate            命中助手腔的比例 ← 最影响 AI 味，也最好量化
    - duplicateOpenerRate   开头 6 个字重复的比例 ← 开场白模板化
    - questionRate          带问号的比例 ← 全在反问和全在自答都不像真人
    - avgChars/medianChars/maxChars  长度分布
    - cjkRate               中文样本占比（防止混进英文模板）

    后三项来自 natural-talk 的机械感清单，是我们原先没数的：
    - negationFlipRate      翻案腔（「不是A，而是B」）占比 ← 一眼 AI 的元凶
    - enumerationRate       一条回复里出现 ≥2 个「短标签：」← 在列清单而不是说话
    - dunhaoPerSentence     每句顿号数 ← 三项以上并列堆在一句里
    - templateRate          「可以这样回／共情型／几种回法」占比
                            ← 把自己当写手、替用户起草回复，是「不像人」的总根
    """
    rows: list[dict[str, object]] = []
    for raw in samples:
        text = str(raw or "").strip()
        if not text:
            continue
        rows.append(
            {
                "chars": len(text),
                "questions": len(re.findall(r"[？?]", text)),
                "tells": len(find_ai_tells(text)),
                "opener": re.sub(r"\s+", "", text)[:6],
                "cjk": bool(_CJK.search(text)),
                "flip": bool(_FLIP_PATTERN.search(text)),
                "enum": len(re.findall(r"[^，。：:、]{1,8}[：:]", text)) >= 2,
                "dunhao": text.count("、") / max(1, len(re.findall(r"[。！？!?]", text)) or 1),
                "template": bool(_TEMPLATE_PROBE.search(text)),
            }
        )
    count = len(rows)
    if not count:
        return {
            "samples": 0,
            "avgChars": 0,
            "medianChars": 0,
            "maxChars": 0,
            "questionRate": 0.0,
            "aiTellRate": 0.0,
            "duplicateOpenerRate": 0.0,
            "cjkRate": 0.0,
            "negationFlipRate": 0.0,
            "enumerationRate": 0.0,
            "dunhaoPerSentence": 0.0,
            "templateRate": 0.0,
        }

    chars = sorted(int(r["chars"]) for r in rows)  # type: ignore[arg-type]
    openers: dict[str, int] = {}
    for row in rows:
        key = str(row["opener"])
        openers[key] = openers.get(key, 0) + 1

    def _rate(n: int) -> float:
        return round(n / count, 3)

    return {
        "samples": count,
        "avgChars": round(sum(chars) / count),
        "medianChars": chars[count // 2],
        "maxChars": chars[-1],
        "questionRate": _rate(sum(1 for r in rows if int(r["questions"]) > 0)),  # type: ignore[arg-type]
        "aiTellRate": _rate(sum(1 for r in rows if int(r["tells"]) > 0)),  # type: ignore[arg-type]
        "duplicateOpenerRate": _rate(sum(n for n in openers.values() if n > 1)),
        "cjkRate": _rate(sum(1 for r in rows if r["cjk"])),  # type: ignore[misc]
        "negationFlipRate": _rate(sum(1 for r in rows if r["flip"])),  # type: ignore[misc]
        "enumerationRate": _rate(sum(1 for r in rows if r["enum"])),  # type: ignore[misc]
        "dunhaoPerSentence": round(
            sum(float(r["dunhao"]) for r in rows) / count, 2  # type: ignore[arg-type]
        ),
        "templateRate": _rate(sum(1 for r in rows if r["template"])),  # type: ignore[misc]
    }

# ── 口语替换表：把书面连接词换成嘴上的说法 ──────────────────
# 只换「同义且不会改变技术含义」的词。像「进行」「处理」这类动宾结构
# 一律不碰——换成「做」会把「进行中」变成「做中」。
_CASUAL_SWAPS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"因此[，,]?\s*"), "所以"),
    (re.compile(r"然而[，,]?\s*"), "但是"),
    (re.compile(r"此外[，,]?\s*"), "另外"),
    (re.compile(r"并且[，,]?\s*"), "而且"),
    (re.compile(r"以及"), "和"),
    (re.compile(r"是否"), "是不是"),
    (re.compile(r"意味着"), "就是说"),
    (re.compile(r"本质上"), "其实就是"),
    (re.compile(r"显著"), "明显"),
    (re.compile(r"立刻"), "马上"),
    (re.compile(r"例如"), "比如"),
    (re.compile(r"非常"), "挺"),
    (re.compile(r"十分"), "挺"),
)


def speak_like_chat(text: str) -> str:
    """把书面连接词换成口语说法。闲聊专用（见 humanize_text 的 casual 参数）。"""
    body = str(text or "")
    if not body:
        return body
    # 标记行是功能指令，不参与替换
    protected, marker_store = _protect_markers(body)
    out = protected
    for pattern, replacement in _CASUAL_SWAPS:
        out = pattern.sub(replacement, out)
    out = re.sub(r"[，,]{2,}", "，", out)
    return _restore_markers(out, marker_store)


# ── 复盘腔：整段收成一句人话 ────────────────────────────────
# 线上原话（2026-10-25）：
#   「说实话，我刚才的回答确实偏了，抱歉。翻回去看……」（179 字）
#   「这句得我自己认——「在呢～」那我根本没在答，是空壳。原因不高级……」（353 字）
#   「因为我刚才那段就是在做自我诊断报告，格式工整得像个 bug 说明，比在呢还像机器。」
# 这是最不像人的一类：被指出错误后写自我诊断报告，还分段论证。
# 真人只会说「行，我搞错了」，然后接着聊。
_SELF_REVIEW_RE = re.compile(
    r"我(?:刚才|上一条|上一句|前面那条|上一轮)|"
    r"翻回去看|翻回头看|从头(?:再)?来|现在正经(?:看|说)|"
    r"空转|没接住|没接上|答偏|答串|接串"
)
_SELF_REVIEW_BLAME_RE = re.compile(r"抱歉|对不起|是我的错|我错了|丢人|不该")
# 不带道歉、但同样是复盘动作的措辞（「翻回去看」「现在从头来」）。
# 单列一份是因为它们出现在正常回答里也可能是真的在翻资料，
# 只有在**整条已经判定为复盘**的前提下才一起删（见 collapse_self_review）。
_SELF_REVIEW_DROP_RE = re.compile(
    r"翻回去看|翻回头看|从头(?:再)?来|现在(?:正经|说正经)|空转|没接住|没接上|"
    r"答偏|答串|接串|自我诊断"
)

# 用户在问「你为什么答错」时，复盘是**被要求的答案**，不能删。
_USER_ASKS_REVIEW_RE = re.compile(
    r"为什么|怎么(?:会|回事)|你刚才|上一条|说错|答错|搞错|敷衍|什么毛病"
)


def collapse_self_review(
    text: str, *, context: str = "", enabled: bool = True
) -> str:
    """把「自我诊断报告」收成一句短话；整条都是报告时返回空串。

    返回空串是有意的：调用方（style.polish_reply / llm）有空串兜底，
    会换成一句随机的短反应或者干脆不发——两者都比三百字的自我复盘像人。
    """
    body = str(text or "").strip()
    if not enabled or not body:
        return body
    if _USER_ASKS_REVIEW_RE.search(str(context or "")):
        return body  # 对方就在问这件事，照实回答是对的
    # 三者任一命中就按复盘处理：明着道歉 / 点评自己懂了没 / 复盘动作词。
    triggered = bool(
        _SELF_COMMENT_RE.search(body)
        or _SELF_REVIEW_BLAME_RE.search(body)
        or _SELF_REVIEW_DROP_RE.search(body)
        or (_SELF_REVIEW_RE.search(body) and _SELF_REVIEW_BLAME_RE.search(body))
    )
    if not triggered:
        return body

    marker_body, marker_lines = _protect_markers(body)
    sentences = [s for s in re.split(r"(?<=[。！？!?\n])", marker_body) if s.strip()]
    def _drop(sentence: str) -> bool:
        # 复盘句和「点评自己」句都算：两者都是把内部状态写进正文。
        if _SELF_COMMENT_RE.search(sentence):
            return True
        if _SELF_REVIEW_DROP_RE.search(sentence):
            return True
        if _SELF_REVIEW_RE.search(sentence) and _SELF_REVIEW_BLAME_RE.search(sentence):
            return True
        # 短道歉句单独成句时（「抱歉我应该先说这个。」）整句删掉：
        # 真要道歉，前面那句「行，我搞错了」已经够了。
        return bool(len(sentence.strip()) <= 20 and _SELF_REVIEW_BLAME_RE.search(sentence))

    kept = [s for s in sentences if not _drop(s)]
    out = "".join(kept).strip()
    # 删空了、或者只剩零碎（标点、半个括号）就整条放弃
    if len(re.sub(r"[\s，,。、；;：:！？!?～~]", "", out)) < 6:
        return ""
    return _restore_markers(out, marker_lines).strip()


# ── 「像不像人」的可数指标 ────────────────────────────────
# 指标本身不做任何改写，只回答一个问题：这一轮是不是又在写文档。
# 接日志以后，改提示词有没有用可以拿数看，而不是靠眼睛。
_AI_FLAVOR_PROBES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("自我复盘", _SELF_REVIEW_RE),
    ("点评自己", _SELF_COMMENT_RE),
    ("判词腔", re.compile(r"[^。！？!?\n]{6,}[：:][^。！？!?\n]{4,}")),
    ("元话语", _TEMPLATE_PROBE),
    ("书面连接", re.compile(r"首先|其次|综上|总的来说|值得注意的是|意味着|本质上")),
)


def ai_flavor_score(text: str, *, context: str = "") -> tuple[int, list[str]]:
    """返回 (分数, 命中的标签)。分数越高越像在写文档。

    分级只用于观测与告警：0~1 正常，2 可疑，3+ 需要人看一眼。
    刻意不做自动改写——自动改写出来的句子语法完整、内容残缺，
    比原来的机器腔更难读。
    """
    body = str(text or "").strip()
    if not body:
        return 0, []
    if _USER_ASKS_REVIEW_RE.search(str(context or "")):
        return 0, []
    labels: list[str] = []
    for label, pattern in _AI_FLAVOR_PROBES:
        if pattern.search(body) and label not in labels:
            labels.append(label)
    # 长度本身就是一条：闲聊超过 150 字，几乎一定是小作文。
    if len(body) > 150:
        labels.append("小作文")
    return len(labels), labels
