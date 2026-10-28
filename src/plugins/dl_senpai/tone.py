"""本轮语气判定：他这是在吐槽，还是在真倾诉。

为什么需要这一层（2026-10 实测）：

人设里【共情的分寸】写得很清楚，连「今天好累」都当例子点名禁止过度共情。
但干跑照样得到「辛苦了，写到两点也太熬了…别把自己累坏」。
把规则挪到提示词末尾（【别演】）之后，客服腔没了，安慰腔还是偶发。

根因不是措辞不够狠，是**通用规则对具体一轮不够具体**：
「日常吐槽不是情绪求助」这句话对每一轮都成立，也就等于对每一轮都不够有用。
所以补这一层：先判断这一轮到底是哪种，再把结论用一句话喂到提示词末尾。
判定纯字符串匹配，零 API 成本、零延迟。

取向刻意保守——**宁可把吐槽误判成 normal，也不能把真倾诉判成 vent**。
判错方向的代价不对称：少损一句最多显得平淡，真难受的人被回一句「谁让你熬夜的」
是硬伤。
"""

from __future__ import annotations

import re

# -- 真倾诉信号：出现任意一条就按 distress 处理 --
# 这些词是「扛不住」的量级，不是「今天有点烦」的量级。
_DISTRESS_WORDS = (
    "难受",
    "崩溃",
    "撑不住",
    "绷不住",
    "扛不住",
    "想死",
    "不想活",
    "活着没意思",
    "失眠",
    "睡不着",
    "吃不下",
    "哭",
    "抑郁",
    "焦虑",
    "想找人说话",
    "想找人说说话",
    "没人在乎",
    "一个人待着",
    "分手",
    "被甩",
    "离职",
    "退学",
    "挂科了",
    "家里出事",
)

# 第一人称 + 持续时间：把「这周一直…」「连着几天…」这类长期状态也算进来
_CHRONIC_RE = re.compile(
    r"(?:这|近|连着|连续|整整)[一两几]?[周月天日年][^。！？\n]{0,12}"
    r"(?:一直|都|老是|总是|天天)"
)

# -- 吐槽信号：事务 + 轻度程度词，命中才可能判 vent --
_CHORE_WORDS = (
    "作业",
    "考试",
    "复习",
    "实验",
    "报告",
    "论文",
    "课设",
    "加班",
    "上班",
    "上班",
    "导师",
    "组会",
    "ddl",
    "早八",
    "军训",
    "答辩",
    "实习",
    "投简历",
    "刷题",
)

_VENT_WORDS = (
    "好累",
    "累死",
    "困死",
    "好困",
    "烦死",
    "好烦",
    "裂开",
    "麻了",
    "绷不住啊",
    "要命",
    "救命",
    "服了",
    "无语",
    "写不完",
    "做不完",
    "看不完",
    "背不完",
    "卷死",
    "摆烂",
    "想摸鱼",
    "想睡觉",
    "想放假",
)

# 玩笑/自嘲标记：有这些基本可以确定不是在求救
_JOKE_MARKERS = ("草", "笑死", "哈哈哈", "哈哈", "狗头", "doge", "qwq", "救命啊", "俺")

# 问句 / 求安慰的明确动作：一旦出现就不当吐槽
_ASK_SUPPORT_RE = re.compile(
    r"(?:能|可以)?(?:陪|听)(?:我|人家)?(?:说)?(?:说|聊)"
    r"|怎么办啊?$|我该怎么办|你说我"
)


