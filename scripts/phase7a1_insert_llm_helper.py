"""向 llm.py 插入 _instrument_model_call 定义（Phase 7A.1）。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "plugins" / "dl_senpai" / "llm.py"

HELPER = '''def _instrument_model_call(
    resp: Any,
    *,
    model: str,
    provider: str,
    latency_ms: float,
    retry_count: int = 0,
) -> None:
    """记录一次模型调用（Phase 7A.1 第二十七~三十节）。

    **唯一 accounting boundary**（第二十八节）：
        这里位于 `_complete_once` —— 真正发 HTTP 请求的地方。
        `ModelGateway.chat()` 只是转发到 `SenpaiLLM.chat()`，
        因此**不得**再记一次，否则同一次调用被统计两遍。

    第三十节：provider 未返回 usage 时记 None（unknown），
    **不用字符数估算冒充 API token**。

    失败静默：观测绝不影响模型调用。
    """
    try:
        from .validation.telemetry import ModelCallTelemetry, record_model_call

        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            total_tokens = usage.get("total_tokens")

        # 仅当 provider 给了 prompt/completion 但没给 total 时才补算总量；
        # 三者都缺 → 保持 None，不猜。
        if (
            total_tokens is None
            and prompt_tokens is not None
            and completion_tokens is not None
        ):
            total_tokens = prompt_tokens + completion_tokens

        record_model_call(
            ModelCallTelemetry(
                model=str(model or ""),
                provider=str(provider or ""),
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated=False,  # provider-reported，不是估算
                status="ok",
                retry_count=int(retry_count or 0),
            )
        )
    except Exception:  # noqa: BLE001 — 观测失败绝不影响模型调用
        pass


'''


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")

    if "def _instrument_model_call(" in src:
        print("已定义，跳过")
        return 0

    anchor = "def truncate_reply(text: str, max_chars: int) -> str:"
    if anchor not in src:
        print("FAIL: 找不到锚点 truncate_reply")
        return 1

    src = src.replace(anchor, HELPER + anchor, 1)
    TARGET.write_text(src, encoding="utf-8")
    print("已插入 _instrument_model_call 定义")
    print("BOM:", TARGET.read_bytes()[:3] == b"\xef\xbb\xbf")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
