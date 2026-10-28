"""图片归一化：发给模型 / 发进群之前，先把图收拾干净。

搬自 qq-bridge 的 media-normalize 层。三条约束都是真实故障点：

1. **长边 1280 封顶**。QQ 原图动辄 3000~4000px，几 MB。视觉模型吃不下那么
   大的图，中转站按字节计费，超限还会直接 400——而失败时她只会说「图看不清」，
   没人知道是尺寸问题。
2. **动图抽帧拼图**。视觉模型只吃到第一帧，一张 gif 等于只看了一眼开头。
   抽 6 帧拼成网格，一次就能看到动作全过程，而计费仍然是一张图。
3. **体积上限 + 质量步进**。压到 JPEG 超限就降质量，还超就再缩边长；
   实在不行才放弃，但**永远返回可用的字节**。

一条不变式：任何一步失败都退回原始字节。宁可发一张大图，也不能因为图处理
崩掉整条回复——用户看到的是「学姐这轮没说话」，比图糊严重得多。
"""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path
from typing import Any

from .observability.logging import log_event

# 默认值刻意保守：1280 长边、1.5MB。QQ 群图在这个尺寸下清晰度足够，
# 视觉模型也不会因为太大而降采样到看不清字。
DEFAULT_MAX_SIDE = 1280
DEFAULT_MAX_BYTES = 1_500_000
# 动图最多抽这么多帧：再多网格就太密，每格小到认不出
DEFAULT_MAX_FRAMES = 6
# JPEG 质量步进的档位。60 已经能看清文字，再往下就该缩尺寸而不是继续压质量。
_QUALITY_STEPS = (85, 75, 65, 60)

_DATA_URL_RE = re.compile(r"^data:(image/[\w+.-]+);base64,(.*)$", re.IGNORECASE | re.DOTALL)


