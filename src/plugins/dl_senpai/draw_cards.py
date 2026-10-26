"""内置角色锚定卡。

为什么需要这个模块：生图是**纯文生图**，角色像不像全看提示词写得准不准。
而写提示词的是聊天模型，它对具体角色的记忆并不可靠——线上真实事故：它把
《明日方舟》的凯尔希写成「精灵尖耳」（实际是菲林族，猫耳 + 猫尾），图自
然完全不像。所以把常用角色的**官方硬特征**固化成数据，命中名字就直接拼进
提示词，不再依赖模型记忆。

维护原则：
- 只写能确定的特征（族属、发色、瞳色、服装主色、标志物）。拿不准的宁可不写，
  写错比不写更糟——它会被当成事实拼进提示词。
- 新增角色往 CARDS 里追加即可，匹配走 aliases（中英文都写上，生图的提示词
  经常是英文）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CharacterCard:
    """一个角色的锚定描述。"""

    name: str
    aliases: tuple[str, ...]
    spec: str


CARDS: tuple[CharacterCard, ...] = (
    CharacterCard(
        name="凯尔希",
        aliases=("凯尔希", "kal'tsit", "kaltsit", "kal tsit", "kaltsit"),
        spec=(
            "《明日方舟》凯尔希（Kal'tsit）官方设定，照此画不要改："
            "菲林族——头顶一对**猫耳**、身后有**猫尾**，绝不是精灵尖耳；"
            "很长很直的浅绿/银绿色头发，绿色眼睛，神情冷淡克制；"
            "白色医生长外套配黑色高领内衬，黑色腰带，肩上有罗德岛标志。"
        ),
    ),
    CharacterCard(
        name="阿米娅",
        aliases=("阿米娅", "amiya"),
        spec=(
            "《明日方舟》阿米娅（Amiya）官方设定，照此画不要改："
            "卡特斯族（兔子）——头顶一对**很长的兔耳**；"
            "深棕色中长发，蓝绿色眼睛，少年少女体型，个子不高；"
            "黑色连帽外套配白色内衬、深色短裤，胸前有**蓝色菱形源石徽章**，戴黑色手套。"
        ),
    ),
)


def match_character_card(*texts: str) -> CharacterCard | None:
    """在若干段文本里找命中的角色卡。

    为什么要多段入参：用户请求和模型写的提示词分散在两处，用户可能只说
    「画凯尔希」，而名字只出现在提示词里，反之亦然——两边都要查。
    """
    haystack = " ".join(str(t or "") for t in texts).lower()
    if not haystack.strip():
        return None
    for card in CARDS:
        for alias in card.aliases:
            if alias.lower() in haystack:
                return card
    return None


def augment_prompt(prompt: str, *hints: str) -> str:
    """命中角色卡时，把官方设定拼到提示词**前面**。

    为什么放前面：提示词有长度上限，超了会被 _truncate_prompt 从尾部截断。
    设定写在前面，被截掉的只会是模型自己补的发挥部分；反过来会把硬特征截没。
    """
    card = match_character_card(prompt, *hints)
    if card is None:
        return prompt
    return f"{card.spec}\n\n{prompt}".strip()
