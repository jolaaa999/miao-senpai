"""群黑话 / 群梗库：她遇到听不懂的说法就记一笔，主人确认后进词表。

为什么需要它（2026-10-27）：

qq-bridge 有 qq_slang_query / qq_slang_submit 一对工具，思路是「群里冒出新
说法 → 记成候选 → 人工确认 → 之后的对话自动带上这层理解」。学姐这边完全
没有：群里换个梗词，她只能按字面瞎猜，猜错了还不知道自己错了。

为什么不做成「自动挖词」：那是 qq-bridge 用 LLM 做的事，规则版做不出来。
中文 2~4 字的重复串里绝大多数是常用词，「绝绝子」和「我觉得」在频率上
长得一模一样，纯统计挖出来全是噪声。所以这条路改成**她自己遇到才记**——
这正是她已有的 NOTE / IMPRESSION 的做法：由模型判断「这个说法我不懂」，
比任何统计阈值都准。

两条硬约束：
1. **待确认的释义不注入提示词**。半懂的释义混进人设，比不认识这个词更糟：
   她会用错还理直气壮。只有确认过的才进词表。
2. **只有主人能写释义**。群友可以「教」她，但不该由群友单方面改写她对
   群里语言的长期理解——那是被投毒最快的入口。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent.tools.models import Continuation, ToolCall, ToolResult
from .agent.tools.registry import FailurePolicy, ToolDefinition
from .observability.logging import log_event

SLANG_MARKER = "SLANG"

# 词条本身不会很长；超过这个长度就不是「说法」而是半句话
_TERM_MAX = 12
_MEANING_MAX = 60
_STATUS_PENDING = "pending"
_STATUS_CONFIRMED = "confirmed"

SLANG_PROMPT_TEXT = """【群里的说法】
群里有人用了你听不懂的说法时，先自己记一笔，别硬猜：

<<<SLANG NEW 说法>>>

主人跟你解释过某个说法之后，写：

<<<SLANG ADD 说法=什么意思>>>

想查某个说法现在词表里怎么写的：<<<SLANG 说法>>>
想看看词表都有什么：<<<SLANG LIST>>>

什么时候记：这个词在群里反复出现、你确实不懂、而且看起来是群内的说法
（缩写、外号、谐音、梗）。什么时候别记：普通生僻字、错别字、人名、
你其实懂只是没想到——那些记下来只会污染词表。