def classify_tone(text: str, *, recent: list[str] | None = None) -> str:
    """返回 "distress" / "vent" / "normal"。

    recent 是最近几条消息（可选）：同一件烦心事反复提，按 distress 处理——
    真人反复说同一件事，通常就是真的想找人接一下。
    """
    body = (text or "").strip()
    if not body:
        return "normal"

    # 1) 真倾诉优先：宁可漏判吐槽，不可漏判求救
    if any(word in body for word in _DISTRESS_WORDS):
        return "distress"
    if _CHRONIC_RE.search(body):
        return "distress"
    if _ASK_SUPPORT_RE.search(body):
        return "distress"

    # 2) 反复提同一件事 → 也按倾诉处理
    if recent:
        # 只看最近 6 条，太老的重复不算
        for prev in list(recent)[-6:]:
            prev_body = (prev or "").strip()
            if prev_body and prev_body != body and _shares_topic(prev_body, body):
                if any(word in prev_body for word in _VENT_WORDS):
                    return "distress"

    # 3) 吐槽：要有具体事务 + 轻度程度词，或者程度词 + 玩笑标记
    has_chore = any(word in body for word in _CHORE_WORDS)
    has_vent = any(word in body for word in _VENT_WORDS)
    has_joke = any(word in body for word in _JOKE_MARKERS)
    if has_vent and (has_chore or has_joke):
        return "vent"
    # 短促的一句「好累」也算吐槽：这么说不像在认真求助
    if has_vent and len(body) <= 12:
        return "vent"

    return "normal"


def _shares_topic(a: str, b: str, *, min_overlap: int = 2) -> bool:
    """两条消息是否在说同一件事：粗粒度看二字片段重叠。

    不引分词库：这里只要「看起来是同一话题」的弱信号，
    精确相似度反而会漏（同一件事换个说法就说不上高了）。
    """
    def grams(s: str) -> set[str]:
        return {s[i : i + 2] for i in range(len(s) - 1)}

    return len(grams(a) & grams(b)) >= min_overlap


# 求教/要步骤的信号：出现这些就说明对方真的想要长回答，不能压
_INSTRUCTION_WORDS = (
    "怎么",
    "如何",
    "为什么",
    "为啥",
    "教程",
    "步骤",
    "详细",
    "讲讲",
    "解释",
    "区分",
    "对比",
    "选哪个",
    "哪个好",
    "帮我写",
    "帮我改",
    "报错",
    "代码",
    "配装",
    "推荐",
    "入门",
)


# 技术/求助信号：出现就不当「随口一问」，宁可回长一点也不压成半句。
#
# 为什么必须补（2026-10-27 干跑查出来的）：
# 「学姐我那个 loss 一直不降」只有 13 字，被当成闲聊，回答从 292 字压到 28 字，
# 压出来的还是一句没用的「先别急着改模型」。用户那边看到的就是「一本正经重复废话」——
# 不是她在说废话，是答案被长度纪律砍掉了。
_TECH_SIGNAL_WORDS = (
    "怎么", "为什么", "为啥", "帮我", "教我", "步骤", "安装", "下载",
    "配置", "设置", "打不过", "卡住", "不降", "闪退", "蓝屏", "失败",
    "崩溃", "跑不起来", "对不上", "求", "教程",
)
# 中文闲聊很少夹英文单词；夹了基本是在讲技术（loss / lr / bug / api）
_ASCII_WORD = re.compile(r"[A-Za-z]{2,}")


def is_casual_prompt(text: str, *, max_chars: int = 25) -> bool:
    """这句是不是「随口一问」，长回答在这种场景下必然显得假。

    为什么需要它：长度纪律写了「默认 1~2 句不超过 60 字」，实测六句吐槽里
    五句超标、最狠 180 字——因为它跟语气规则一样是**通用**约束，
    对具体一轮不够具体。只有认得出「这轮本来就该短」，压短才站得住脚。

    求教类的消息一律返回 False：那种问法对方真的想要步骤，
    压短就成了懒。宁可长。

    阈值默认 25 字（原先 15）：实测 21 字的「今天真的好累啊，作业写到两点还没完」
    判不成随口一问，于是照样回 150 字——留了个明摆着的缺口。
    风险是长消息被误压，靠求教词表和不含问号来兜。
    """
    body = (text or "").strip()
    if not body:
        return False
    if len(body) > max_chars:
        return False
    if any(word in body for word in _INSTRUCTION_WORDS):
        return False
    if any(word in body for word in _TECH_SIGNAL_WORDS):
        return False
    if _ASCII_WORD.search(body):
        return False
    return True
