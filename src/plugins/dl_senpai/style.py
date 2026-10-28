from __future__ import annotations

import random
import time

import re

_FAKE_EMOJI_RE = re.compile(
    r"[\[（(【]\s*[^\]）)】]{0,24}?(emoji|表情|微笑|温柔|开心|哭|炸毛)\s*[^\]）)】]{0,12}?[\]）)】]",
    re.IGNORECASE,
)
# 黄脸/手势类 emoji（😀😤😏😂🥺👍👀）。
#
# 为什么只剥这一类：爱心💕、奖牌🥇、闪光✨ 这些是签到/好感文案里既有的
# 装饰符号（checkin.py / affection.py 的固定模板），不是「拿表情代替说话」；
# 一起剥会把那些界面改花。要治的是她拿 emoji 当情绪出口的习惯。
# 前后允许空格，剥的时候连空格一起吃掉，免得留下「好呀 」这种尾巴。
_FACE_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # 表情（😀😤😏😂😭🙂😳…）
    "\U0001F900-\U0001F92F"  # 补充表情（🤔🤗🤭🤫…）
    "\U0001F970-\U0001F97A"  # 🥰🥵🥶🥴🥺
    "\U0001FAE0-\U0001FAE8"  # 🫠🫥🫨 新一批
    "\U0001F440-\U0001F450"  # 👀👈👉👍 眼睛与手势
    "]\uFE0F?"
)
# emoji 剥掉后留下的空格规则：中文之间不留空格，英文之间留一个。
_CJK_BETWEEN_SPACE_RE = re.compile(
    r"(?<=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])[ \t]+(?=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])"
)

def strip_face_emoji(text: str) -> str:
    """剥掉黄脸/手势类 emoji。

    提示词里已经明令禁止，这里是兜底：模型仍会漏出几个（尤其情绪重的轮次）。
    只剥"用人脸表达情绪"的那一类，物件符号（💕🥇✨）原样保留。
    """
    if not text:
        return text
    cleaned = _FACE_EMOJI_RE.sub("", text)
    if cleaned == text:
        return text
    # 剥完常留双空格（「就这 😏 还行」→「就这  还行」）：先收成一个，
    # 再去掉夹在两个中文字之间的那个空格（中文本来不加空格）；
    # 英文之间保留一个，否则「hello world」会被粘成「helloworld」。
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = _CJK_BETWEEN_SPACE_RE.sub("", cleaned)
    return cleaned.strip()

_STICKER_MARKER_RE = re.compile(r"<<<STICKER\s*[^>\n]+?>>>", re.IGNORECASE)
_VOICE_MARKER_RE = re.compile(r"<<<VOICE(?:\s+[^>\n]+?)?>>>", re.IGNORECASE)
_CARD_LEAK_RE = re.compile(
    r"\[?\s*<<<CARD\s*\{.*?\}\s*>>>\s*\]?",
    re.DOTALL | re.IGNORECASE,
)
# 伪指令闭合符的容错写法。模型经常把 >>> 写成 >>、＞＞、】】，甚至写成 > > >；
# 只认 >>> 的后果是标记原样留在正文里 —— 2026-10-15 在群记忆里找到 7 处
# 「正文 + <<<IMPRESSION {...}>>」，模型还在标记后接着自言自语。这是所有
# 「控制标记漏进正文」问题的共同根因，所以收成一处常量，别再各写各的。
MARKER_CLOSE_PATTERN = r"(?:>+\s*>+\s*>+|>>|＞＞＞|＞＞|】】|】>|】|$)"