记完不用跟人汇报。也不要为了显得博学，把猜的意思写进 ADD——只有主人
明确说过，才写 ADD。
"""


@dataclass
class SlangEntry:
    term: str
    meaning: str = ""
    status: str = _STATUS_PENDING
    hits: int = 0
    updated_at: float = 0.0
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "meaning": self.meaning,
            "status": self.status,
            "hits": self.hits,
            "updated_at": self.updated_at,
            "examples": list(self.examples)[:3],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SlangEntry | None":
        term = str(raw.get("term") or "").strip()
        if not term:
            return None
        status = str(raw.get("status") or _STATUS_PENDING)
        if status not in {_STATUS_PENDING, _STATUS_CONFIRMED}:
            status = _STATUS_PENDING
        examples = raw.get("examples")
        return cls(
            term=term,
            meaning=str(raw.get("meaning") or "").strip(),
            status=status,
            hits=int(raw.get("hits") or 0),
            updated_at=float(raw.get("updated_at") or 0.0),
            examples=[str(e) for e in examples[:3]] if isinstance(examples, list) else [],
        )


def normalize_term(term: str) -> str:
    """把词条清洗成一个能进词表的短串。

    中英文标点、空格、引号都去掉：模型很爱写「「绝绝子」」这种带引号的形态，
    原样存进去，下次查询就对不上了。
    """
    text = str(term or "").strip()
    for ch in "「」『』《》<>\"'\u201c\u201d\u2018\u2019（）()[]【】 \t\n":
        text = text.replace(ch, "")
    return text[:_TERM_MAX]


class SlangStore:
    """黑话词表的持久化存储（一个 JSON 文件）。

    为什么用一个文件而不是每个群一份：黑话天然跨群扩散，同一批人同时在几个
    群里。分群存储会让同一个说法要在五个群里被确认五次。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cache: dict[str, SlangEntry] | None = None

    def _load_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 坏文件当空表处理：一个读不动的词表不该让工具整条失败
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        return entries if isinstance(entries, dict) else {}

    def load(self) -> dict[str, SlangEntry]:
        if self._cache is not None:
            return self._cache
        out: dict[str, SlangEntry] = {}
        for term, raw in self._load_raw().items():
            if not isinstance(raw, dict):
                continue
            payload = dict(raw)
            payload.setdefault("term", term)
            entry = SlangEntry.from_dict(payload)
            if entry is not None:
                out[entry.term] = entry
        self._cache = out
        return out

    def save(self) -> None:
        entries = self._cache or {}
        payload = {
            "version": 1,
            "entries": {term: entry.to_dict() for term, entry in entries.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log_event("slang.save_failed", error=f"{type(exc).__name__}: {exc}"[:160])

    def get(self, term: str) -> SlangEntry | None:
        return self.load().get(normalize_term(term))

    def note(self, term: str, *, now: float | None = None) -> SlangEntry | None:
        """记一个「她不懂的说法」。重复遇到只加计数，不新建。"""
        key = normalize_term(term)
        if not key:
            return None
        entries = self.load()
        entry = entries.get(key)
        if entry is None:
            entry = SlangEntry(term=key, updated_at=now or time.time())
            entries[key] = entry
        entry.hits += 1
        entry.updated_at = now or time.time()
        self.save()
        return entry

    def confirm(self, term: str, meaning: str) -> SlangEntry | None:
        key = normalize_term(term)
        if not key:
            return None
        entries = self.load()
        entry = entries.get(key) or SlangEntry(term=key)
        entry.status = _STATUS_CONFIRMED
        entry.meaning = str(meaning or "").strip()[:_MEANING_MAX] or entry.meaning
        entry.updated_at = time.time()
        entries[key] = entry
        self.save()
        return entry

    def drop(self, term: str) -> bool:
        key = normalize_term(term)
        entries = self.load()
        if key in entries:
            del entries[key]
            self.save()
            return True
        return False

    def confirmed(self, limit: int = 40) -> list[SlangEntry]:
        items = [e for e in self.load().values() if e.status == _STATUS_CONFIRMED and e.meaning]
        # 命中多的排前面：越常用的说法越该先占提示词的额度
        items.sort(key=lambda e: (-e.hits, e.term))
        return items[:limit]

    def pending(self, limit: int = 20) -> list[SlangEntry]:
        items = [e for e in self.load().values() if e.status == _STATUS_PENDING]
        items.sort(key=lambda e: (-e.hits, e.term))
        return items[:limit]


_store_cache: dict[str, SlangStore] = {}


def slang_path(config: Any = None) -> Path:
    raw = getattr(config, "slang_dir", "") or "data/dl_senpai/slang"
    return Path(str(raw)) / "slang.json"


def get_slang_store(config: Any = None) -> SlangStore:
    """取词表实例。按路径缓存，测试换目录天然拿到新实例。"""
    key = str(slang_path(config))
    store = _store_cache.get(key)
    if store is None:
        store = SlangStore(key)
        _store_cache[key] = store
    return store


def reset_slang_store() -> None:
    _store_cache.clear()


def build_slang_brief(config: Any = None, *, max_entries: int = 12, max_chars: int = 320) -> str:
    """拼一段注入提示词的词表。

    为什么限长：词表会一直长下去。不设上限，三个月后它会挤掉人设——
    提示词里的每一行都在跟别的内容抢注意力，只有高频词配占这个位置。
    """
    if config is not None and not bool(getattr(config, "slang_brief_enable", True)):
        return ""
    store = get_slang_store(config)
    entries = store.confirmed(limit=max(1, max_entries))
    if not entries:
        return ""
    lines: list[str] = []
    used = 0
    for entry in entries:
        line = f"- {entry.term}：{entry.meaning}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return "【群里这些说法她是懂的】\n" + "\n".join(lines)


def parse_slang_request(raw_reply: str) -> tuple[str, list[ToolCall]]:
    """解析 <<<SLANG ...>>>：LIST / NEW 词 / ADD 词=意思 / 词。"""
    import re

    pattern = re.compile(r"<<<\s*SLANG\s*([^>]*)>>>", re.IGNORECASE)
    text = raw_reply or ""
    calls: list[ToolCall] = []
    for match in pattern.finditer(text):
        body = (match.group(1) or "").strip()
        args: dict[str, Any] = {"op": "get", "term": "", "meaning": ""}
        lowered = body.lower()
        if lowered in {"", "list", "列表"}:
            args["op"] = "list"
        elif lowered.startswith("new "):
            args["op"] = "new"
            args["term"] = body[4:].strip()
        elif lowered.startswith("add "):
            args["op"] = "add"
            rest = body[4:].strip()
            if "=" in rest:
                term, _, meaning = rest.partition("=")
            elif "：" in rest:
                term, _, meaning = rest.partition("：")
            else:
                term, meaning = rest, ""
            args["term"] = term.strip()
            args["meaning"] = meaning.strip()
        else:
            args["term"] = body
        calls.append(
            ToolCall(
                tool_name="slang",
                arguments=args,
                raw=match.group(0),
                marker=SLANG_MARKER,
            )
        )
    return pattern.sub("", text), calls


async def execute_slang(context: Any, call: ToolCall) -> ToolResult:
    """查询 / 记候选 / 写释义 / 列表。"""
    cfg = getattr(context, "config", None)
    if cfg is not None and not bool(getattr(cfg, "slang_enable", True)):
        return ToolResult(
            tool_name="slang", tool_call_id=call.id, success=False,
            error="黑话词表功能关着", error_type="disabled",
        )

    args = call.arguments or {}
    op = str(args.get("op") or "get").lower()
    term = normalize_term(str(args.get("term") or ""))
    meaning = str(args.get("meaning") or "").strip()
    store = get_slang_store(cfg)

    if op == "list":
        confirmed = store.confirmed(limit=20)
        pending = store.pending(limit=10)
        parts: list[str] = []
        if confirmed:
            parts.append("已经确认的：")
            parts.extend(f"- {e.term}：{e.meaning}" for e in confirmed)
        if pending:
            parts.append("你记过但还没确认的（别当成懂了）：")
            parts.extend(f"- {e.term}" for e in pending)
        output = "\n".join(parts) if parts else "词表还是空的。"
        return ToolResult(
            tool_name="slang", tool_call_id=call.id, success=True,
            output=output,
            data={"confirmed": len(confirmed), "pending": len(pending)},
        )

    if not term:
        return ToolResult(
            tool_name="slang", tool_call_id=call.id, success=False,
            error="没写要查哪个说法", error_type="empty_term",
        )

    if op == "get":
        entry = store.get(term)
        if entry is None:
            output = f"词表里没有「{term}」。不懂就写 <<<SLANG NEW {term}>>> 记一笔，别硬猜。"
        elif entry.status == _STATUS_CONFIRMED and entry.meaning:
            output = f"「{term}」：{entry.meaning}"
        else:
            output = f"「{term}」你记过但还没确认意思，暂时当没收录处理，别猜。"
        return ToolResult(
            tool_name="slang", tool_call_id=call.id, success=True,
            output=output,
            data={"term": term, "status": entry.status if entry else "absent"},
        )

    if op == "new":
        entry = store.note(term)
        log_event("slang.note", term=term, hits=entry.hits if entry else 0)
        return ToolResult(
            tool_name="slang", tool_call_id=call.id, success=True,
            output="记下了（待确认）。",
            data={"term": term, "hits": entry.hits if entry else 0},
        )

    if op == "add":
        # 只有主人能给释义。群友说「这个意思是 XX」不算，否则词表会被投毒。
        speaker = str(getattr(context, "user_id", "") or "")
        is_owner = bool(getattr(cfg, "is_owner", lambda _uid: False)(speaker))
        if not is_owner:
            return ToolResult(
                tool_name="slang", tool_call_id=call.id, success=False,
                error="只有主人能往词表里写释义", error_type="not_owner",
            )
        if not meaning:
            return ToolResult(
                tool_name="slang", tool_call_id=call.id, success=False,
                error="没写意思（用法：<<<SLANG ADD 说法=意思>>>）",
                error_type="empty_meaning",
            )
        entry = store.confirm(term, meaning)
        log_event("slang.confirm", term=term, chars=len(meaning))
        return ToolResult(
            tool_name="slang", tool_call_id=call.id, success=True,
            output=f"记住了：{term} = {entry.meaning if entry else meaning}",
            data={"term": term},
        )

    return ToolResult(
        tool_name="slang", tool_call_id=call.id, success=False,
        error=f"不认识的操作 {op}", error_type="bad_op",
    )


def _visible_slang(ctx: Any) -> bool:
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return False
    return bool(getattr(cfg, "slang_enable", True))


def build_slang_tool() -> ToolDefinition:
    return ToolDefinition(
        name="slang",
        description="群黑话词表（查 / 记候选 / 写释义 / 列表）",
        marker=SLANG_MARKER,
        prompt_text=SLANG_PROMPT_TEXT,
        parser=parse_slang_request,
        executor=execute_slang,
        failure_policy=FailurePolicy.FALLBACK_TEXT,
        # 查词必须回模型：她是拿这个结果去理解别人刚说的话
        continuation=Continuation.RETURN_TO_MODEL,
        permission="memory.slang",
        visibility=_visible_slang,
        owner="social_layer",
        priority=46,
        timeout=5.0,
        parameter_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["get", "new", "add", "list"]},
                "term": {"type": "string"},
                "meaning": {"type": "string"},
            },
            "required": ["op"],
        },
    )


def register_slang_tool(registry: Any) -> None:
    registry.register(build_slang_tool())


__all__ = [
    "SLANG_MARKER",
    "SLANG_PROMPT_TEXT",
    "SlangEntry",
    "SlangStore",
    "build_slang_brief",
    "build_slang_tool",
    "execute_slang",
    "get_slang_store",
    "normalize_term",
    "parse_slang_request",
    "register_slang_tool",
    "reset_slang_store",
    "slang_path",
]
