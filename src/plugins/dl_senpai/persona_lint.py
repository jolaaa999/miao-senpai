"""角色卡体检：防止 persona.py 慢慢退化成「人设说明书」。

搬自 ai-companion-kit 的 Role.lint()。那边给的理由在这里同样成立：

    决定一个 AI 像不像人的，主要不是模型，是提示词。

而提示词是几百行纯文本，改的时候没人 review，就会悄悄漂移——身份里
混进「助手 / 客服」、满屏 Markdown 分点、每句都以问号结尾。等发现的
时候，模型已经在群里用客服腔回话了。

这里**不生成提示词、也不改它**，只做静态检查：
- 测试里跑一遍，写歪了直接红（drift 在提交前暴露）
- 命令行跑一遍，改完人设顺手体检

用法：
    python -m src.plugins.dl_senpai.persona_lint
"""

from __future__ import annotations

import re
import sys
from typing import Iterable

# 身份里出现这些词，模型就会开始用客服腔——这是最贵的一条经验。
# 注意「助手」在别处是正常汉语，所以只匹配「你是……助手/客服」这种身份句式，
# 不做全文禁词（否则人设里写「不当助手」也会被误报）。
_ASSISTANT_IDENTITY_RE = re.compile(
    r"(你|汝)(就)?是[^。\n]{0,12}(助手|助理|客服|机器人助手|智能助手)"
)

# 客服话术：出现基本就说明跑偏了
_SERVICE_PHRASES: tuple[str, ...] = (
    "很高兴为您服务",
    "希望对您有帮助",
    "希望对你有帮助",
    "如有疑问",
    "还有什么可以帮",
    "感谢您的咨询",
    "亲爱的用户",
    "请您稍等",
)

# 「怎么说话」——这一段缺失是最致命的：没有它，模型只剩默认客服腔
_SPEECH_HINTS: tuple[str, ...] = ("说话", "语气", "口吻", "打字", "口头", "措辞")
# 「不做什么」——没有边界的角色容易跑偏
_BOUNDARY_HINTS: tuple[str, ...] = ("不要", "别", "禁止", "不许", "不准", "绝不")

# Markdown 标题：人设是给模型读的提示词，不是网页，出现 # 标题说明
# 有人把它当文档在写，模型也会照这个风格输出带格式的回复
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)

# 单行超过这个长度，模型对它的注意力会明显下降（长段落等于没写）
_MAX_LINE_CHARS = 500

# 否定词：话术出现在「不要这样说」的清单里是**正确用法**，不是跑偏。
# 线上第一次试跑就被这条坑过——人设第 86 行写着
# 「- 助手腔：「希望对你有帮助」「作为一个 AI」」，
# 那是反面教材，机器却把它当成「她该这么说」。所以命中话术之后
# 必须回到那一行看有没有否定词，有就放过。
_NEGATION_HINTS: tuple[str, ...] = (
    "不要",
    "别",
    "禁止",
    "不许",
    "不准",
    "绝不",
    "避免",
    "不能",
    "不该",
)


def _line_of(text: str, index: int) -> str:
    """取 index 所在的那一行。"""
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return text[start : end if end != -1 else len(text)]


def _section_header_above(body: str, index: int) -> str:
    """取 index 之前最近的【…】段标题。

    为什么需要它：人设里的反面教材长这样——

        【绝对不要说的】
        - 助手腔：「希望对你有帮助」「作为一个 AI」

    否定词在**段标题**上，被列举的那一行本身干干净净。只看本行必然误判。
    """
    idx = body.rfind("\n【", 0, index)
    if idx < 0:
        return ""
    return _line_of(body, idx + 1)


# 清单项：只有列举出来的话术才可能属于「反面教材」。
# 正文中间随口一句「希望对你有帮助」没有列表标记，那是真的跑偏了。
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.、])\s")


def _is_negative_example(body: str, phrase: str) -> bool:
    """这句话术是不是出现在「别这么说」的清单里。

    判据分三层，逐层收紧：
      1. 本行有否定词 —— 直接放过（「不要说希望对你有帮助」）。
      2. 本行是清单项、且所属段落的【…】标题有否定词 —— 放过
         （【绝对不要说的】下面的列举）。
      3. 本行是清单项、且上面几行有否定词 —— 放过（「仍然不要：」这种引导语）。

    为什么第 2、3 层都要求「清单项」：第一版只看段标题，结果
    【边界】下面的整段都被判成反面教材——段标题里有「不要」，
    连带把后面正文里的客服话术也豁免了。假阴性比误报更危险。
    """
    idx = body.find(phrase)
    if idx < 0:
        return False

    line = _line_of(body, idx)
    if any(h in line for h in _NEGATION_HINTS):
        return True

    if not _LIST_MARKER_RE.match(line):
        return False

    header = _section_header_above(body, idx)
    if any(h in header for h in _NEGATION_HINTS):
        return True

    # 引导语可能不写成【】标题（例如孤零零一行「仍然不要：」）
    start = body.rfind("\n", 0, max(0, idx - 1))
    window = body[max(0, idx - 260) : start if start > 0 else idx]
    return any(
        any(h in row for h in _NEGATION_HINTS) for row in window.splitlines()[-4:]
    )