# 载荷可有可无：模型既写 <<<IMPRESSION>>>（见 tools/builtin.py），
# 也写 <<<IMPRESSION {…}>>，两种都必须清掉，末端的截断残片也一样。
_MUTE_LEAK_RE = re.compile(
    r"\[?\s*<<<MUTE(?:\s*\{.*?\})?\s*" + MARKER_CLOSE_PATTERN + r"\s*\]?",
    re.DOTALL | re.IGNORECASE,
)
_IMPRESSION_LEAK_RE = re.compile(
    r"\[?\s*<<<IMPRESSION(?:\s*\{.*?\})?\s*" + MARKER_CLOSE_PATTERN + r"\s*\]?",
    re.DOTALL | re.IGNORECASE,
)
_SEARCH_LEAK_RE = re.compile(
    r"<<<SEARCH\s*[^\n]*?(?:>>>|>>|＞＞＞|＞＞|】】|】>|】|＞＞|$)",
    re.IGNORECASE | re.MULTILINE,
)
_BROWSE_LEAK_RE = re.compile(
    r"<<<BROWSE\s*[^\n]*?(?:>>>|>>|＞＞＞|＞＞|】】|】>|】|＞＞|$)",
    re.IGNORECASE | re.MULTILINE,
)
# 群资源读取标记（<<<GROUP 公告>>> 之类）。工具没被启用时模型仍可能
# 照着记忆里的格式写出来，这一层保证它不会作为正文发给群友。
_GROUP_LEAK_RE = re.compile(
    r"<<<GROUP\s*[^\n]*?(?:>>>|>>|＞＞＞|＞＞|】】|】>|】|＞＞|$)",
    re.IGNORECASE | re.MULTILINE,
)
# 模型爱模仿记忆里的伪系统日志
_FAKE_SYS_LOG_RE = re.compile(
    r"[\[【][^\]】]{0,80}?(群名片变为|改名片为|禁言了|set_group_card|<<<CARD|<<<MUTE|<<<IMPRESSION)[^\]】]{0,80}?[\]】]",
    re.IGNORECASE,
)
# 本地小模型常把「语音已发送」旁注 / 成员候选 / 系统纠错原样吐出
_FAKE_VOICE_NOTE_RE = re.compile(
    r"[\[【]?\s*学姐发了一条语音[：:][^\]】\n]{0,120}\s*[\]】]?",
)
_FAKE_STICKER_NOTE_RE = re.compile(
    r"[\[【]?\s*学姐发了一个表情[\]】]?",
)
_MEMBER_CANDIDATE_LEAK_RE = re.compile(
    r"【成员候选】[\s\S]{0,800}?(?=(\n\n|$))",
)
_INTERNAL_SYS_NUDGE_RE = re.compile(
    r"[（(]系统[）)].{0,80}?(?:不要复读|换一句新的短回复).{0,40}",
)
# 对方明明在说话，却硬说「发不了消息」——只删含该胡说的短句
_MUTE_CANT_TALK_RE = re.compile(
    r"[^。！？\n]{0,24}"
    r"(?:现在|暂时)?"
    r"(?:发不了消息|不能发消息|没法发消息|发不出消息)"
    r"[^。！？\n]{0,24}[。！？～~.…]?"
)
_FALSE_MUTE_PHRASES = (
    "已经帮你禁言了",
    "小笨蛋已经禁言了",
    "已经禁言了",
    "帮你禁言了",
)
_ASSISTANT_FILLER = (
    "如果你有什么问题或者需要聊聊",
    "学姐还是很愿意倾听",
    "如果你还有其他需要帮忙的，记得随时告诉我哦",
    "如果你还有其他需要帮忙的",
    "记得随时告诉我哦",
    "学姐人设就是",
    "希望对你有帮助",
    "作为一个AI",
    "作为一个 AI",
)
# 心理咨询腔：不像正常人会说的群聊话
_COUNSELOR_FILLER = (
    "有什么心事也可以跟学姐说说，别憋着",
    "有什么心事也可以跟学姐说说",
    "有什么心理话也可以说一说嘛，别憋着",
    "有什么心理话也可以说一说嘛",
    "有什么心理话也可以说一说",
    "是不是最近遇到了什么烦心事？学姐在呢，来跟学姐说说吧",
    "是不是最近遇到了什么烦心事",
    "来跟学姐说说吧",
    "来跟学姐说说",
    "别憋着～",
    "别憋着",
    "学姐觉得你有点急脾气哦",
    "学姐会在旁边呢",
    "学姐在这呢，有什么心事也可以跟学姐说说",
    "学姐在这里呢，有什么心理话也可以说一说嘛",
    "如果你还有什么需要帮忙的",
)

