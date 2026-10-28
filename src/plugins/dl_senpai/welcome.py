from __future__ import annotations

import random

from .style import polish_reply

# 只欢迎：一个字都不提认证。
# 为什么需要这一套：认证话术承诺「十分钟内不认证就踢出」，但学姐不是群管时，
# 既踢不了人，也接不住「认证」两个字（命令 guard 要求该用户在待认证名单里），
# 写了就是空承诺——实测新人照着 @她 发「认证」，她只能现场瞎编（"只认画图订单"）。
WELCOME_TEMPLATES = (
    "{name} 欢迎来到百度迎新群～学姐接收你的投递啦，随便坐，有坑喊学姐～",
    "{name} 新同学入群成功～先随意逛逛，想吐槽职场和算法题都可以 @学姐～",
    "{name} 来啦！欢迎欢迎，有问题直接 @学姐 就好～",
)

# 带认证话术：只有学姐确实是群管、且该群认证流程开着时才发（能登记、能倒计时、能踢）。
WELCOME_TEMPLATES_VERIFY = (
    "{name} 欢迎来到百度迎新群～学姐接收你的投递啦。请在十分钟内 @学姐 发「认证」，超时会被请出群哦。",
    "{name} 新同学入群成功～请在十分钟内 @学姐 发送「认证」完成报到，超时会踢出，有坑再喊我保修～",
    "{name} 来啦！十分钟内先 @学姐 回一句「认证」就行，超时请出群，认证完再吐槽职场和算法题～",
)


def build_welcome_text(
    nickname: str,
    *,
    need_verify: bool = False,
    rng: random.Random | None = None,
) -> str:
    """拼一句入群欢迎。

    need_verify 决定要不要带上「十分钟内发「认证」」这套话术。
    默认 False（只欢迎）：只有"能踢人 + 认证流程真的开着"时那句话才成立，
    判断在调用方（handlers.handle_group_increase），这里只负责挑模板。
    """
    name = (nickname or "新同学").strip() or "新同学"
    pool = WELCOME_TEMPLATES_VERIFY if need_verify else WELCOME_TEMPLATES
    tmpl = (rng or random).choice(pool)
    return polish_reply(tmpl.format(name=name))
