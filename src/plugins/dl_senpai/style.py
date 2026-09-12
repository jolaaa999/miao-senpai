from __future__ import annotations

import re

_FAKE_EMOJI_RE = re.compile(
    r"[\[（(【]\s*[^\]）)】]{0,24}?(emoji|表情|微笑|温柔|开心|哭|炸毛)\s*[^\]）)】]{0,12}?[\]）)】]",
    re.IGNORECASE,
)
_STICKER_MARKER_RE = re.compile(r"<<<STICKER\s*[^>\n]+?>>>", re.IGNORECASE)
_VOICE_MARKER_RE = re.compile(r"<<<VOICE(?:\s+[^>\n]+?)?>>>", re.IGNORECASE)
_CARD_LEAK_RE = re.compile(
    r"\[?\s*<<<CARD\s*\{.*?\}\s*>>>\s*\]?",
    re.DOTALL | re.IGNORECASE,
)
_MUTE_LEAK_RE = re.compile(
    r"\[?\s*<<<MUTE\s*\{.*?\}\s*>>>\s*\]?",
    re.DOTALL | re.IGNORECASE,
)
_IMPRESSION_LEAK_RE = re.compile(
    r"\[?\s*<<<IMPRESSION\s*\{.*?\}\s*>>>\s*\]?",
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

# Markdown / 助手排版味：QQ 聊天不该出现
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_BOLD_UNDER_RE = re.compile(r"__(.+?)__")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_MD_HR_RE = re.compile(r"(?m)^\s*([-*_])\1{2,}\s*$")
_MD_LIST_RE = re.compile(r"(?m)^\s*[-*•]\s+")
_MD_ORDERED_RE = re.compile(r"(?m)^\s*\d+[.)、]\s+")


def strip_markdown_flavor(text: str) -> str:
    """去掉 **加粗**、列表横杠、标题井号等 AI 排版符号，保留正文。"""
    text = text or ""
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_BOLD_UNDER_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_HR_RE.sub("", text)
    text = _MD_LIST_RE.sub("", text)
    # 有序列表保留数字感太说明书；改成普通换行句子
    text = _MD_ORDERED_RE.sub("", text)
    # 残留成对星号/下划线
    text = text.replace("**", "").replace("__", "")
    return text


def strip_unapplied_mute_claims(text: str) -> str:
    """本轮没有真正禁言成功时，清掉「已经禁言了」类嘴炮。"""
    text = text or ""
    for phrase in _FALSE_MUTE_PHRASES:
        text = text.replace(phrase, "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[，,]{2,}", "，", text)
    text = re.sub(r"[～~]{2,}", "～", text)
    return text.strip(" ，,")


def polish_reply(text: str) -> str:
    """轻量润色：去掉假表情占位、泄漏指令、倾诉腔与「发不了消息」胡说；空回复兜底。"""
    text = (text or "").strip()
    if not text:
        return "在呢～"
    text = _STICKER_MARKER_RE.sub("", text)
    text = _VOICE_MARKER_RE.sub("", text)
    text = _CARD_LEAK_RE.sub("", text)
    text = _MUTE_LEAK_RE.sub("", text)
    text = _IMPRESSION_LEAK_RE.sub("", text)
    text = _SEARCH_LEAK_RE.sub("", text)
    text = _BROWSE_LEAK_RE.sub("", text)
    text = _FAKE_SYS_LOG_RE.sub("", text)
    text = _FAKE_VOICE_NOTE_RE.sub("", text)
    text = _FAKE_STICKER_NOTE_RE.sub("", text)
    text = _MEMBER_CANDIDATE_LEAK_RE.sub("", text)
    text = _INTERNAL_SYS_NUDGE_RE.sub("", text)
    text = _FAKE_EMOJI_RE.sub("", text)
    text = _MUTE_CANT_TALK_RE.sub("", text)
    for filler in _ASSISTANT_FILLER:
        text = text.replace(filler, "")
    for filler in _COUNSELOR_FILLER:
        text = text.replace(filler, "")
    text = strip_markdown_flavor(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # 清掉因删套话留下的空壳句
    lines = []
    for line in text.splitlines():
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
    return text or "在呢～"


# 兼容旧调用名
append_meow = polish_reply