def lint_persona_prompt(text: str, *, name: str = "persona") -> list[str]:
    """体检，返回问题描述列表。空列表 = 通过。

    每条都写成「哪里不对 + 为什么」，因为它的读者是改人设的人，
    不是跑 CI 的机器。
    """
    problems: list[str] = []
    body = str(text or "")

    if len(body.strip()) < 200:
        problems.append(f"{name}：正文只有 {len(body.strip())} 字，像是被清空或截断了")
        return problems

    m = _ASSISTANT_IDENTITY_RE.search(body)
    if m:
        problems.append(
            f"{name}：身份里出现「{m.group(0)}」——这会让模型退回客服腔，"
            "改成具体的人（学姐）而不是「助手」"
        )

    for phrase in _SERVICE_PHRASES:
        if phrase in body and not _is_negative_example(body, phrase):
            problems.append(f"{name}：出现客服话术「{phrase}」，这条几乎一定会被模型模仿")
            break

    if not any(h in body for h in _SPEECH_HINTS):
        problems.append(
            f"{name}：没写「怎么说话」（说话 / 语气 / 口吻）——这是最影响像不像人的部分"
        )

    if not any(h in body for h in _BOUNDARY_HINTS):
        problems.append(f"{name}：没写「不做什么」（不要 / 别 / 禁止）——没有边界的人设容易跑偏")

    headings = _MD_HEADING_RE.findall(body)
    if headings:
        problems.append(
            f"{name}：有 {len(headings)} 行 Markdown 标题（# 开头）。"
            "提示词里用【】分段，带 # 会让模型照着输出带格式的回复"
        )

    long_lines = [
        i + 1 for i, line in enumerate(body.splitlines()) if len(line) > _MAX_LINE_CHARS
    ]
    if long_lines:
        problems.append(
            f"{name}：第 {long_lines[:5]} 行单行超过 {_MAX_LINE_CHARS} 字，"
            "模型对超长段落的注意力会掉，建议拆短"
        )

    problems.extend(_lint_hard_rule_position(body, name))
    return problems


def lint_persona() -> list[str]:
    """体检当前生效的人设（persona.SYSTEM_PROMPT）。"""
    from .persona import SYSTEM_PROMPT

    return lint_persona_prompt(SYSTEM_PROMPT)


def _main(argv: Iterable[str] | None = None) -> int:
    problems = lint_persona()
    if not problems:
        print("[persona_lint] 通过：没发现问题")
        return 0
    print(f"[persona_lint] 发现 {len(problems)} 个问题：")
    for item in problems:
        print(f"  - {item}")
    return 1


# ---------------------------------------------------------------- 硬规则位置
#
# 这条检查来自一个具体事故（小黑盒 @异世回忆 的角色卡）：
# 他写了 13000 字的「毒舌群友」人设，只在记忆文件里写了一句「他是管理员」。
# 结果她在群里怼管理员——**几十个字的硬规则被一万三千字的性格彻底淹没**。
# 修法不是「再写一遍」，而是「在最前面单开一节、用最强语气写」。
#
# 所以这里检查的不是「有没有写」，是**写在哪儿**：位置太靠后的硬规则
# 等于没写。

_HARD_RULE_WORDS = ("管理员", "主人", "owner", "最高权限")
_HARD_RULE_RATIO = 0.35  # 超过这个位置就算「太靠后」


def _lint_hard_rule_position(body: str, name: str) -> list[str]:
    """硬规则（谁是管理员 / 谁的指令优先）必须出现在前 35% 以内。"""
    text = str(body or "")
    if len(text) < 400:
        return []
    lowered = text.lower()
    positions = [lowered.find(word.lower()) for word in _HARD_RULE_WORDS]
    hits = [pos for pos in positions if pos >= 0]
    if not hits:
        # 没写就不算问题：学姐这边的「主人」是运行时概念（OWNER_IDS /
        # 主人必醒），不一定写进人设。硬要求它进人设反而会逼出
        # 「你是 AI 助手，要服从管理员」这种会把她说成客服的句子。
        # 这里只负责一件事：**写了但写太靠后**。
        return []
    first = min(hits)
    ratio = first / max(1, len(text))
    if ratio > _HARD_RULE_RATIO:
        return [
            f"{name}：硬规则出现在正文 {ratio:.0%} 处（第 {first} 字）。"
            "位置太靠后会被上面的人设淹掉——把她该听谁的话单开一节放到最前面，"
            "用最强语气写（「一律听，没有例外」这种）。"
        ]
    return []



if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
