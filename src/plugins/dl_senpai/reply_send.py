from __future__ import annotations

import time

import asyncio
import random
import re
from datetime import datetime
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11 import Message as OBMessage

from .persona import get_senpai_name

_PARA_SPLIT = re.compile(r"\n{2,}")
_LINE_SPLIT = re.compile(r"(?<=\n)")
_SENT_SPLIT = re.compile(r"(?<=[。！？.!?\n])")
_BUBBLE_SENT = re.compile(r"(?<=[。！？!?～~…])")


# 合并转发的切点优先级：小标题行 > 空行 > 列表项 > 句子。
# 按字数硬切会把「一、结论」和它的正文劈到两个节点里，读的人还得自己拼回去；
# QQ 的聊天记录卡片又不能展开全文，切碎了就是真的丢了上下文。
_HEADING_LINE_RE = re.compile(
    r"^(?:[一二三四五六七八九十]+[、.．]|\d+[.)、]|[·•▪‣]\s|[-*]\s+"
    r"|[（(][一二三四五六七八九十\d]+[)）]|【[^】]{1,20}】)"
)
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")
# 列表成员不触发切分：连续几条 · 本来就是同一个列表，
# 拆成独立块再拼回去，只会在每条之间多出一行空行。
_LIST_LINE_RE = re.compile(r"^(?:[·•▪‣]\s?|[-*]\s+|\d+[.)、]\s?)")


def _logical_blocks(text: str) -> list[str]:
    """把正文切成「语义完整」的块：先按空行分段，段内遇到小标题/列表项再切。"""
    blocks: list[str] = []
    for para in _PARA_SPLIT_RE.split(text):
        cur: list[str] = []
        for line in para.split("\n"):
            stripped = line.strip()
            # 只让「非列表」的小标题触发切分，列表项跟着上一行走
            if cur and not _LIST_LINE_RE.match(stripped) and _HEADING_LINE_RE.match(stripped):
                blocks.append("\n".join(cur))
                cur = [line]
            else:
                cur.append(line)
        if cur:
            blocks.append("\n".join(cur))
    return [b.strip("\n") for b in blocks if b.strip()]


def _split_long_block(block: str, limit: int) -> list[str]:
    """单块超限时按句子切；句子本身比上限还长就只能按字数硬切。"""
    pieces: list[str] = []
    cur = ""
    for sent in _SENT_SPLIT.split(block):
        if not sent:
            continue
        while len(sent) > limit:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(sent[:limit])
            sent = sent[limit:]
        if cur and len(cur) + len(sent) > limit:
            pieces.append(cur)
            cur = sent
        else:
            cur += sent
    if cur:
        pieces.append(cur)
    return [x.strip() for x in pieces if x.strip()]


