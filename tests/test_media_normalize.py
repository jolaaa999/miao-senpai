"""图片归一化：尺寸 / 体积 / 动图拼帧。"""

from __future__ import annotations

import base64
import io

from PIL import Image

from dl_senpai.media_normalize import (
    gif_to_grid_bytes,
    guess_mime,
    is_animated,
    normalize_data_url,
    normalize_file,
    normalize_image_bytes,
    snapshot,
)


def _png(size=(64, 48), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _big_jpeg(size=(3000, 2000), noise: bool = False) -> bytes:
    """造一张真正压不小的图。

    纯色图哪怕 3000px 也只有几十 KB，测不出「按体积步进」那条路——所以
    这里用逐像素随机噪点，JPEG 压不动它。
    """
    import random

    rnd = random.Random(7)
    img = Image.new("RGB", size)
    if noise:
        pixels = [
            (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            for _ in range(size[0] * size[1])
        ]
        img.putdata(pixels)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_snapshot_reads_metadata():
    info = snapshot(_png())
    assert info["width"] == 64
    assert info["height"] == 48
    assert info["format"] == "PNG"
    assert info["animated"] is False


def test_snapshot_of_garbage_is_empty():
    assert snapshot(b"not an image") == {}


def test_small_image_is_returned_unchanged():
    raw = _png()
    # 已经够小：必须原样返回，重编码只会掉画质
    assert normalize_image_bytes(raw) is raw


def test_large_image_is_shrunk_to_max_side():
    raw = _big_jpeg((3000, 2000))
    out = normalize_image_bytes(raw, max_side=1280, max_bytes=5_000_000)
    info = snapshot(out)
    assert out != raw
    assert max(info["width"], info["height"]) <= 1280
    assert info["format"] == "JPEG"


def test_byte_budget_is_enforced():
    raw = _big_jpeg((1600, 1200), noise=True)
    assert len(raw) > 200_000
    out = normalize_image_bytes(raw, max_side=1280, max_bytes=120_000)
    assert len(out) <= 120_000


def test_rgba_png_does_not_crash():
    buf = io.BytesIO()
    Image.new("RGBA", (2000, 1500), (10, 20, 30, 100)).save(buf, format="PNG")
    out = normalize_image_bytes(buf.getvalue(), max_side=800)
    assert snapshot(out)["format"] == "JPEG"


def test_garbage_returns_original():
    raw = b"\x00\x01\x02 not really an image"
    assert normalize_image_bytes(raw) == raw


def _animated_gif(frames: int = 4) -> bytes:
    images = [Image.new("RGB", (80, 60), (i * 40 % 255, 100, 200)) for i in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, format="GIF", save_all=True, append_images=images[1:], duration=100)
    return buf.getvalue()


def test_is_animated_detects_gif():
    assert is_animated(_animated_gif()) is True
    assert is_animated(_png()) is False


def test_gif_becomes_single_frame_grid():
    raw = _animated_gif(6)
    out = gif_to_grid_bytes(raw, max_frames=6, max_side=1280)
    info = snapshot(out)
    # 拼图的结果一定是单帧静态图：模型只看得到一帧，拼完才看得全
    assert info["frames"] == 1
    assert info["format"] == "JPEG"


def test_normalize_image_bytes_routes_animated_to_grid():
    out = normalize_image_bytes(_animated_gif(4))
    assert snapshot(out)["frames"] == 1


def test_normalize_data_url_roundtrip():
    raw = _big_jpeg((2400, 1600))
    url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    out = normalize_data_url(url, max_side=800, max_bytes=400_000)
    assert out.startswith("data:image/jpeg;base64,")
    payload = base64.b64decode(out.split(",", 1)[1])
    assert max(snapshot(payload)["width"], snapshot(payload)["height"]) <= 800


def test_normalize_data_url_leaves_http_urls_alone():
    url = "https://example.com/a.png"
    assert normalize_data_url(url) == url


def test_normalize_data_url_tolerates_broken_base64():
    url = "data:image/png;base64,!!!!not-base64!!!!"
    # 解不开就原样交回去：这条链路不许因为一张坏图崩掉
    assert normalize_data_url(url).startswith("data:image/png;base64,")


def test_guess_mime():
    assert guess_mime(_png()) == "image/png"
    assert guess_mime(b"GIF89a") == "image/gif"
    assert guess_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"


def test_normalize_file_writes_smaller_copy(tmp_path):
    src = tmp_path / "big.jpg"
    src.write_bytes(_big_jpeg((2600, 1800)))
    out = normalize_file(src, out_dir=tmp_path / "out")
    assert out.exists()
    assert out.suffix == ".jpg"
    assert max(snapshot(out.read_bytes())["width"], snapshot(out.read_bytes())["height"]) <= 1280


def test_normalize_file_returns_source_when_already_small(tmp_path):
    src = tmp_path / "small.png"
    src.write_bytes(_png())
    assert normalize_file(src) == src