def split_marker_lines(text: str) -> tuple[str, list[str]]:
    """把含工具标记（<<<CARD ...>>>、<<<STICKER ...>>> 等）的行剥出来。

    为什么必须剥：这些标记是**功能指令**，不是文本。
    截断或删除小句时如果顺手把它切掉，名片/表情包/语音就全都不触发了，
    而且外面看不出来——回复看着正常，只是「什么都没发生」。
    实测踩过：截断后 before_len=64 → after_len=7，一张名片就没了。
    """
    prose_lines: list[str] = []
    marker_lines: list[str] = []
    for line in (text or "").split("\n"):
        if "<<<" in line:
            marker_lines.append(line)
        else:
            prose_lines.append(line)
    return "\n".join(prose_lines), marker_lines


def _join_back(prose: str, marker_lines: list[str]) -> str:
    """把正文和标记行拼回去，标记保持在末尾。"""
    body = (prose or "").rstrip()
    if not marker_lines:
        return body
    tail = "\n".join(marker_lines)
    return f"{body}\n{tail}" if body else tail


# 元话语残渣标记：这几句说明模型在「替用户起草回复」，不是在说话
_META_LEADINS = (
    "可以这样回",
    "这样回",
    "可以这么回",
    "可以这样写",
    "几个版本",
    "几种语气",
    "几种风格",
    "挑一个",
    "供你参考",
    "你可以说",
    "参考如下",
    "示例如下",
    "如下：",
    "温柔版",
    "幽默版",
)

# 分句：句末标点后切一刀，保留标点方便拼回去
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?～~\n])")
# 再按逗号一级切开，这样只删「别硬撑」这种小句，不动整句
_CLAUSE_SPLIT = re.compile(r"(?<=[，,、；;])")


def strip_comfort_clauses(text: str) -> str:
    """把安慰腔的小句直接删掉——不靠模型听话，靠确定性处理。

    为什么走这条路：这个模型对「别安慰」的服从度实测压不下来（开/不开判定
    都是 33%），而让它自己判、自己改、带人格重说，三条路都输出了不能发的东西。
    既然改不动它的嘴，就在出站前把安慰那句剪掉。

    粒度是**逗号级**，不是截断整句：
    「抱抱，两点也太狠了😭 今天先补觉、别硬撑」→「两点也太狠了😭 今天先补觉」
    整句砍掉会把有用的话一起扔了。

    兜底很重要：删空了（整条都是安慰）就返回原文。
    宁可留一条安慰，也不能发一条空的。
    """
    raw = (text or "").strip()
    if not raw:
        return raw
    body, marker_lines = split_marker_lines(raw)
    body = body.strip()
    if not body:
        return raw

    kept_sentences: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(body):
        if not sentence.strip():
            continue
        clauses = _CLAUSE_SPLIT.split(sentence)
        kept = [c for c in clauses if not has_comfort_tone(c)]
        if not kept:
            continue
        joined = "".join(kept).rstrip("，,、；; ")
        if joined.strip():
            kept_sentences.append(joined)

    result = "".join(kept_sentences).strip()
    # 太短说明只剩标点或半句话，那不如不发改动
    if len(result) < 8:
        return raw
    # 元话语残渣：模型偶尔会写「可以这样回：<安慰的话>」，
    # 把安慰剪掉就只剩「可以这样回」——这句比安慰更不能发。
    if any(token in result for token in _META_LEADINS):
        return raw
    return _join_back(result, marker_lines)


# 清单行的特征：列表符号开头，或前 8 个字里带「标签：」。
# 这类内容（菜单、步骤、方案对比）不是「她分两句说」，拆泡会把清单拆散。
_LISTISH_LINE = re.compile(r"^\s*(?:[-*+•]|\d+[.、)])\s*")


