"""统一「发言人 + 正文」的拼法。

为什么单独一个函数：这句话同时出现在三个地方——线上主链路（llm.py）、
补救重试的历史（agent/recovery.py）、写记忆/历史的格式化（vision.py）。
以前三处各写一遍 f"{sender}：{text}"，改一处漏一处就等于没改。

为什么是「[名字] 说：内容」而不是「名字：内容」：
名字可能带标点、也可能本身就是常用词（「失语.」「小橙子」）。
旧格式下模型会把「失语.：」当成正文开头，实测把它拆成
「「失语.」是个名字？还是在说你自己无语了？」——因为「失语」是个常用词。
方括号 + 「说：」让标签在视觉上和句子分开，和正文不像同一种东西。
"""

from __future__ import annotations

__all__ = ["format_user_turn"]


def format_user_turn(sender: str, text: str) -> str:
    """把「谁 + 说了什么」拼成模型看到的那一行。"""
    who = (sender or "").strip() or "对方"
    body = (text or "").strip()
    if not body:
        return f"[{who}] 说：（空消息）"
    return f"[{who}] 说：{body}"
