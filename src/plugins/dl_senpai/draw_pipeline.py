"""生图参考流水线：先找 3 张参考图，再挑一张锚定生成。

## 为什么值得多花这十几秒

单张参考图定身份，失败方式太多：搜到的可能是同人二创、可能是别人、可能只是一张
构图猎奇的图。而且**没有任何东西能交叉验证它**——错了也只能错到底。

三张各自独立搜来的图能多做两件事：

1. **共识特征**：三张里有两张以上都出现的发色/瞳色/服装，才写进提示词。
   单张图上看到「深紫褐色短发」可能只是那张图上色的问题，三张都这么说才可信。
2. **选出最像的那张**：问视觉模型「这三张里哪张最适合作为这个角色的身份锚点」，
   比按 URL 顺序拿第一张准得多（第一张经常是缩略图或无关图）。

## 为什么最后只送一张进 images.edit

- 中转后端对多图输入的支持未经验证，赌不起；
- 参考图越多，主体越容易被稀释，三张混喂常出四不像。

所以：**3 张都拿、用它们做交叉核对、只用最像的那 1 张锚定**。

## 成本纪律

整个流水线只多一次视觉调用：三张图一次性发过去，同时问「共同特征是什么」和
「哪张最好用」。不做「逐张描述再取交集」——那要 3 次调用，结果还不如一次看完准。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# 目标张数。少于这个数也照常跑（降级链：3 → 2 → 1 → 纯文字）
TARGET_REFERENCES = 3
# 上游图生图对体积敏感，太大的图直接 400
_MAX_BYTES = 4 * 1024 * 1024
_MAX_TRAITS = 8


@dataclass(frozen=True)
class RefCandidate:
    """一张候选参考图。raw 只放内存——参考图是一次性的，落盘要管清理和并发覆盖。"""

    name: str
    raw: bytes
    source: str
    trust: float = 0.0
    page_url: str = ""

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    def as_dict(self, *, with_hash: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "source": self.source,
            "trust": round(self.trust, 2),
            "bytes": len(self.raw),
        }
        if with_hash:
            data["sha256"] = self.sha256[:16]
        if self.page_url:
            data["page_url"] = self.page_url
        return data


@dataclass(frozen=True)
class RefAnalysis:
    """一次视觉调用的两个产出：能信的共同特征、以及该用哪张。"""

    traits: tuple[str, ...] = ()
    best_index: int = 0
    reason: str = ""
    ok: bool = False
    # 参考图和已知设定是否对得上。对不上就别拿它当锚点——实测搜到的三张
    # 全是棕发二创，拿它们画出来必然是棕发，再让自检去发现已经白烧一张图了。
    consistent: bool = True
    differences: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "traits": list(self.traits),
            "best_index": self.best_index,
            "reason": self.reason[:120],
            "ok": self.ok,
            "consistent": self.consistent,
            "differences": list(self.differences),
        }


_ANALYZE_PROMPT = """你在核对一组角色参考图，用于判断一个已知角色的身份特征。

目标角色：{subject}

图里共有 {count} 张图（按顺序编号 1..{count}）。

请做三件事：
1. 找出这些图里**共同出现**的视觉特征（发色/发型、瞳色、标志性服饰、角/耳/尾等
   种族特征、核心配饰、主色调）。只列两张以上都有的特征；只有单张图有的、
   明显是画师个人发挥的，不要列。最多 {max_traits} 条，每条不超过 12 个字。
2. 判断哪一张最适合作为"最权威的身份参考"——要求最清晰、最能代表这个角色、
   最接近官方设定（而不是画风猎奇或姿势遮挡的那张）。
3. 判断这些图与下面的「已知设定」是否对得上。

已知设定（可能为空）：
{expected}

第 3 件事细说：这些图和上面的已知设定在**硬特征**上对不对得上——
发色、瞳色、种族特征（角/耳/尾）、主服饰。画风、线条、上色方式、构图、
姿势上的差异**不算**不一致。

如果三张里有两张以上在硬特征上与设定冲突，consistent 填 false，
并在 differences 里一句话写清冲突在哪（例如"三张均为棕发，设定为银白偏浅紫"）。
设定为空时 consistent 一律填 true。

