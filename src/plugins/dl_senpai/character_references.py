"""角色参考图库 + 网图可信度评估。

为什么本地库排在网络前面：

1. 稳定 —— 网上搜到的「艾雅法拉立绘」可能是同人、可能是别的角色、可能带水印；
   本地库是你自己确认过的那几张。
2. 快 —— 读盘是毫秒级，搜图要 20~35 秒（实测 image_find 单次 32~33 秒）。
3. 不浪费 —— 同一个角色反复画时，没必要每次都联网。

目录约定（V1，2026-10-27）：

    data/dl_senpai/character_references/
      arknights/
        eyjafjalla/
          manifest.json          # 必须：声明每张图的来源与是否已核验
          official_fullbody_01.png
          official_portrait_01.png

**没有 manifest 的图不算已验证参考图**。这是刻意的：上一阶段的教训是
「搜到一张图就用」比「不给参考图」更差——deviantart 上的同人图把艾雅法拉画成
棕发 + 精灵耳，images.edit 原样搬了过来，成品被视觉模型判为「野生二创」。
所以进强 reference 模式（images.edit）的图，必须过两道关：

    ① 人工在 manifest 里标 verified=true（来源可追踪）
    ② 运行时视觉模型校验设定不冲突（verify_reference）

只读不写：这里**不会**把网上搜到的图偷偷存进来——那等于用未核实的图污染参考库。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nonebot import logger

from .character_registry import CharacterProfile

# 认这些后缀就够：库里不该出现 webp/avif 这类生图接口不一定接受的格式。
_REF_SUFFIXES = (".png", ".jpg", ".jpeg")
# 单张上限：再大就不是"参考图"，是原画文件了，读进内存会拖慢整条链路。
_MAX_BYTES = 8 * 1024 * 1024
_MANIFEST_NAME = "manifest.json"

# 图片来源类型。语义必须准确：没确认过官方来源就不许写 official。
SOURCE_OFFICIAL = "official"
SOURCE_WIKI = "wiki"
SOURCE_DATABASE = "trusted_database"
SOURCE_FAN_ART = "fan_art"
SOURCE_SOCIAL = "social_media"
SOURCE_UNKNOWN = "unknown"

# 域名 → (类型, 可信度)。这是**启发式**，不是真理：域名判断只能给一个先验，
# 最终是否采信由 manifest 与视觉校验决定。所以没有把某个站写死成"绝对可信"。
_DOMAIN_HINTS: tuple[tuple[tuple[str, ...], str, float], ...] = (
    (("hypergryph.com", "mihoyo.com", "hoyolab.com", "crypton.co.jp", "sega.jp"), SOURCE_OFFICIAL, 1.0),
    (("prts.wiki", "arknights.wiki", "fandom.com", "wikipedia.org", "moegirl.org", "萌娘百科"), SOURCE_WIKI, 0.8),
    (("animecharactersdatabase", "myanimelist", "bangumi.tv"), SOURCE_DATABASE, 0.7),
    (("deviantart.com", "wixmp.com", "pixiv.net", "danbooru", "gelbooru", "yande.re",
      "konachan", "zerochan.net", "huaban.com", "duitang.com"), SOURCE_FAN_ART, 0.2),
    (("weibo.com", "tieba.baidu.com", "bilibili.com", "hdslb.com", "xiaohongshu.com",
      "twitter.com", "x.com", "reddit.com", "facebook.com", "pinterest."), SOURCE_SOCIAL, 0.3),
)


# reference_role：这张图适合拿来锚定什么。
# 为什么需要：一张半身官方立绘当「全身图」的参考，构图会被强行拉成半身；
# 肖像头像用全身图同理。第一版只预留字段 + 极简规则，不做智能选择。
ROLE_IDENTITY = "identity"
ROLE_FULLBODY = "fullbody"
ROLE_PORTRAIT = "portrait"
_KNOWN_ROLES = (ROLE_IDENTITY, ROLE_FULLBODY, ROLE_PORTRAIT)


@dataclass(frozen=True)
class ReferenceEntry:
    """manifest 里的一条参考图声明。"""

    file: str
    type: str = "verified_reference"
    verified: bool = False
    priority: int = 0
    notes: str = ""
    source_url: str = ""
    source_type: str = ""
    # 完整性：sha256 是「这张图没被人换过」的唯一证据。
    # 只靠 verified=true 是不够的——文件被替换后清单照样说 verified。
    sha256: str = ""
    verification_method: str = ""
    verified_at: str = ""
    role: str = ROLE_IDENTITY

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReferenceEntry | None:
        name = str(data.get("file") or "").strip()
        if not name:
            return None
        try:
            priority = int(data.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        role = str(data.get("reference_role") or data.get("role") or ROLE_IDENTITY).strip().lower()
        return cls(
            file=name,
            type=str(data.get("type") or "verified_reference").strip(),
            verified=bool(data.get("verified")),
            priority=priority,
            notes=str(data.get("notes") or "").strip(),
            source_url=str(data.get("source_url") or "").strip(),
            source_type=str(data.get("source_type") or "").strip(),
            sha256=str(data.get("sha256") or "").strip().lower(),
            verification_method=str(data.get("verification_method") or "").strip(),
            verified_at=str(data.get("verified_at") or "").strip(),
            role=role if role in _KNOWN_ROLES else ROLE_IDENTITY,
        )

    def as_dict(self) -> dict[str, Any]:
        """写回 manifest 用。"""
        out: dict[str, Any] = {
            "file": self.file,
            "type": self.type,
            "verified": self.verified,
            "priority": self.priority,
            "reference_role": self.role,
        }
        for key, value in (
            ("sha256", self.sha256),
            ("verification_method", self.verification_method),
            ("verified_at", self.verified_at),
            ("source_type", self.source_type),
            ("source_url", self.source_url),
            ("notes", self.notes),
        ):
            if value:
                out[key] = value
        return out


@dataclass(frozen=True)
class CharacterManifest:
    """一个角色目录的 manifest。"""

    character: str = ""
    localized_name: str = ""
    franchise: str = ""
    verified: bool = False
    references: tuple[ReferenceEntry, ...] = ()

    def verified_entries(self) -> tuple[ReferenceEntry, ...]:
        """只返回人工核验过的条目，按 priority 降序。"""
        items = [e for e in self.references if e.verified]
        items.sort(key=lambda e: e.priority, reverse=True)
        return tuple(items)


@dataclass(frozen=True)
class ReferenceCandidate:
    """一张候选参考图（本地库或网图）及其可信度画像。"""

    filename: str
    raw: bytes = b""
    source_url: str = ""
    source_domain: str = ""
    source_type: str = SOURCE_UNKNOWN
    source_trust: float = 0.0
    verified: bool = False        # 是否人工核验（本地库 manifest）
    priority: int = 0
    role: str = ROLE_IDENTITY
    integrity_ok: bool = True
    visual_score: float | None = None
    identity_score: float | None = None
    issue: str = ""

    def as_pair(self) -> tuple[str, bytes]:
        return self.filename, self.raw

    def describe(self) -> str:
        return (
            f"file={self.filename} type={self.source_type} trust={self.source_trust:.2f} "
            f"verified={self.verified} priority={self.priority}"
        )


# ---------------- 本地库 ----------------


def library_root(config: Any) -> Path:
    raw = str(getattr(config, "character_reference_dir", "") or "data/dl_senpai/character_references")
    return Path(raw)


def profile_dir(config: Any, profile: CharacterProfile) -> Path:
    return library_root(config) / profile.slug


def load_manifest(config: Any, profile: CharacterProfile) -> CharacterManifest | None:
    """读角色目录的 manifest.json。缺失或损坏都返回 None（不抛）。"""
    path = profile_dir(config, profile) / _MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 — 清单坏了不该让生图挂掉
        logger.warning(f"dl_senpai character manifest unreadable path={path}: {exc}")
        return None
    if not isinstance(raw, dict):
        return None
    entries = tuple(
        e for e in (ReferenceEntry.from_dict(x) for x in raw.get("references") or [] if isinstance(x, dict)) if e
    )
    return CharacterManifest(
        character=str(raw.get("character") or "").strip(),
        localized_name=str(raw.get("localized_name") or "").strip(),
        franchise=str(raw.get("franchise") or "").strip(),
        verified=bool(raw.get("verified")),
        references=entries,
    )


def sha256_of(raw: bytes) -> str:
    """图的指纹。导入、比对、防替换都用它。"""
    return hashlib.sha256(raw).hexdigest()


def _looks_like_image(raw: bytes) -> bool:
    """按文件头判断是不是真图。

    为什么不用 image_gen._sniff_image_ext：那个函数是「猜扩展名」用的，
    认不出来时**兜底返回 .png**，永远为真——拿它当校验会把坏文件放进生图请求。
    """
    if raw.startswith(b"\x89PNG") or raw.startswith(b"\xff\xd8\xff"):
        return True
    return raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"


def list_references(config: Any, profile: CharacterProfile) -> list[Path]:
    """目录里所有像参考图的文件（不看 manifest，仅用于统计/排查）。"""
    directory = profile_dir(config, profile)
    if not directory.is_dir():
        return []
    return [
        p
        for p in sorted(directory.iterdir())
        if p.is_file() and p.suffix.lower() in _REF_SUFFIXES and p.stat().st_size <= _MAX_BYTES
    ]


def count_references(config: Any, profile: CharacterProfile) -> int:
    return len(list_references(config, profile))


def count_verified_references(config: Any, profile: CharacterProfile) -> int:
    """人工核验过的参考图数量（manifest 里有、文件也在）。"""
    manifest = load_manifest(config, profile)
    if manifest is None:
        return 0
    directory = profile_dir(config, profile)
    return sum(
        1
        for e in manifest.verified_entries()
        if (directory / e.file).is_file()
    )


def _role_preference(user_request: str) -> tuple[str, ...]:
    """按用户话里的构图线索排个序（极简规则，不做智能选择）。

    头像/半身 → 肖像图更合适；全身/立绘 → 全身图更合适。
    都没有线索就按 identity 优先、其余按 priority 排。
    """
    text = user_request or ""
    if re.search(r"头像|肖像|半身|胸像|portrait|headshot", text, re.I):
        return (ROLE_PORTRAIT, ROLE_IDENTITY, ROLE_FULLBODY)
    if re.search(r"全身|立绘|站姿|全身像|full", text, re.I):
        return (ROLE_FULLBODY, ROLE_IDENTITY, ROLE_PORTRAIT)
    return (ROLE_IDENTITY, ROLE_FULLBODY, ROLE_PORTRAIT)


def verify_reference_integrity(
    directory: Path, entry: ReferenceEntry
) -> tuple[bool, str, bytes | None]:
    """校验一张参考图是否还是清单里那一张。返回 (ok, issue, bytes)。

    为什么必须校验：manifest 说 verified=true 只证明「当初」核验过。
    文件被换成另一张图（手滑覆盖、同步冲突、误放同人图）后，清单是不会变脸的，
    于是系统会拿着未核实的图继续喂 images.edit。hash 比对是唯一的防线。
    """
    path = directory / entry.file
    if not path.is_file():
        return False, "file_missing", None
    try:
        raw = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        return False, f"unreadable:{type(exc).__name__}", None
    if len(raw) > _MAX_BYTES:
        return False, "too_large", None
    if not _looks_like_image(raw):
        return False, "not_an_image", None
    if entry.sha256:
        actual = sha256_of(raw)
        if actual != entry.sha256:
            # 这条日志的字符串是契约的一部分：trace 会原样记录 reference_integrity_failed
            return False, "reference_integrity_failed", None
    return True, "", raw


def resolve_library_reference(
    config: Any,
    profile: CharacterProfile,
    *,
    require_verified: bool = True,
    user_request: str = "",
    diagnostics: list[str] | None = None,
) -> ReferenceCandidate | None:
    """取本地库里最优先的一张可用参考图。

    require_verified=True（默认）时只认 manifest 里 verified=true 的条目：
    没有人工核验的图不该进 images.edit——上一阶段实测的教训。
    """
    directory = profile_dir(config, profile)
    manifest = load_manifest(config, profile)

    entries: list[ReferenceEntry] = []
    if manifest is not None:
        entries = list(manifest.verified_entries() if require_verified else manifest.references)
        if not require_verified:
            entries.sort(key=lambda e: e.priority, reverse=True)
    if not entries:
        if require_verified:
            # 有图但没核验：留痕，否则「为什么不用本地图」很难查
            if list_references(config, profile):
                logger.info(
                    f"dl_senpai character reference library ignored character={profile.canonical_name} "
                    f"reason=no_verified_manifest"
                )
            return None
        # 未要求核验时退回目录扫描（旧行为）
        loose = list_references(config, profile)
        if not loose:
            return None
        entries = [ReferenceEntry(file=p.name, verified=False) for p in loose]

    # 角色偏好排序：同样的 priority 下，构图匹配的那张先上
    pref = _role_preference(user_request)
    entries = sorted(
        entries,
        key=lambda e: (pref.index(e.role) if e.role in pref else len(pref), -e.priority),
    )

    for entry in entries:
        ok, issue, raw = verify_reference_integrity(directory, entry)
        if not ok or raw is None:
            logger.warning(
                f"dl_senpai character reference unusable character={profile.canonical_name} "
                f"file={entry.file} issue={issue}"
            )
            if diagnostics is not None:
                diagnostics.append(f"{entry.file}:{issue}")
            continue
        candidate = ReferenceCandidate(
            filename=entry.file,
            raw=raw,
            source_type=entry.source_type or ("verified_reference" if entry.verified else SOURCE_UNKNOWN),
            source_trust=1.0 if entry.verified else 0.5,
            verified=entry.verified,
            priority=entry.priority,
            role=entry.role,
            integrity_ok=True,
        )
        logger.info(
            f"dl_senpai character reference from library character={profile.canonical_name} "
            f"{candidate.describe()} bytes={len(raw)}"
        )
        return candidate
    return None


def find_library_reference(config: Any, profile: CharacterProfile) -> tuple[str, bytes] | None:
    """兼容旧调用：返回 (文件名, 字节)。现在只返回人工核验过的图。"""
    candidate = resolve_library_reference(config, profile)
    return candidate.as_pair() if candidate is not None else None


# ---------------- 网图来源可信度 ----------------


def classify_source(url: str) -> tuple[str, str, float]:
    """(域名, 类型, 可信度)。判断不了就是 unknown/0.2——不要假装知道。"""
    raw = str(url or "").strip()
    domain = raw.split("//")[-1].split("/")[0].split("@")[-1].lower()
    if not domain:
        return "", SOURCE_UNKNOWN, 0.0
    for keywords, source_type, trust in _DOMAIN_HINTS:
        if any(k in domain for k in keywords):
            return domain, source_type, trust
    return domain, SOURCE_UNKNOWN, 0.2


def candidate_from_url(url: str, filename: str, raw: bytes) -> ReferenceCandidate:
    domain, source_type, trust = classify_source(url)
    return ReferenceCandidate(
        filename=filename,
        raw=raw,
        source_url=str(url or ""),
        source_domain=domain,
        source_type=source_type,
        source_trust=trust,
        verified=False,
        # 优先级只用于排序展示：网图一律排在本地核验图之后
        priority=0,
    )


# ---------------- 设定一致性校验 ----------------

_VERIFY_PROMPT = """这是角色「{name}」，来自《{franchise}》。官方关键特征：
{facts}