def snapshot(raw: bytes) -> dict[str, Any]:
    """读一张图的元信息。读不动就返回空 dict（例如不是图片）。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            frames = int(getattr(img, "n_frames", 1) or 1)
            return {
                "width": img.width,
                "height": img.height,
                "format": (img.format or "").upper(),
                "frames": frames,
                "bytes": len(raw),
                "animated": frames > 1,
            }
    except Exception:  # noqa: BLE001 — 读不出来当「不是图」处理
        return {}


def is_animated(raw: bytes) -> bool:
    return bool(snapshot(raw).get("animated"))


def _to_rgb(img: Any) -> Any:
    """统一转 RGB。

    PNG 的透明通道和 P 模式调色板是重压时最常见的两个坑：直接存 JPEG 会报
    「cannot write mode RGBA as JPEG」。透明区域填白而不是填黑——填黑会让
    表情包边缘出现一圈脏边。
    """
    from PIL import Image

    if img.mode in ("RGB", "L"):
        return img.convert("RGB")
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return img.convert("RGB")


def _encode_jpeg(img: Any, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _shrink_to_fit(img: Any, max_bytes: int) -> bytes:
    """质量 + 尺寸双重步进，直到塞进 max_bytes。"""
    from PIL import Image

    current = img
    for quality in _QUALITY_STEPS:
        data = _encode_jpeg(current, quality)
        if len(data) <= max_bytes:
            return data
    # 质量到底还超：按面积比例缩边长，最多缩三轮（每轮 0.75）
    for _ in range(3):
        new_size = (
            max(1, int(current.width * 0.75)),
            max(1, int(current.height * 0.75)),
        )
        current = current.resize(new_size, Image.LANCZOS)
        data = _encode_jpeg(current, _QUALITY_STEPS[-1])
        if len(data) <= max_bytes:
            return data
    return _encode_jpeg(current, _QUALITY_STEPS[-1])


def normalize_image_bytes(
    raw: bytes,
    *,
    max_side: int = DEFAULT_MAX_SIDE,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> bytes:
    """压到长边 max_side、体积 max_bytes 以内。失败原样返回。"""
    if not raw:
        return raw
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            frames = int(getattr(img, "n_frames", 1) or 1)
            # 动图单独走拼图：否则下面只处理第一帧，动作就丢了
            if frames > 1:
                return gif_to_grid_bytes(raw, max_frames=DEFAULT_MAX_FRAMES, max_side=max_side, max_bytes=max_bytes)
            if img.width <= max_side and img.height <= max_side and len(raw) <= max_bytes:
                return raw  # 已经够小，不动它（重编码必然掉画质）
            work = _to_rgb(img)
            if work.width > max_side or work.height > max_side:
                work.thumbnail((max_side, max_side), Image.LANCZOS)
            return _shrink_to_fit(work, max_bytes)
    except Exception as exc:  # noqa: BLE001 — 归一化失败绝不能影响主链路
        log_event("media.normalize_failed", error=f"{type(exc).__name__}: {exc}"[:160])
        return raw


def gif_to_grid_bytes(
    raw: bytes,
    *,
    max_frames: int = DEFAULT_MAX_FRAMES,
    max_side: int = DEFAULT_MAX_SIDE,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> bytes:
    """把动图抽帧拼成一张网格图。

    为什么是网格而不是「只取中间一帧」：猫摔跤那种动图，中间帧往往是最糊的
    那一帧；网格能同时表达「起手—过程—结果」，看的人（模型）才读得出发生了什么。
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            frames: list[Any] = []
            total = int(getattr(img, "n_frames", 1) or 1)
            if total <= 1:
                return normalize_image_bytes(raw, max_side=max_side, max_bytes=max_bytes)
            step = max(1, total // max_frames)
            for index in range(0, total, step):
                if len(frames) >= max_frames:
                    break
                try:
                    img.seek(index)
                    frames.append(_to_rgb(img.copy()))
                except EOFError:
                    break
            if not frames:
                return raw

            count = len(frames)
            cols = 2 if count <= 4 else 3
            rows = (count + cols - 1) // cols
            cell = max(1, max_side // cols)
            # 每格等比缩到 cell，再按实际行列尺寸拼（避免留大片黑边）
            thumbs = []
            for frame in frames:
                copy = frame.copy()
                copy.thumbnail((cell, cell), Image.LANCZOS)
                thumbs.append(copy)
            cell_w = max(t.width for t in thumbs)
            cell_h = max(t.height for t in thumbs)
            canvas = Image.new("RGB", (cols * cell_w, rows * cell_h), (255, 255, 255))
            for index, thumb in enumerate(thumbs):
                x = (index % cols) * cell_w + (cell_w - thumb.width) // 2
                y = (index // cols) * cell_h + (cell_h - thumb.height) // 2
                canvas.paste(thumb, (x, y))
            return _shrink_to_fit(canvas, max_bytes)
    except Exception as exc:  # noqa: BLE001
        log_event("media.grid_failed", error=f"{type(exc).__name__}: {exc}"[:160])
        return raw


def guess_mime(raw: bytes) -> str:
    """按文件头猜 MIME。归一化之后一律是 JPEG，但原始字节可能是 PNG。"""
    if raw[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw[:3] == b"GIF":
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def normalize_data_url(
    data_url: str,
    *,
    max_side: int = DEFAULT_MAX_SIDE,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """把 base64 data URL 归一化后重新拼回去。

    非 data URL（普通 http 链接）原样返回：那类图没法不下载就压，
    而下载会多一次真实网络请求，代价比收益大。
    """
    text = str(data_url or "")
    match = _DATA_URL_RE.match(text)
    if not match:
        return text
    try:
        raw = base64.b64decode(match.group(2), validate=False)
    except Exception:  # noqa: BLE001 — 解不开就原样交回去
        return text
    normalized = normalize_image_bytes(raw, max_side=max_side, max_bytes=max_bytes)
    if normalized is raw or normalized == raw:
        return text
    encoded = base64.b64encode(normalized).decode("ascii")
    return f"data:{guess_mime(normalized)};base64,{encoded}"


def normalize_data_url_configured(data_url: str) -> str:
    """按配置归一化 data URL（视觉管线用）。

    配置读不到就按默认值压——这条链路在「图太大被中转站拒了」时是唯一的
    补救机会，不该因为读配置失败就整段跳过。
    """
    max_side, max_bytes = DEFAULT_MAX_SIDE, DEFAULT_MAX_BYTES
    try:
        from .config import get_config

        cfg = get_config()
        if not bool(getattr(cfg, "media_normalize_enable", True)):
            return data_url
        max_side = int(getattr(cfg, "media_max_side", DEFAULT_MAX_SIDE) or DEFAULT_MAX_SIDE)
        max_bytes = int(getattr(cfg, "media_max_bytes", DEFAULT_MAX_BYTES) or DEFAULT_MAX_BYTES)
    except Exception:  # noqa: BLE001
        pass
    return normalize_data_url(data_url, max_side=max_side, max_bytes=max_bytes)


def normalize_file(
    path: str | Path,
    *,
    out_dir: str | Path | None = None,
    max_side: int = DEFAULT_MAX_SIDE,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Path:
    """把本地图片文件归一化，返回可发送的路径。

    能原样用就原样用（不写盘）；需要压缩时才在 out_dir 里落一份 jpg。
    out_dir 默认与源文件同目录，后缀换成 .norm.jpg。
    """
    src = Path(path)
    try:
        raw = src.read_bytes()
    except OSError:
        return src
    normalized = normalize_image_bytes(raw, max_side=max_side, max_bytes=max_bytes)
    if normalized == raw:
        return src
    target_dir = Path(out_dir) if out_dir else src.parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{src.stem}.norm.jpg"
        target.write_bytes(normalized)
        return target
    except OSError as exc:
        log_event("media.write_failed", error=f"{type(exc).__name__}: {exc}"[:160])
        return src


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_FRAMES",
    "DEFAULT_MAX_SIDE",
    "gif_to_grid_bytes",
    "guess_mime",
    "is_animated",
    "normalize_data_url",
    "normalize_data_url_configured",
    "normalize_file",
    "normalize_image_bytes",
    "snapshot",
]