def _looks_like_list(lines: list[str]) -> bool:
    hits = 0
    for line in lines:
        if _LISTISH_LINE.match(line):
            hits += 1
        elif re.search(r"^[^，。！？,!?]{1,8}[：:]", line):
            hits += 1
    return hits >= 2


def shape_bubbles(text: str, *, max_bubbles: int = 3, max_chars: int = 60) -> str:
    """把回复塑形成 2~N 条短话，用换行分隔，交给出站拆泡。

    为什么要有这一步：拆泡的合法性来自「她本来就想分两句发」，而这个意图
    只能由换行表达。出站那层只认换行（bubble_require_newline），所以这里
    必须先把输出变成「一行一句」的形态。不然只有两条路——要么整段塞一条
    （像机器人），要么出站按标点硬切（像被剁碎的段落）。

    三条规则：
    1. 模型自己分了 2~N 行、而且不是清单 → 保留行结构，那是她的意图；
    2. 单段长文 → 按整句切，取前 N 句，合计不超过预算；
    3. 永远整句保留，不吐半句话（和 trim_to_sentence_budget 同一原则）。

    与 trim_to_sentence_budget 的分工：那个是「压成一句」，这个是「拆成几句」。
    开拆泡时用这个，关掉拆泡时仍然用那个。
    """
    raw_text = (text or "").strip()
    if not raw_text:
        return raw_text
    max_bubbles = max(1, int(max_bubbles))
    if max_bubbles <= 1:
        return trim_to_sentence_budget(raw_text, max_chars)

    body, marker_lines = split_marker_lines(raw_text)
    body = body.strip()
    if not body:
        return raw_text

    # 规则 1：她自己分的行，保留
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if 2 <= len(lines) <= max_bubbles and not _looks_like_list(lines):
        kept = [trim_to_sentence_budget(ln, max_chars) for ln in lines]
        return _join_back("\n".join(k for k in kept if k), marker_lines)

    # 规则 2 之前先把多行压平：清单、或者行数超过拆泡泡数的，
    # 都不算「她想分条说」。留着换行会被出站误当成拆泡信号，
    # 结果把一张菜单拆成三条气泡发出去。
    flat = re.sub(r"\s*\n+\s*", "，", body).strip() if "\n" in body else body

    # 规则 2：按整句取前 N 句
    sentences = [x.strip() for x in _SENTENCE_SPLIT.split(flat) if x.strip()]
    if not sentences:
        return raw_text
    picked: list[str] = []
    total = 0
    for sentence in sentences:
        if len(picked) >= max_bubbles:
            break
        if picked and total + len(sentence) > max_chars:
            break
        picked.append(sentence)
        total += len(sentence)
    if len(picked) <= 1:
        # 用压平后的 flat，不是 raw_text：raw_text 里还留着换行，
        # 原样返回会被出站当成「她要分条发」，一张菜单就拆成三条气泡。
        # 标记行必须拼回去：<<<STICKER …>>> 这类是工具指令，
        # 丢了就等于把她要发表情的行为吞掉了（实测表情/卡片全部失效）。
        return _join_back(trim_to_sentence_budget(flat, max_chars), marker_lines)
    # 单句超预算的（一个句号写到底）再按逗号收一下，跟原来那条规则一致
    picked = [trim_to_sentence_budget(p, max_chars) for p in picked]
    return _join_back("\n".join(p for p in picked if p), marker_lines)