# 书面总结/提示/客套/升华腔。这些人味最薄的中转句在 QQ 打字里不该出现。
# 只收**删掉不伤原意**的固定说法：「总的来说」即使出现在正经回答里，
# 去掉也只是少了一句衔接词，不会改变结论。
_AI_SUMMARY_FILLER = (
    "总的来说，",
    "总的来说",
    "总而言之，",
    "总而言之",
    "综上所述，",
    "综上所述",
    "一句话总结：",
    "一句话总结",
    "值得注意的是，",
    "值得注意的是",
    "需要注意的是，",
    "需要注意的是",
    "值得一提的是，",
    "值得一提的是",
    "这确实是一个很好的问题",
    "这确实是个很好的问题",
    "这是一个很好的问题",
    "这是个很好的问题",
    "加油，未来可期",
    "相信你会越来越好",
    "愿你越来越好",
    "未来可期",
)

# 心理咨询式的共情套话。只收"任何场合都假"的固定句式 ——
# 「辛苦了」「抱抱」这类日常话不在此列：它们是正常中文，
# 只在"单独成句、纯粹为安慰而说"时才显得敷衍，而那种情况删掉后
# 整行会变空，交给收尾的空壳行清理丢掉即可。
_EMPATHY_CLICHE_FILLER = (
    "我完全理解你的感受",
    "我理解你的感受",
    "我完全能理解你的感受",
    "我能理解你的心情",
    "我理解你的心情",
    "我懂你的感受",
    "你的感受是完全合理的",
    "你的感受是合理的",
    "你的感受很重要",
    "你一定很难受吧",
    "你现在一定很难受",
    "听起来你最近压力很大",
    "听起来你压力很大",
    "这一定很不容易",
    "这一定不容易",
    "你已经很努力了",
    "你已经做得很好了",
    "你已经做得很好啦",
    "别对自己太苛刻",
    "要好好照顾自己",
    "记得好好照顾自己",
    "需要我陪你聊聊吗",
    "需要学姐陪你聊聊吗",
    "有什么想说的都可以跟学姐说",
    "有什么想说的都可以说",
    "学姐在这里陪着你",
    "我在这里陪着你",
)

# 只由安慰话构成的**整句**（「抱抱你」「辛苦了」）。这些词本身是正常中文，
# 见词就删会误伤（「今天辛苦了」得留着），所以只在独自成句时才认。
_SOLO_COMFORT_RE = re.compile(
    r"^(?:抱抱(?:你)?|摸摸(?:你)?(?:头)?|心疼你|辛苦(?:了|你)|加油|别难过|会好的|没事的)$"
)
# 切句用的边界：句末标点后切一刀
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！!？?～~])")
# 判断"还剩没剩正文"时要无视的标点
_PUNCT_ONLY = "，,。.、；;：:～~！!？? \t\r\n"


def _strip_filler_phrases(text: str, phrases: tuple[str, ...]) -> str:
    """删套话短语，**连同紧跟其后的一个标点**。

    只删词不删标点会在句间留下「。，。」这种孤零零的标点串，
    比不删还难看；多吞一个标点就干净了。
    """
    for phrase in phrases:
        text = re.sub(re.escape(phrase) + r"[，,。.、；;：:！!？?～~]?", "", text)
    return text


def _drop_solo_comfort_sentences(text: str) -> str:
    """去掉整句只有安慰套话的句子。

    兜底很关键：**删完什么都不剩就原样返回** ——
    对方真的在难过时，「抱抱你」是她唯一能说的话，
    宁可留着，也不能把它换成「在呢～」。
    """
    parts = _SENTENCE_SPLIT_RE.split(text)
    kept: list[str] = []
    dropped = False
    for part in parts:
        core = part.strip().strip(_PUNCT_ONLY)
        if core and _SOLO_COMFORT_RE.match(core):
            dropped = True
            continue
        kept.append(part)
    result = "".join(kept)
    if dropped and not result.strip().strip(_PUNCT_ONLY):
        return text
    return result

# Markdown / 助手排版味：QQ 聊天不该出现
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_BOLD_UNDER_RE = re.compile(r"__(.+?)__")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
# 代码围栏：qx60 转义写，源码里不出现反引号。QQ 不渲染 markdown，
# 围栏行留着就是纯噪音；但块内内容必须原样保留——缩进和 ASCII 图全靠它。
_MD_FENCE_BLOCK_RE = re.compile(
    r"(?ms)^[ \t]*\x60\x60\x60[^\n]*\n(.*?)^[ \t]*\x60\x60\x60[^\n]*$"
)
# 没配对的围栏（模型偶尔漏结尾）只删标记行，内容留在原位。
_MD_FENCE_LINE_RE = re.compile(r"(?m)^[ \t]*\x60\x60\x60[^\n]*$")

