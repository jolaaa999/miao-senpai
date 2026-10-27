"""别名角色参考图库：本地目录优先，找不到再上网找。

为什么本地库排在网络前面：

1. 稳定 —— 网上搜到的「艾雅法拉立绘」可能是同人、可能是别的角色、可能带水印；
   本地库是你自己确认过的那几张。
2. 快 —— 读盘是毫秒级，搜图要 6~8 秒（实测）。
3. 不浪费 —— 同一个角色反复画时，没必要每次都联网。

目录约定（与规格一致）：

    data/dl_senpai/character_references/
      arknights/
        eyjafjalla/
          profile.json        (可选，覆盖包内档案)
          ref_01.png
          ref_02.png

只读不写：这里**不会**把网上搜到的图偷偷存进来——那等于用未核实的图污染
参考库。要入库请人工确认后放进去。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from nonebot import logger

from .character_registry import CharacterProfile

# 认这些后缀就够：库里不该出现 webp/avif 这类生图接口不一定接受的格式。
_REF_SUFFIXES = (".png", ".jpg", ".jpeg")
# 单张上限：再大就不是"参考图"，是原画文件了，读进内存会拖慢整条链路。
_MAX_BYTES = 8 * 1024 * 1024


def library_root(config: Any) -> Path:
    raw = str(getattr(config, "character_reference_dir", "") or "data/dl_senpai/character_references")
    return Path(raw)


def profile_dir(config: Any, profile: CharacterProfile) -> Path:
    return library_root(config) / profile.slug


def list_references(config: Any, profile: CharacterProfile) -> list[Path]:
    """按文件名排序返回可用的参考图（ref_00 优先，顺序稳定）。"""
    directory = profile_dir(config, profile)
    if not directory.is_dir():
        return []
    items = [
        p
        for p in sorted(directory.iterdir())
        if p.is_file() and p.suffix.lower() in _REF_SUFFIXES and p.stat().st_size <= _MAX_BYTES
    ]
    return items


def _looks_like_image(raw: bytes) -> bool:
    """按文件头判断是不是真图。

    为什么不用 image_gen._sniff_image_ext：那个函数是「猜扩展名」用的，
    认不出来时**兜底返回 .png**，永远为真——拿它当校验会把坏文件放进生图请求。
    """
    if raw.startswith(b"\x89PNG") or raw.startswith(b"\xff\xd8\xff"):
        return True
    return raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"


def find_library_reference(config: Any, profile: CharacterProfile) -> tuple[str, bytes] | None:
    """取库里第一张能读的图，返回 (文件名, 字节)。没有就 None。"""
    for path in list_references(config, profile):
        try:
            raw = path.read_bytes()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"dl_senpai character reference unreadable path={path}: {exc}")
            continue
        if not _looks_like_image(raw):
            # 后缀对但内容不是图（改名过的截图、占位文件），跳过
            logger.warning(f"dl_senpai character reference not an image path={path}")
            continue
        logger.info(
            f"dl_senpai character reference from library character={profile.canonical_name} "
            f"file={path.name} bytes={len(raw)}"
        )
        return path.name, raw
    return None


def count_references(config: Any, profile: CharacterProfile) -> int:
    return len(list_references(config, profile))


# ---------------- 参考图可信度校验 ----------------

_VERIFY_PROMPT = """这是角色「{name}」，来自《{franchise}》。官方关键特征：
{facts}

请判断这张图能不能当她生图的**身份参考图**：画风不限、姿势不限，但人物设定必须与上面一致。
如果出现明显冲突（例如官方是猫耳却画成精灵耳、发色完全不同），就不能用。

只输出 JSON，不要别的话：
{{"ok": true/false, "issue": "不符之处，没问题就留空"}}"""


async def verify_reference(
    config: Any, profile: CharacterProfile, raw: bytes
) -> tuple[bool, str]:
    """下载到网图后先验一遍：这张图配不配当她的身份参考图。

    为什么必须验（2026-10-27 实测证据）：同一个「画一张艾雅法拉」跑两次，
    - 纯文本（无参考图）：视觉模型判定「可识别为艾雅法拉，羊角/银白浅紫长发/
      罗德岛黑制服/火山法杖均符合」；
    - 用 deviantart 搜到的同人图做图生图：判定「头发呈棕色、出现官方设定里
      不存在的尖长精灵耳，属于野生二创」——**比不找参考图还差**。
    图生图会把参考图的错误设定原样搬过来，所以网图必须先过这道关。

    fail-open：校验本身出错（模型没回、网络抖动）时放行，因为它只是增强项，
    不能因为验证器故障就让整条参考图链路失效。本地库与用户上传的图不走这里。
    """
    try:
        from .draw_enhance import _chat, _data_url

        facts = "\n".join(f"- {line}" for line in profile.visual_identity.lines()) or "- （暂无结构化特征）"
        if profile.negative_notes:
            facts += "\n必须避免：" + "；".join(profile.negative_notes)
        prompt = _VERIFY_PROMPT.format(
            name=profile.display_name,
            franchise=profile.franchise_display,
            facts=facts,
        )
        text = await _chat(
            config,
            [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _data_url(raw)}},
            ],
            max_tokens=200,
        )
    except Exception as exc:  # noqa: BLE001 — 校验是增强项，故障即放行
        logger.warning(f"dl_senpai character reference verify skipped: {type(exc).__name__}: {exc}")
        return True, ""

    if not text:
        return True, ""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return True, ""
    try:
        data = json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return True, ""
    ok = bool(data.get("ok", True))
    issue = str(data.get("issue") or "").strip()
    if not ok:
        logger.info(
            f"dl_senpai character reference rejected character={profile.canonical_name} "
            f"issue={issue[:80]!r}"
        )
    return ok, issue


__all__ = [
    "count_references",
    "verify_reference",
    "find_library_reference",
    "library_root",
    "list_references",
    "profile_dir",
]
