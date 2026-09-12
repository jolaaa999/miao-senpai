"""QQ 空间评论：按动态内容生成短评（LLM），失败则内容感知回退。"""

from __future__ import annotations

import random
import re
from typing import TYPE_CHECKING

from nonebot import logger
from openai import AsyncOpenAI

if TYPE_CHECKING:
    from .config import PluginConfig

_SYSTEM = """你是群里的「深度学习学姐」，在好友 QQ 空间下自然评论。
要求：
1. 必须紧扣对方这条动态的内容或氛围，禁止空泛套话（如单独的「好看」「冲」「不错哦」）
2. 像真人随口回一句，口语、轻松，可带轻微玩笑，不要煽情鸡汤
3. 只输出评论正文本身：不要引号、不要前缀、不要表情包代码、不要话题标签堆砌
4. 长度控制在 8～36 个汉字（或同等信息量），一句话即可
5. 若动态几乎没文字（可能是纯图），就根据「发了张图/日常分享」自然接一句，仍要具体一点
6. 不要自称 AI，不要暴露机器人"""


def _fallback_comment(summary: str, nickname: str) -> str:
    """无 LLM 或失败时：按关键词选更贴一点的句子，再掺一点随机。"""
    text = (summary or "").strip()
    nick = (nickname or "").strip() or "你"
    low = text.lower()

    pools: list[str]
    if not text or len(text) < 2:
        pools = [
            f"{nick}这张图氛围感可以啊",
            "这波日常记录得挺有意思",
            "路过瞄了一眼，感觉今天心情不错？",
            "哈哈哈这个瞬间挺好玩的",
        ]
    elif any(k in text for k in ("吃", "餐", "火锅", "奶茶", "咖啡", "美食", "外卖", "蛋糕")):
        pools = [
            "看着就饿了，下次带学姐一个呗",
            "这顿选得可以啊，好吃吗",
            "美食博主本人了属于是",
            f"哟，{nick}又在好好吃饭，支持",
        ]
    elif any(k in text for k in ("旅行", "旅游", "景点", "打卡", "飞机", "高铁", "出差", "风景")):
        pools = [
            "风景真不错，羡慕能出门晃悠",
            "这站打卡很出片啊",
            "路上注意安全，回来开分享会哈",
            "看完想请假了……",
        ]
    elif any(k in text for k in ("考试", "作业", "论文", "ddl", "DDL", "复习", "开学", "课")):
        pools = [
            "加油，学姐远程给你加个 buff",
            "ddl 战士……记得喝水休息会",
            "看完更焦虑了，但还是支持你冲",
            "写完记得来群里报喜啊",
        ]
    elif any(k in text for k in ("猫", "狗", "宠物", "喵", "汪")):
        pools = [
            "好家伙，被毛孩子可爱到了",
            "请立刻上交更多毛孩子照片",
            "这小家伙镜头感绝了",
        ]
    elif any(k in text for k in ("难过", "累", "崩溃", "emo", "郁闷", "失眠")):
        pools = [
            "抱抱，先休息一下别硬撑",
            "听起来挺累的，有需要群里喊一声",
            "辛苦了，今晚早点睡吧",
        ]
    elif any(k in text for k in ("生日", "快乐", "恭喜", "录用", "上岸", "毕业")):
        pools = [
            "恭喜恭喜！！太牛了",
            "好消息就该放大声说，冲啊",
            f"替{nick}开心到起飞",
        ]
    elif "http" in low or "www." in low:
        pools = [
            "链接我先存着，回头细看",
            "这个分享及时，谢了",
            "有点意思，我去围观一下",
        ]
    else:
        # 抽取动态里几个字做挂钩，避免完全套话
        hook = re.sub(r"\s+", "", text)[:10]
        pools = [
            f"关于「{hook}」这点我有同感",
            f"看到你写{hook}，莫名被戳到了",
            "这条挺真实的，学姐认真看完了",
            f"{nick}今天状态不错嘛，继续保持",
            "哈哈哈行，这动态我给过",
        ]
    return random.choice(pools)


def _clean_comment(raw: str) -> str:
    s = (raw or "").strip()
    s = s.strip("「」『』\"'“”‘’")
    s = re.sub(r"^(评论[:：]|回复[:：]|学姐[:：])\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    # 太长截断到标点
    if len(s) > 40:
        cut = s[:40]
        for sep in ("。", "！", "？", "…", "~", "～", "!", "?"):
            i = cut.rfind(sep)
            if i >= 8:
                cut = cut[: i + 1]
                break
        s = cut
    return s


def _too_generic(s: str) -> bool:
    bad = {"好看", "好看！", "冲", "冲！", "不错哦", "不错", "支持", "赞", "666", "牛"}
    return s.strip() in bad or len(s.strip()) < 4


async def generate_qzone_comment(
    config: "PluginConfig",
    *,
    summary: str,
    nickname: str = "",
) -> str:
    """生成一条贴合动态的评论；失败则回退。"""
    use_llm = bool(getattr(config, "qzone_comment_llm", True))
    if use_llm and config.api_configured():
        try:
            text = await _llm_comment(config, summary=summary, nickname=nickname)
            text = _clean_comment(text)
            if text and not _too_generic(text):
                return text
            logger.warning(f"dl_senpai qzone comment llm too generic: {text!r}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"dl_senpai qzone comment llm failed: {exc}")
    return _fallback_comment(summary, nickname)


async def _llm_comment(
    config: "PluginConfig", *, summary: str, nickname: str
) -> str:
    client = AsyncOpenAI(
        api_key=config.active_api_key(),
        base_url=config.active_base_url(),
        timeout=float(getattr(config, "timeout", 60) or 60),
    )
    body = (summary or "").strip() or "（对方发了几乎没文字的动态，可能是图片/心情）"
    who = (nickname or "").strip() or "好友"
    user = (
        f"好友昵称：{who}\n"
        f"动态内容：{body[:200]}\n"
        "请写一条空间评论："
    )
    resp = await client.chat.completions.create(
        model=config.active_model(),
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=min(1.2, float(getattr(config, "temperature", 1.0) or 1.0) + 0.15),
        max_tokens=80,
    )
    return (resp.choices[0].message.content or "").strip()
