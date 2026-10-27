"""生图链路的结构化追踪（ImageGenerationTrace）。

为什么必须有：出图"不像"是最难查的一类问题——用户只会说"画得不对"，
而链路上有六个环节都可能背锅：

    角色没识别 / Prompt 改写丢角色 / 参考图没加载 / 模型映射异常 /
    参考图下载失败 / 模型自己漂移

没有 trace 时只能靠猜。这里把每一步的**输入输出摘要**串成一条记录，
一次生图一条，日志里带上同一个 trace_id，事后能逐段对上。

安全约束：任何字段落日志前都过 _redact，绝不写出 API Key、Bearer 头、
带签名的 URL 参数。debug 模式额外落盘，也只写同一套脱敏后的字段。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nonebot import logger

from .observability.logging import log_event

# 常见密钥形态：sk-xxx / Bearer xxx / key=xxx / token=xxx。
# 每条规则都要说清楚「哪部分是秘密」：只盖住秘密本体，前缀（sk-、Bearer ）
# 留着，日志里才能看出这里原本是什么，而不至于整段变成 *** 丢掉上下文。
# 宁可多盖一层，也不要哪天把中转站的 key 写进日志。
_SECRET_PATTERNS = (
    # 整段就是秘密（sk-...），前后缀都算秘密
    (re.compile(r"sk-[A-Za-z0-9_\-]{6,}"), False),
    # Bearer <token>：前缀 Bearer 保留
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{6,}"), False),
    # api_key=xxx / token=xxx：键名保留
    (re.compile(r"(?i)\b(?:api[_-]?key|key|token|secret)\s*[=:]\s*[A-Za-z0-9._\-]{6,}"), False),
    # URL 查询参数 ?api_key=xxx：参数名保留
    (re.compile(r"(?i)[?&](?:key|api_key|token|access_token)=[^&\s]{6,}"), False),
)

# 盖掉值时保留的「键名」前缀，匹配到之后按第一个分隔符切
_KEY_PREFIX_RE = re.compile(r"^([^=:?&\s]*(?:[=:]|[?&][a-z_]+=))", re.IGNORECASE)


def _mask(match: re.Match[str]) -> str:
    """保留键名、盖掉值。

    为什么不像常规做法那样保留第一个分组：分组内容本身就是秘密
    （sk-xxxx 的整体是 key），保留它等于没脱敏——这个错犯过一次。
    """
    text = match.group(0)
    prefix = _KEY_PREFIX_RE.match(text)
    if prefix and prefix.group(1) != text:
        return prefix.group(1) + "***"
    return "***"


def redact(text: str) -> str:
    """把疑似密钥替换成 ***。"""
    out = str(text or "")
    for pattern, _keep in _SECRET_PATTERNS:
        out = pattern.sub(_mask, out)
    return out


def base_url_host(url: str) -> str:
    """只留 host（去掉协议、路径、凭据）。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    host = raw.split("//")[-1].split("/")[0]
    if "@" in host:  # user:pass@host
        host = host.split("@")[-1]
    return host