def trim_to_sentence_budget(text: str, max_chars: int) -> str:
    """随口一句的场景下按**整句**收口。返回的文本永远语义完整。

    为什么非要这么做：字数是这个模型唯一压不住的东西。
    「不超过 60 字」写在人设里没用，挪到提示词末尾没用，本轮直接写
    「别超过 30 字」也没用——实测该 164 字还是 164 字。既然嘴管不住，
    就出站时按句收。

    两条裁剪策略（2026-10-25 加的第二条）：

    1. **常规**：按整句累加到预算为止，只在句末标点后切，第一句再长也照发。
    2. **保头保尾**：正文明显超预算（> 预算 2 倍）且至少三句时，保留首句 +
       末句，砍掉中间。

    为什么加第 2 条：常规策略只留第一句，日志里出现过 401 字 → 31 字、
    375 字 → 33 字。模型本来写的是「先反应、再解释、最后一句点题」，
    砍掉头尾只留中间，或者只留头，发出来就是断头断尾的判词——
    「这一刀就变得很致命：绩点是软排序，德育线是硬门槛。」
    这种句子比长句子更像机器。真人长回复也是先说一句、最后补一句，
    中间那坨解释反而是最可以省掉的。

    与硬截断的区别（用户明确否过「说到一半停住」）：
    永远整句保留，绝不吐半句话。
    """
    raw_text = (text or "").strip()
    if not raw_text:
        return raw_text
    # 标记行不参与截断，先剥出来
    body, marker_lines = split_marker_lines(raw_text)
    body = body.strip()
    if max_chars <= 0 or len(body) <= max_chars:
        return raw_text

    sentences = [s for s in _SENTENCE_SPLIT.split(body) if s.strip()]

    # 策略 2：超预算太多且句子够多 → 保头保尾
    if len(sentences) >= 3 and len(body) > max_chars * 2:
        head = sentences[0].strip()
        tail = sentences[-1].strip()
        # 两句加起来仍在预算给的两倍以内才用这个策略：
        # 否则头尾各自都很长，拼起来还是一条小作文，不如走常规策略。
        if head and tail and len(head) + len(tail) <= max_chars * 2:
            # 首句可能本来就没有句末标点（整行被当成一句切开）。
            # 直接拼会得到「…三个层面的意思你后面那个句号…」这种黏连句，
            # 所以缺标点时补一个句号——补标点不算改内容。
            if not head.endswith(("。", "！", "？", "!", "?", "～", "~", "…")):
                head += "。"
            return _join_back(head + tail, marker_lines)

    kept_sentences: list[str] = []
    total = 0
    for sentence in sentences:
        if kept_sentences and total + len(sentence) > max_chars:
            break
        kept_sentences.append(sentence)
        total += len(sentence)
    result = "".join(kept_sentences).rstrip()

    # 第一句本身就超预算时，再对它按逗号切一刀。
    #
    # 为什么要有这一步：实测她回过一句 79 字的「可以回群友，"看几个人…
    # 我直接投一票。」——整段一个句号，按句切等于不切，于是 60 的预算形同虚设。
    # 逗号后仍是完整小句，不算「说到一半停住」。
    if result and len(result) > max_chars:
        clauses = [c for c in _CLAUSE_SPLIT.split(result) if c.strip()]
        if len(clauses) > 1:
            kept_clauses: list[str] = []
            total_clause = 0
            for clause in clauses:
                if kept_clauses and total_clause + len(clause) > max_chars:
                    break
                kept_clauses.append(clause)
                total_clause += len(clause)
            trimmed = "".join(kept_clauses).rstrip().rstrip("，,、；;")
            if len(trimmed) >= 4:
                result = trimmed

    if not result:
        return raw_text
    return _join_back(result, marker_lines)


# ── 判词腔探针 ──────────────────────────────────────────────
# 「这一刀就变得很致命：绩点是软排序，德育线是硬门槛。」
# 「那就是这套规则必然会筛出来的人：绩点低，德育线卡得高。」
#
# 这类句子是线上最常见的「不像人」形态：一句到底、带解释冒号、
# 有断言词、没有第一人称也没有语气词。它不缺信息，缺的是**说话的人**。
# 这里只用于**观测**（日志 / 评估脚本），不做删除——删了就只剩空串。
_VERDICT_WORDS = (
    "本质",
    "其实就",
    "本质上",
    "等于",
    "意味着",
    "必然",
    "注定",
    "根本",
    "核心",
    "关键",
    "致命",
    "筛出来",
    "说白了",
)