def split_forward_nodes(
    text: str, *, target_chars: int = 600, max_nodes: int = 8
) -> list[str]:
    """把长回答切成合并转发的若干节点。

    每一节尽量是能单独读懂的单元（一个小标题带它的正文），而不是
    「第 600 个字到第 1200 个字」。target_chars 是上限，不是目标长度。
    """
    text = (text or "").strip()
    if not text:
        return []
    limit = max(200, int(target_chars))
    nodes: list[str] = []
    for block in _logical_blocks(text):
        pieces = _split_long_block(block, limit) if len(block) > limit else [block]
        for piece in pieces:
            if nodes and len(nodes[-1]) + 2 + len(piece) <= limit:
                nodes[-1] = nodes[-1] + "\n\n" + piece
            else:
                nodes.append(piece)
    # 尾节太短就并回上一节：只剩一行「出处」独占一个节点，
    # 点开卡片的人还得再翻一页才看见它，不如跟正文放一起。
    if len(nodes) > 1 and len(nodes[-1]) < max(120, limit // 4):
        tail = nodes.pop()
        nodes[-1] = nodes[-1] + "\n\n" + tail
    # 节点太多往后合并：十个节点翻起来比一大段还累，8 节够用了
    if len(nodes) > max_nodes:
        head = nodes[: max_nodes - 1]
        nodes = head + ["\n\n".join(nodes[max_nodes - 1 :])]
    return nodes


def split_reply_chunks(text: str, chunk_chars: int) -> list[str]:
    """把长回复拆成多段，优先按段落/行/句子切分。"""
    text = (text or "").strip()
    if not text:
        return []
    limit = max(80, chunk_chars)
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining.strip())
            break
        window = remaining[:limit]
        cut = _best_cut(window)
        if cut < max(40, limit // 4):
            cut = limit
        piece = remaining[:cut].rstrip()
        if piece:
            chunks.append(piece)
        remaining = remaining[cut:].lstrip()
    return [c for c in chunks if c]


def should_split_image_voice(
    image: MessageSegment | None,
    voice: MessageSegment | None,
) -> bool:
    """图片和语音同时存在时拆成独立消息，避免 NapCat 多附件上传失败。"""
    return image is not None and voice is not None


# 判断一段文本是不是「含代码」：代码跨气泡会断在半个表达式上，读的人接不起来。
_CODE_HINT_RE = re.compile(
    r"^\s*(#|\}|\{|for\b|while\b|if\b|else\b|return\b|int\b|void\b|char\b|float\b|"
    r"double\b|struct\b|def\b|class\b|import\b|from\b)|\s*[;{}]\s*$"
)


def _looks_like_code(text: str) -> bool:
    """多数行都像代码时返回 True（阈值取一半，避免散文里夹一行分号就误判）。"""
    lines = [line for line in (text or "").split("\n") if line.strip()]
    if len(lines) < 3:
        return False
    hits = sum(1 for line in lines if _CODE_HINT_RE.match(line))
    return hits >= 3 and hits * 2 >= len(lines)


# 模型偶尔把内部协议标记漏给用户（实测 <<<CODE 少写闭括号，整段 Python 发进群里）。
# 这里是发送前的最后一道闸：不管哪个调用方、哪条路径，最终文本都要过一遍。
# 只剥「<<< + 大写标记名」这种协议形态，正常的正文不受影响。
_RESIDUAL_BLOCK_RE = re.compile(
    r"<<<CODE[ \t]*[^>\n]*>{0,3}[ \t]*\n?(.*?)(?:\n?[ \t]*<<<END[ \t]*>{0,3}|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_RESIDUAL_INLINE_RE = re.compile(r"<<<[A-Z][A-Z0-9_]{1,20}(?:[ \t][^\n>]*)?>{0,3}")
_RESIDUAL_ORPHAN_RE = re.compile(r"^[ \t]*<<<.*$", re.MULTILINE)


def strip_residual_markers(text: str) -> str:
    """剥掉未解析的协议标记，避免内部指令原样外发。

    顺序有讲究：先删整块（连块内代码一起删），再删行内标记，最后删孤立残行。
    反过来的话，整块删除时块内的行已经被行内规则动过，位置就错位了。
    """
    out = text or ""
    if "<<<" not in out:
        return out
    out = _RESIDUAL_BLOCK_RE.sub("", out)
    out = _RESIDUAL_INLINE_RE.sub("", out)
    out = _RESIDUAL_ORPHAN_RE.sub("", out)
    # 删干净后常剩下两三行空行，收成一处，别让气泡里出现大段空白
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def _lint_cjk_spaces(text: str) -> str:
    """把中文之间的空格改成逗号（「好的 我看下」→「好的，我看下」）。

    为什么要出站再走一道：模型偶尔会把停顿打成空格。中文群里那样打字一眼
    就是排版，不是手打；而 qq-bridge 干脆把中文空格当成「分条信号」——
    这边不分条，直接改成逗号，读起来才是正常语气。
    英文单词/数字之间的空格一律不动（DeepSeek V3、3 kg 那种是必要的）。
    """
    if not text:
        return text
    try:
        from .config import get_config

        if not bool(getattr(get_config(), "cjk_space_fix_enable", True)):
            return text
    except Exception:  # noqa: BLE001 — 配置取不到就按默认开处理
        pass
    from .bubble_lint import fix_cjk_spaces

    return fix_cjk_spaces(text)


def _lint_bubbles(bubbles: list[str]) -> list[str]:
    """把「一句话被拆成两条」的气泡合回一条；开关关掉时原样返回。

    为什么必须在出站补一道：split_human_bubbles 是按标点和换行做的**机械**
    切分，它只看字符位置，不看语义，所以会切在「这是哪个番的」后面，下一条
    再接「表情包？」。半句话被拆成两个气泡，是 QQ 上最像机器人的形态之一。
    """
    if not bubbles:
        return bubbles
    max_chars = 160
    try:
        from .config import get_config

        cfg = get_config()
        if not bool(getattr(cfg, "bubble_lint_enable", True)):
            return bubbles
        max_chars = int(getattr(cfg, "bubble_merge_max_chars", 160) or 160)
    except Exception:  # noqa: BLE001
        pass
    from .bubble_lint import merge_incomplete_bubbles

    return merge_incomplete_bubbles(bubbles, max_chars=max_chars)


def split_human_bubbles(
    text: str,
    *,
    min_chars: int = 40,
    prefer_chars: int = 120,
    max_bubbles: int = 3,
    require_newline: bool = False,
) -> list[str]:
    """把闲聊短回复拆成多条 QQ 气泡（像真人连发）。

    太短不拆；超长交给合并转发路径，这里只处理「刚好能碎成 2～N 句」的情况。

    require_newline=True 时**只按换行拆**，没有换行就原样返回。
    为什么不按标点拆：拆泡的前提是「她本来就想分两句发」，那个意图只能由
    换行表达。按句号硬切等于替她决定怎么断句，切出来是段落的三块而不是
    三句话——线上被吐槽「一本正经重复废话」时，一部分就是这么来的。
    """
    text = (text or "").strip()
    if not text:
        return []
    max_bubbles = max(1, int(max_bubbles))
    # 只允许一条就别拆：下面的「从头合并」虽然也能还原成一条，但拼接会
    # 重排换行（\n\n 变 \n），等于悄悄改了原文格式。
    if max_bubbles <= 1:
        return [text] if text.strip() else []
    # 没有换行 = 她要一口气说完，那就别动
    if require_newline and "\n" not in text:
        return [text]
    min_chars = max(1, int(min_chars))
    prefer_chars = max(min_chars, int(prefer_chars))
    if len(text) < min_chars or max_bubbles <= 1:
        return [text]

    # 优先空行，其次单换行，再按句末
    # sep 必须跟着来源走：按换行切出来的片段要按换行拼回去，否则「正文紧跟代码」
    # 这种没有空行的回复会被粘成一整行（实测代码块全糊在一起，没法看也没法复制）。
    raw_parts: list[str] = []
    sep = ""
    if "\n\n" in text:
        raw_parts = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
        sep = "\n\n"
    elif "\n" in text:
        raw_parts = [p.strip() for p in text.split("\n") if p.strip()]
        sep = "\n"
    else:
        raw_parts = [p.strip() for p in _BUBBLE_SENT.split(text) if p.strip()]
        if len(raw_parts) <= 1:
            return [text]

    # 含代码就别拆气泡：拆点会落在代码中间，气泡分两条发出去更没法读。
    if _looks_like_code(text):
        return [text]

    # 换行分出来的行不要再合并：她已经用换行说了「这是另一条消息」。
    # 「草 / 真的假的 / 行」三条各 2~4 个字，下面的合并逻辑会按字数把它们
    # 粘成一条，等于白拆。
    if "\n" in text and len(raw_parts) <= max_bubbles:
        return [p for p in raw_parts if p]

    # 合并过碎片段，避免一字一气泡
    merged: list[str] = []
    buf = ""
    soft_limit = max(20, prefer_chars // 2)
    for part in raw_parts:
        if not buf:
            buf = part
            continue
        if len(buf) < soft_limit and len(buf) + len(part) <= prefer_chars:
            buf = f"{buf}{sep}{part}"
        else:
            merged.append(buf)
            buf = part
    if buf:
        merged.append(buf)

    if len(merged) <= 1:
        return [text]

    # 仍过多则从前合并，保留末尾情绪句
    while len(merged) > max_bubbles:
        merged[0] = merged[0] + sep + merged[1]
        del merged[1]
    return [m for m in merged if m]


def _best_cut(window: str) -> int:
    for pattern in (_PARA_SPLIT, _LINE_SPLIT, _SENT_SPLIT):
        matches = list(pattern.finditer(window))
        if matches:
            return matches[-1].end()
    space = window.rfind(" ")
    if space > len(window) // 3:
        return space + 1
    return len(window)


def _segment_file_hint(segment: MessageSegment) -> str:
    data = segment.data if isinstance(segment.data, dict) else {}
    hint = str(data.get("file") or data.get("url") or "").strip()
    if hint:
        return hint[:96]
    return segment.type


def _reply_media_summary(
    *,
    sticker: MessageSegment | None,
    voice: MessageSegment | None,
    image: MessageSegment | None,
    split_media: bool,
) -> str:
    parts: list[str] = []
    if sticker is not None:
        parts.append("sticker")
    if image is not None:
        parts.append("image" + ("(split)" if split_media else ""))
    if voice is not None:
        parts.append("voice" + ("(split)" if split_media else ""))
    return ",".join(parts) if parts else "text-only"


def build_forward_nodes(
    chunks: list[str],
    *,
    user_id: int,
    nickname: str | None = None,
    sticker: MessageSegment | None = None,
    image: MessageSegment | None = None,
) -> list[MessageSegment]:
    nickname = nickname or get_senpai_name()
    nodes: list[MessageSegment] = []
    for chunk in chunks:
        nodes.append(
            MessageSegment.node_custom(
                user_id=user_id,
                nickname=nickname,
                content=OBMessage(chunk),
            )
        )
    if image is not None:
        nodes.append(
            MessageSegment.node_custom(
                user_id=user_id,
                nickname=nickname,
                content=OBMessage(image),
            )
        )
    if sticker is not None:
        nodes.append(
            MessageSegment.node_custom(
                user_id=user_id,
                nickname=nickname,
                content=OBMessage(sticker),
            )
        )
    return nodes


def _instrument_send(
    *,
    text: str = "",
    success: bool = True,
    media_present: bool = False,
    media_failure: bool = False,
    fallback: bool = False,
    duration_ms: float = 0.0,
) -> None:
    """记录一次发送（Phase 7A.1 第三十六/三十八节）。

    **统一边界**：只有 reply_send 记。legacy / runtime / 其它调用方
    都不得各自再记一次，否则一次发送被算多遍（第三十七条）。

    第三十九节：一次 Agent response 可能因多气泡/碎句触发多次
    QQ send，因此这里每次调用记一次 send operation；response 与
    send operation 的区分由 telemetry 侧表达。

    失败静默：观测绝不能影响发送。
    """
    try:
        from .validation.telemetry import record_send

        record_send(
            success=success,
            operations=1,
            bubbles=1,
            media_failure=media_failure,
            fallback=fallback,
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001 — 观测失败不影响发送
        pass


async def _send_image_with_retry(
    bot: Bot,
    event: MessageEvent,
    image: MessageSegment,
) -> bool:
    hint = _segment_file_hint(image)
    from .send_guard import get_send_guard

    guard = get_send_guard()
    for attempt in range(1, 3):
        # 熔断打开时直接放弃：图片本来就有两次重试，叠加失败重试
        # 正是原项目踩过的「重试风暴」——在腾讯眼里这就是标准机器人。
        allowed, reason = guard.before_send()
        if not allowed:
            logger.warning(f"dl_senpai: image send skipped ({reason}) file={hint!r}")
            return False
        try:
            await bot.send(event, OBMessage(image))
            guard.on_success()
            logger.info(
                f"dl_senpai: reply image sent ok attempt={attempt}/2 file={hint!r}"
            )
            return True
        except Exception as exc:  # noqa: BLE001
            guard.on_failure()
            logger.warning(
                f"dl_senpai: reply image send failed attempt={attempt}/2 "
                f"file={hint!r}: {exc}"
            )
    logger.error(f"dl_senpai: reply image send give up file={hint!r}")
    return False


async def _send_voice_segment(
    bot: Bot,
    event: MessageEvent,
    voice: MessageSegment,
) -> bool:
    hint = _segment_file_hint(voice)
    from .send_guard import get_send_guard

    guard = get_send_guard()
    allowed, reason = guard.before_send()
    if not allowed:
        logger.warning(f"dl_senpai: voice send skipped ({reason}) file={hint!r}")
        return False
    try:
        await bot.send(event, OBMessage(voice))
        guard.on_success()
        logger.info(f"dl_senpai: reply voice sent ok file={hint!r}")
        return True
    except Exception as exc:  # noqa: BLE001
        guard.on_failure()
        logger.warning(f"dl_senpai: reply voice send failed file={hint!r}: {exc}")
        return False


def _dry_run_enabled() -> bool:
    """干跑模式是否开启。读不到配置就当没开（别因为配置异常把消息吞掉）。"""
    try:
        from .config_runtime.runtime import get_config

        return bool(getattr(get_config(), "dry_run", False))
    except Exception:  # noqa: BLE001 — 配置读不出来时宁可正常发，也不能静默吞消息
        return False


def _log_dry_run(
    text: str,
    *,
    sticker: MessageSegment | None = None,
    voice: MessageSegment | None = None,
    image: MessageSegment | None = None,
) -> None:
    """干跑：把她「本来会发出去」的东西写成一行日志。

    带字数，方便直接判断长度改得对不对——这正是要用干跑的原因。
    """
    media = []
    if image is not None:
        media.append("图片")
    if voice is not None:
        media.append("语音")
    if sticker is not None:
        media.append("表情")
    suffix = f" media={'+'.join(media)}" if media else ""
    logger.info(f"[干跑] 会说（{len(text)} 字）：{text}{suffix}")


async def send_text_reply(
    bot: Bot,
    event: MessageEvent,
    text: str,
    *,
    sticker: MessageSegment | None = None,
    voice: MessageSegment | None = None,
    image: MessageSegment | None = None,
    reply_max_chars: int,
    reply_chunk_chars: int,
    nickname: str | None = None,
    humanize_send: bool = False,
    typing_delay_ms_min: int = 400,
    typing_delay_ms_max: int = 1200,
    bubble_max: int = 3,
    bubble_min_chars: int = 40,
    bubble_prefer_chars: int = 120,
    bubble_total_max_chars: int = 700,
    bubble_require_newline: bool = False,
    forward_min_chars: int = 400,
    forward_node_chars: int = 600,
) -> None:
    nickname = nickname or get_senpai_name()
    """短消息直发；超长则用合并转发（聊天记录）多段发送。

    开启 humanize_send 时，中等长度闲聊会拆成多气泡并带打字延迟。
    图片与语音同时存在时拆成独立消息；图片发送失败会单独重试一次。
    正文带媒体发送失败时自动降级为纯文字，并尽量补发图片/语音。
    """
    # 先剥协议残留再判空：剥完可能就什么都不剩，那就该安静不发
    text = strip_residual_markers((text or "").strip())
    # 中文空格 → 逗号。放在判空之前：改写只删空格加标点，不会把正文改没。
    text = _lint_cjk_spaces(text)
    if not text and sticker is None and voice is None and image is None:
        return

    # 干跑：链路全跑完，只有「发出去」这一步换成打日志。
    # 挂在这里而不是更上游，是因为 runtime / legacy / 兜底文案最终都从
    # 这一个函数出站——拦一处就全拦住了，不用改三条链路。
    if _dry_run_enabled():
        _log_dry_run(text, sticker=sticker, voice=voice, image=image)
        return

    # Phase 7A.1：统一发送边界计时（第三十六条）。
    # 这是**唯一**统计点 —— legacy / runtime / reply_send 的调用方
    # 都不得各自再记一次，否则一次发送被算多遍（第三十七条）。
    _send_started = time.perf_counter()

    split_media = should_split_image_voice(image, voice)
    bundled_image = None if split_media else image
    bundled_voice = None if split_media else voice
    deferred_image = image if split_media else None
    deferred_voice = voice if split_media else None
    media_summary = _reply_media_summary(
        sticker=sticker,
        voice=voice,
        image=image,
        split_media=split_media,
    )

    try:
        await _send_text_reply_once(
            bot,
            event,
            text,
            sticker=sticker,
            voice=bundled_voice,
            image=bundled_image,
            reply_max_chars=reply_max_chars,
            reply_chunk_chars=reply_chunk_chars,
            nickname=nickname,
            humanize_send=humanize_send,
            typing_delay_ms_min=typing_delay_ms_min,
            typing_delay_ms_max=typing_delay_ms_max,
            bubble_max=bubble_max,
            bubble_min_chars=bubble_min_chars,
            bubble_prefer_chars=bubble_prefer_chars,
            bubble_total_max_chars=bubble_total_max_chars,
            bubble_require_newline=bubble_require_newline,
            forward_min_chars=forward_min_chars,
            forward_node_chars=forward_node_chars,
        )
        logger.info(f"dl_senpai: reply body sent ok media={media_summary}")
        _instrument_send(
            text=text, success=True,
            media_present=any(x is not None for x in (sticker, voice, image)),
            duration_ms=(time.perf_counter() - _send_started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        if sticker is None and bundled_image is None and bundled_voice is None:
            _instrument_send(
                text=text, success=False, duration_ms=(time.perf_counter() - _send_started) * 1000
            )
            raise
        logger.warning(
            f"dl_senpai: reply body send failed, fallback text-only "
            f"media={media_summary}: {exc}"
        )
        # 第四十一节：媒体失败 → 记 MEDIA_SEND_FAILURE；随后降级纯文字
        # → 再记 FALLBACK_TEXT。这样能算出真实媒体降级率。
        _instrument_send(
            text=text, success=False, media_failure=True,
            duration_ms=(time.perf_counter() - _send_started) * 1000,
        )
        await _send_text_reply_once(
            bot,
            event,
            text,
            sticker=None,
            voice=None,
            image=None,
            reply_max_chars=reply_max_chars,
            reply_chunk_chars=reply_chunk_chars,
            nickname=nickname,
            humanize_send=humanize_send,
            typing_delay_ms_min=typing_delay_ms_min,
            typing_delay_ms_max=typing_delay_ms_max,
            bubble_max=bubble_max,
            bubble_min_chars=bubble_min_chars,
            bubble_prefer_chars=bubble_prefer_chars,
            bubble_total_max_chars=bubble_total_max_chars,
            bubble_require_newline=bubble_require_newline,
            forward_min_chars=forward_min_chars,
            forward_node_chars=forward_node_chars,
        )
        logger.info("dl_senpai: reply text-only fallback sent ok")
        _instrument_send(text=text, success=True, fallback=True)
        if bundled_image is not None:
            await _send_image_with_retry(bot, event, bundled_image)
        if bundled_voice is not None:
            await _send_voice_segment(bot, event, bundled_voice)

    if deferred_image is not None:
        await _send_image_with_retry(bot, event, deferred_image)
    if deferred_voice is not None:
        await _send_voice_segment(bot, event, deferred_voice)


# 深夜打字更慢（搬自 ai-companion-kit 的 Humanizer）。
# 理由是真人凌晨半睡半醒，回得比白天慢一截；固定延迟反而显得「秒回」。
_NIGHT_HOURS = (2, 7)
_NIGHT_FACTOR = 1.6
# 条间停顿里偶尔来一次明显更长的：像走神、像被别的事打断。
# 每条都均匀停顿反而机械——真人发消息的节奏是不均匀的。
_LONG_GAP_CHANCE = 0.25
_LONG_GAP_EXTRA_MS = (1500, 4000)


def _humanize_pause_ms(
    ms_min: int,
    ms_max: int,
    *,
    humanize_extra: bool,
    chars: int = 0,
    ms_per_char: int = 0,
    hard_max: int = 0,
) -> int:
    """算出这一条该停多久。纯函数，方便单测钉住节奏规则。

    chars/ms_per_char 给定时按字数算（真人手打约 110ms/字），否则用固定区间。
    为什么要按字数：固定区间下，回 3 个字和回 30 个字等一样久——
    这本身就是机器味，真人短句几乎是秒回的。
    """
    lo = max(0, int(ms_min))
    hi = max(lo, int(ms_max))

    if chars > 0 and ms_per_char > 0:
        delay_ms = chars * int(ms_per_char)
        delay_ms = max(lo, delay_ms)
        if hard_max > 0:
            delay_ms = min(int(hard_max), delay_ms)
    else:
        delay_ms = random.randint(lo, hi) if hi > 0 else 0

    if not humanize_extra or delay_ms <= 0:
        return delay_ms

    hour = datetime.now().hour
    start, end = _NIGHT_HOURS
    if start <= hour < end:
        delay_ms = int(delay_ms * _NIGHT_FACTOR)

    if random.random() < _LONG_GAP_CHANCE:
        extra_lo, extra_hi = _LONG_GAP_EXTRA_MS
        delay_ms += random.randint(extra_lo, extra_hi)
    return delay_ms


async def _typing_pause(
    ms_min: int,
    ms_max: int,
    *,
    humanize_extra: bool = False,
    chars: int = 0,
    ms_per_char: int = 0,
    hard_max: int = 0,
) -> None:
    delay_ms = _humanize_pause_ms(
        ms_min,
        ms_max,
        humanize_extra=humanize_extra,
        chars=chars,
        ms_per_char=ms_per_char,
        hard_max=hard_max,
    )
    # 抖动放在这里而不是 _humanize_pause_ms 里：那个是纯函数，
    # 现有单测按确定值钉节奏规则，掺随机数会让它们变成薛定谔的测试。
    #
    # 为什么需要抖动：按字数线性算出来的间隔太规整，每条都差不多，
    # 一眼就是机器。真人手打有快有慢，同一句话两次能差一半。
    if delay_ms > 0:
        try:
            from .config import get_config

            ratio = float(getattr(get_config(), "typing_jitter_ratio", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            ratio = 0.0
        if ratio > 0:
            delay_ms = int(delay_ms * random.uniform(1.0 - ratio, 1.0 + ratio))
            delay_ms = max(30, delay_ms)
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000.0)


def _typing_profile() -> tuple[int, int]:
    """从配置取 (每字毫秒, 单条封顶毫秒)。取不到就退回固定区间语义。"""
    try:
        from .config import get_config

        cfg = get_config()
        return (
            int(getattr(cfg, "typing_ms_per_char", 0) or 0),
            int(getattr(cfg, "typing_ms_max", 0) or 0),
        )
    except Exception:  # noqa: BLE001
        return 0, 0


async def _human_pause(
    bot: Bot,
    event: MessageEvent,
    text: str,
    *,
    ms_min: int,
    ms_max: int,
    humanize_extra: bool = False,
) -> None:
    """「正在输入」+ 按字数等待 + 关状态。

    三件事绑在一起是因为它们必须成对出现：开了状态却忘了关，
    对方会一直看到「正在输入」，比不显示更假。
    """
    from .typing_status import get_typing_reporter

    reporter = get_typing_reporter()
    started = False
    try:
        started = await reporter.start(bot, event, text)
    except Exception:  # noqa: BLE001 — 上报失败绝不能影响发送
        started = False

    ms_per_char, hard_max = _typing_profile()
    try:
        await _typing_pause(
            ms_min,
            ms_max,
            humanize_extra=humanize_extra,
            chars=len(text or ""),
            ms_per_char=ms_per_char,
            hard_max=hard_max,
        )
    finally:
        if started:
            try:
                await reporter.stop(bot, event)
            except Exception:  # noqa: BLE001
                pass


async def _guarded_send(bot: Bot, event: MessageEvent, message: Any) -> bool:
    """带闸门的单条发送。

    闸门（熔断 / 每分钟上限）在 send 之前判断：拒绝时**连尝试都不发起**，
    这正是防风控的关键——原项目就是败在「失败了还一直重试」上。

    返回 False 表示没发出去（被闸门拦了或发送失败）。这里不抛异常：
    调用方多处在气泡循环里，抛出去会把后面的气泡一起打断。
    """
    from .send_guard import get_send_guard

    guard = get_send_guard()
    allowed, reason = guard.before_send()
    if not allowed:
        logger.warning(f"dl_senpai: send blocked ({reason})")
        # 拦下来的话要记账：她这一轮生成的内容没有到对方手上。
        # 不记的话她会以为自己说过了——原版最惨的一次是通道卡了 23 分钟，
        # 她在那边正常聊天，一条都没发出去，修好后只能说「你刚才那些话丢了」。
        _record_send_failure(reason, event)
        return False
    try:
        await bot.send(event, message)
    except Exception as exc:  # noqa: BLE001
        guard.on_failure()
        logger.warning(f"dl_senpai: send failed: {exc}")
        _record_send_failure(f"{type(exc).__name__}: {exc}", event)
        return False
    guard.on_success()
    _record_outgoing(message, event)
    return True


def _session_key_of(event: Any) -> str:
    """会话键，只用于给状态文件分类；拿不到就空串。"""
    try:
        group_id = getattr(event, "group_id", None)
        if group_id:
            return f"group:{group_id}"
        user_id = getattr(getattr(event, "user_id", None), "__str__", lambda: "")()
        return f"private:{user_id}" if user_id else ""
    except Exception:  # noqa: BLE001
        return ""


def _message_text_of(message: Any) -> str:
    """从 OneBot 消息里抠出纯文本，供「开场白去重」用。"""
    parts: list[str] = []
    for seg in message if isinstance(message, list) else []:
        try:
            if getattr(seg, "type", "") == "text":
                parts.append(str((seg.data or {}).get("text") or ""))
        except Exception:  # noqa: BLE001
            continue
    return "".join(parts).strip()


def _record_send_failure(reason: str, event: Any) -> None:
    try:
        from .context_extras import note_send_failure

        note_send_failure(reason, session_id=_session_key_of(event))
    except Exception as exc:  # noqa: BLE001 — 记账失败不能影响发送路径
        logger.debug(f"dl_senpai send failure note skipped: {exc}")


def _record_outgoing(message: Any, event: Any) -> None:
    try:
        from .context_extras import note_outgoing

        text = _message_text_of(message)
        if text:
            note_outgoing(text, session_id=_session_key_of(event))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"dl_senpai opener note skipped: {exc}")


async def _send_text_reply_once(
    bot: Bot,
    event: MessageEvent,
    text: str,
    *,
    sticker: MessageSegment | None,
    voice: MessageSegment | None,
    image: MessageSegment | None,
    reply_max_chars: int,
    reply_chunk_chars: int,
    nickname: str,
    humanize_send: bool = False,
    typing_delay_ms_min: int = 400,
    typing_delay_ms_max: int = 1200,
    bubble_max: int = 3,
    bubble_min_chars: int = 40,
    bubble_prefer_chars: int = 120,
    bubble_total_max_chars: int = 700,
    bubble_require_newline: bool = False,
    forward_min_chars: int = 400,
    forward_node_chars: int = 600,
) -> None:
    threshold = max(1, reply_max_chars)

    # 长文先走合并转发（聊天记录卡片）。
    # 放在拟人碎句之前判断：500~1500 字原本会被拆成一串气泡连发，群里直接刷屏；
    # 单条塞下去又长到没人愿意读。卡片折叠成一条，点开是按小标题分好的节。
    forward_min = max(0, int(forward_min_chars))
    if text and forward_min and len(text) > forward_min:
        nodes_text = split_forward_nodes(text, target_chars=forward_node_chars)
        # 400~600 这个区间按 600 切只会得到一节，那样卡片点开就一条，
        # 跟普通消息没区别。这里按字数对半再切一次，保证至少分两段。
        if len(nodes_text) == 1:
            half = max(200, min(forward_node_chars, len(text) // 2 + 20))
            nodes_text = split_forward_nodes(text, target_chars=half)
        if nodes_text:
            nodes = build_forward_nodes(
                nodes_text,
                user_id=int(bot.self_id),
                nickname=nickname,
                sticker=sticker,
                image=image,
            )
            try:
                await _send_forward(bot, event, nodes)
                if voice is not None:
                    await bot.send(event, OBMessage(voice))
                logger.info(
                    f"dl_senpai: forward reply {len(text)} chars -> "
                    f"{len(nodes_text)} node(s) "
                    f"(min={forward_min}, node_max={forward_node_chars})"
                )
                return
            except Exception as exc:  # noqa: BLE001
                # 卡片发不出去就逐条发，不能把回复弄丢
                logger.warning(f"dl_senpai: forward reply failed, fallback sequential: {exc}")
                for i, chunk in enumerate(nodes_text):
                    piece = OBMessage(chunk)
                    if sticker is not None and i == len(nodes_text) - 1:
                        piece = piece + OBMessage(sticker)
                    await _guarded_send(bot, event, piece)
                if image is not None:
                    await bot.send(event, OBMessage(image))
                if voice is not None:
                    await bot.send(event, OBMessage(voice))
                return

    # 拟人碎句：仅短于合并转发阈值的正文
    if (
        humanize_send
        and text
        and len(text) <= threshold
    ):
        total_chars = len(text)
        # 合计字数超阈值就整条单发，不再拆，也不转卡片。
        # 为什么不再转卡片（2026-10-25 改）：一条 200 字的正经答疑被塞进
        # 「聊天记录」卡片，群里看是突兀的；而它本来也不适合拆成 3 条。
        # 拆分只服务短句闲聊，长内容保持原样最不打扰。
        bubbles = (
            [text]
            if total_chars > bubble_total_max_chars
            else split_human_bubbles(
                text,
                min_chars=bubble_min_chars,
                prefer_chars=bubble_prefer_chars,
                max_bubbles=bubble_max,
                require_newline=bubble_require_newline,
            )
        )
        # 机械切分之后再补一道语义兜底：半句话被切到两条气泡里的，合回去。
        bubbles = _lint_bubbles(bubbles)
        if len(bubbles) >= 2:
            await _typing_pause(typing_delay_ms_min // 2, typing_delay_ms_min)
            sent = 0
            for i, chunk in enumerate(bubbles):
                # 真·正在输入 + 按字数等待；首条也走这里（她得「打」出来）
                await _human_pause(
                    bot,
                    event,
                    chunk,
                    ms_min=typing_delay_ms_min,
                    ms_max=typing_delay_ms_max,
                    humanize_extra=i > 0,
                )
                piece = OBMessage(chunk)
                last = i == len(bubbles) - 1
                if last and image is not None:
                    piece = piece + OBMessage(image)
                if last and sticker is not None:
                    piece = piece + OBMessage(sticker)
                if last and voice is not None:
                    piece = piece + OBMessage(voice)
                if await _guarded_send(bot, event, piece):
                    sent += 1
            logger.info(f"dl_senpai: humanize sent {sent}/{len(bubbles)} bubble(s)")
            return

    if not text or len(text) <= threshold:
        if humanize_send and text:
            await _human_pause(
                bot, event, text,
                ms_min=typing_delay_ms_min,
                ms_max=typing_delay_ms_max,
            )
        msg = OBMessage(text) if text else OBMessage()
        if image is not None:
            msg = msg + OBMessage(image)
        if sticker is not None:
            msg = msg + OBMessage(sticker)
        if voice is not None:
            msg = msg + OBMessage(voice)
        if not msg:
            return
        await _guarded_send(bot, event, msg)
        return

    chunks = split_reply_chunks(text, reply_chunk_chars)
    if len(chunks) <= 1:
        msg = OBMessage(text)
        if image is not None:
            msg = msg + OBMessage(image)
        if sticker is not None:
            msg = msg + OBMessage(sticker)
        if voice is not None:
            msg = msg + OBMessage(voice)
        await _guarded_send(bot, event, msg)
        return

    nodes = build_forward_nodes(
        chunks,
        user_id=int(bot.self_id),
        nickname=nickname,
        sticker=sticker,
        image=image,
    )
    try:
        await _send_forward(bot, event, nodes)
        if voice is not None:
            await bot.send(event, OBMessage(voice))
        logger.info(f"dl_senpai: sent forward reply with {len(nodes)} node(s)")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai: forward reply failed, fallback sequential: {exc}")
        for i, chunk in enumerate(chunks):
            piece = OBMessage(chunk)
            if sticker is not None and i == len(chunks) - 1:
                piece = piece + OBMessage(sticker)
            await _guarded_send(bot, event, piece)
        if voice is not None:
            await bot.send(event, OBMessage(voice))


async def _send_forward(bot: Bot, event: MessageEvent, nodes: list[MessageSegment]) -> Any:
    if isinstance(event, GroupMessageEvent):
        return await bot.send_group_forward_msg(group_id=event.group_id, messages=nodes)
    if isinstance(event, PrivateMessageEvent):
        return await bot.send_private_forward_msg(user_id=event.user_id, messages=nodes)
    # 兜底：当成普通消息拼接发送
    return await bot.send(event, OBMessage(nodes))
