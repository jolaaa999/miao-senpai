"""会话记忆关键词召回（轻量，无 embedding）。"""

from __future__ import annotations

import re
from typing import Any

_CN_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z0-9_]{3,}")
_STOP = frozenset(
    {
        "这个",
        "那个",
        "什么",
        "怎么",
        "不是",
        "就是",
        "我们",
        "你们",
        "可以",
        "因为",
        "所以",
        "但是",
        "然后",
        "如果",
        "已经",
        "真的",
        "感觉",
        "知道",
        "觉得",
        "现在",
        "学姐",
        "哈哈",
        "谢谢",
        "记得",
        "还记",
    }
)


def _ngrams(s: str, n: int) -> list[str]:
    if len(s) < n:
        return []
    return [s[i : i + n] for i in range(len(s) - n + 1)]


def extract_keywords(text: str) -> set[str]:
    text = (text or "").strip().lower()
    if not text:
        return set()
    out: set[str] = set()
    for m in _LATIN.finditer(text):
        tok = m.group(0)
        if tok not in _STOP and not tok.isdigit():
            out.add(tok)
    for m in _CN_RUN.finditer(text):
        run = m.group(0)
        if len(run) <= 4:
            if len(run) >= 2 and run not in _STOP:
                out.add(run)
            continue
        for n in (2, 3, 4):
            for g in _ngrams(run, n):
                if g not in _STOP:
                    out.add(g)
    return out


def score_text(text: str, keywords: set[str]) -> float:
    if not keywords or not text:
        return 0.0
    lower = text.lower()
    hits = sum(1 for k in keywords if k in lower)
    if hits <= 0:
        return 0.0
    # 短句命中更「专」
    length_penalty = 1.0 / (1.0 + len(text) / 400.0)
    return hits * (1.0 + 0.15 * hits) * length_penalty


# 召回时每条只留这么多字：够认出「聊过什么」，不够拿来复读。
# 心情滤镜对「情绪对得上」的加权：+20%。
# 为什么不是 +100%：那样只要有情绪就把相关度扔了，会先想起一件毫不相干但同情绪的旧事。
_MOOD_BOOST = 1.2

_RECALL_PREVIEW_CHARS = 14


def _blur(snippet: str, keep: int = _RECALL_PREVIEW_CHARS) -> str:
    """把一句旧话模糊成「只有个印象」的样子。

    为什么是截断而不是摘要：摘要要调模型（每轮一次，慢且贵），
    而这里只需要一个能触发回忆的钩子。开头 14 个字通常就够认出话题，
    剩下的细节让她自己问，也顺便治了「照搬旧话」。
    """
    text = (snippet or "").strip()
    if len(text) <= keep:
        return text
    return text[:keep].rstrip("，,、；;：: ") + "…"