def is_verdict_style(text: str) -> bool:
    """这句是不是「判词腔」。只用于统计与日志，不参与裁剪。"""
    body = (text or "").strip()
    if len(body) < 14:
        return False
    # 带到人称就不算：真人下判断时通常也在跟人说话
    if any(word in body for word in ("你", "我", "咱", "吧", "呢", "啊", "哈")):
        return False
    if not any(word in body for word in _VERDICT_WORDS):
        return False
    # 一句到底（最多一个句末标点）才叫判词；多句是正常叙述
    if body.count("。") + body.count("！") + body.count("？") > 1:
        return False
    return True


def build_casual_hint(max_chars: int = 30) -> str:
    """对方只是随口一句时，给一条**本轮具体**的长度指令。

    为什么不再靠人设里那条「默认 1~2 句不超过 60 字」：它是通用约束，
    实测六句吐槽里五句超标。而「本轮具体」这个位置确实有用——
    真倾诉那一条挪到同一位置后，安慰率从 22% 涨到 55%。

    数字给的是锚点，不用怕：实测锚点会让她「写到接近那个数」，
    对闲聊正是想要的效果（写 30 就往 30 靠，而不是往 200 去）。
    """
    if max_chars <= 0:
        return ""
    # 「别列选项」是实测出来的：问「晚上吃啥」，她会给
    # 「人多：火锅/烧烤，省事：炒菜/盖饭，清淡：粥+小菜」这种菜单。
    # 群里没人这样说话——真人是直接挑一个，或者干脆说随便。
    # 最后一句是根因补丁：实测她会把自己当成写手，回「可以这样回／共情型：…」。
    # 人设里已经加了一段讲身份，但那在人设中段；这句贴着本轮消息放，位置更有效。
    return (
        f"【这条】随口一句，回一句就够，别超过 {max_chars} 字。"
        "别列选项、别分类，直接说一个就行。"
        "你自己怎么想就怎么说，不要写「可以这样回」「共情型」。"
    )


def build_tone_hint(verdict: str) -> str:
    """把判定结论渲染成提示词末尾的一句话。

    只有 vent / distress 才注入：normal 不给指令，免得每轮都在提醒她
    「注意语气」——那本身就会让回复变僵硬。
    """
    # 措辞踩过两个坑，都记在这儿别再踩：
    # 1) 不能出现「他」——模型会把这条消息当成「当前用户在聊第三方」，
    #    于是开始帮用户起草回复，实测输出「回他：辛苦啦…」。
    # 2) 不能写成「直接回对方本人」这种祈使句——整段在提示词最末，
    #    读起来像用户临时下的任务，实测输出「你是想让我帮你回这位群友吧？」。
    # 所以这里只描述语气，不发出指令。
    # 措辞极其克制：任何「并列举例」都会被她当成内容清单照着写。
    # 实测「给个实在建议（睡觉、拆任务、先别写）」让平均字数从 53 涨到 113。
    if verdict == "vent":
        # 刻意不写「损一句」「给个建议」这类祈使短语：模型会把它当成任务，
        # 输出「可以这样回：…」「给你几个版本挑一个」这种元话语。
        # 只陈述「这条是什么」，怎么接交给她自己决定。
        return "【这条的语气】随口吐槽，不是求助。"
    if verdict == "distress":
        return "【这条的语气】是真的不好受，安静接一句就好。"
    return ""


