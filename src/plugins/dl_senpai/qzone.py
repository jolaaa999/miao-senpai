"""QQ 空间说说：先写草稿，主人点头再发。

为什么默认只存草稿
------------------
说说挂在她的号上，但那个号是主人的。发出去就是公开留痕：
可以截图、可以被转发、可以被认出是谁在养。
所以默认路径是「她写好 → 落盘成草稿 → 主人看到那句『发』才真的发出去」。
QZONE_AUTO_POST=true 才允许她自己发，且每天最多 1 条。

发布走什么
----------
走 QQ 空间 web 的 emotion_cgi_publish_v6 接口：
    1. 从登录态里取 p_skey，用 hash33 算出 g_tk
    2. 打开自己的空间首页，从 HTML 里抠 qzonetoken
    3. POST 表单（content + 一堆固定字段）
为什么不用 Playwright 点 UI：说说的输入框是富文本编辑器，选择器和弹层
一改版就废，而且失败时说不清是"没找到按钮"还是"被风控"。
接口这条路失败原因更明确（未登录/验签/风控），也快得多。

**诚实标注：这条发布链路还没实测过**——要等主人登录一次才能验。
所以第一次真发之前，工具有 dry_run 档：只做到"拿到 token + 组好表单"，
不提交。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "QzoneError",
    "clear_drafts",
    "drop_draft",
    "QzoneResult",
    "compose_text",
    "list_drafts",
    "latest_draft",
    "mark_published",
    "publish",
    "save_draft",
]

_DEFAULT_DRAFT_FILE = "data/dl_senpai/qzone_drafts.json"
_PUBLISH_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
)
_SPACE_URL = "https://user.qzone.qq.com/{uin}"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 说说里不该出现的东西。不是道德审查，是这三样会让整条说说被判广告/折叠，
# 或者直接把主人的联系方式抖出去。
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_GROUP_RE = re.compile(r"(加|进)\s*(QQ|qq)?\s*群|群号\s*[:：]?\s*\d{5,}", re.IGNORECASE)

_TOKEN_RE = re.compile(r'window\.g_qzonetoken\s*=\s*\(function\(\)\{\s*try\{return\s*"([^"]+)"')


class QzoneError(Exception):
    """不能发 / 发不出去。message 是给她和主人看的人话。"""


@dataclass
class QzoneResult:
    ok: bool = False
    text: str = ""
    tid: str = ""
    error: str = ""
    dry_run: bool = False


@dataclass
class Draft:
    """一条待发的说说草稿。"""

    draft_id: str = ""
    text: str = ""
    created_at: float = 0.0
    published_at: float = 0.0
    tid: str = ""
    note: str = ""

    @property
    def pending(self) -> bool:
        return not self.published_at


# ------------------------------------------------------------------ 内容清洗


def compose_text(text: str, cfg: Any = None) -> str:
    """把她的原话清成能发的说说。不合格就抛 QzoneError（人话原因）。"""
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        raise QzoneError("内容是空的")
    # 多余空行压成一个：说说里空行会被渲染成一大片空隙
    cleaned = re.sub(r"\n{2,}", "\n", raw)
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n")).strip()

    limit = int(getattr(cfg, "qzone_max_chars", 120) or 0)
    if limit > 0 and len(cleaned) > limit:
        raise QzoneError(f"太长了（{len(cleaned)} 字），说说最多 {limit} 字")
    if _URL_RE.search(cleaned):
        raise QzoneError("说说里不带链接（会被判广告，也容易暴露你自己）")
    if _PHONE_RE.search(cleaned):
        raise QzoneError("说说里不要出现手机号")
    if _GROUP_RE.search(cleaned):
        raise QzoneError("说说里不做加群引流")
    return cleaned


# ------------------------------------------------------------------ 草稿存储


def _draft_path(cfg: Any) -> Path:
    raw = str(getattr(cfg, "qzone_draft_file", "") or "")
    return Path(raw or _DEFAULT_DRAFT_FILE)


def _load_drafts(cfg: Any) -> list[dict[str, Any]]:
    path = _draft_path(cfg)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get("drafts") if isinstance(raw, dict) else raw
    return [r for r in (rows or []) if isinstance(r, dict)]


def _save_drafts(rows: list[dict[str, Any]], cfg: Any) -> None:
    path = _draft_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 只留最近 50 条：草稿是给人看的，攒太多反而没人翻
    path.write_text(
        json.dumps({"drafts": rows[-50:]}, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def save_draft(text: str, cfg: Any = None, *, note: str = "") -> Draft:
    """存一条草稿。内容不合格直接抛错，不留脏数据。"""
    cleaned = compose_text(text, cfg)
    rows = _load_drafts(cfg)
    draft = Draft(
        draft_id=f"{int(time.time())}-{len(rows) + 1}",
        text=cleaned,
        created_at=time.time(),
        note=note,
    )
    rows.append(
        {
            "draft_id": draft.draft_id,
            "text": draft.text,
            "created_at": draft.created_at,
            "published_at": 0.0,
            "tid": "",
            "note": note,
        }
    )
    _save_drafts(rows, cfg)
    return draft


def _to_draft(row: dict[str, Any]) -> Draft:
    return Draft(
        draft_id=str(row.get("draft_id") or ""),
        text=str(row.get("text") or ""),
        created_at=float(row.get("created_at") or 0),
        published_at=float(row.get("published_at") or 0),
        tid=str(row.get("tid") or ""),
        note=str(row.get("note") or ""),
    )


def list_drafts(cfg: Any = None, *, only_pending: bool = False) -> list[Draft]:
    items = [_to_draft(r) for r in _load_drafts(cfg)]
    if only_pending:
        items = [d for d in items if d.pending]
    return items


def latest_draft(cfg: Any = None) -> Draft | None:
    pending = list_drafts(cfg, only_pending=True)
    return pending[-1] if pending else None


def drop_draft(draft_id: str, cfg: Any = None) -> bool:
    """删掉一条还没发的草稿。删不掉（已发/没有）返回 False。

    为什么要能删：她存完会自己复核一遍，发现写坏了只能留着当垃圾，
    攒几十条之后主人根本不想翻。
    """
    rows = _load_drafts(cfg)
    kept = [
        row
        for row in rows
        if not (
            str(row.get("draft_id") or "") == str(draft_id)
            and not float(row.get("published_at") or 0)
        )
    ]
    if len(kept) == len(rows):
        return False
    _save_drafts(kept, cfg)
    return True


def clear_drafts(cfg: Any = None, *, include_published: bool = False) -> int:
    """清空草稿，返回删掉几条。默认保留已发过的记录（那是台账）。"""
    rows = _load_drafts(cfg)
    kept = [
        row
        for row in rows
        if include_published is False and float(row.get("published_at") or 0)
    ]
    removed = len(rows) - len(kept)
    if removed:
        _save_drafts(kept, cfg)
    return removed


def mark_published(draft_id: str, *, tid: str = "", cfg: Any = None) -> bool:
    rows = _load_drafts(cfg)
    hit = False
    for row in rows:
        if str(row.get("draft_id") or "") == str(draft_id):
            row["published_at"] = time.time()
            row["tid"] = tid
            hit = True
            break
    if hit:
        _save_drafts(rows, cfg)
    return hit


# ------------------------------------------------------------------ 真发


def gtk_from_skey(skey: str) -> str:
    """hash33：QQ 空间所有写接口的 g_tk 都是这么算的。"""
    acc = 5381
    for ch in skey or "":
        acc += (acc << 5) + ord(ch)
    return str(acc & 0x7FFFFFFF)


def _cookies_from_state(cfg: Any) -> dict[str, str]:
    from .web_login import state_path

    path = state_path("qzone", cfg)
    if not path.exists():
        raise QzoneError("QQ空间还没登录过，先跑 python scripts\\login-web.py qzone")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QzoneError("登录态文件读不出来，重新登录一次") from exc
    jar: dict[str, str] = {}
    for cookie in data.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if name and value:
            jar[name] = value
    if not jar.get("p_skey") and not jar.get("skey"):
        raise QzoneError("登录态里没有 p_skey，重新登录一次")
    return jar


async def publish(
    text: str, cfg: Any = None, *, dry_run: bool = False, timeout: float = 25.0
) -> QzoneResult:
    """真发一条说说。dry_run=True 时只验到"能组出请求"就停。"""
    import httpx

    cleaned = compose_text(text, cfg)
    jar = _cookies_from_state(cfg)
    from .web_login import assert_not_owner_account, read_logged_uin

    uin = assert_not_owner_account("qzone", cfg) or read_logged_uin("qzone", cfg)
    if not uin:
        raise QzoneError("从登录态里没认出 QQ 号，重新登录一次")

    gtk = gtk_from_skey(jar.get("p_skey") or jar.get("skey") or "")
    headers = {
        "User-Agent": _UA,
        "Referer": _SPACE_URL.format(uin=uin),
        "Origin": "https://user.qzone.qq.com",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    form = {
        "syn_tweet_verson": "1",
        "paramstr": "1",
        "who": "1",
        "conformat": "1",
        "timeline_msg": "1",
        "feedversion": "1",
        "ver": "1",
        "ugc_right": "1",
        "to_sign": "0",
        "hostuin": uin,
        "code_version": "1",
        "format": "json",
        "qzreferrer": _SPACE_URL.format(uin=uin),
        "content": cleaned,
    }

    async with httpx.AsyncClient(
        headers=headers, cookies=jar, timeout=timeout, follow_redirects=True
    ) as client:
        # 1) 空间首页拿 qzonetoken（没有它会被判非法请求）
        try:
            space = await client.get(_SPACE_URL.format(uin=uin))
        except httpx.HTTPError as exc:
            raise QzoneError(f"打开空间首页失败（{type(exc).__name__}）") from exc
        token_match = _TOKEN_RE.search(space.text or "")
        qzonetoken = token_match.group(1) if token_match else ""
        if not qzonetoken and not dry_run:
            raise QzoneError("没拿到 qzonetoken，登录态大概过期了，重新登录一次")

        if dry_run:
            # 只到这一步：证明 cookie/g_tk/token 都齐了，但绝不提交
            return QzoneResult(
                ok=True,
                text=cleaned,
                error="",
                dry_run=True,
                tid=f"dry-run(gtk={gtk}, token={'有' if qzonetoken else '无'})",
            )

        params = {"g_tk": gtk}
        payload = dict(form)
        if qzonetoken:
            payload["qzonetoken"] = qzonetoken
        try:
            resp = await client.post(
                _PUBLISH_URL, params=params, data=payload
            )
        except httpx.HTTPError as exc:
            raise QzoneError(f"提交失败（{type(exc).__name__}）") from exc

    body = (resp.text or "").strip()
    # 接口成功时回 {"code":0,"subcode":0,...,"tid":"..."}；失败会带 message
    hit = re.search(r'"tid"\s*:\s*"([^"]+)"', body)
    if '"code":0' in body or hit:
        return QzoneResult(ok=True, text=cleaned, tid=hit.group(1) if hit else "")
    snippet = body[:200].replace("\n", " ")
    reason = "被风控或登录态失效" if not hit else ""
    raise QzoneError(f"发不出去{('：' + reason) if reason else ''}（{snippet}）")
