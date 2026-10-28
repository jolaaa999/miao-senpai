"""出站文本质检：气泡断点与中文空格。

qq-bridge 在发送前会做两个「软提醒」（detect 后回灌给模型让它自己改）：

- 某条消息以逗号/顿号结尾，或以「的/了/吗/是/在/把……」这类必须接后续成分的
  词结尾，而**下一条又接着中文** —— 说明她把同一句话拆到了两条气泡里；
- 「中文之间打空格」 —— 真人不会那么打字（连中英文之间的空格都会分条）。

喵学姐这边没有「再给模型一稿」的回合，所以这里**就地修**，而不是提醒：

- fix_cjk_spaces：中文之间的空格 → 中文逗号；
- merge_incomplete_bubbles：半句话被拆成两条气泡 → 合回一条。

为什么值得单独做一块：这两类问题在 QQ 上极其显眼。气泡以「的」结尾、
下一条突然接「就」，读起来像被剁了一刀（这是「机器人」比「话多」更伤的地方）；
而「好的 我看下」里那个空格，一眼就是排版，不是打字。

两条都要**保守**：宁可漏修，也不能把两句本来独立的话硬粘成一句。
"""

from __future__ import annotations

#: 这些字结尾基本等于「话没说完，后面还得接东西」。
#:
#: 刻意**不含**语气词（了/吗/呢/吧/啊/呀/嘛）：它们通常是说完的标志，
#: 「你怎么了」是完整一句，把它和下一条「没事吧」粘起来就错得很难看。
_INCOMPLETE_CHARS = "的是在把被让给就都还又也很太最没这那和与或而但从向不以对"

#: 多字连接词：句尾出现这些，几乎一定还有下文。
_INCOMPLETE_WORDS: tuple[str, ...] = (
    "因为",
    "所以",
    "但是",
    "可是",
    "然后",
    "如果",
    "要是",
    "虽然",
    "不过",
    "其实",
    "而且",
    "或者",
    "不是",
    "就是",
    "那个",
    "这个",
)

#: 以这些标点结尾 = 句子没结束（和 presence 里的判定保持同一套语义）。
_NON_TERMINAL = "，,、；;：:—–-"

#: 这些短句虽然落在上面两类里，但本身就是完整一句话。
_COMPLETE_SHORT: frozenset[str] = frozenset(
    {
        "好的",
        "行吧",
        "算了",
        "知道了",
        "可以了",
        "没事了",
        "走了",
        "睡了",
        "来了",
        "懂了",
        "明白了",
        "就这样",
        "不是",
        "但是",
        "所以",
        "然后呢",
    }
)

#: 中文字符范围（含扩展 A 与兼容区）。判 CJK 只为了决定「要不要替换空格」，
#: 不需要覆盖全部 Unicode 表意文字。
_CJK_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0xF900, 0xFAFF),
)

#: 中文标点：空格两侧只要都是中文或中文标点，就一定是打字问题，不是排版。
_CJK_PUNCT = "，。、；：？！…～—－（）《》「」『』“”‘’％"


def _is_cjk(ch: str) -> bool:
    """字符是不是汉字。"""
    if not ch:
        return False
    code = ord(ch[0])
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def _is_cjk_side(ch: str) -> bool:
    """这一侧算不算「中文语境」（汉字或中文标点）。"""
    return bool(ch) and (_is_cjk(ch) or ch in _CJK_PUNCT)


def fix_cjk_spaces(text: str) -> str:
    """把中文之间的空格改成中文逗号（「好的 我看下」→「好的，我看下」）。

    只处理**两侧都是中文语境**的空格：英文单词间隔（DeepSeek V3）、
    数字与单位（3 kg）一律不动——那些空格是必要的。

    为什么用逗号而不是直接删掉空格：删成「好的我看下」在 QQ 上也是机器味
    （真人打字会顺手点个逗号），而逗号本来就是这个停顿的位置。
    """
    raw = text or ""
    if not raw or (" " not in raw and "\t" not in raw):
        return raw
    out: list[str] = []
    chars = list(raw)
    for i, ch in enumerate(chars):
        if ch not in (" ", "\t"):
            out.append(ch)
            continue
        prev = out[-1] if out else ""
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if _is_cjk_side(prev) and _is_cjk_side(nxt):
            out.append("，")
        else:
            out.append(ch)
    return "".join(out)


def _starts_cjk(text: str) -> bool:
    """去掉开头标点/空格后，第一个实义字符是不是中文。"""
    for ch in text:
        if ch.isspace() or ch in "，,。.、；;：:！!？?~～…—":
            continue
        return _is_cjk(ch)
    return False


def should_join(prev: str, nxt: str, *, max_chars: int = 160) -> bool:
    """这两条气泡原本是不是同一句话（该合起来发）。

    三条判据，缺一不可：上一条**明显没说完**、下一条**是中文接着往下说**、
    合起来不至于长到不像一句口语。
    """
    prev = (prev or "").strip()
    nxt = (nxt or "").strip()
    if not prev or not nxt:
        return False
    if prev in _COMPLETE_SHORT:
        return False
    if not _starts_cjk(nxt):
        return False
    if max_chars > 0 and len(prev) + len(nxt) > max_chars:
        return False
    if prev[-1] in _NON_TERMINAL:
        return True
    if prev.endswith(_INCOMPLETE_WORDS):
        return True
    # 单字结尾（「我」「的」）误判率高：至少两个字才按虚词结尾处理
    return len(prev) >= 2 and prev[-1] in _INCOMPLETE_CHARS


def merge_incomplete_bubbles(
    bubbles: list[str], *, max_chars: int = 160
) -> list[str]:
    """把「一句话被拆成两条」的气泡合回一条。

    不加分隔符：原文本来就是一句话，中间什么都没少，加逗号反而是替她改口。

    典型修法：
        ["前两句是宣传，", "第三句暴露了经典"] → ["前两句是宣传，第三句暴露了经典"]
        ["这是哪个番的", "表情包？"]           → ["这是哪个番的表情包？"]
    """
    items = [str(b).strip() for b in (bubbles or []) if str(b).strip()]
    if len(items) <= 1:
        return items
    out: list[str] = []
    for cur in items:
        if out and should_join(out[-1], cur, max_chars=max_chars):
            out[-1] = out[-1] + cur
        else:
            out.append(cur)
    return out


__all__ = [
    "fix_cjk_spaces",
    "merge_incomplete_bubbles",
    "should_join",
]