# 安慰腔标记：命中就算「进安慰模式」。
# 刻意宽松——宁可把「辛苦了」这种正常话算成误报，也不能漏掉真安慰，
# 因为这里的用途是「拦住并改写」，误伤一次只是少一句客套话。
COMFORT_MARKERS = (
    # 明着安慰的
    "抱抱",
    "拍拍",
    "心疼",
    "辛苦了",
    "辛苦啦",
    "辛苦你了",
    "你已经很努力",
    "别对自己太苛刻",
    "我理解你的感受",
    "一定很难受",
    "听起来你压力",
    "压力很大",
    "会好起来的",
    "别难过",
    # 嘱咐式的（基准里最常见的漏网类型：「先睡吧别熬了」「身体扛不住」）
    "别熬",
    "不要熬夜",
    "早点睡",
    "先睡吧",
    "注意身体",
    "身体最重要",
    "身体优先",
    "身体扛不住",
    "身体会扛不住",
    "别硬撑",
    "别硬扛",
    "别太累",
    "要好好照顾自己",
    "照顾好自己",
    "记得好好休息",
    "好好休息",
    "保重",
    "多喝热水",
    "早点休息",
    "去睡吧",
    "睡吧",
)


def has_comfort_tone(text: str) -> bool:
    """这句回复是不是进入了安慰模式。"""
    body = text or ""
    return any(marker in body for marker in COMFORT_MARKERS)


_TONE_LABEL = {"vent": "吐槽", "distress": "真倾诉", "normal": "普通"}


def tone_label(verdict: str) -> str:
    """日志用：把英文枚举换成中文，方便直接搜日志。"""
    return _TONE_LABEL.get(verdict, verdict)

# 闲聊里的逗号要换成空格。要保护的是「1,000」这类数字分隔和英文里的语法逗号，
# 所以先把它们换成占位符，处理完再换回来。
# 占位符用 chr(0) 拼出来而不是写转义：直接写转义会被写文件那层吃掉，变成真的 NUL。
_GUARD = chr(0)
_COMMA_NUM_GUARD = re.compile(r"(?<=\d),(?=\d)")
_COMMA_EN_GUARD = re.compile(r"(?<=[A-Za-z]),(?=[ 	]*[A-Za-z])")
_ANY_COMMA = re.compile(r"[，,]")
_TRAILING_PERIOD = re.compile(r"[。.]+[ 	]*$")
_MULTI_SPACE = re.compile(r"[ 	]{2,}")


def space_out_commas(text: str) -> tuple[str, int]:
    """把闲聊里的逗号换成空格、去掉句末句号。返回 (新文本, 换掉的逗号数)。

    为什么要在出站做这件事：小黑盒那篇《提升大肥鱼活人感技巧》第四条说得很直接——
    「逗号一多就书面，空格一多就网上」，而且这是他实测收益最大的一条。

    为什么不能只靠人设：这条写进人设之后干跑照样是
    「累成这样啊，那就先别干了，躺平一会儿。」三个逗号一个没少。
    模型对「标点怎么用」这种细粒度指令基本不服从，只能出站整形。

    只管闲聊分支（调用方保证）。技术答疑不走这里：那里逗号是语义分隔，
    换成空格会读成结巴。

    跳过条件（宁可不动，也别把人话改坏）：
    - 含代码围栏；
    - ASCII 占比超过 30%（英文/代码/公式为主，逗号是语法不是停顿）；
    - 数字之间、英文单词之间的逗号。
    """
    body = text or ""
    if not body.strip():
        return body, 0
    fence = chr(96) * 3
    if "'" * 3 in body or fence in body:
        return body, 0
    ascii_ratio = sum(1 for ch in body if ord(ch) < 128) / max(1, len(body))
    if ascii_ratio > 0.3:
        return body, 0

    guard_num = _GUARD + "N" + _GUARD
    guard_en = _GUARD + "E" + _GUARD
    out_lines: list[str] = []
    replaced = 0
    for line in body.split(chr(10)):
        piece = _COMMA_NUM_GUARD.sub(guard_num, line)
        piece = _COMMA_EN_GUARD.sub(guard_en, piece)
        piece, hits = _ANY_COMMA.subn(" ", piece)
        replaced += hits
        piece = piece.replace(guard_num, ",").replace(guard_en, ",")
        # 句末句号去掉；问号、感叹号、省略号留着——那些是情绪，不是书面感
        piece = _TRAILING_PERIOD.sub("", piece)
        piece = _MULTI_SPACE.sub(" ", piece).strip()
        out_lines.append(piece)
    return chr(10).join(out_lines), replaced


