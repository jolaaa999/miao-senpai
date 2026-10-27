"""生图参考图：派子 agent 去网上找，不用等对方发。

为什么需要这一层：
用户说「画一张凯尔希」时，没有参考图，模型只能凭记忆编设定——实测把猫耳
写成精灵耳、浅绿发写成白发。给一张官方立绘当图生图输入，比在提示词里写
十行设定有效得多；而让对方「先发张参考图给我」是把活推回给用户。

什么时候不找（例外）：
要画她自己（自拍 / 换装 / 看长相）时不找。她的外观在 _APPEARANCE_DEFAULT_PROMPT
里已经是精确描述，塞一张网图进去只会把脸换成别人、画风也一起漂。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from nonebot import logger

from .observability.logging import log_event
from .subagent import SubAgentSpec, run_llm_agent

# 参考图子 agent 的名字：日志里要能一眼看出这是哪类作业
REFERENCE_AGENT = "draw_reference"

# 自拍/换装类请求：不找参考图（她的外观有固定 prompt，见模块说明）
_SELF_DRAW_RE = re.compile(
    r"自拍|你长(什么样|啥样)|长什么样|照片|爆照|看看你|你的?样子|"
    r"穿(上|这|条|件|个)|换(衣服|装|一身)|给?你换|试试.{0,6}(衣|裙|jk|汉服|lo)"
)


def wants_self_portrait(user_request: str) -> bool:
    """这句是不是在让画「学姐自己」。

    为什么要单独判：这类请求的正确画法是固定 prompt，参考图会毁掉人设一致性。
    """
    return bool(_SELF_DRAW_RE.search(user_request or ""))


def build_reference_goal(
    *, prompt: str, user_request: str, character: str = ""
) -> str:
    """写清「找什么图」，越具体子 agent 越不会搜偏。"""
    if character:
        subject = f"《明日方舟》角色「{character}」的官方立绘 / 设定图"
        queries = f"{character} 官方立绘, {character} 明日方舟 立绘"
    else:
        brief = (prompt or user_request or "").strip().replace("\n", " ")[:60]
        subject = f"「{brief}」的参考图"
        queries = f"{brief} 参考图, {brief} 立绘"
    return (
        f"找一张可以当绘图参考的图片：{subject}\n"
        "要求：官方立绘或设定图优先；正面或全身、清晰、主体居中、不要水印、不要表情包、"
        "不要同人二创（会带偏画风）。\n"
        f"建议搜索词：{queries}\n"
        "做法：调一次 image_find（参数 query=搜索词）就能拿到一张校验合格的图，"
        "拿到就立刻收工，payload 给 "
        '{"url":"图片直链","source":"来源页"}。\n'
        "实在找不到就收工，answer 里说明没找到，payload 留空。"
    )


# 画图请求里的口水词：拼搜索词之前先剥掉。
# 「帮我画一张凯尔希的图片」→「凯尔希」，比整句丢给搜索靠谱得多。
_REQUEST_NOISE_RE = re.compile(
    r"(帮我|给我|帮忙|麻烦|请|能不能|可以|画一?[张个幅]?|生成|来一?[张个]|制作|"
    r"图片|插画|壁纸|头像|图|的|了|吧|呀|啊|嘛|好吗|行吗|谢谢)"
)


def _bare_subject(text: str) -> str:
    """把一句请求剥成能当搜索词的主体词。

    「画一只橘猫」剥完是「只橘猫」，量词得单独去掉——搜索词里多一个「只」
    会让搜到的图跑偏到「只」字相关的结果上。
    """
    cleaned = _REQUEST_NOISE_RE.sub(" ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"^(?:画|来|搞|弄|生成)?一?[只个张条幅份]", " ", cleaned).strip()
    return cleaned[:24]


def _direct_queries(*, prompt: str, user_request: str, character: str = "") -> list[str]:
    """先由代码拼几个搜索词。

    为什么不交给模型：它经常还没搜就说「我没有联网工具」，
    或者把「凯尔希 立绘」写成一整段设定描述。拼搜索词是机械活。
    """
    queries: list[str] = []
    if character:
        # 英文角色名（Eyjafjalla）走英文串：官方美术图在英文图站更全，
        # 中文串搜英文名基本搜不到东西。
        if all(ord(ch) < 128 for ch in character):
            queries += [
                f"{character} official art",
                f"{character} Arknights full body",
            ]
        else:
            queries += [
                f"{character} 官方立绘",
                f"{character} 立绘 png",
                f"{character} 设定图",
            ]
    subject = _bare_subject(user_request) or _bare_subject(prompt)
    if subject:
        queries += [f"{subject} 立绘", f"{subject} 官方图"]
    if prompt:
        queries.append(f"{_bare_subject(prompt)[:16]} 插画")
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out[:5]


async def _direct_search(
    config: Any,
    *,
    prompt: str,
    user_request: str,
    character: str = "",
    per_query_timeout: float | None = None,
    max_parallel: int = 3,
    download_budget: float = 45.0,
    return_candidate: bool = False,
) -> Any:
    """代码自己搜一轮，成功返回 (文件名, 字节)。

    为什么要这一层：2026-10-27 07:46 的日志里，子 agent 拿到工具清单后
    仍然回答「当前没有可用的 image_find 工具」，直接把参考图放弃了。
    而「拼搜索词 + 调一次 image_find + 下载校验」全是代码能干的事，
    不该押在模型肯不肯调工具上。子 agent 只在代码空手时出场。

    为什么必须并行：image_find 走的是抓页面那条路，实测单次 32~33 秒
    （2026-10-27 08:52 实测）。原来串行 + 每个只等 18 秒，等于每个词都超时，
    结果是「搜得到但永远拿不到」——线上表现为退化成纯文生图。现在改成
    前 3 个搜索词并行、单次给 35 秒，一轮 35 秒左右拿到候选。
    """
    import time as _time

    from .image_gen import _load_reference_image
    from .subagent import call_tool

    queries = _direct_queries(prompt=prompt, user_request=user_request, character=character)
    if not queries:
        return None
    timeout = float(
        per_query_timeout
        or getattr(config, "draw_reference_query_timeout", 35.0)
        or 35.0
    )

    async def _search(query: str) -> tuple[str, list[str]]:
        """单个搜索词：返回 (query, 候选 url 列表)。"""
        try:
            payload = await asyncio.wait_for(
                call_tool("image_find", {"query": query, "limit": 3}),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 — 一个词失败不该中断整轮
            logger.debug(f"dl_senpai draw reference direct query failed q={query}: {exc}")
            return query, []
        urls: list[str] = []
        head = str((payload or {}).get("url") or "").strip()
        if head:
            urls.append(head)
        # 首选 url 有时是列表页缩略图，多留一个候选做兜底
        for cand in (payload or {}).get("candidates") or []:
            if isinstance(cand, dict):
                cu = str(cand.get("url") or "").strip()
                if cu and cu not in urls:
                    urls.append(cu)
        return query, urls[:2]

    results = await asyncio.gather(
        *[_search(q) for q in queries[:max(1, max_parallel)]],
        return_exceptions=True,
    )
    pooled: list[tuple[str, str]] = []
    for item in results:
        if isinstance(item, tuple) and len(item) == 2:
            query, urls = item
            pooled.extend((query, url) for url in urls)
    if not pooled:
        return None

    # 下载要卡总预算：三个词六个候选依次试，最坏情况会拖到几分钟，
    # 而生图整体还有 draw_job_timeout 兜着，这里超了就直接放弃参考图。
    deadline = _time.monotonic() + download_budget
    for query, url in pooled:
        if _time.monotonic() >= deadline:
            logger.info("dl_senpai draw reference direct download budget exhausted")
            break
        loaded = await _load_reference_image(
            [url], timeout=max(20.0, float(getattr(config, "draw_timeout", 120.0)))
        )
        if loaded is None:
            continue
        log_event(
            "draw.reference_direct",
            query=query[:60],
            url=url[:200],
            bytes=len(loaded[1]),
        )
        if return_candidate:
            # 带上来源信息：调用方要按“官方/wiki/同人站”决定能不能进 images.edit
            from .character_references import candidate_from_url

            return candidate_from_url(url, loaded[0], loaded[1])
        return loaded
    return None


async def find_reference_image(
    config: Any,
    *,
    prompt: str,
    user_request: str,
    timeout: float | None = None,
    character: str = "",
    with_meta: bool = False,
) -> Any:
    """找一张参考图。with_meta=True 时返回 ReferenceCandidate（带来源域名/类型/可信度），
    否则返回旧的 (文件名, 字节) 二元组。

    为什么要来源信息：同样是「一张艾雅法拉」，官网立绘和 deviantart 同人图对身份
    的价值完全相反。只看图不看来源，就只能事后被视觉校验兜住，代价是白跑一遍 edit。
    """
    """跑一遍参考图子 agent，成功则返回 (文件名, 原始字节)。

    任何失败都返回 None：参考图只是增强项，不能因为它把整次生图拖垮——
    调用方会退回纯文生图。
    """
    from .draw_cards import match_character_card
    from .image_gen import _load_reference_image

    if not bool(getattr(config, "draw_find_reference", True)):
        return None
    if wants_self_portrait(user_request):
        logger.info("dl_senpai draw reference skipped: 自拍/换装类不找参考图")
        return None

    card = match_character_card(prompt or "", user_request or "")
    # 角色名来源：内置角色卡 → 调用方传入的规范名（CharacterResolver 给的英文名）。
    # 为什么优先用英文名：搜索源对英文角色名的召回明显更好（实测 2/2 命中）。
    subject_name = card.name if card is not None else (character or "").strip()

    # 第一步：代码自己搜（几秒钟的事），不惊动模型。
    # 命中就直接拿去图生图，省掉整个子 agent 回合（也省掉它「我没有工具」的风险）。
    try:
        direct = await asyncio.wait_for(
            _direct_search(
                config,
                prompt=prompt,
                user_request=user_request,
                character=subject_name,
                return_candidate=with_meta,
            ),
            timeout=min(80.0, float(timeout or 80.0)),
        )
    except Exception as exc:  # noqa: BLE001 — 直搜失败照旧走子 agent
        logger.debug(f"dl_senpai draw reference direct search failed: {exc}")
        direct = None
    if direct is not None:
        # direct 可能是 (文件名, 字节)，也可能是 ReferenceCandidate（with_meta=True）：
        # 两种情况都要能把字节数打出来，别想着下标通用。
        direct_pair = direct.as_pair() if hasattr(direct, "as_pair") else direct
        logger.info(
            f"dl_senpai draw reference (direct) bytes={len(direct_pair[1])}"
        )
        return direct
    logger.info("dl_senpai draw reference: 直搜没拿到，交给子 agent 换策略")

    goal = build_reference_goal(
        prompt=prompt,
        user_request=user_request,
        character=subject_name,
    )
    spec = SubAgentSpec(
        name=REFERENCE_AGENT,
        goal=goal,
        tools=("image_find", "image_search", "image_probe"),
        max_steps=max(1, int(getattr(config, "draw_reference_steps", 4) or 4)),
        timeout=float(timeout or getattr(config, "draw_reference_timeout", 75.0) or 75.0),
    )
    try:
        answer = await asyncio.wait_for(
            run_llm_agent(config, spec), timeout=spec.timeout
        )
    except asyncio.TimeoutError:
        log_event("draw.reference_timeout", level="warning", timeout=spec.timeout)
        return None
    except Exception as exc:  # noqa: BLE001 — 找图是增强项，失败不能影响出图
        log_event(
            "draw.reference_failed",
            level="warning",
            error=f"{type(exc).__name__}: {exc}",
        )
        return None

    url = str((answer.payload or {}).get("url") or "").strip()
    if not url:
        # 模型经常收工时忘记把 url 写进 payload，用最后一次工具结果兜底
        obs = answer.last_observation or {}
        url = str(obs.get("url") or "").strip()
        if not url:
            cands = obs.get("candidates") or []
            if isinstance(cands, list) and cands and isinstance(cands[0], dict):
                url = str(cands[0].get("url") or "").strip()
    if not url:
        # 模型偶尔把链接写在 answer 里而不是 payload，兜一下
        for token in (answer.answer or "").split():
            if token.startswith(("http://", "https://")):
                url = token.strip("，。,.;；)")
                break
    if not url:
        log_event(
            "draw.reference_empty",
            level="warning",
            answer=(answer.answer or "")[:120],
        )
        return None

    loaded = await _load_reference_image(
        [url], timeout=max(20.0, float(getattr(config, "draw_timeout", 120.0)))
    )
    if loaded is None:
        log_event("draw.reference_download_failed", level="warning", url=url[:200])
        return None
    logger.info(
        f"dl_senpai draw reference found url={url[:90]} bytes={len(loaded[1])} "
        f"steps={answer.steps}"
    )
    log_event("draw.reference_found", url=url[:200], bytes=len(loaded[1]), steps=answer.steps)
    if with_meta:
        from .character_references import candidate_from_url

        return candidate_from_url(url, loaded[0], loaded[1])
    return loaded