# 长文门槛：达到这个字数才保留列表结构。
# 短闲聊里冒出一串编号像说明书（去 AI 味的要求），长回复里序号却是信息——
# 两种场景取舍相反，只能按长度分叉。
_MD_KEEP_STRUCTURE_MIN = 400

_MD_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_MD_HR_RE = re.compile(r"(?m)^\s*([-*_])\1{2,}\s*$")
_MD_LIST_RE = re.compile(r"(?m)^\s*[-*•]\s+")
_MD_ORDERED_RE = re.compile(r"(?m)^\s*\d+[.)、]\s+")


def strip_markdown_flavor(text: str) -> str:
    """去掉 **加粗**、列表横杠、标题井号等 AI 排版符号，保留正文。

    按长度分叉的理由：短闲聊和长回复对排版的诉求相反。闲聊里冒出一串编号
    像说明书，长技术回复里序号本身就是信息，删了就读不懂了。

    代码围栏无论长短都只去掉标记行、保留内容：QQ 没有等宽字体，标记行
    只是噪音，而内容（缩进、ASCII 图）删掉就真没了。

    有序编号另有一条与长度无关的例外：连着两条以上编号算「真在讲步骤」，
    短回复也保留（见下方分支注释）。persona 允许办事流程用编号列步骤，
    无脑删会让模型照做、用户却拿到一坨没有编号的步骤。
    """
    text = text or ""
    keep_structure = len(text) >= _MD_KEEP_STRUCTURE_MIN

    # 先把围栏内容抽成占位符再跑其余替换：否则块内的 `- x`、`1. x` 会被
    # 下面的列表规则当成正文改掉，代码和图就散了。
    blocks: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        blocks.append(match.group(1).rstrip("\n"))
        return f"\x01{len(blocks) - 1}\x01"

    text = _MD_FENCE_BLOCK_RE.sub(_stash, text)
    # 结尾围栏漏写时上面的配对不成立，这里兜底删掉孤立的标记行
    text = _MD_FENCE_LINE_RE.sub("", text)

    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_BOLD_UNDER_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_HR_RE.sub("", text)
    if keep_structure:
        # 长文保留「这是个列表项」的信号，横杠换成中文里更顺的间隔点
        text = _MD_LIST_RE.sub("· ", text)
    else:
        text = _MD_LIST_RE.sub("", text)
        # 有序列表只在「孤零零一条编号」时才当说明书腔删掉。连续两条以上编号
        # 是真人讲步骤的正常写法，而且 persona 明确允许「办事流程用 1. 2. 3.
        # 列步骤」——这里再删就自相矛盾：模型照做，用户拿到的步骤却没有编号。
        if len(_MD_ORDERED_RE.findall(text)) < 2:
            text = _MD_ORDERED_RE.sub("", text)
    # 残留成对星号/下划线
    text = text.replace("**", "").replace("__", "")
    if blocks:
        text = re.sub(r"\x01(\d+)\x01", lambda m: blocks[int(m.group(1))], text)
    return text


_PLACEHOLDER_RE = re.compile(r"\x01(\d+)\x01")


def _stash_code_blocks(text: str) -> tuple[str, list[str]]:
    """把围栏代码块的内容抽成占位符，返回（去掉围栏的正文, 块内容列表）。

    为什么要抽：polish_reply 后面的清理规则是给闲聊话术写的——压重复句读、
    删行尾标点、按行丢空壳句。这些落在代码上就是破坏内容：
    `for(;;)` 的 ;; 被压成「，」、「...args」被压成「。」、行尾分号被当句读删掉。
    抽成占位符后整条流水线碰不到它，最后原样放回。

    围栏标记行本身不保留：QQ 没有等宽字体，留着只是噪音。
    未闭合的围栏也删标记、留内容（模型偶尔漏写收尾）。
    """
    blocks: list[str] = []

    def _keep(match: re.Match[str]) -> str:
        blocks.append(match.group(1).rstrip("\n"))
        return f"\x01{len(blocks) - 1}\x01"

    text = _MD_FENCE_BLOCK_RE.sub(_keep, text or "")
    # 收尾围栏漏写时上面的配对不成立，这段内容同样得保住：
    # 否则后面的闲聊清理规则会把它当正文改（行尾分号被删、;; 被压成「，」）。
    orphan = _MD_FENCE_LINE_RE.search(text)
    if orphan is not None:
        blocks.append(text[orphan.end():].strip("\n"))
        text = text[: orphan.start()] + f"\x01{len(blocks) - 1}\x01"
    return text, blocks