def select_recent_and_recall(
    history: list[dict[str, str]],
    query: str,
    *,
    recent_turns: int = 24,
    recall_top: int = 4,
    mood_word: str = "",
) -> tuple[list[dict[str, str]], str]:
    """返回 (近期 history 给 messages, 召回 brief)。

    history 是 user/assistant 交替消息列表（不含当前句）。
    recent_turns 按「消息条数」计（不是对话轮数对）。
    """
    if not history:
        return [], ""
    recent_n = max(2, int(recent_turns))
    recall_n = max(0, int(recall_top))
    if len(history) <= recent_n or recall_n <= 0:
        return history[-recent_n:] if len(history) > recent_n else list(history), ""

    recent = history[-recent_n:]
    older = history[:-recent_n]
    keywords = extract_keywords(query)
    # 心情滤镜（2026-10-26）：她现在什么心情，就更容易想起哪一类事。
    # 实现上是「同极性 +20% 加成」，不是单纯次级键——实测关键词分几乎不会相等
    # （同样命中「德育线」，4.183 和 4.213 就已经分好先后了），只做次级键等于
    # 什么都不做，那就成假功能了。+20% 的尺度是：情绪对得上的能压过微小的
    # 分数差，但压不过明显更相关的那条，也不会淘汰任何命中。
    polarity = 0
    if mood_word:
        try:
            from .mood_filter import mood_polarity

            polarity = mood_polarity(mood_word)
        except Exception:  # noqa: BLE001 — 滤镜坏了就当没有
            polarity = 0
    if not keywords:
        return recent, ""

    # 按「一对」或单条打分，优先 assistant+前面的 user
    scored: list[tuple[float, int, int, str]] = []
    # 被心情加成抬过分的候选数：用来判断 +20% 到底有没有起作用
    boosted = 0
    for i, msg in enumerate(older):
        content = str(msg.get("content") or "")
        role = str(msg.get("role") or "")
        sc = score_text(content, keywords)
        if sc <= 0:
            continue
        # 只引对方说过的话，不引她自己的旧回复。
        #
        # 为什么比以前更狠（原来只是过一遍 AI 味过滤）：memory 是追加的，
        # 早期条目全是旧人设时期的产物——「呜……才不是猫娘啦！耳朵不是猫耳，
        # 是狐狸的」这种。当成「回忆」喂回去，模型会以为这是自己现在的口吻，
        # 线上表现就是「之前的聊天记录又记得，又很混乱」。
        # 话题本来就在对方的话里，只引对方那句，信息一点不少，污染全没了。
        if role != "user":
            continue
        snippet = content.strip().replace("\n", " ")
        # 只留开头一小段，故意给个模糊的印象（2026-10-25 改）。
        #
        # 以前是原文最多 120 字整条塞进去，等于把当事人的原话又念了一遍。
        # 人回忆旧事是毛边的：「他问过德育线怎么算」——不会复述完整句子。
        # 全文回放有两个坏处：一是她会照着原话拼接（复读），二是旧话题的
        # 细节跟眼前这句混在一起，看着就「又记得又混乱」。
        snippet = _blur(snippet)
        mismatch = 0
        if polarity:
            try:
                from .mood_filter import text_polarity

                mismatch = 0 if text_polarity(content) == polarity else 1
            except Exception:  # noqa: BLE001
                mismatch = 0
        # 情绪对得上的那条给 +20%，剩下的仍按原分排序
        if polarity and mismatch == 0:
            adjusted = sc * _MOOD_BOOST
            boosted += 1
        else:
            adjusted = sc
        scored.append((adjusted, mismatch, i, snippet))

    # 排序三段：加成后的分 → 情绪是否对得上 → 越新越前
    scored.sort(key=lambda x: (-x[0], x[1], -x[2]))
    picked: list[str] = []
    seen: set[str] = set()
    for _, _, _, line in scored:
        if line in seen:
            continue
        seen.add(line)
        picked.append(line)
        if len(picked) >= recall_n:
            break
    if mood_word:
        # 打点：只记数量与极性，不记内容。
        # **polarity=0 也要记**：那是「有心情词但词表认不出」——滤镜静默失效的
        # 最典型形态。只记非零极性的话，这种失败在日志里根本看不见。
        # boosted 恒为 0 = 加成没起作用（词表或 +20% 的尺度要调）。
        try:
            from .observability.logging import log_event

            log_event(
                "memory.mood_filter",
                polarity=polarity,
                candidates=len(scored),
                boosted=boosted,
                picked=len(picked),
            )
        except Exception:  # noqa: BLE001 — 观测失败不能影响对话
            pass

    if not picked:
        return recent, ""
    # 措辞刻意把「更早」「可能无关」写进去：这些条目按关键词命中捞上来的，
    # 常常是几天前的旧话题。不提示的话她会把旧事和眼前这句混在一起答。
    brief = (
        "【更早的对话·只剩个大概】（模糊印象，不是原话；多半跟现在没关系，别复读、别硬接，"
        "要细节就去问他）\n" + "\n".join(f"- {p}" for p in picked)
    )
    return recent, brief


def rank_person_items(
    items: list[str],
    query: str,
    *,
    keep: int,
) -> list[str]:
    """按当前句关键词重排 traits/moments，无命中则保持原序截断。"""
    if not items:
        return []
    keep = max(1, int(keep))
    keywords = extract_keywords(query)
    if not keywords:
        return items[:keep]
    scored = [(score_text(it, keywords), idx, it) for idx, it in enumerate(items)]
    if all(s <= 0 for s, _, _ in scored):
        return items[:keep]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [it for _, _, it in scored[:keep]]