只输出 JSON：
{{"traits":["..."],"best_index":1,"reason":"一句话说明为什么选这张",
"consistent":true,"differences":["..."]}}"""


# 参考图黑名单：记下"这个角色在网上找不到可用设定图"这个事实。
#
# 为什么需要：2026-10-27 实测艾雅法拉连续三次搜到的都是二创（棕灰发、红粉瞳、
# 红灰白连衣裙），每次都要花 75~248 秒找图 + 60 秒核对，才发现"图不对、不能用"，
# 然后再按档案重画。也就是说每次画图都先白等两三分钟。
# 记住这个结论，下次直接跳过找图——代价只是 7 天内不重试，
# 过期后自动重试（图源可能变好，不该永久封印）。
_BLOCK_TTL_SECONDS = 7 * 24 * 3600


def _blocklist_path() -> Path:
    from .entity_resolver import cache_path

    return cache_path().parent / "reference_blocklist.json"


def _load_blocklist() -> dict[str, Any]:
    import json

    try:
        raw = _blocklist_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — 黑名单只是优化，坏了就当空
        return {}


def references_known_unusable(subject: str) -> bool:
    """这个角色的网图是不是刚被判定过不可用。"""
    import time as _time

    if not subject:
        return False
    entry = _load_blocklist().get(subject.strip().lower())
    if not isinstance(entry, dict):
        return False
    ts = float(entry.get("ts") or 0.0)
    return bool(ts) and (_time.time() - ts) < _BLOCK_TTL_SECONDS


def mark_references_unusable(subject: str, *, differences: tuple[str, ...] = ()) -> None:
    """记下"这个角色的网图与设定不符"。写失败不影响出图。"""
    import json
    import time as _time

    if not subject:
        return
    try:
        data = _load_blocklist()
        data[subject.strip().lower()] = {
            "ts": _time.time(),
            "diffs": [str(d)[:120] for d in differences[:3]],
        }
        path = _blocklist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)      # 原子替换，避免并发写坏一半
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"dl_senpai blocklist write failed: {type(exc).__name__}: {exc}")


async def _download(url: str, *, timeout: float = 20.0) -> bytes | None:
    """下载一张图。失败返回 None——参考图是增强项，不能因为它挂掉整次生图。"""
    import httpx

    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = resp.content
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"dl_senpai ref download failed: {type(exc).__name__}: {exc}")
        return None
    if not raw or len(raw) > _MAX_BYTES:
        return None
    if not _is_image_bytes(raw):
        return None
    return raw


def _is_image_bytes(raw: bytes) -> bool:
    """按文件头判断是不是图。

    为什么要自己判：很多图床把 webp/jpeg 标成 png，名实不符时上游 images.edit
    会直接 400；更常见的是搜到的其实是 HTML 错误页，喂进去必然失败。
    """
    if not raw:
        return False
    return (
        raw[:3] == b"\xff\xd8\xff"
        or raw[:8] == b"\x89PNG\r\n\x1a\n"
        or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")
    )


async def _web_candidates(
    user_request: str, *, subject: str, target: int, timeout: float
) -> list[RefCandidate]:
    """联网搜图补足。这里刻意复用子智能体那套搜索工具，避免两套实现漂移。"""
    from .subagent import _tool_image_search

    queries: list[str] = []
    if subject:
        queries.append(f"{subject} 官方立绘")
        queries.append(f"{subject} 全身 设定")
    if user_request and user_request not in queries:
        queries.append(f"{user_request[:24]} 插画")

    out: list[RefCandidate] = []
    seen: set[str] = set()
    for query in queries[:2]:
        try:
            found = await _tool_image_search({"query": query, "limit": max(3, target)})
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"dl_senpai ref search failed: {type(exc).__name__}: {exc}")
            continue
        for hit in (found.get("hits") or []):
            if len(out) >= target:
                return out
            url = str(hit.get("image_url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            raw = await _download(url, timeout=timeout)
            if raw is None:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            if any(c.sha256 == digest for c in out):
                continue      # 同一张图被多个 query 搜到，去重
            out.append(
                RefCandidate(
                    name=url.rsplit("/", 1)[-1][:40] or "web.jpg",
                    raw=raw,
                    source=f"web:{str(hit.get('title') or '')[:20]}",
                    trust=0.5,
                    page_url=str(hit.get("page_url") or ""),
                )
            )
    return out


async def collect_references(
    config: Any,
    *,
    profile: Any = None,
    user_request: str = "",
    subject: str = "",
    user_pair: tuple[str, bytes] | None = None,
    target: int = TARGET_REFERENCES,
    budget: float = 75.0,
) -> list[RefCandidate]:
    """凑够 target 张参考图。顺序是固定的：用户图 > 本地已核验 > 网搜。

    这个顺序不能反：用户亲手发的图是他想要的那个样子，本地库是人工核验过的，
    网搜是三者里唯一可能张冠李戴的来源。
    """
    out: list[RefCandidate] = []
    # 用户亲手发的图永远优先，黑名单只拦"联网找图"这一步
    if user_pair is not None and user_pair[1]:
        out.append(
            RefCandidate(name=user_pair[0] or "user.jpg", raw=user_pair[1],
                         source="user_image", trust=1.0)
        )

    if profile is not None and len(out) < target:
        try:
            from .character_references import resolve_library_reference

            diag: list[str] = []
            picked = resolve_library_reference(
                config, profile, user_request=user_request, diagnostics=diag
            )
            if picked is not None and picked.raw:
                digest = hashlib.sha256(picked.raw).hexdigest()
                if not any(c.sha256 == digest for c in out):
                    out.append(
                        RefCandidate(name=picked.filename, raw=picked.raw,
                                     source="local_verified", trust=0.95)
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"dl_senpai ref library failed: {type(exc).__name__}: {exc}")

    # 用户自己发了图就不再联网找：他要的是「照这张画」，补两张风格不同的网图
    # 只会把主体带偏。本地已核验库仍然可以补，因为那是同一个角色。
    has_user_image = any(c.source == "user_image" for c in out)
    if len(out) < target and not has_user_image and references_known_unusable(subject):
        logger.info(
            f"dl_senpai ref search skipped subject={subject!r} reason=known_unusable"
        )
        return out[:target]
    if len(out) < target and not has_user_image:
        import asyncio

        timeout = float(getattr(config, "draw_reference_timeout", 20.0) or 20.0)
        # 必须有总预算：2026-10-27 实测一次找图耗掉 248 秒（搜索 + 逐张下载都慢），
        # 把后面 246 秒的生成时间挤没了，作业直接超时。宁可用不满 3 张，
        # 也不能让找图把生成挤死——参考图是增强项，出图才是目的。
        try:
            web = await asyncio.wait_for(
                _web_candidates(
                    user_request, subject=subject, target=target - len(out), timeout=timeout
                ),
                timeout=max(10.0, float(budget)),
            )
        except asyncio.TimeoutError:
            logger.warning(f"dl_senpai ref search budget exhausted budget={budget}s")
            web = []
        out.extend(web)

    return out[:target]


async def analyze_references(
    config: Any,
    candidates: list[RefCandidate],
    *,
    subject: str = "",
    expectations: str = "",
) -> RefAnalysis:
    """一次调用看完全部候选图：产出共同特征 + 指出哪张最适合做锚点。"""
    if not candidates:
        return RefAnalysis()
    # 只有一张就没有"选"和"共识"可言，省掉这次调用
    if len(candidates) == 1:
        return RefAnalysis(traits=(), best_index=0, reason="single_reference", ok=True)

    try:
        from .draw_enhance import _chat, _data_url
    except Exception:  # noqa: BLE001
        return RefAnalysis()

    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": _ANALYZE_PROMPT.format(
                subject=subject or "(未指定)",
                count=len(candidates),
                max_traits=_MAX_TRAITS,
                expected=(expectations or "").strip() or "(无，跳过一致性判断)",
            ),
        }
    ]
    for cand in candidates:
        try:
            content.append({"type": "image_url", "image_url": {"url": _data_url(cand.raw)}})
        except Exception:  # noqa: BLE001
            continue
    if len(content) <= 1:
        return RefAnalysis()

    import asyncio as _asyncio

    try:
        # 核对同样要有上限：三张大图一起发，视觉模型慢起来没有边界
        text = await _asyncio.wait_for(_chat(config, content, max_tokens=400), timeout=60.0)
    except _asyncio.TimeoutError:
        logger.warning("dl_senpai ref analyze timeout 60s")
        return RefAnalysis()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai ref analyze failed: {type(exc).__name__}: {exc}")
        return RefAnalysis()

    import json

    data: dict[str, Any] = {}
    match = re.search(r"\{.*\}", text or "", re.S)
    if match:
        try:
            data = json.loads(match.group(0))
        except Exception:  # noqa: BLE001
            data = {}
    traits = tuple(
        str(t).strip() for t in (data.get("traits") or [])[:_MAX_TRAITS] if str(t).strip()
    )
    try:
        best = int(data.get("best_index") or 1) - 1     # 模型按 1-based 编号回答
    except (TypeError, ValueError):
        best = 0
    if not (0 <= best < len(candidates)):
        best = 0
    return RefAnalysis(
        traits=traits, best_index=best,
        reason=str(data.get("reason") or ""), ok=True,
        consistent=bool(data.get("consistent", True)),
        differences=tuple(
            str(d).strip() for d in (data.get("differences") or [])[:4] if str(d).strip()
        ),
    )


def pick_anchor(candidates: list[RefCandidate], analysis: RefAnalysis) -> RefCandidate | None:
    """选出送进 images.edit 的那一张。

    用户自己发的图永远优先——他要的是"照这张画"，不是"照你搜到的画"。
    其余情况听视觉模型的判断。
    """
    if not candidates:
        return None
    for cand in candidates:
        if cand.source == "user_image":
            return cand
    index = analysis.best_index if 0 <= analysis.best_index < len(candidates) else 0
    return candidates[index]


def traits_block(traits: tuple[str, ...]) -> str:
    """把共识特征写成提示词片段。

    措辞上明确标成「多张参考图里一致出现的特征」，它既不是人工核验的硬约束，
    也不是单张图的随意描述——介于两者之间，写错了也只是软提示。
    """
    if not traits:
        return ""
    lines = "\n".join(f"- {t}" for t in traits)
    return (
        "Traits consistently present across the reference images "
        "(treat as supporting evidence, not as a hard rule):\n" + lines
    )


__all__ = [
    "TARGET_REFERENCES",
    "mark_references_unusable",
    "references_known_unusable",
    "RefAnalysis",
    "RefCandidate",
    "analyze_references",
    "collect_references",
    "pick_anchor",
    "traits_block",
]