def _restore_code_blocks(text: str, blocks: list[str]) -> str:
    """把 _stash_code_blocks 抽走的代码内容放回占位符处。"""
    if not blocks:
        return text
    return _PLACEHOLDER_RE.sub(lambda m: blocks[int(m.group(1))], text)


def strip_unapplied_mute_claims(text: str) -> str:
    """本轮没有真正禁言成功时，清掉「已经禁言了」类嘴炮。"""
    text = text or ""
    for phrase in _FALSE_MUTE_PHRASES:
        text = text.replace(phrase, "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[，,]{2,}", "，", text)
    text = re.sub(r"[～~]{2,}", "～", text)
    return text.strip(" ，,")


# -- 模型把内心盘算 / 伪造多人对话当正文吐出来 --
#
# 背景（2026-10-13 线上事故）：群里一条回复 704 字，前半段是自我分析
# （「对方在问…按人设…不用检索，不用工具…」），后半段是模型替对方续写的
# 十几轮假对话（形如「失语：你是猫娘吗」）。根因是 relay 上的推理模型把
# 思考过程内联进了 content，而 prompt 里用户消息又按「昵称：内容」拼装，
# 模型顺着这个格式一路编下去。下面是代码侧兜底，prompt 侧同时补了硬禁令。

# 思考分隔符。形态不固定，见到过字面量 end、</thinking> 等。
# 不用 \b 是因为 Python 的 \w 含 CJK：「end学」两边都算 \w，\b 根本不匹配。
_THINK_DELIM_RE = re.compile(
    r"<\|?end\|?>|(?<![A-Za-z])end(?![A-Za-z])|</?think(?:ing)?>|【思考结束】",
    re.IGNORECASE,
)

# 「盘算」的典型措辞。必须与分隔符同时命中才认定是思考前言——
# 只看分隔符太容易误伤，正常回答里也可能出现 end。
_PLAN_HINT_RE = re.compile(
    r"对方(在|问|说)|按人设|人设是|不用检索|不用工具|应该回|这里就是|"
    r"可以(稍微|带点)|别官方腔|不要复读|他(之前|又)|要(短|长)一点|让我想|"
    # 2026-10-15 补：线上两条回复的思考前言用的正是下面这些措辞，原词表
    # 覆盖不到（实测对它们原样返回），于是思考原样发了出去 —— 群记忆里能看到
    # 用户把那段思考引用回来当把柄。判据仍要求「分隔符 + 措辞」同时命中，
    # 正常回答里出现这些词不会被误切。
    r"用户(问|在问|说)|检索(结果|里|到)|可以说了|语气(可以|要)|回答(要|别)|"
    r"别太长|短一点|这是他的|可以记一下|要口语"
)

# 说话人前缀，形如「某某：」。
# 名字里允许 . 和 ·：真实昵称有「失语.」这种，原字符集把点排除在外，
# 于是「失语.：你是猫娘吗」十几轮伪造对话一条都没被截断（2026-10-15 线上，
# 那条回复 554 字里大半是伪造转录）。
# 候选名最长 4 字：真实昵称基本在这个量级，而 8 字会把「让我多回一次失语」
# 整段吞成一个候选名（它只出现一次，于是真正的重复名反而检不出来）。
_SPEAKER_PREFIX_RE = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9_\-\.·]{1,4})\s*[：:]")

# 紧跟在开引号/开括号后面的「名字：」是在**引用对方原话**，不是编写台词。
# 2026-10-25 线上事故：对方昵称是「失语.」，而 prompt 里用户消息本来就按
# 「昵称：内容」渲染，她只要引用一次原话（形如「比如「失语.：什么意思」」），
# 旧规则就从那里砍到结尾 —— 三条回复分别只剩 108 字、179 字和「在呢～」。
_QUOTE_OPEN_CHARS = "「『“‘\"'【(（《<＜"
# URL 协议头（http:// 这类）：冒号后紧跟 //，不是说话人前缀
_URL_SCHEME_TAIL = "//"