请判断这张图能不能当她生图的**身份参考图**：画风不限、姿势不限，但人物设定必须与上面一致。
如果出现明显冲突（例如官方是猫耳却画成精灵耳、发色完全不同），就不能用。

只输出 JSON，不要别的话：
{{"ok": true/false, "score": 0.0-1.0, "issue": "不符之处，没问题就留空"}}"""


async def verify_reference(
    config: Any, profile: CharacterProfile, raw: bytes
) -> tuple[bool, str, float | None]:
    """下载到网图后先验一遍：这张图配不配当她的身份参考图。

    返回 (ok, issue, score)。

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
        return True, "", None

    if not text:
        return True, "", None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return True, "", None
    try:
        data = json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return True, "", None
    ok = bool(data.get("ok", True))
    issue = str(data.get("issue") or "").strip()
    score: float | None = None
    try:
        if data.get("score") is not None:
            score = max(0.0, min(1.0, float(data["score"])))
    except (TypeError, ValueError):
        score = None
    if not ok:
        logger.info(
            f"dl_senpai character reference rejected character={profile.canonical_name} "
            f"issue={issue[:80]!r}"
        )
    return ok, issue, score


# ---------------- 档案与图库一致性检查 ----------------

def validate_character_profile(
    config: Any, profile: CharacterProfile
) -> list[str]:
    """检查档案与参考图库之间明显的结构性冲突，返回问题描述列表。

    为什么是「返回问题」而不是「自动修」：档案是人工维护的事实来源，图库是
    人工核验的素材来源，两者打架时到底哪个对，只有人能判断。程序擅自选一个，
    等于制造一个没人知道从哪来的设定。所以这里只产出 warning 给管理员看。

    检查项（都是能确定判断的结构性问题）：
    - manifest 声明了某张图，文件却不在
    - manifest 的 character / franchise 和档案对不上（目录放错角色）
    - manifest.verified=true 但档案本身还是 draft（容易出现"有图无档"）
    - 目录里有图但没 manifest（来源不可追踪）
    """
    issues: list[str] = []
    directory = profile_dir(config, profile)
    manifest = load_manifest(config, profile)

    if manifest is None:
        if list_references(config, profile):
            issues.append(
                f"character={profile.canonical_name} 目录里有参考图但没有 {_MANIFEST_NAME}，"
                f"来源不可追踪（不会被当作已验证参考图使用）"
            )
        return issues

    if manifest.character and manifest.character.lower() != profile.canonical_name.lower():
        issues.append(
            f"character 不一致: manifest={manifest.character} profile={profile.canonical_name}"
        )
    if manifest.franchise and profile.franchise and manifest.franchise.lower() != profile.franchise.lower():
        issues.append(
            f"franchise 不一致: manifest={manifest.franchise} profile={profile.franchise}"
        )

    for entry in manifest.references:
        if not (directory / entry.file).is_file():
            issues.append(f"manifest 声明的文件不存在: {entry.file}")

    if manifest.verified and profile.effective_status() == "draft":
        issues.append(
            f"character={profile.canonical_name} 参考图已核验，但档案仍是 draft——"
            f"建议按 README 核验字段后再升 profile_status"
        )
    return issues