def trim_by_char_budget(
    messages: list[dict[str, str]],
    max_chars: int,
    *,
    min_keep: int = 2,
) -> list[dict[str, str]]:
    """从最新往回累加字符，超预算就丢掉最老的几条。

    为什么只限条数不够：memory_recent_turns 管的是**条数**，
    但一条能有多长完全没有上界——有人贴一段报错日志或转一段聊天记录，
    一句就上万字，24 条照样能把上下文塞满。条数和字符两道都得有。

    min_keep 保证不会「刚说完就忘」：即使单条超长，最后这一轮也照留。
    默认只保 2 条（一问一答）而不是更多——保太多，小预算就等于失效。
    """
    if max_chars <= 0 or not messages:
        return messages

    total = 0
    kept = 0
    for msg in reversed(messages):
        cost = len(str(msg.get("content") or ""))
        if kept >= min_keep and total + cost > max_chars:
            break
        total += cost
        kept += 1
    if kept >= len(messages):
        return messages
    return messages[-kept:]


def _is_ai_flavored(text: str) -> bool:
    """这条旧回复是不是「机器味」样本。命中就不该拿去当回忆。"""
    try:
        from .humanize import ai_flavor_score
    except Exception:  # noqa: BLE001 — 过滤失效最多是多带两条旧话
        return False
    score, _labels = ai_flavor_score(text)
    return score >= 1


def limit_assistant_turns(
    messages: list[dict[str, str]], max_assistant: int
) -> list[dict[str, str]]:
    """只保留最近 N 条她自己的回复，更早的整段丢掉。

    为什么要砍：memory 是 append-only 的，越靠前的内容越老，往往是旧人设
    留下的（猫娘腔、「（尾巴一盘）」这类扮演、判词腔）。把它们作为 messages
    发给模型，等于给模型演示「你平时就这么说话」，越聊越偏。
    砍的时候按**成对**丢：切点之后如果第一条是 assistant，往后挪到 user，
    否则模型会收到一条没有上文的孤立回复。
    """
    if max_assistant <= 0 or not messages:
        return messages
    count = 0
    cut = 0
    for i in range(len(messages) - 1, -1, -1):
        if str(messages[i].get("role")) == "assistant":
            count += 1
            if count > max_assistant:
                cut = i + 1
                break
    out = list(messages[cut:])
    while out and str(out[0].get("role")) != "user":
        out.pop(0)
    return out


def _as_message(item: dict[str, Any]) -> dict[str, Any]:
    """转成 OpenAI messages 形状，但把 ts 一起带上。

    为什么带 ts：调用方要判断「这条是不是昨天的」，才能决定要不要加时间前缀。
    不带的话，隔夜请求和刚刚说的话在模型眼里完全没有区别（2026-10-28 的
    「你这BOT怎么还在昨天的话题」就是这么来的）。落盘前上层会剥掉它。
    """
    msg: dict[str, Any] = {"role": item["role"], "content": item["content"]}
    ts = item.get("ts")
    if isinstance(ts, (int, float)) and ts > 0:
        msg["ts"] = float(ts)
    return msg


def messages_for_llm_with_recall(
    history: list[dict[str, str]],
    query: str,
    *,
    recent_turns: int,
    recall_top: int,
    enable: bool,
    max_chars: int = 0,
    max_assistant_turns: int = 0,
    mood_word: str = "",
) -> tuple[list[dict[str, Any]], str]:
    if not enable:
        kept = limit_assistant_turns(
            trim_by_char_budget(history, max_chars), max_assistant_turns
        )
        return [_as_message(m) for m in kept], ""
    recent, brief = select_recent_and_recall(
        history,
        query,
        recent_turns=recent_turns,
        recall_top=recall_top,
        mood_word=mood_word,
    )
    recent = limit_assistant_turns(
        trim_by_char_budget(recent, max_chars), max_assistant_turns
    )
    return [_as_message(m) for m in recent], brief