# 正常回答里会用「X：」的高频词，不能当说话人。
# 误判的代价是把整段回复从中间截断，所以宁可多列一些。
_SPEAKER_STOPWORDS = frozenset({
    "注意", "提示", "说明", "例如", "比如", "总结", "答案", "结论", "建议",
    "步骤", "方法", "原理", "补充", "备注", "结果", "原因", "问题", "重点",
    "参考", "警告", "错误", "正确", "示例", "用法", "参数", "返回", "输入",
    "输出", "代码", "版本", "时间", "地址", "链接", "温馨提示", "划重点",
})


def strip_thinking_prefix(text: str) -> str:
    """切掉模型吐在正文开头的「内心盘算」，只留真正要说的话。

    判据是「分隔符 + 分隔符之前确实像盘算」同时成立，两个条件缺一不可。
    """
    if not text:
        return text
    for m in _THINK_DELIM_RE.finditer(text):
        head = text[: m.start()]
        tail = text[m.end():].lstrip(" \t\r\n：:，,。.、")
        if len(head) >= 12 and tail and _PLAN_HINT_RE.search(head):
            return tail
    return text


def _bare_hits(text: str, name: str) -> list[int]:
    """找出某个名字的「裸」出现位置（引用里的不算、URL 协议头不算）。

    所谓裸，就是**紧跟在开引号/开括号后**的那种不算 —— 那是在引用对方原话，
    不是替人编台词。2026-10-25 的误截就是栽在这里：对方昵称「失语.」，她引用
    一句「失语.：什么意思」，旧规则当成了伪造转录，从引号里一路砍到结尾。
    """
    if not name:
        return []
    hits: list[int] = []
    for m in re.finditer(re.escape(name) + r"\s*[：:]", text):
        if text[m.end(): m.end() + 2] == _URL_SCHEME_TAIL:
            continue
        if m.start() > 0 and text[m.start() - 1] in _QUOTE_OPEN_CHARS:
            continue
        hits.append(m.start())
    return hits


def _generic_hits(text: str) -> dict[str, list[int]]:
    """拿不到昵称时的兜底：统计「短名：」的出现位置，按名字分组。"""
    buckets: dict[str, list[int]] = {}
    for m in _SPEAKER_PREFIX_RE.finditer(text):
        cand = m.group(1)
        if cand.isdigit() or cand in _SPEAKER_STOPWORDS:
            continue
        if text[m.end(): m.end() + 2] == _URL_SCHEME_TAIL:
            continue
        if m.start() > 0 and text[m.start() - 1] in _QUOTE_OPEN_CHARS:
            continue
        buckets.setdefault(cand, []).append(m.start())
    return buckets


def _same_speaker(cand: str, speaker: str) -> bool:
    """两个名字是否算同一个（容忍昵称尾部的点/间隔号）。"""
    trim = ".·。，, "
    return bool(cand) and bool(speaker) and cand.strip(trim) == speaker.strip(trim)


def strip_fake_transcript(text: str, *, speaker: str = "") -> str:
    """截断模型伪造的多人对话，保留截断点之前的正常内容。

    判据（2026-10-25 重写）：
      1. **同一个名字出现 >=2 次**（且不在引号内）——正常回复不会这样，
         这是 2026-10-13 / 10-15 两次伪造转录事故的形态；
      2. 只出现一次却顶在正文最前面 ⇒ 那是模型给对方写了个台头，
         摘掉前缀即可，**不要**把整条回复砍掉。

    为什么去掉「见到对方昵称一次就砍」：prompt 本身按「昵称：内容」渲染用户
    消息，回复里引用原话再正常不过。旧规则把引用当成伪造转录，会把整条回复
    砍成半句、甚至砍成空串（线上表现就是只剩一句「在呢～」）。

    已知昵称（speaker）优先：它最准，不受「前面几个汉字也算候选名」干扰。
    拿不到昵称时才退回「同一个短名重复出现」的通用判据。
    """
    if not text:
        return text

    name = (speaker or "").strip()
    hits = _bare_hits(text, name) if name else []
    if not hits:
        buckets = _generic_hits(text)
        repeated = [pos for poses in buckets.values() if len(poses) >= 2 for pos in poses]
        if repeated:
            return text[: min(repeated)].rstrip()
        # 只有一处的台头：只在「确实是对方的名字」时才摘，避免误删「引言：」这类
        first = [(c, ps[0]) for c, ps in buckets.items() if ps[0] == 0]
        if first and _same_speaker(first[0][0], name):
            m = _SPEAKER_PREFIX_RE.match(text)
            if m is not None and text[m.end():].strip():
                return text[m.end():].lstrip(" \t")
        return text

    if len(hits) >= 2:
        return text[: hits[0]].rstrip()

    # 单次且贴在最前面：只摘前缀，砍成空串会退化成「在呢～」
    if hits[0] == 0:
        m = re.match(re.escape(name) + r"\s*[：:]", text)
        rest = text[m.end():].lstrip(" \t") if m else ""
        if rest:
            return rest
    return text