# ---------------- 图库导入与清单写回 ----------------
#
# 只由 scripts/character_reference.py 调用（人工导入流程）。运行时**只读**：
# 生图链路永远不该往图库里写东西，否则会出现「搜到一张同人图 → 自动入库 →
# 下次当成已验证参考图」这种自我污染。

_MAX_NAME_SEQ = 99


def _manifest_path(config: Any, profile: CharacterProfile) -> Path:
    return profile_dir(config, profile) / _MANIFEST_NAME


def _load_raw_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 — 清单坏了，让调用方决定是否重建
        raise ValueError(f"manifest 无法解析: {exc}") from exc


def _save_raw_manifest(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _next_filename(directory: Path, prefix: str, role: str, suffix: str) -> str:
    """挑一个没被占用的编号文件名。不覆盖、不猜。"""
    for seq in range(1, _MAX_NAME_SEQ + 1):
        candidate = f"{prefix}_{role}_{seq:02d}{suffix}"
        if not (directory / candidate).exists():
            return candidate
    raise ValueError(f"{directory} 里 {prefix}_{role}_* 编号用尽了（>{_MAX_NAME_SEQ}）")


def add_reference_to_library(
    config: Any,
    profile: CharacterProfile,
    raw: bytes,
    *,
    original_name: str = "",
    role: str = ROLE_IDENTITY,
    priority: int = 100,
    verified: bool = False,
    method: str = "",
    source_url: str = "",
    source_type: str = "",
    notes: str = "",
    at: str = "",
) -> tuple[bool, str]:
    """把一张本地图导入参考库并登记进 manifest。返回 (ok, 说明)。

    拒绝的三种情况，都是踩过的坑：
    - 不是真图片（改名的截图、占位文件）→ 会在 images.edit 处 400；
    - 超过 8MB → 读进内存拖慢整条链路；
    - sha256 已存在 → 同一张图被编号成 ref_01/ref_02/ref_03，清单越来越长
      但信息量没变，还会让优先级排序失去意义。
    """
    if not raw:
        return False, "文件是空的"
    if len(raw) > _MAX_BYTES:
        return False, f"文件过大: {len(raw)} bytes > {_MAX_BYTES}"
    if not _looks_like_image(raw):
        return False, "不是有效图片（只认 PNG/JPEG/WEBP 文件头）"
    if role not in _KNOWN_ROLES:
        return False, f"未知 reference_role: {role}（可用: {', '.join(_KNOWN_ROLES)}）"

    directory = profile_dir(config, profile)
    manifest_path = _manifest_path(config, profile)
    data = _load_raw_manifest(manifest_path)
    entries = [x for x in data.get("references") or [] if isinstance(x, dict)]

    digest = sha256_of(raw)
    for item in entries:
        if str(item.get("sha256") or "").strip().lower() == digest:
            return False, f"reference already exists: {item.get('file')}（sha256 相同）"

    suffix = Path(original_name).suffix.lower()
    if suffix not in _REF_SUFFIXES:
        suffix = ".png"
    prefix = "official" if (source_type or "").strip().lower() == SOURCE_OFFICIAL else "ref"
    try:
        filename = _next_filename(directory, prefix, role, suffix)
    except ValueError as exc:
        return False, str(exc)

    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_bytes(raw)

    entry = {
        "file": filename,
        "type": (f"official_{role}" if prefix == "official" else "verified_reference"),
        "verified": bool(verified),
        "priority": int(priority),
        "reference_role": role,
        "sha256": digest,
    }
    if verified:
        entry["verification_method"] = (method or "human_review").strip()
        if at:
            entry["verified_at"] = at.strip()
    if source_type:
        entry["source_type"] = source_type.strip()
    if source_url:
        entry["source_url"] = source_url.strip()
    if notes:
        entry["notes"] = notes.strip()

    entries.append(entry)
    data.setdefault("character", profile.canonical_name)
    data.setdefault("localized_name", profile.localized_names[0] if profile.localized_names else "")
    data.setdefault("franchise", profile.franchise)
    data["references"] = entries
    # 顶层 verified 只在「至少一张图核验过」时为真，别让它跟条目状态脱节
    data["verified"] = any(bool(x.get("verified")) for x in entries)
    _save_raw_manifest(manifest_path, data)
    state = "verified=true" if verified else "verified=false（导入但未核验，不会被用于 images.edit）"
    return True, f"{filename} → {state} sha256={digest[:12]}…"


def set_reference_verified(
    config: Any,
    profile: CharacterProfile,
    filename: str,
    *,
    verified: bool,
    method: str = "",
    note: str = "",
    at: str = "",
) -> tuple[bool, str]:
    """人工核验/撤销某张参考图。撤销不删文件，只把 verified 打回 false。"""
    manifest_path = _manifest_path(config, profile)
    data = _load_raw_manifest(manifest_path)
    entries = [x for x in data.get("references") or [] if isinstance(x, dict)]
    target = None
    for item in entries:
        if str(item.get("file") or "") == filename:
            target = item
            break
    if target is None:
        return False, f"manifest 里没有 {filename}"

    if verified:
        path = profile_dir(config, profile) / filename
        if not path.is_file():
            return False, f"文件不存在: {filename}"
        raw = path.read_bytes()
        if not _looks_like_image(raw):
            return False, f"不是有效图片: {filename}"
        digest = sha256_of(raw)
        # 核验即锁指纹：以后文件被换掉，sha256 对不上会被拒绝使用
        if target.get("sha256") and str(target["sha256"]).lower() != digest:
            return False, f"文件与清单里的 sha256 不一致（文件被改过）: {filename}"
        target["sha256"] = digest
        target["verification_method"] = (method or "human_review").strip()
        if at:
            target["verified_at"] = at.strip()
    else:
        target.pop("verified_at", None)
        target["verification_method"] = ""
    target["verified"] = bool(verified)
    if note:
        target["notes"] = note.strip()

    data["references"] = entries
    data["verified"] = any(bool(x.get("verified")) for x in entries)
    _save_raw_manifest(manifest_path, data)
    return True, f"{filename} → verified={str(bool(verified)).lower()}"


def reference_integrity_report(config: Any, profile: CharacterProfile) -> list[dict[str, Any]]:
    """逐条检查图库：文件在不在、头对不对、大小、sha256 是否一致。

    给 CLI 用，也给排查用。返回每条的检查结果，不修改任何文件。
    """
    directory = profile_dir(config, profile)
    manifest = load_manifest(config, profile)
    if manifest is None:
        return []
    report: list[dict[str, Any]] = []
    for entry in manifest.references:
        ok, issue, raw = verify_reference_integrity(directory, entry)
        report.append(
            {
                "file": entry.file,
                "verified": entry.verified,
                "role": entry.role,
                "priority": entry.priority,
                "sha256": entry.sha256[:12],
                "size": len(raw) if raw else 0,
                "ok": ok,
                "issue": issue,
            }
        )
    return report


__all__ = [
    "ROLE_FULLBODY",
    "ROLE_IDENTITY",
    "ROLE_PORTRAIT",
    "add_reference_to_library",
    "reference_integrity_report",
    "set_reference_verified",
    "sha256_of",
    "verify_reference_integrity",
    "SOURCE_DATABASE",
    "SOURCE_FAN_ART",
    "SOURCE_OFFICIAL",
    "SOURCE_SOCIAL",
    "SOURCE_UNKNOWN",
    "SOURCE_WIKI",
    "CharacterManifest",
    "ReferenceCandidate",
    "ReferenceEntry",
    "candidate_from_url",
    "classify_source",
    "count_references",
    "count_verified_references",
    "find_library_reference",
    "library_root",
    "list_references",
    "load_manifest",
    "profile_dir",
    "resolve_library_reference",
    "validate_character_profile",
    "verify_reference",
]