@dataclass
class ImageGenerationTrace:
    """一次生图的完整轨迹。"""

    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    original_prompt: str = ""          # 用户原话（永不删除）
    intent: str = "general_image"      # known_character_generation / general_image
    character: str = ""
    franchise: str = ""
    character_confidence: float = 0.0
    character_reason: str = ""         # 凭什么认出/没认出
    reference_mode: str = "none"       # user_image / local_verified / web_verified / web_probe_only / web_rejected / none
    reference_count: int = 0
    reference_source: str = ""         # 例如 character_library / web:deviantart.com
    reference_verified: bool = False   # 这张参考图是否人工核验过（本地库 manifest）
    reference_source_trust: float = 0.0
    # 档案可信度快照：提示词里的硬约束是不是有依据，看这两个字段
    character_profile_status: str = ""
    verified_fields: tuple[str, ...] = ()
    hint_fields: tuple[str, ...] = ()
    # 身份评分（只测量，不触发重画）
    identity_score: float | None = None
    identity_dimensions: dict[str, float | None] = field(default_factory=dict)
    identity_issues: tuple[str, ...] = ()
    identity_verdict: str = ""
    configured_model: str = ""
    resolved_model: str = "unknown"    # 中转站 alias 无法确认真实后端
    provider: str = ""
    base_url_host: str = ""
    size: str = ""
    quality: str = ""
    final_prompt: str = ""
    generation_mode: str = "text_only"  # edit / reference / text_only
    result: str = "pending"            # success / no_image / failed / skipped
    error: str = ""
    started_at: float = field(default_factory=time.monotonic)
    elapsed_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def finish(self, result: str, *, error: str = "") -> ImageGenerationTrace:
        self.result = result
        self.error = redact(error)[:300]
        self.elapsed_ms = (time.monotonic() - self.started_at) * 1000.0
        return self

    def as_fields(self) -> dict[str, Any]:
        """日志字段：全部脱敏，长度截断。"""
        fields: dict[str, Any] = {
            "trace_id": self.trace_id,
            "intent": self.intent,
            "original_prompt": redact(self.original_prompt)[:200],
            "character": self.character or None,
            "franchise": self.franchise or None,
            "character_confidence": round(self.character_confidence, 2) or None,
            "character_reason": self.character_reason or None,
            "reference_mode": self.reference_mode,
            "reference_count": self.reference_count,
            "reference_source": self.reference_source or None,
            "reference_verified": self.reference_verified or None,
            "reference_source_trust": round(self.reference_source_trust, 2) or None,
            "character_profile_status": self.character_profile_status or None,
            "verified_fields": list(self.verified_fields) or None,
            "hint_fields": list(self.hint_fields) or None,
            "identity_score": round(self.identity_score, 3) if self.identity_score is not None else None,
            "identity_dimensions": {k: (round(v, 3) if isinstance(v, (int, float)) else v)
                                    for k, v in self.identity_dimensions.items()} or None,
            "identity_issues": list(self.identity_issues) or None,
            "identity_verdict": self.identity_verdict or None,
            "configured_model": self.configured_model or None,
            "resolved_model": self.resolved_model,
            "provider": self.provider or None,
            "base_url_host": self.base_url_host or None,
            "generation_mode": self.generation_mode,
            "final_prompt_chars": len(self.final_prompt or ""),
            "final_prompt": redact(self.final_prompt)[:400] or None,
            "result": self.result,
            "error": self.error or None,
            "elapsed_ms": round(self.elapsed_ms),
        }
        if self.extra:
            fields["extra"] = self.extra
        return {k: v for k, v in fields.items() if v is not None}

    def log(self, *, event: str = "draw.trace", level: str = "info") -> None:
        log_event(event, level=level, **self.as_fields())


def new_trace(config: Any, *, original_prompt: str) -> ImageGenerationTrace:
    """按当前配置建一条 trace（模型/渠道信息一次填好）。"""
    def _get(name: str, default: Any = "") -> Any:
        try:
            return getattr(config, name, default)
        except Exception:  # noqa: BLE001
            return default

    trace = ImageGenerationTrace(original_prompt=(original_prompt or "").strip())
    try:
        trace.configured_model = str(config.draw_model_resolved())
    except Exception:  # noqa: BLE001
        trace.configured_model = str(_get("draw_model", ""))
    trace.provider = str(_get("llm_provider", "") or "")
    try:
        trace.base_url_host = base_url_host(str(config.draw_base_url_resolved()))
    except Exception:  # noqa: BLE001
        trace.base_url_host = base_url_host(str(_get("draw_base_url", "")))
    trace.size = str(_get("draw_size", "") or "")
    trace.quality = str(_get("draw_quality", "") or "")
    return trace


def write_debug_dump(config: Any, trace: ImageGenerationTrace) -> None:
    """debug 模式下把整条 trace 落盘（仍是脱敏后的字段）。"""
    if not bool(getattr(config, "image_debug", False)):
        return
    path = Path(str(getattr(config, "image_trace_file", "") or "data/dl_senpai/draw_traces.jsonl"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(trace.as_fields())
        payload["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — 调试输出失败绝不能影响出图
        logger.debug(f"dl_senpai draw trace dump skipped: {exc}")
    # debug 打开时再往普通日志里打一条完整的（仍然脱敏）
    logger.info(json.dumps(trace.as_fields(), ensure_ascii=False)[:1500])


__all__ = [
    "ImageGenerationTrace",
    "base_url_host",
    "new_trace",
    "redact",
    "write_debug_dump",
]