# -- 工具标记残片：解析失败的 marker 不许进正文，更不许进记忆 --
#
# 2026-10-15 线上事故：模型写成 <<<CODE ... <<<END>>> 而不是原生调用，那段
# Python 被拆成 3 个气泡**原样发给了用户**，又被写成 assistant 历史。之后
# 每一轮她都把上一轮当成「那道题还没答完」，于是一直补答旧问题 —— 用户侧
# 看到的就是「答非所问、话很混乱」。
_CODE_LEAK_RE = re.compile(
    r"<<<\s*CODE\b[\s\S]*?(?:<<<\s*END\b[^\n]*|\Z)", re.IGNORECASE
)
# 兜底：任何没被工具消费掉的 <<<MARKER 残片（含缺闭合、行尾截断）
_GENERIC_MARKER_LEAK_RE = re.compile(
    r"<<<\s*[A-Z][A-Z_]{1,15}\b[\s\S]*?(?:>>>|>>|＞＞＞|＞＞|】】|】>|】|$)",
    re.IGNORECASE | re.MULTILINE,
)
_END_RESIDUE_RE = re.compile(r"<<<\s*END\b[^\n]*", re.IGNORECASE)


def strip_marker_residue(text: str) -> str:
    """剥掉残留的工具标记。

    抽成公开函数是有意的：polish_reply 是发送前的必经之路，而
    ChatMemory.load_all 在**读历史**时也要用同一套规则 —— 否则历史里那条
    标记残渣会被反复喂回 prompt，模型一路补答那个旧问题。
    """
    if not text or "<<<" not in text:
        return text
    text = _CODE_LEAK_RE.sub("", text)
    text = _GENERIC_MARKER_LEAK_RE.sub("", text)
    text = _END_RESIDUE_RE.sub("", text)
    return text


# -- 空串兜底：别再吐同一个「在呢～」 --
#
# 2026-10-25 线上数据：一天 99 条发送里 32 条是「在呢～」，其中 12 条还配着图。
# 来源是工具调用轮（生图/截图/记忆）模型没有正文，被这里兜底成一句固定话；
# 同一个群里连着出现过六条一模一样的。
#
# 真人不会对着自己发的图说「在呢～」。所以改成两句规则：
#   1. 兜底不再固定一句，从短反应里挑，且避开上一次用过的；
#   2. 同一会话冷却期内再触发就**直接不说话**——宁可不发，也不发第二次。
_FALLBACK_LINES: tuple[str, ...] = ("嗯？", "在。", "咋了", "你说", "啊？")
_FALLBACK_COOLDOWN_SEC = 180.0
_fallback_last: dict[str, tuple[float, str]] = {}


def _empty_fallback(
    session_key: str = "", speaker: str = "", allow: bool = True
) -> str:
    """正文被清空时的兜底。冷却期内、或本轮本来就有媒体时返回空串。

    allow=False 用在工具轮（生图 / 截图）：那时正文本来就该是空的，
    媒体才是这一轮要发的东西，补一句「嗯？」只是噪音。
    """
    if not allow:
        return ""
    key = str(session_key or "") or f"speaker:{speaker}"
    now = time.monotonic()
    last = _fallback_last.get(key)
    if last and now - last[0] < _FALLBACK_COOLDOWN_SEC:
        return ""
    pool = [line for line in _FALLBACK_LINES if not last or line != last[1]]
    line = random.choice(pool or list(_FALLBACK_LINES))
    _fallback_last[key] = (now, line)
    return line


def reset_fallback_state() -> None:
    """测试用：清掉兜底冷却记录。"""
    _fallback_last.clear()


