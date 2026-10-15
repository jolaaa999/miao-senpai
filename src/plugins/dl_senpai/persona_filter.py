"""识别并中和"别人贴给学姐的角色设定模板"。

背景：群里常有人贴一整套角色设定（「你是傲娇猫娘『凛』…【说话规则】…」），
或者贴别的 bot 的人设。这些文本会原样进入学姐的上下文甚至被存进记忆，
把她往别的性格上带 —— 改了人设文件也压不住上下文里的实例。

处理原则：**中和，不删除**。
把疑似设定替换成一句说明，让学姐知道"对方想让我扮演别的角色"，
从而能正经地拒绝；而不是让那条消息凭空消失，导致她答非所问。

保守优先：宁可漏判，不可误伤。误判会让学姐对正常聊天装看不见。
"""

from __future__ import annotations

from .observability.logging import log_event

# 强锚点：命中任意一个就足以判定。这些词几乎只在"贴模板"时出现，
# 日常聊天不会说"【角色设定】"。
_STRONG_ANCHORS = (
    "角色设定",
    "人物设定",
    "人设设定",
    "设定如下",
    "人设如下",
    "设定模板",
    "你的设定是",
    "以下是你的设定",
    "接下来你是",
    "从现在起你是",
)

# 弱锚点：单独出现不算（"这个设定挺有意思"是正常评论），
# 必须配合下面某个指令结构才判定。
_WEAK_ANCHORS = ("角色扮演", "扮演", "人设", "设定")

# 指令结构词：模板体的骨架。
_DIRECTIVES = (
    "你是",
    "你要",
    "你来",
    "请你",
    "必须",
    "规则",
    "要求",
    "不得",
    "禁止",
    "语气",
    "自称",
    "说话",
)

# 角色词：出现这些说明在指定一个"角色"，而不是在描述内容。
_ROLE_HINTS = (
    "猫娘",
    "女仆",
    "执事",
    "管家",
    "精灵",
    "机娘",
    "病娇",
    "御姐",
    "萝莉",
    "兽耳",
    "狐娘",
    "天使",
    "恶魔",
)

# 只有角色词 + "你是"、却没有模板头时，得长到像一整段设定才判定。
# 否则「你是傲娇吗」这种询问会被误伤 —— 实测群里真有人这么问过。
_MIN_LEN_FOR_BARE = 40

# 疑问句否决词。带着这些词的消息是在**问**学姐是不是某个角色，
# 不是在下设定指令。「学姐你是猫娘吗」必须放过去 ——
# persona 里专门写了"被叫猫娘会鼓起脸抗议"，拦掉就等于让她不搭理人。
_QUESTION_MARKERS = ("吗", "呢", "?", "？", "是不是", "难道", "怎么", "如何", "为什么")

# 疑问否决只在"没有模板头"时生效：真设定模板里也可能带问句
# （「你今天开心吗」这种设定细节），命中强锚点时不该被否决。
_STRONG_ENOUGH_TO_OVERRIDE = ("角色设定", "人物设定", "人设设定", "设定如下", "人设如下", "设定模板")

# 替换后的文案。刻意写明"想让学姐扮演别的角色"，
# 这样学姐读到就知道该正经拒绝，而不是当成普通聊天内容接下去。
PLACEHOLDER = "（对方粘贴了一段「角色设定」模板，想让学姐扮演别的角色）"


def looks_like_role_setting(text: str) -> bool:
    """判断这段文本像不像"贴在给某个 AI 设角色"。

    三条判据，命中任意一条即返回 True：
    0. 已中和过的占位文案 → 直接放行（幂等）
    1. 强锚点（角色设定 / 设定如下 …）
    2. 弱锚点 + 指令结构（例如"扮演" + "规则"）
    3. 角色词 + 你是/请你 + （足够长 或 带指令结构），且不是疑问句
    """
    t = (text or "").strip()
    if not t:
        return False
    # 已经中和过的文本一律放行。
    # 只靠"文案里别写锚点词"来保证幂等是不可靠的 —— 实测占位文案里的
    # 「角色设定」本身就命中了强锚点，第二次调用照样会被拦。
    # 引用消息那次过滤正是对已替换的文本再跑一遍，所以这个守卫是必需的。
    if PLACEHOLDER in t:
        return False
    if any(a in t for a in _STRONG_ANCHORS):
        return True
    if any(w in t for w in _WEAK_ANCHORS) and any(d in t for d in _DIRECTIVES):
        return True
    if any(h in t for h in _ROLE_HINTS) and any(
        d in t for d in ("你是", "你来", "请你")
    ):
        # 疑问句先否决：问"你是不是猫娘"不等于"设定你是猫娘"
        if any(q in t for q in _QUESTION_MARKERS) and not any(
            a in t for a in _STRONG_ENOUGH_TO_OVERRIDE
        ):
            return False
        return len(t) >= _MIN_LEN_FOR_BARE or any(d in t for d in _DIRECTIVES)
    return False


def neutralize(text: str) -> tuple[str, bool]:
    """命中就整体替换成说明文案；返回 (处理后的文本, 是否命中)。

    为什么整体替换而不是"抠掉那一段"：判据命中的消息本身就是一段设定，
    抠掉局部只会剩下半截更乱的文字。保守判据已经保证了误伤面足够小。

    为什么要记日志：拦了什么必须可回溯，否则出现"学姐怎么突然不搭理人"
    的投诉时无从查起。日志只记长度和命中特征词 —— 不记原文，
    否则日志里又存了一份设定，等于污染换了个地方待着。
    """
    if not looks_like_role_setting(text):
        return text, False
    log_event(
        "persona_filter.blocked",
        chars=len(text or ""),
        hits=_matched_features(text or ""),
    )
    return PLACEHOLDER, True


def _matched_features(text: str) -> list[str]:
    """命中的特征词（供日志排查用，不含原文）。"""
    found = [a for a in _STRONG_ANCHORS if a in text]
    found += [w for w in _WEAK_ANCHORS if w in text]
    found += [h for h in _ROLE_HINTS if h in text]
    return found[:6]


__all__ = ["looks_like_role_setting", "neutralize", "PLACEHOLDER"]