def strip_bubble_periods(text: str) -> str:
    """把每一行末尾的句号去掉（拆泡之后再调一次）。

    为什么需要第二次：space_out_commas 在拆泡**之前**跑，那时是一个整段，
    只能删掉最后那个句号；中间的「再拖下去就成夜宵了。」是拆泡后才变成行尾的，
    于是留着句号——一条气泡以句号收尾，看着就是在写作文而不是在打字。
    """
    lines = [(_TRAILING_PERIOD.sub("", ln)).rstrip() for ln in (text or "").split(chr(10))]
    return chr(10).join(lines)

# 破折号：群里真人几乎不打 ——。2026-10-28 线上 38 条闲聊里 12 条带，
# 这是「文青腔」最刺眼的标记，比逗号更能暴露她不是在打字。
_LONG_DASH = re.compile(r"\s*(?:——|--)\s*")

# 整行只有括号动作：「（笑了一下）」。真人不会单独发一条只有动作的消息，
# 但线上真出现过 6 个字一条、内容只有一个括号。
_ACTION_ONLY_LINE = re.compile(r"^\s*[（(][^（）()]{1,20}[）)]\s*$")


def shape_for_send(
    text: str,
    *,
    casual: bool = False,
    space_split: bool = True,
) -> tuple[str, dict[str, int]]:
    """出站最后一道整形。返回 (新文本, 各项计数)，计数只为打点用。

    为什么需要这一层：线上走 agent（agent/response.py -> polish_reply ->
    reply_send），那条链只删污染；口语化那几道工序全在 llm.chat 里，
    而线上 100% 不走它（2026-10-28 日志 19/19 条 agent.entry）。
    结果就是干跑看着像人，群里发的全是逗号句号破折号齐全的完整句。

    只做四件「改坏风险最低」的事：
    1. 删掉整行只有（动作）的行；
    2. 折叠连续空行（空行会拆出空气泡）；
    3. casual 时破折号换空格，再走 space_out_commas；
    4. 不截断长度：agent 的回答可能刚查过资料，按句截断的风险大于收益，
       长度交给 prompt 压。

    保护：含代码围栏、或整段是表格的，一律不整形（代码和表格里的逗号是语法）；
    <<< 标记行单独留出、原样接在最后，它们的字节不能动。
    """
    body = str(text or "")
    if not body.strip():
        return body, {}

    counts = {"action": 0, "blank": 0, "dash": 0, "comma": 0}

    lines: list[str] = []
    for line in body.split(chr(10)):
        if not line.strip():
            # 连续空行折叠：留一个空行当分段，多出来的会变成空气泡
            if lines and lines[-1] == "":
                counts["blank"] += 1
                continue
            lines.append("")
            continue
        if _ACTION_ONLY_LINE.match(line):
            counts["action"] += 1
            continue
        lines.append(line)
    trimmed = chr(10).join(lines).strip()
    # 万一整条就是个括号动作，删干净了也得退回原文：宁可不完美，不能发空消息
    if not trimmed:
        return body, {}

    marker_tail = [ln for ln in trimmed.split(chr(10)) if "<<" in ln]
    prose = chr(10).join(ln for ln in trimmed.split(chr(10)) if "<<" not in ln).strip()

    fence = chr(96) * 3
    protected = fence in prose or "|" in prose or "'" * 3 in prose
    if prose and casual and not protected:
        if space_split:
            dashed, dash_hits = _LONG_DASH.subn(" ", prose)
            counts["dash"] = dash_hits
            prose, comma_hits = space_out_commas(dashed)
            counts["comma"] = comma_hits

    out = chr(10).join([part for part in [prose] if part] + marker_tail)
    return (out or body), counts