def polish_reply(
    text: str,
    *,
    speaker: str = "",
    session_key: str = "",
    allow_fallback: bool = True,
) -> str:
    """轻量润色：去掉假表情占位、泄漏指令、倾诉腔与「发不了消息」胡说；空回复兜底。"""
    text = (text or "").strip()
    if not text:
        return _empty_fallback(session_key, speaker, allow_fallback)
    # 先处理结构性污染（思考前言 / 伪造对话）再做逐条正则清理：
    # 这两类污染会把 marker 和假台词混在一起，先切干净后面才好认。
    text = strip_thinking_prefix(text)
    text = strip_fake_transcript(text, speaker=speaker)
    # 代码块 / ASCII 图先抽出来，整条流水线跑完再原样放回。
    # 后面的规则是给闲聊话术写的，落在代码上会真破坏内容（见 _stash_code_blocks）。
    text, _code_blocks = _stash_code_blocks(text)
    # 标记残片在**抽出代码块之后**清：她真的贴一段含 <<< 的示例代码时，
    # 那些字符是内容而不是控制标记，不该被剥掉（占位符 N 不受影响）。
    text = strip_marker_residue(text)
    if not text:
        return _empty_fallback(session_key, speaker)
    text = _STICKER_MARKER_RE.sub("", text)
    text = _VOICE_MARKER_RE.sub("", text)
    text = _CARD_LEAK_RE.sub("", text)
    text = _MUTE_LEAK_RE.sub("", text)
    text = _IMPRESSION_LEAK_RE.sub("", text)
    text = _SEARCH_LEAK_RE.sub("", text)
    text = _GROUP_LEAK_RE.sub("", text)
    text = _BROWSE_LEAK_RE.sub("", text)
    text = _FAKE_SYS_LOG_RE.sub("", text)
    text = _FAKE_VOICE_NOTE_RE.sub("", text)
    text = _FAKE_STICKER_NOTE_RE.sub("", text)
    text = _MEMBER_CANDIDATE_LEAK_RE.sub("", text)
    text = _INTERNAL_SYS_NUDGE_RE.sub("", text)
    text = _FAKE_EMOJI_RE.sub("", text)
    # 真 emoji 也剥：她习惯用 😤😏😑 收尾，提示词压不住时靠这层兜底
    text = strip_face_emoji(text)
    text = _MUTE_CANT_TALK_RE.sub("", text)
    for filler in _ASSISTANT_FILLER:
        text = text.replace(filler, "")
    for filler in _COUNSELOR_FILLER:
        text = text.replace(filler, "")
    text = _strip_filler_phrases(text, _AI_SUMMARY_FILLER)
    text = _strip_filler_phrases(text, _EMPATHY_CLICHE_FILLER)
    text = strip_markdown_flavor(text)
    # 删掉短语后句间可能剩下成串句读，压成一个。
    # 只处理句读类：！？连用是正常中文（「真的吗！？」），不能压。
    text = re.sub(r"[，,、；;：:]{2,}", "，", text)
    text = re.sub(r"[。.]{2,}", "。", text)
    text = re.sub(r"[，,、；;：:][。.]+", "。", text)
    text = re.sub(r"[。.]+[，,、；;：:]+", "。", text)
    text = re.sub(r"[。.](?=[！!？?])", "", text)
    text = _drop_solo_comfort_sentences(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # 清掉因删套话留下的空壳句
    lines = []
    for line in text.splitlines():
        # 删掉套话后半句可能以标点开头（「，早点睡」），先清掉再看是不是空壳。
        # 「…」不在清理集里：省略号开头的句子是真人口语，不能误伤。
        line = re.sub(r"^[，,。、；;：:～~]+\s*", "", line)
        # 行尾同样可能剩下孤立的句读（「…抱抱你，」）
        line = re.sub(r"[，,、；;：:]+\s*$", "", line)
        s = line.strip(" ，,。.~～")
        if not s:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        # 整行只剩空括号也丢掉
        if s in {"[]", "【】", "()"}:
            continue
        lines.append(line.rstrip())
    text = "\n".join(lines).strip()
    # 放回代码块要放在最后一步：任何后续清理都不该再碰它
    text = _restore_code_blocks(text, _code_blocks)
    return text or _empty_fallback(session_key, speaker)


# 兼容旧调用名
append_meow = polish_reply
