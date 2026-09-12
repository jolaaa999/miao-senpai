from __future__ import annotations

import random

from .style import polish_reply

WELCOME_TEMPLATES = (
    "{name} 欢迎来到百度迎新群～学姐接收你的投递啦。请在十分钟内 @学姐 发「认证」，超时会被请出群哦。",
    "{name} 新同学入群成功～请在十分钟内 @学姐 发送「认证」完成报到，超时会踢出，有坑再喊我保修～",
    "{name} 来啦！十分钟内先 @学姐 回一句「认证」就行，超时请出群，认证完再吐槽职场和算法题～",
)


def build_welcome_text(nickname: str, *, rng: random.Random | None = None) -> str:
    name = (nickname or "新同学").strip() or "新同学"
    tmpl = (rng or random).choice(WELCOME_TEMPLATES)
    return polish_reply(tmpl.format(name=name))
