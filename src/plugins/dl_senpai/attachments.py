"""把群友/私聊发来的文件变成模型能读的文本。

为什么单独成模块：下载与解析都是网络/重型 IO，既不属于 handler，
也不该塞进工具执行器里。桥接层只负责把「有哪些文件」写进 extra，
真正下载发生在模型决定调用 read_doc 的那一刻 —— 这样可以避免
为每条带附件的消息都白白下载一遍（尤其大 PDF）。

安全边界（与 creator_layer 的取舍一致）：
    - 文件来自消息段，路径由 OneBot 给出，模型无法构造任意路径
    - 单文件 20MB 上限，超出直接拒绝
    - 抽取文本截断到 8000 字符，避免一次吃爆上下文
    - 解析失败返回空串而非抛异常：读不了就说读不了，不能拦掉回复
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 8000

# 纯文本类：直接按文本解码，不需要第三方库
_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".log", ".json", ".csv", ".tsv",
    ".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml", ".xml",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".go", ".rs",
    ".sql", ".sh", ".ps1", ".bat", ".html", ".htm",
}

# 二进制文档类：分派到各自的解析器
_BINARY_SUFFIXES = {".docx", ".pptx", ".xlsx", ".xlsm", ".pdf"}

SUPPORTED_SUFFIXES = _TEXT_SUFFIXES | _BINARY_SUFFIXES

# 界面/入口用：给模型看的支持格式说明
SUPPORTED_HINT = "pdf / docx / pptx / xlsx / txt / md / csv / json / 代码文件"


@dataclass
class Attachment:
    """一个待读的附件（只有元数据，字节等模型真的要读时再取）。"""

    name: str = ""
    size: int = 0
    file_id: str = ""
    busid: int = 0
    url: str = ""
    group_id: str = ""
    user_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def suffix(self) -> str:
        return Path(self.name or "").suffix.lower()

    @property
    def readable(self) -> bool:
        return self.suffix in SUPPORTED_SUFFIXES

    def to_brief(self) -> dict[str, Any]:
        """给 extra 用的精简字典（不带 url，避免把临时链接写进日志）。"""
        return {
            "name": self.name,
            "size": self.size,
            "file_id": self.file_id,
            "busid": self.busid,
            "url": self.url,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "readable": self.readable,
        }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def extract_file_segments(message: Any) -> list[dict[str, Any]]:
    """从 OneBot 消息里取出 type=file 的段（按出现顺序）。"""
    found: list[dict[str, Any]] = []
    if message is None:
        return found
    for seg in message:
        if getattr(seg, "type", None) != "file":
            continue
        data = dict(getattr(seg, "data", {}) or {})
        if data:
            found.append(data)
    return found


def build_attachments(
    message: Any,
    *,
    group_id: str = "",
    user_id: str = "",
) -> list[Attachment]:
    """把消息里的 file 段转成 Attachment 列表（不下载）。

    字段名兼容 NapCat 的几种写法：file_id / id 都可能出现，
    file 与 name 都可能是文件名。
    """
    out: list[Attachment] = []
    for data in extract_file_segments(message):
        name = str(data.get("file") or data.get("name") or "").strip()
        out.append(
            Attachment(
                name=name,
                size=_as_int(data.get("file_size") or data.get("size")),
                file_id=str(data.get("file_id") or data.get("id") or "").strip(),
                busid=_as_int(data.get("busid")),
                url=str(data.get("url") or "").strip(),
                group_id=str(group_id or ""),
                user_id=str(user_id or ""),
                extra=data,
            )
        )
    return out


async def _resolve_url(bot: Any, att: Attachment) -> str:
    """拿到可下载的 URL：优先段里自带的，其次问 OneBot。"""
    if att.url.startswith(("http://", "https://")):
        return att.url

    # 群文件：NapCat 的 get_group_file_url 需要 file_id + busid
    if att.group_id and att.file_id:
        try:
            resp = await bot.call_api(
                "get_group_file_url",
                group_id=att.group_id,
                file_id=att.file_id,
                busid=att.busid,
            )
            url = str((resp or {}).get("url") or "")
            if url.startswith(("http://", "https://")):
                return url
        except Exception:  # noqa: BLE001 — 换下一种方式继续试
            pass

    # 私聊文件：只需要 user_id + file_id
    if not att.group_id and att.file_id and att.user_id:
        try:
            resp = await bot.call_api(
                "get_private_file_url",
                user_id=att.user_id,
                file_id=att.file_id,
            )
            url = str((resp or {}).get("url") or "")
            if url.startswith(("http://", "https://")):
                return url
        except Exception:  # noqa: BLE001
            pass

    # 兜底：部分实现对群/私聊都提供了 get_file
    if att.file_id:
        try:
            resp = await bot.call_api("get_file", file_id=att.file_id)
            url = str((resp or {}).get("url") or "")
            if url.startswith(("http://", "https://")):
                return url
        except Exception:  # noqa: BLE001
            pass

    return ""


async def download_attachment(bot: Any, att: Attachment) -> bytes | None:
    """下载附件字节；失败或超限返回 None。"""
    import httpx

    if att.size and att.size > MAX_FILE_BYTES:
        return None

    url = await _resolve_url(bot, att)
    if not url:
        return None

    try:
        async with httpx.AsyncClient(
            timeout=60.0, follow_redirects=True, trust_env=False
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
    except Exception:  # noqa: BLE001
        return None

    if len(data) > MAX_FILE_BYTES:
        return None
    return data


def _decode_text(data: bytes) -> str:
    """按常见编码依次尝试解码，最后兜底 utf-8 替换。"""
    for enc in ("utf-8", "utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _text_from_docx(data: bytes) -> str:
    import docx  # python-docx

    doc = docx.Document(io.BytesIO(data))
    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    # 表格里的内容往往正是用户想问的重点，单独拼出来
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _text_from_pptx(data: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    parts: list[str] = []
    for idx, slide in enumerate(prs.slides, 1):
        parts.append("[第" + str(idx) + "页]")
        for shape in slide.shapes:
            text = getattr(shape, "text", "")
            if text and text.strip():
                parts.append(text.strip())
    return "\n".join(parts)


def _text_from_xlsx(data: bytes) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts: list[str] = []
    try:
        for ws in wb.worksheets:
            parts.append("[工作表 " + str(ws.title) + "]")
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    parts.append(" | ".join(cells).rstrip(" |"))
    finally:
        wb.close()
    return "\n".join(parts)


def _text_from_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    # 只取前 40 页：几百页的书全读进来也没意义，还会吃爆上下文
    for idx, page in enumerate(reader.pages[:40], 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — 个别页损坏不该拖垮整份文档
            text = ""
        if text.strip():
            parts.append("[第" + str(idx) + "页]\n" + text.strip())
    return "\n".join(parts)


def _text_from_html(data: bytes) -> str:
    text = _decode_text(data)
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return soup.get_text("\n", strip=True)
    except Exception:  # noqa: BLE001 — 没装 bs4 就退回粗暴去标签
        return re.sub(r"<[^>]+>", "", text)


def _text_from_csv(data: bytes) -> str:
    text = _decode_text(data)
    rows: list[str] = []
    try:
        for row in csv.reader(io.StringIO(text)):
            if any(c.strip() for c in row):
                rows.append(" | ".join(row))
    except Exception:  # noqa: BLE001
        return text
    return "\n".join(rows)


def extract_text(name: str, data: bytes) -> str:
    """按扩展名把字节抽成文本；不支持或失败都返回空串。

    整个分派包在 try 里：群里发来的文件什么情况都有（截断的、
    改了扩展名的、加密的、空文件），任何一个解析器的异常都不能
    变成用户看到的报错。
    """
    suffix = Path(name or "").suffix.lower()
    try:
        if suffix in (".html", ".htm"):
            return _text_from_html(data)

        if suffix in (".csv", ".tsv"):
            return _text_from_csv(data)

        if suffix == ".json":
            text = _decode_text(data)
            try:
                return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except Exception:  # noqa: BLE001
                return text

        if suffix in _TEXT_SUFFIXES:
            return _decode_text(data)

        if suffix == ".docx":
            return _text_from_docx(data)
        if suffix == ".pptx":
            return _text_from_pptx(data)
        if suffix in (".xlsx", ".xlsm"):
            return _text_from_xlsx(data)
        if suffix == ".pdf":
            return _text_from_pdf(data)
    except Exception:  # noqa: BLE001 — 解析失败按「读不出来」处理
        return ""

    return ""


def clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """截断过长文本，并明确告知被截断（模型需要知道自己没看全）。"""
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "\n…（内容过长，已截断，仅显示前 " + str(limit) + " 字）"


__all__ = [
    "Attachment",
    "MAX_FILE_BYTES",
    "MAX_TEXT_CHARS",
    "SUPPORTED_HINT",
    "SUPPORTED_SUFFIXES",
    "build_attachments",
    "clip",
    "download_attachment",
    "extract_file_segments",
    "extract_text",
]
