"""生图意图识别：先判断「用户要的是哪一类图」，再决定走哪条链路。

## 为什么不能只有一条路

旧链路是二元的：命中角色档案 → 角色管线，否则 → 普通生图。
于是「画犬夜叉」和「画一只柴犬」走了同一条路，前者丢掉身份，后者被塞进
一堆没用的角色约束。而「照这张照片画成漫画」更是完全没位置放。

四条路各自的最优解完全不同：

    general_generation      猫、城市、坦克、原创角色 —— 不该查任何实体，
                            不搜图、不加身份约束，越快越好
    known_entity_generation 艾雅法拉、雷电将军、初音未来 —— 要锁身份，
                            但**不要求**本地有档案
    reference_generation    用户给了图 —— 图就是最高优先级依据，不许转成文字
    image_edit              用户要改一张已有的图

## 成本纪律

    general                     0 次额外模型调用（纯规则判断）
    known entity cache hit      0 次
    known entity cache miss     最多 1 次轻量文本解析
    reference search            默认 0 次
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .entity_resolver import Entity, UniversalEntityResolver

# 四种意图
INTENT_GENERAL = "general_generation"
INTENT_KNOWN = "known_entity_generation"
INTENT_REFERENCE = "reference_generation"
INTENT_EDIT = "image_edit"

# ---- 规则词表 ----------------------------------------------------------
# 这些是「类别名词」：出现它们说明用户在描述**一类东西**，不是某个具名实体。
# 画一只橘猫、画一台坦克、画一个城市 —— 都不该去查角色库。
_CATEGORY_WORDS = (
    "猫", "狗", "犬", "狐", "兔", "鸟", "鱼", "马", "熊", "龙", "虎", "狼", "鹿",
    "少女", "少年", "女孩", "男孩", "女性", "男性", "人物", "角色", "人",
    "坦克", "汽车", "卡车", "飞船", "飞机", "机甲", "机器人", "摩托", "自行车",
    "城市", "街道", "风景", "森林", "海边", "沙滩", "山", "太空", "宇宙", "房间",
    "建筑", "房子", "城堡", "飞船", "剑", "枪", "刀", "盔甲", "蛋糕", "咖啡",
    "插画", "壁纸", "头像", "封面", "海报", "设定图", "立绘", "画面", "场景",
    "猫娘", "兽耳", "拟人", "生物", "怪物", "妖怪", "精灵", "天使", "恶魔", "忍者",
    "骑士", "法师", "战士", "公主", "女仆", "JK", "柴犬", "橘猫", "哈士奇",
    "西装", "裙子", "长裙", "睡衣", "泳装", "和服", "礼服", "外套", "校服", "便装",
)

# 风格词：出现风格词通常是在描述画面基调，而不是点名某个 IP 角色。
_STYLE_WORDS = (
    "赛博朋克", "蒸汽朋克", "水彩", "油画", "国风", "水墨", "像素", "厚涂", "写实",
    "cg", "3d", "二次元", "动漫风", "极简", "flat", "复古", "胶片", "黑白",
)

# 量词：中文里「一个/一只/一台…」几乎总是引导一类东西，而不是某个专名。
# 只认双字量词：「这个角色」里的「个」不是量词，单字会让
# 「照着这个角色画」被判成类别描述。
_MEASURE_RE = re.compile(
    r"(一个|一只|一台|一张|一幅|一位|一条|一头|一群|一辆|一艘|一架|一座|一间|一对|一片|一栋)"
)

# 明确要原创角色
_ORIGINAL_RE = re.compile(r"原创|自创|自己设计|我的角色|我自己|\boc\b", re.IGNORECASE)

# 编辑动作：作用在「已有的图」上
_EDIT_RE = re.compile(
    r"把(这|那|我).{0,4}(图|照片|画).{0,6}(改|变|换|修|做|弄|画)"
    r"|(换成|改成|变成|修一下|重画|p图|抠图|去背景|加个字|加水印)"
)

# 用户提到图 / 参照
_REFERENCE_RE = re.compile(
    r"照着|按照|参考|参照|这张|那张|这副|这幅|我发的|上面的|这个人物|这个角色|这个人|照片里的人"
)

# 候选名的切分边界：动作、介词、连词、标点。
# 「画雷电将军穿西装」→ 雷电将军 | 穿 | 西装
_BOUNDARY_RE = re.compile(
    r"[，,。.;；!！?？、\s]"
    r"|穿着|穿|戴着|戴|坐在|站在|躺着|躺在|拿着|拿|抱着|抱|骑着|骑"
    r"|和|与|跟|还有|但|但是|不要|去掉|别|换成|改成|在|正在|一起|唱歌|跳舞|打架"
    r"|打游戏|喝咖啡|看书|睡觉|微笑|回头|望向"
)

# 生图动词前缀，判断前先剥掉，否则「画」会被当成名字的一部分
_VERB_PREFIX_RE = re.compile(
    r"^(请|帮我|帮忙|给我|麻烦|能不能|可以|来|我要|我想|想)?"
    r"(画|生成|绘|出|做|来)\s*(一张|一幅|一个|一只|台|张|幅|个|只)?"
    r"(图|图片|画|插画|壁纸|头像)?"
)

# ---- 显式覆盖（用户主动修改身份属性）-----------------------------------
# 「画艾雅法拉穿西装」不是身份错误，是用户要求。必须让显式修改压过默认约束，
# 否则系统会一边听用户的、一边在提示词里塞回原设定，最后画成四不像。
_OVERRIDE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("horns", re.compile(r"(不要|去掉|没有|别画|不画|摘掉|不长).{0,3}(角|犄角|羊角|鹿角)")),
    ("ears", re.compile(r"(不要|去掉|没有|别画|不画).{0,3}(耳|耳朵)")),
    ("hair", re.compile(r"(换成|改成|染成|剪成|变成).{0,4}(发|发型|发色|短发|长发|双马尾)")),
    ("clothing", re.compile(
        r"(穿|换上|换成|改成|穿着).{0,6}(西装|裙子|裙|泳装|睡衣|和服|制服|外套|T恤|战服|礼服|便装|运动服)"
        r"|(西装|泳装|睡衣|和服|便装|礼服)")) ,
    ("accessories", re.compile(r"(不要|去掉|没有|别画).{0,3}(法杖|武器|配饰|背包|帽子|眼镜|剑)")),
    ("palette", re.compile(r"(换成|改成|调成).{0,4}(颜色|色调|配色)")),
)

# 维度关键词：判断「用户到底动了哪个身份维度」
_DIMENSION_WORDS: dict[str, tuple[str, ...]] = {
    "hair": ("头发", "发型", "发色", "长发", "短发", "双马尾", "刘海"),
    "horns": ("角", "羊角", "犄角", "鹿角"),
    "ears": ("耳朵", "兽耳"),
    "clothing": ("衣", "服", "西装", "裙", "外套", "制服", "泳装", "睡衣", "和服"),
    "accessories": ("配饰", "法杖", "武器", "背包", "眼镜", "帽子"),
    "palette": ("配色", "颜色", "色调"),
}


@dataclass(frozen=True)
class ImageIntent:
    """一次生图请求的结构化理解结果。"""

    type: str = INTENT_GENERAL
    entities: tuple[Entity, ...] = ()
    user_references: tuple[str, ...] = ()
    edit_target: str = ""
    scene: str = ""
    style: str = ""
    confidence: float = 0.0
    reason: str = ""
    entity_source: str = ""
    entity_cache_hit: bool = False
    # 用户显式覆盖掉的维度 -> "overridden" / "removed"
    overrides: tuple[tuple[str, str], ...] = ()
    resolution_calls: int = 0     # 这次判断花了几次模型调用（成本可观测）

    @property
    def is_known_entity(self) -> bool:
        return self.type == INTENT_KNOWN and bool(self.entities)

    def override_map(self) -> dict[str, str]:
        return {k: v for k, v in self.overrides}

    def as_trace(self) -> dict[str, Any]:
        return {
            "image_intent": self.type,
            "entity_count": len(self.entities),
            "entity_names": [e.canonical_name or e.name for e in self.entities],
            "entity_source": self.entity_source,
            "entity_confidence": round(max((e.confidence for e in self.entities), default=0.0), 3),
            "entity_cache_hit": self.entity_cache_hit,
            "intent_reason": self.reason,
            "intent_resolution_calls": self.resolution_calls,
            "explicit_overrides": dict(self.overrides),
        }


def strip_verb_prefix(text: str) -> str:
    """剥掉「请帮我画一张」这类前缀，剩下的才是要分析的内容。"""
    return _VERB_PREFIX_RE.sub("", str(text or "").strip()).strip()


# 双字以上的类别词才可做子串匹配；单字（人/犬/龙/鱼…）只在整词相等时才算，
# 否则「鸣人」「犬夜叉」这类真名会被误杀。
_CATEGORY_STRICT = tuple(w for w in _CATEGORY_WORDS if len(w) >= 2)


def has_category_word(text: str) -> bool:
    """整句级别的宽松判断：允许单字（「画一条龙」要靠「龙」命中）。"""
    low = str(text or "").lower()
    return any(word.lower() in low for word in _CATEGORY_WORDS)


def _is_category_phrase(cand: str) -> bool:
    """候选名级别的严格判断：只在确实是一类东西时才否决它。"""
    if cand in _CATEGORY_WORDS:
        return True
    low = cand.lower()
    return any(word.lower() in low for word in _CATEGORY_STRICT)


def has_style_word(text: str) -> bool:
    low = str(text or "").lower()
    return any(word.lower() in low for word in _STYLE_WORDS)


def looks_like_general_description(text: str) -> bool:
    """判断这句话是否在描述**一类东西**而不是点名某个实体。

    两条独立依据，任一成立即可（都要求「不是专名写法」）：
      1. 用户明说要原创；
      2. 量词引导 + 类别词/风格词 —— 「画一只橘猫」「画一台未来坦克」
         「画一个赛博朋克东京」都是这个形状，而「画一张艾雅法拉」虽然有量词，
         但「艾雅法拉」不是类别词，所以不会被误判。
    """
    body = strip_verb_prefix(text)
    if _ORIGINAL_RE.search(body):
        return True
    # 量词看原句：strip_verb_prefix 会把「一只」当量词剥掉，剥完就丢了这个信号
    has_measure = bool(_MEASURE_RE.search(str(text or "")))
    if has_measure and (has_category_word(body) or has_style_word(body)):
        return True
    # 没有量词但通篇是风格+类别词（「赛博朋克城市夜景」）也算通用描述
    if not has_measure and has_style_word(body) and has_category_word(body):
        return True
    return False


def extract_entity_candidates(text: str, *, limit: int = 4) -> list[str]:
    """从请求里抠出可能是「具名实体」的片段。

    这是**保守**的启发式：只负责「有没有值得解析的名字」，
    真正的实体判定交给解析器（它知道《鸣人》是角色、《橘猫》不是）。
    宁可漏掉一句话，也不要为了命中率把普通名词送去解析——每次 miss 都是一次模型调用。
    """
    body = strip_verb_prefix(text)
    if not body:
        return []
    parts = _BOUNDARY_RE.split(body)
    out: list[str] = []
    for part in parts:
        cand = part.strip("的了吧呢啊呀 ，,。.、")
        if not (2 <= len(cand) <= 10):
            continue
        if _ORIGINAL_RE.search(cand):
            continue
        # 通用类别词 / 风格词出现在候选里 → 这是描述，不是名字
        if _is_category_phrase(cand) or has_style_word(cand):
            continue
        # 纯动词/虚词残留
        if cand in {"画面", "图片", "样子", "感觉", "风格", "效果", "东西"}:
            continue
        # 至少要有一个非常见汉字结构：中文名允许全汉字，但要求不是纯形容词
        if not re.search(r"[\u4e00-\u9fffA-Za-z]", cand):
            continue
        out.append(cand)
    return list(dict.fromkeys(out))[:limit]


def detect_explicit_overrides(text: str) -> dict[str, str]:
    """找出用户主动要求改掉/去掉的身份维度。

    这一步是**显式优先**的依据：用户说「穿西装」，服装约束就必须降级；
    说「不要羊角」，角就不能再出现在提示词里。
    """
    body = str(text or "")
    found: dict[str, str] = {}
    for dimension, pattern in _OVERRIDE_PATTERNS:
        if pattern.search(body):
            found[dimension] = "removed" if dimension in {"horns", "ears"} else "overridden"
    return found


def validate_entity_preservation(entity_name: str, aliases: tuple[str, ...], prompt: str) -> bool:
    """改写后的提示词里，实体身份还在不在？

    旧 enhance 会把「艾雅法拉」洗成「羊角少女」——句子通顺、语义相关，
    但身份没了，出图必然是一张泛泛的二次元羊角少女。
    所以任何改写之后都要验一遍：原名、别名、或拉丁名，至少留一个。
    """
    if not entity_name:
        return True
    text = str(prompt or "")
    low = text.lower()
    names = [entity_name, *aliases]
    for name in names:
        candidate = str(name or "").strip()
        if len(candidate) >= 2 and candidate.lower() in low:
            return True
    return False


async def analyze_image_intent(
    user_request: str,
    *,
    draft_prompt: str = "",
    has_user_image: bool = False,
    config: Any = None,
    resolver: UniversalEntityResolver | None = None,
) -> ImageIntent:
    """主入口：判断走哪条链路。

    判断顺序是刻意排的——先做零成本的判断，最后才考虑花一次模型调用。
    """
    text = str(user_request or "").strip()
    overrides = tuple(detect_explicit_overrides(text).items())
    scene = text
    style = ",".join(w for w in _STYLE_WORDS if w.lower() in text.lower())

    # ① 用户给了图：图本身就是最高优先级依据。
    #    这里绝不能走「Vision 转文字再丢图」——那等于把唯一可信的身份依据扔掉。
    if has_user_image:
        intent_type = INTENT_EDIT if _EDIT_RE.search(text) else INTENT_REFERENCE
        return ImageIntent(
            type=intent_type,
            user_references=("user_image",),
            edit_target=text if intent_type == INTENT_EDIT else "",
            scene=scene,
            style=style,
            confidence=1.0,
            reason="user_image_present",
            entity_source="user_reference",
            overrides=overrides,
        )

    if not text:
        return ImageIntent(type=INTENT_GENERAL, reason="empty_request", overrides=overrides)

    # ② 本地缓存命中：这是最便宜也最准的一条（人工核验过或之前解析过）
    from .character_resolver import resolve_character

    resolution = resolve_character(text, draft_prompt=draft_prompt, config=config)
    if resolution.is_known and resolution.profile is not None:
        profile = resolution.profile
        entity = Entity(
            name=resolution.matched or profile.canonical_name,
            canonical_name=profile.canonical_name,
            localized_name=getattr(profile, "localized_name", "") or "",
            franchise=getattr(profile, "franchise", "") or "",
            type="fictional_character",
            traits=tuple(profile.human_verified_fields()) + tuple(profile.assistant_reviewed_fields()),
            confidence=resolution.confidence,
            source="cache",
            cache_hit=True,
        )
        return ImageIntent(
            type=INTENT_KNOWN,
            entities=(entity,),
            scene=scene,
            style=style,
            confidence=resolution.confidence,
            reason="local_cache_hit",
            entity_source="cache",
            entity_cache_hit=True,
            overrides=overrides,
        )

    # ③ 明显是「一类东西」：不解析、不搜图，直接通用生成。
    if looks_like_general_description(text):
        return ImageIntent(
            type=INTENT_GENERAL,
            scene=scene,
            style=style,
            confidence=0.9,
            reason="generic_description",
            entity_source="rule",
            overrides=overrides,
        )

    # ④ 有像名字的片段 → 最多一次轻量解析（拿不到就只留名字，照样画）
    candidates = extract_entity_candidates(text)
    if not candidates:
        return ImageIntent(
            type=INTENT_GENERAL,
            scene=scene,
            style=style,
            confidence=0.7,
            reason="no_entity_candidate",
            entity_source="rule",
            overrides=overrides,
        )

    res = resolver or UniversalEntityResolver(config)
    entities = await res.resolve(candidates, request=text)
    calls = getattr(res, "last_call_count", 0)

    # 解析出的实体必须「确实是具名的」：低置信或 unknown_entity 一律视为没认出，
    # 走通用路径，但**保留用户原话**（不缩写成泛化描述）。
    strong = [
        e for e in entities
        if e.confidence >= 0.5 and e.type != "unknown_entity" and (e.canonical_name or e.name)
    ]
    if not strong:
        return ImageIntent(
            type=INTENT_GENERAL,
            scene=scene,
            style=style,
            confidence=0.5,
            reason="entities_unresolved",
            entity_source="dynamic_resolver",
            entity_cache_hit=False,
            overrides=overrides,
            resolution_calls=calls,
        )

    return ImageIntent(
        type=INTENT_KNOWN,
        entities=tuple(strong),
        scene=scene,
        style=style,
        confidence=max(e.confidence for e in strong),
        reason="dynamic_resolved",
        entity_source="dynamic_resolver" if not any(e.cache_hit for e in strong) else "cache",
        entity_cache_hit=any(e.cache_hit for e in strong),
        overrides=overrides,
        resolution_calls=calls,
    )


__all__ = [
    "INTENT_EDIT",
    "INTENT_GENERAL",
    "INTENT_KNOWN",
    "INTENT_REFERENCE",
    "ImageIntent",
    "analyze_image_intent",
    "detect_explicit_overrides",
    "extract_entity_candidates",
    "looks_like_general_description",
    "strip_verb_prefix",
    "validate_entity_preservation",
]
