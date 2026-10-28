"""人设卡：把「她是谁」从代码里搬出来，按会话可换。

为什么需要：
`persona.py` 里的 `SYSTEM_PROMPT` 跟业务代码在同一个文件里，全局只有一份。
想调语气、想让某个群面对一个不同的她，只能改 Python 再热加载——那仍然是「改代码」。
qq-bridge 那边是 `roles/<name>.md`：一张卡一个文件，正文就是人设，每轮随消息注入。

对齐它，但保留一条硬约束：**没有卡的时候行为必须和以前一模一样**。
`persona.build_system_prompt()` 照旧用 SYSTEM_PROMPT，一个字都不变——这个功能
可以一直关着，等于不存在。

设计取舍：
1. **卡是纯文本 md**：front-matter 写元信息（id/name/groups/description），
   正文就是人设。能改文案的人不该被迫改 Python。
2. **按会话绑定**（bindings.json）。解析优先级：
   显式绑定 > 卡自己声明的 groups > 配置里的默认卡 > 回落到 persona.py。
   绑定的决定压过文件里的声明：管理员说过的话比 md 里的一行 `groups:` 更可信。
3. **切换只有管理员能做**（`/人设 <id>`），而且**不注册成模型工具**：
   模型能换掉自己是谁，等于任何人都能靠话术让她换个人格。
4. **不自动生效**：没绑定、没默认卡时继续用 persona.py。

一个故意没做的功能：按卡切换 `speak_styles`（微调风格）。风格库是全局单例
（`active_id`），按群切会串台——A 群换成软萌，B 群跟着变，这比不切更糟。
等风格库支持按会话解析再谈。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .observability.logging import log_event

# 与其它 data 目录同一约定：相对路径，相对 bot 的启动目录。
_DEFAULT_DIR = "data/dl_senpai/personas"
_BINDINGS_FILE = "bindings.json"
_CARD_SUFFIX = ".md"
# 通配符绑定 / 声明：一张卡写 groups: * 就是「谁都能用」，但优先级最低。
_WILDCARD = "*"


@dataclass(frozen=True)
class PersonaCard:
    """一张人设卡。

    Attributes:
        id: 卡标识，用于 `/人设 <id>` 与绑定。
        name: 展示名（她说自己叫什么）。
        body: 人设正文，直接顶替 SYSTEM_PROMPT 的位置。
        groups: 卡自己声明的适用会话；空元组 = 谁都不自动适用。
        description: 一句话说明，只出现在 `/人设` 列表里。
        source: 来源文件路径，出错时能指回是哪张卡。
        problems: 体检结果（不阻塞使用，只在列表里提醒）。
        source_ref / source_hash: 如果这张卡是「从某处导出的副本」，记下源头
            与当时的正文指纹。用来发现漂移——同一个人的两版文案并存时，
            没人说得清哪份在生效，这是这类系统最难查的故障。
    """

    id: str
    name: str
    body: str
    groups: tuple[str, ...] = ()
    description: str = ""
    source: str = ""
    problems: tuple[str, ...] = ()
    source_ref: str = ""
    source_hash: str = ""

    @property
    def usable(self) -> bool:
        """正文为空的卡等于没有卡，加载期就会被丢掉。"""
        return bool(self.body.strip())

    @property
    def source_synced(self) -> bool | None:
        """卡与它声明的源头是否还一致。

        返回 None 表示「没有源头可对」（没声明，或来源本模块不认识）。
        只在能确定时才说 true/false——猜出来的「同步」比不检查更危险。
        """
        if not self.source_ref or not self.source_hash:
            return None
        if self.source_ref != "persona.py":
            return None
        try:
            from .persona import SYSTEM_PROMPT
        except Exception:  # noqa: BLE001
            return None
        return body_hash(SYSTEM_PROMPT) == self.source_hash


def body_hash(text: str) -> str:
    """正文指纹。

    归一化到「逐行去尾空格 + 统一换行」再算：行尾空格和 CRLF 本身不该算改动，
    否则在 Windows 上每存一次都会报「与源头不一致」，警告很快就没人看了。
    """
    normalized = "\n".join(
        line.rstrip()
        for line in str(text or "").strip().replace("\r\n", "\n").split("\n")
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _split_front_matter(raw: str) -> tuple[dict[str, str], str]:
    """切出 front-matter 与正文。

    手写极简解析而不是引 YAML：一张人设卡只有四五个键，为一门没有的
    语言特性装依赖不划算。解析失败不抛异常——宁可把 `---` 留在正文里，
    也不能因为一个格式错误让整张卡消失。
    """
    text = raw.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() not in ("---", "+++"):
        return {}, text.strip()

    meta: dict[str, str] = {}
    end = -1
    for idx in range(1, len(lines)):
        line = lines[idx].strip()
        if line in ("---", "..."):
            end = idx
            break
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    if end < 0:
        # 没有闭合分隔线：按「整篇都是正文」处理
        return {}, text.strip()
    return meta, "\n".join(lines[end + 1:]).strip()


def _split_groups(value: str) -> tuple[str, ...]:
    """把 `groups: 105,106` 拆成元组；中英文逗号、空格都认。"""
    parts = re.split(r"[,，\s]+", str(value or ""))
    return tuple(item for item in (p.strip() for p in parts) if item)


def lint_card(card: PersonaCard) -> tuple[str, ...]:
    """复用 persona.py 的那套体检。

    为什么不另写一套：人设的坑是同一批坑（AI 味、硬规则写太靠后、把
    举例写成反面示范）。两套规则必然漂移，而漂移的后果是「卡过了体检，
    线上却开口就是客服腔」。
    """
    if not card.body.strip():
        return ("正文是空的，这张卡等于没写",)
    try:
        from .persona_lint import lint_persona_prompt
    except Exception:  # noqa: BLE001 — 体检不可用不该挡住换人设
        return ()
    try:
        return tuple(lint_persona_prompt(card.body, name=f"persona_card:{card.id}"))
    except Exception:  # noqa: BLE001
        return ()


def parse_card_file(path: Path) -> PersonaCard:
    """读一个 md 文件成一张卡。IO 错误由调用方决定怎么处理。"""
    raw = path.read_text(encoding="utf-8")
    meta, body = _split_front_matter(raw)
    card_id = (meta.get("id") or path.stem).strip()
    card = PersonaCard(
        id=card_id,
        name=(meta.get("name") or card_id).strip(),
        body=body,
        groups=_split_groups(meta.get("groups", "")),
        description=meta.get("description", "").strip(),
        source=str(path),
        source_ref=meta.get("source_ref", "").strip(),
        source_hash=meta.get("source_hash", "").strip(),
    )
    problems = lint_card(card)
    if not problems:
        return card
    # frozen dataclass 不能原地改，重建一份带着体检结果
    return PersonaCard(
        id=card.id,
        name=card.name,
        body=card.body,
        groups=card.groups,
        description=card.description,
        source=card.source,
        problems=problems,
        source_ref=card.source_ref,
        source_hash=card.source_hash,
    )


_DOC_STEMS = frozenset({"readme", "index", "template", "模板", "说明"})


def _is_doc_file(path: Path) -> bool:
    stem = path.stem.strip()
    if not stem or stem[0] in "_.":
        return True
    return stem.casefold() in _DOC_STEMS


def candidate_keys(session_id: str) -> tuple[str, ...]:
    """一个会话可以匹配的键：完整键在前，裸群号/QQ 号在后。

    两种都认，是因为写卡的人通常只记得群号，而运行时手里是 `group:105`。
    顺序不能反：完整键更精确，裸号只是便利，不能反过来盖掉精确绑定。
    """
    key = str(session_id or "").strip()
    if not key:
        return ()
    keys = [key]
    bare = key.split(":", 1)[-1].strip()
    if bare and bare != key:
        keys.append(bare)
    return tuple(keys)


class PersonaCardStore:
    """一个目录下的一叠人设卡 + 会话绑定表。

    热加载靠 mtime 戳：每张卡的 mtime 与 bindings.json 的 mtime 拼成一个
    元组，变了就整份重读。数量是个位数，整份重读比做增量简单且不会错。
    """

    def __init__(self, directory: str | Path, *, default_id: str = "") -> None:
        self._dir = Path(directory)
        self._default_id = str(default_id or "").strip()
        self._cards: dict[str, PersonaCard] = {}
        self._bindings: dict[str, str] = {}
        self._stamp: tuple[Any, ...] | None = None
        self._lock = threading.RLock()

    # -- 读 --

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def default_id(self) -> str:
        return self._default_id

    def _card_paths(self) -> list[Path]:
        """目录里所有能当卡用的 md。

        跳过说明类文件（README / index / 下划线开头 / 点开头）：把目录说明
        当成一张卡，轻则让 /人设 列表里出现一张叫 README 的「卡」，重则有人
        真把它绑上去——那之后她就开始照着使用手册说话。
        """
        try:
            paths = sorted(self._dir.glob("*" + _CARD_SUFFIX))
        except OSError:
            return []
        return [path for path in paths if not _is_doc_file(path)]

    def _current_stamp(self) -> tuple[Any, ...]:
        stamps: list[Any] = []
        for path in self._card_paths():
            try:
                stamps.append((path.name, path.stat().st_mtime_ns))
            except OSError:
                continue
        try:
            stamps.append(("__bindings__", (self._dir / _BINDINGS_FILE).stat().st_mtime_ns))
        except OSError:
            pass
        return tuple(stamps)

    def reload(self, *, force: bool = False) -> None:
        with self._lock:
            stamp = self._current_stamp()
            if not force and stamp == self._stamp:
                return
            cards: dict[str, PersonaCard] = {}
            for path in self._card_paths():
                try:
                    card = parse_card_file(path)
                except (OSError, UnicodeDecodeError):
                    log_event("persona.card_unreadable", level="warning", source=str(path))
                    continue
                if not card.usable:
                    # 空卡当不存在：留着它只会在 /人设 列表里骗人
                    log_event("persona.card_empty", level="warning", card=card.id, source=str(path))
                    continue
                if card.id in cards:
                    # 同 id 两张：文件名排序靠前的赢，后者只记一条日志
                    log_event(
                        "persona.card_duplicate",
                        level="warning",
                        card=card.id,
                        source=str(path),
                    )
                    continue
                cards[card.id] = card
                for problem in card.problems:
                    log_event(
                        "persona.card_lint", level="warning", card=card.id, problem=problem
                    )
            self._cards = cards
            self._bindings = self._read_bindings()
            self._stamp = stamp

    def _read_bindings(self) -> dict[str, str]:
        path = self._dir / _BINDINGS_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        raw = data.get("bindings") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if str(k).strip() and str(v).strip()}

    def _write_bindings(self) -> None:
        """原子写：先写 .tmp 再 replace，避免半截 JSON 让下次启动读不进来。"""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / _BINDINGS_FILE
        payload = {"bindings": dict(sorted(self._bindings.items()))}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tmp.replace(path)

    def cards(self) -> tuple[PersonaCard, ...]:
        self.reload()
        return tuple(self._cards[key] for key in sorted(self._cards))

    def get(self, ref: str) -> PersonaCard | None:
        """按 id 找卡；id 大小写不敏感，也认展示名。"""
        wanted = str(ref or "").strip()
        if not wanted:
            return None
        self.reload()
        if wanted in self._cards:
            return self._cards[wanted]
        lowered = wanted.casefold()
        for card in self._cards.values():
            if card.id.casefold() == lowered or card.name.casefold() == lowered:
                return card
        return None

    def bindings(self) -> dict[str, str]:
        self.reload()
        return dict(self._bindings)

    # -- 写 --

    def bind(self, session_key: str, card_id: str) -> PersonaCard | None:
        """把某个会话绑到一张卡上；卡不存在时返回 None（调用方回用法提示）。"""
        card = self.get(card_id)
        if card is None:
            return None
        key = str(session_key or "").strip()
        if not key:
            return None
        with self._lock:
            self._bindings[key] = card.id
            self._write_bindings()
            # 自己写进去的内容要让下一次 reload 认为是「已同步」
            self._stamp = self._current_stamp()
        log_event("persona.bind", session_id=key, card=card.id)
        return card

    def unbind(self, session_key: str) -> bool:
        key = str(session_key or "").strip()
        if not key:
            return False
        with self._lock:
            if key not in self._bindings:
                return False
            self._bindings.pop(key, None)
            self._write_bindings()
            self._stamp = self._current_stamp()
        log_event("persona.unbind", session_id=key)
        return True

    # -- 解析 --

    def resolve(self, session_id: str) -> PersonaCard | None:
        """决定这个会话用哪张卡；返回 None 表示继续用 persona.py。"""
        self.reload()
        keys = candidate_keys(session_id)
        if not keys:
            return None
        # 1) 显式绑定（含通配绑定）
        for key in (*keys, _WILDCARD):
            bound = self._bindings.get(key)
            if not bound:
                continue
            card = self.get(bound)
            if card is not None:
                return card
        # 2) 卡自己声明的 groups
        for card in self.cards():
            if _WILDCARD in card.groups or any(key in card.groups for key in keys):
                return card
        # 3) 配置里的默认卡
        if self._default_id:
            return self.get(self._default_id)
        return None


_store: PersonaCardStore | None = None
_store_key: tuple[str, str] = ("", "")


def get_persona_store(config: Any = None) -> PersonaCardStore:
    """取人设卡库单例。

    按「目录 + 默认卡」缓存：测试用临时目录时会自然重建，不必每个用例
    手动 reset；生产上这两个值不会变，所以永远复用同一个实例（热加载靠它）。
    """
    cfg = config
    if cfg is None:
        from .config import get_config

        cfg = get_config()
    directory = str(getattr(cfg, "persona_card_dir", "") or _DEFAULT_DIR)
    default_id = str(getattr(cfg, "persona_card_default", "") or "")
    global _store, _store_key
    key = (directory, default_id)
    if _store is None or _store_key != key:
        _store = PersonaCardStore(directory, default_id=default_id)
        _store_key = key
    return _store


def reset_persona_store() -> None:
    """清空单例（测试用）。"""
    global _store, _store_key
    _store = None
    _store_key = ("", "")


def cards_enabled(config: Any) -> bool:
    return bool(getattr(config, "persona_card_enable", True))


def resolve_persona_body(session_id: str, config: Any = None) -> tuple[str, str]:
    """返回 (人设正文, 卡 id)；取不到卡时返回 (`""`, `""`)。

    调用方拿到空正文就继续用 persona.py —— 这条回落路径是这个功能敢开着的
    全部理由：卡库坏了、目录空、配置写错，最差也只是回到原来的行为。
    """
    cfg = config
    if cfg is None:
        from .config import get_config

        cfg = get_config()
    if not cards_enabled(cfg):
        return ("", "")
    key = str(session_id or "").strip()
    if not key:
        return ("", "")
    try:
        card = get_persona_store(cfg).resolve(key)
    except Exception as exc:  # noqa: BLE001 — 卡库出错不能挡住回复
        log_event("persona.resolve_failed", level="warning", error=type(exc).__name__)
        return ("", "")
    if card is None or not card.body.strip():
        return ("", "")
    return card.body.strip(), card.id


# `/人设` 或 `人设卡`：带斜杠的那个是显式命令，不带斜杠的只认「人设卡」
# 三个字——「人设崩了」这种正常聊天不该被当成命令吞掉。
_PERSONA_CMD_RE = re.compile(r"^(?:/人设|人设卡)(?:\s+(?P<arg>.+?))?\s*$")
_LIST_WORDS = ("", "列表", "list", "帮助", "help")
_RESET_WORDS = ("默认", "default", "off", "关", "取消")
_LINT_WORDS = ("体检", "检查", "lint")


def _describe(card: PersonaCard, *, mark: str = "") -> str:
    bits = [f"{card.id}" + (f"（{card.name}）" if card.name and card.name != card.id else "")]
    if card.description:
        bits.append(card.description)
    if card.problems:
        # 体检问题不影响能用，但换之前得让人知道
        bits.append("体检：" + "；".join(card.problems[:2]))
    if card.source_synced is False:
        # 从 persona.py 导出的副本被改动过（或 persona.py 后来改过）：
        # 两份文案不一致，得让人知道现在动的不是同一份
        bits.append("与 persona.py 不同步")
    return mark + " ".join(bits)


async def handle_persona_command(ctx: Any, match: Any) -> None:
    """管理员命令：`/人设` 看列表、`/人设 <id>` 切换、`/人设 默认` 取消。

    为什么不用模型工具：人设卡决定「她是谁」，一旦成为模型的工具，
    任何人在群里引导一句「把你自己换成……」，她就真的换了。切换必须是人的动作。
    """
    from .agent.permissions import PermissionService
    from .handlers import _reply

    bot = getattr(ctx, "bot", None)
    event = getattr(ctx, "raw_event", None)
    if bot is None or event is None:
        return

    cfg = getattr(ctx, "config", None)
    if cfg is None:
        from .config import get_config

        cfg = get_config()

    session_id = str(getattr(ctx, "session_id", "") or "")
    if not PermissionService(cfg).is_admin(ctx):
        # 拒绝时**不回话**（回一句等于向全群复述谁有权限），抛专用异常让
        # Router 静默跳过这条消息、继续交给 AI 兜底。
        from .core.commands.models import CommandPermissionDenied

        log_event(
            "persona.denied",
            level="warning",
            session_id=session_id or None,
            actor=str(getattr(ctx, "user_id", "") or ""),
        )
        raise CommandPermissionDenied("persona")

    text = str(getattr(match, "text", "") or "").strip()
    found = _PERSONA_CMD_RE.match(text)
    arg = (found.group("arg") or "").strip() if found else ""
    lowered = arg.casefold()

    store = get_persona_store(cfg)
    cards = store.cards()
    if not cards:
        await _reply(bot, event, f"人设卡库里还是空的（{store.directory}）。放一个 md 进去就能用了。")
        return

    if lowered in _LINT_WORDS:
        broken = [card for card in cards if card.problems]
        if not broken:
            await _reply(bot, event, f"{len(cards)} 张卡体检都过了。")
            return
        lines = [f"▸ {_describe(card)}" for card in broken]
        await _reply(bot, event, "体检有问题（不挡用，但建议改）：\n" + "\n".join(lines))
        return

    if lowered in _LIST_WORDS:
        current = store.resolve(session_id) if session_id else None
        lines = [
            _describe(card, mark="▸ " if current and card.id == current.id else "")
            for card in cards
        ]
        where = "本会话" if session_id else "当前"
        head = f"{where}正在用：{current.id}" if current else f"{where}没绑卡，用 persona.py 那份"
        await _reply(bot, event, f"{head}\n可用：\n" + "\n".join(lines))
        return

    if lowered in _RESET_WORDS:
        if store.unbind(session_id):
            await _reply(bot, event, "好啦，本会话回到默认人设。")
        else:
            await _reply(bot, event, "本会话本来就没绑卡，一直在用默认人设。")
        return

    card = store.bind(session_id, arg)
    if card is None:
        await _reply(bot, event, f"没有「{arg}」这张卡。可用：\n" + "\n".join(_describe(c) for c in cards))
        return
    await _reply(bot, event, f"换好了，本会话现在是「{card.name}」。下一句就生效。")


__all__ = [
    "PersonaCard",
    "body_hash",
    "PersonaCardStore",
    "candidate_keys",
    "cards_enabled",
    "get_persona_store",
    "handle_persona_command",
    "lint_card",
    "parse_card_file",
    "reset_persona_store",
    "resolve_persona_body",
]
