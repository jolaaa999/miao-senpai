"""学姐的日记本：记忆是「关于你的事」，日记是「她自己的一天」。

搬自 ai-companion-kit 的 packages/companion/src/diary.js + diary-author.js，
按学姐这套 Python 结构重写。两处关键设计原样保留：

1. **日记不进记忆账本**。记忆有硬上限（每人 16 条特征、8 条瞬间），
   写满就会被顶掉；日记天天写，一年几百条。混在一个账本里，
   最后一定是「她记得昨天酸奶酸了」却「忘了你是谁」。所以单独存。

2. **注入 prompt 只喂最近 3 条**。日记是「她昨天干了什么」，
   不是第二本记忆账本。全塞进去既超 token 预算，也会让她每轮都在
   回味旧事、接不上眼前的话题。

为什么写日记要单独一条提示词，不复用 llm.chat()：
    对话是「接一句、回一句」的形状；日记是独白——没人在跟她说话，
    她得自己找话说。用对话的 prompt 去写，出来的必然是汇报腔
    （「今天和群友交流了很多」）。所以这里另起一份要求
    「1~4 句、第一人称、别写总结句」的提示词。
"""

from __future__ import annotations

import hashlib
import math
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

try:
    from nonebot import logger
except Exception:  # noqa: BLE001 — 单测/独立脚本可能没有 nonebot

    class _Logger:
        def warning(self, msg: str) -> None:
            print(msg)

        def info(self, msg: str) -> None:
            print(msg)

        def exception(self, msg: str) -> None:
            print(msg)

    logger = _Logger()  # type: ignore[assignment]


# 项目根目录下的默认落盘位置（src/plugins/dl_senpai/diary.py → 上溯三级）
_DEFAULT_DIR = Path(__file__).resolve().parents[3] / "data" / "dl_senpai" / "diary"

# 换着写，别每次从同一个角度开口。
# 原项目用这份清单解决了「天天都是今天真是充实的一天」——不是靠要求
# 「别重复」，而是靠给她一个具体的方向。九条，一周内不会撞。
DIARY_ANGLES: tuple[str, ...] = (
    "窗外 / 天气 / 光线",
    "你自己身上的小毛病（懒、嘴硬、爱较真……挑一个）",
    "一件特别小的事：吃的、喝的、看到的东西",
    "突然想起以前的一件事",
    "你嘴上没说出口的那句话",
    "你偷偷干的事",
    "某样让你烦的东西",
    "今天想他但没说的那部分",
    "你正在等的东西",
)

# 单条日记的硬上限：超了截断而不是报错——日记是自娱自乐的东西，
# 不值得为长度把整条丢掉。
_DEFAULT_MAX_CHARS = 600
# 正文里不允许出现的东西：Markdown 结构。她要写的是随手记，不是文档。
_MARKDOWN_RE = re.compile(r"^(#{1,6}\s|[-*+]\s|\d+[.、]\s)", re.MULTILINE)
_WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _clean_line(text: Any, *, max_len: int) -> str:
    """压成单行：日记正文允许换行，但标题/情绪/标签必须是一行。"""
    s = re.sub(r"[\r\n\t]+", " ", str(text or "").strip())
    s = re.sub(r"\s{2,}", " ", s)
    if max_len > 0 and len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _clock_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


# 日记分档与寿命（2026-10-25 加，抄自小黑盒「大肥鱼优化篇2」的思路）。
#
# 为什么要有档位：以前日记只追加不遗忘，几个月后注入的还是同一批旧条目，
# 人设改了、语气变了，老日记却一直在把旧口吻往回拉——线上表现就是
# 「之前的聊天记录又记得，又很混乱」。人会淡忘小事、记得大事，这里照抄这个比例：
#   普通 7 天（小事小记）→ 重要 21 天 → 刻骨铭心 90 天
# 寿命一律从写下那天算绝对时长，不跟「最后一次被想起」走：
# 翻看不续命，只有明确延长才加天数（否则一条破事会被反复翻到永久）。
TIER_NORMAL = "普通"
TIER_IMPORTANT = "重要"
TIER_UNFORGETTABLE = "刻骨铭心"
TIER_PARKED = "搁着"

TIER_DAYS = {TIER_NORMAL: 7, TIER_IMPORTANT: 21, TIER_UNFORGETTABLE: 90}
TIER_EXTEND_DAYS = {TIER_NORMAL: 3, TIER_IMPORTANT: 7, TIER_UNFORGETTABLE: 30}
# 剩余天数低于它就在「快到期」清单里提醒（按档位分频，见 due_soon）
TIER_WARN_DAYS = {TIER_NORMAL: 2, TIER_IMPORTANT: 5, TIER_UNFORGETTABLE: 30}
_TIER_ALIASES = {
    "普通": TIER_NORMAL, "一般": TIER_NORMAL, "normal": TIER_NORMAL,
    "重要": TIER_IMPORTANT, "important": TIER_IMPORTANT,
    "刻骨铭心": TIER_UNFORGETTABLE, "非常重要": TIER_UNFORGETTABLE,
    "unforgettable": TIER_UNFORGETTABLE,
    "搁着": TIER_PARKED, "搁置": TIER_PARKED, "parked": TIER_PARKED,
}


def normalize_tier(value: Any) -> str:
    """把外部传进来的档位名收成四种之一。认不出来就按「普通」算。"""
    key = str(value or "").strip()
    return _TIER_ALIASES.get(key, _TIER_ALIASES.get(key.lower(), TIER_NORMAL))


@dataclass
class DiaryEntry:
    """一条日记。字段对应原项目 diary.add() 接受的对象。"""

    text: str
    created_at: float = 0.0
    title: str = ""
    mood: str = ""
    tags: list[str] = field(default_factory=list)
    # 这条是按哪个「开口角度」写的。存下来是为了下次挑角度时避开，
    # 否则九条角度会被随机数反复撞到同一条。
    angle: str = ""
    # -- 遗忘机制（2026-10-25）--
    tier: str = TIER_NORMAL
    # 当前档位下的总寿命天数（延长会在它上面累加）。
    # 0 表示「用档位默认值」，这样旧数据不用迁移。
    life_days: int = 0
    # 软删除时间：划掉不删数据，只标记；所有注入与查询都过滤它。
    deleted_at: float = 0.0
    # 上次提醒的时间，用来按档位控制提醒频率（普通 1 天一次、重要 3 天、铭心 7 天）。
    warned_at: float = 0.0
    # 稳定 id：注入视图只给 id，要看正文得按 id 调展开
    uid: str = ""

    @property
    def day(self) -> str:
        return _day_of(self.created_at or time.time())

    @property
    def entry_id(self) -> str:
        """稳定 id：优先用存下来的，老数据按「时间+正文」算一个不冲突的短哈希。"""
        if self.uid:
            return self.uid
        seed = f"{int(self.created_at)}|{self.text[:60]}"
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]

    @property
    def total_life_days(self) -> int:
        """这条日记能活多少天。搁着 = 无限（返回 0 表示冻结）。"""
        if self.tier == TIER_PARKED:
            return 0
        base = TIER_DAYS.get(self.tier, TIER_DAYS[TIER_NORMAL])
        return max(base, int(self.life_days or base))

    @property
    def is_parked(self) -> bool:
        return self.tier == TIER_PARKED

    @property
    def is_deleted(self) -> bool:
        return bool(self.deleted_at)

    def days_left(self, now: float | None = None) -> int:
        """还剩几天。冻结/搁着的返回一个很大的数（永不衰减）。"""
        if self.is_parked or self.is_deleted:
            return 9999
        total = self.total_life_days
        if total <= 0:
            return 9999
        elapsed = ((now if now is not None else time.time()) - (self.created_at or 0)) / 86400.0
        # 向上取整：还剩 0.9 天要显示「还剩 1 天」，用 int() 截断会算成 0 天
        # 从而直接被判过期——一条刚写下 6 天的日记当场被忘掉就是这个原因。
        return int(math.ceil(total - elapsed))

    def life_ratio(self, now: float | None = None) -> float:
        """剩余寿命比例。用来决定注入时给多清楚（见 DiaryStore.context）。"""
        total = self.total_life_days
        if self.is_parked or total <= 0:
            return 1.0
        left = self.days_left(now)
        return max(0.0, min(1.0, left / float(total)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.created_at,
            "day": self.day,
            "title": self.title,
            "mood": self.mood,
            "tags": list(self.tags),
            "angle": self.angle,
            "text": self.text,
            "tier": self.tier,
            "life_days": self.life_days,
            "deleted_at": self.deleted_at,
            "warned_at": self.warned_at,
            "id": self.entry_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiaryEntry:
        tags_raw = data.get("tags") or []
        return cls(
            text=str(data.get("text") or "").strip(),
            created_at=float(data.get("t") or data.get("created_at") or 0),
            title=str(data.get("title") or "").strip(),
            mood=str(data.get("mood") or "").strip(),
            tags=[str(t).strip() for t in tags_raw if str(t).strip()]
            if isinstance(tags_raw, list)
            else [],
            angle=str(data.get("angle") or "").strip(),
            tier=normalize_tier(data.get("tier")),
            life_days=int(data.get("life_days") or 0),
            deleted_at=float(data.get("deleted_at") or 0),
            warned_at=float(data.get("warned_at") or 0),
            uid=str(data.get("id") or data.get("uid") or "").strip(),
        )


class DiaryStore:
    """两份落盘：一份机器读（JSONL 只追加），一份人读（每天一个 md）。

    为什么同时要两份：JSONL 好读好算，但人要翻的时候一屏 JSON 很痛苦；
    md 按天分文件，用记事本直接打开就能看。原项目就是这么干的，
    而且这个「人能直接翻」的属性，是她愿意写日记的前提之一。
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self.dir = Path(directory) if directory else _DEFAULT_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    # -- 路径 --
    @property
    def index_path(self) -> Path:
        return self.dir / "index.jsonl"

    def day_path(self, day: str) -> Path:
        return self.dir / f"{day}.md"

    # -- 写 --
    def add(self, entry: DiaryEntry | dict[str, Any] | str) -> DiaryEntry:
        """追加一条。三种入参都收：她生成的 dict、手工写的字符串、已有对象。"""
        if isinstance(entry, str):
            item = DiaryEntry(text=entry.strip(), created_at=time.time())
        elif isinstance(entry, dict):
            item = DiaryEntry.from_dict(entry)
        else:
            item = entry

        if not item.text.strip():
            raise ValueError("日记正文是空的")
        if not item.created_at:
            item.created_at = time.time()

        with self._lock:
            # 落盘失败要抛出来：调用方（own_life / 命令）需要知道「没写成」，
            # 而不是以为写进去了。
            with self.index_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
            self._rewrite_day_file(item.day)

        logger.info(
            f"dl_senpai diary added day={item.day} title={item.title!r} "
            f"chars={len(item.text)}"
        )
        return item

    # -- 读 --
    def entries(self, *, include_deleted: bool = False) -> list[DiaryEntry]:
        """读全部。坏行跳过，不让一行脏数据把整本日记读废。

        默认过滤掉「划掉」的条目（回收站）：它们还在文件里，但任何注入、
        任何统计都不该再看见它们。
        """
        if not self.index_path.exists():
            return []
        out: list[DiaryEntry] = []
        try:
            raw = self.index_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(f"dl_senpai diary read failed: {exc}")
            return []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                entry = DiaryEntry.from_dict(data)
                if entry.text and (include_deleted or not entry.is_deleted):
                    out.append(entry)
        return out

    # -- 遗忘、延期、回收站（2026-10-25）--
    def find(self, entry_id: str) -> DiaryEntry | None:
        """按 id 找一条（含回收站里的）。"""
        target = str(entry_id or "").strip()
        if not target:
            return None
        for e in self.entries(include_deleted=True):
            if e.entry_id == target:
                return e
        return None

    def _save_all(self, rows: list[DiaryEntry]) -> None:
        """整本重写 index.jsonl。

        为什么不是「追加一行修改记录」：日记条数很少（一天几条），整本重写
        成本和一次追加差不多，换来的是文件里没有同一 id 的两份状态，
        读的时候不需要做「取最后一条」的合并逻辑。
        """
        with self._lock:
            tmp = self.index_path.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for e in rows:
                    f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
            tmp.replace(self.index_path)
            for day in {e.day for e in rows}:
                self._rewrite_day_file(day)

    def _mutate(self, entry_id: str, fn: Any, *, log: str = "") -> DiaryEntry | None:
        """按 id 改一条并落盘。

        为什么必须重新从磁盘读一份再改：find() 返回的是**副本**，
        改副本再 _save_all(磁盘行) 等于把旧值写回去——延长了却看不出效果。
        这个坑当场踩过：日志说 +3 天，重新读还是 7 天。
        """
        rows = self.entries(include_deleted=True)
        target = next((e for e in rows if e.entry_id == entry_id), None)
        if target is None:
            return None
        fn(target)
        self._save_all(rows)
        if log:
            logger.info(f"dl_senpai diary {log} id={target.entry_id}")
        return target

    def extend(self, entry_id: str, days: int | None = None) -> DiaryEntry | None:
        """延长保存天数。只有明确调用它才加天数——翻看不续命。"""
        holder: dict[str, int] = {}

        def _apply(e: DiaryEntry) -> None:
            add = int(days if days is not None else TIER_EXTEND_DAYS.get(e.tier, 3))
            holder["add"] = add
            # 在「当前总寿命」的基础上加，而不是在档位默认值上加：
            # 否则延长过两次的条目会被重新算回 7+3。
            e.life_days = e.total_life_days + add

        return self._mutate(entry_id, _apply, log="extended")

    def set_tier(self, entry_id: str, tier: str) -> DiaryEntry | None:
        """升降档。按新档位的初始寿命**重新算**，不在旧寿命上叠加。"""
        new_tier = normalize_tier(tier)

        def _apply(e: DiaryEntry) -> None:
            e.tier = new_tier
            e.life_days = TIER_DAYS.get(new_tier, 0) if new_tier != TIER_PARKED else 0

        return self._mutate(entry_id, _apply, log=f"tier->{new_tier}")

    def park(self, entry_id: str) -> DiaryEntry | None:
        """搁着：冻结计时，不扣寿命。躲着的事最不应该忘。"""
        def _apply(e: DiaryEntry) -> None:
            e.tier = TIER_PARKED

        return self._mutate(entry_id, _apply, log="parked")

    def unpark(self, entry_id: str, tier: str = TIER_IMPORTANT) -> DiaryEntry | None:
        """从搁着里拿出来：回到指定档位，寿命重新算。"""
        return self.set_tier(entry_id, tier)

    def remove(self, entry_id: str) -> DiaryEntry | None:
        """划掉：软删除进回收站，不真删数据。"""
        def _apply(e: DiaryEntry) -> None:
            e.deleted_at = time.time()

        return self._mutate(entry_id, _apply, log="removed")

    def restore(self, entry_id: str) -> DiaryEntry | None:
        def _apply(e: DiaryEntry) -> None:
            e.deleted_at = 0.0

        return self._mutate(entry_id, _apply, log="restored")

    def expire(self, now: float | None = None) -> list[DiaryEntry]:
        """把到期的一条条划进回收站，返回这次忘了什么。

        为什么先进回收站而不是直接删：万一哪天她自己想起来「我是不是
        记过这个」，还能从回收站翻出来核对。数据不删，只是不再注入。
        """
        moment = now if now is not None else time.time()
        rows = self.entries(include_deleted=True)
        expired = [e for e in rows if not e.is_deleted and e.days_left(moment) <= 0]
        if not expired:
            return []
        stamp = time.time()
        for e in expired:
            e.deleted_at = stamp
        self._save_all(rows)
        logger.info(f"dl_senpai diary expired count={len(expired)}")
        return expired

    def due_soon(self, now: float | None = None) -> list[DiaryEntry]:
        """快到期的日记（按档位分频提醒，避免每轮都念一遍）。

        提醒频率：普通 1 天一次、重要 3 天、刻骨铭心 7 天。
        只报事实（还剩几天），不写「你该看看了」这种引导——要不要留是她自己的事。
        """
        moment = now if now is not None else time.time()
        out: list[DiaryEntry] = []
        for e in self.entries():
            left = e.days_left(moment)
            warn_within = TIER_WARN_DAYS.get(e.tier, 2)
            if left <= 0 or left > warn_within:
                continue
            gap = {TIER_NORMAL: 1, TIER_IMPORTANT: 3, TIER_UNFORGETTABLE: 7}.get(e.tier, 1)
            if e.warned_at and (moment - e.warned_at) < gap * 86400:
                continue
            out.append(e)
        return out

    def mark_warned(self, entries: list[DiaryEntry], now: float | None = None) -> None:
        """记下「已经提醒过」。不落盘就会每轮都提醒同一批。"""
        if not entries:
            return
        moment = now if now is not None else time.time()
        ids = {e.entry_id for e in entries}
        rows = self.entries(include_deleted=True)
        for e in rows:
            if e.entry_id in ids:
                e.warned_at = moment
        self._save_all(rows)

    def recent(self, n: int = 3) -> list[DiaryEntry]:
        if n <= 0:
            return []
        return self.entries()[-n:]

    def context(self, n: int = 3, mood_word: str = "") -> str:
        """渲染成可以塞进提示词的一段。没有日记就返回空串，调用方跳过。

        只给**模糊视图**，不给正文（2026-10-25 改，抄自「大肥鱼优化篇2」）：
        人回忆旧事是带毛边的——「10月18日，心里有点沉，他好像说过失恋」，
        不会把整段原文念一遍。以前注入全文，结果是照着旧日记复述，
        以及旧人设的措辞被反复回灌。
        剩余寿命越少给得越糊：
            剩余 > 一半：心情 + 短标题（正文第一小句，截 16 字）
            两成 ~ 一半：只给心情
            不到两成：只剩日期
        """
        # 有心情词时多取一倍做候选：只在最近 2n 条里挑，避免把三个月前的事
        # 翻上来——那是回忆，不是走神。
        rows = self.recent(n * 2 if mood_word else n)
        if not rows:
            return ""
        # 心情滤镜（2026-10-26）：心情沉的时候先想起沉的旧事，高兴的时候先想起亮的那件。
        # 候选只放到 2n，避免把三个月前的事翻上来——那是回忆，不是走神。
        # 只改「先想起哪条」，条目总数和模糊程度都不变。
        mood_moved = 0
        mood_used = 0
        mood_polarity_value = 0
        if mood_word:
            try:
                from .mood_filter import mood_polarity as _mood_polarity

                mood_polarity_value = _mood_polarity(mood_word)
            except Exception:  # noqa: BLE001
                mood_polarity_value = 0
        if mood_word and len(rows) > 1:
            try:
                from .mood_filter import rank_by_mood

                def _mood_text(e: "DiaryEntry") -> str:
                    return f"{e.title} {e.mood} {e.text}"

                # recent() 是旧→新，滤镜要按「新的优先」做稳定排序
                ranked, mood_moved = rank_by_mood(
                    list(reversed(rows)), mood_word, text_of=_mood_text, keep=n
                )
                rows = list(reversed(ranked))
                mood_used = 1
            except Exception:  # noqa: BLE001 — 滤镜坏了就按原顺序
                rows = rows[-n:]
        else:
            rows = rows[-n:]
        if mood_word:
            # 打点：判断滤镜是不是假功能的唯一依据。
            # polarity=0 表示心情词不在词表里（滤镜静默失效）；
            # moved=0 且 used=1 表示跑了但排序没变（候选情绪都一样）。
            logger.info(
                f"dl_senpai diary mood_pick polarity={mood_polarity_value} "
                f"moved={mood_moved} used={mood_used} rows={len(rows)} n={n}"
            )
        lines = ["【你自己日记里还剩下的东西】（模糊印象，不是原话；别照抄，别提「日记」两个字）"]
        for e in rows:
            head = f"  {e.day}"
            if e.tier != TIER_NORMAL:
                head = f"  【{e.tier}】{e.day}"
            body = re.sub(r"\s+", " ", e.text).strip()
            ratio = e.life_ratio()
            if ratio > 0.5:
                if e.mood:
                    head += f" · {e.mood}"
                hint = body[:16] + ("…" if len(body) > 16 else "")
                if hint:
                    head += f"　{hint}"
            elif ratio > 0.2:
                if e.mood:
                    head += f" · {e.mood}"
            # 不到两成：什么都不补，只剩日期——快忘干净的东西不该给细节
            lines.append(head + f"　id={e.entry_id}")
        lines.append("（这些是你自己的事，别当成新发生的事讲给别人听，也别复述；想问细节就想，不想就别提）")
        return "\n".join(lines)

    def avoid_text(self, n: int = 3) -> str:
        """近期写过的标题/开头，用来明确告诉模型「这几件别再写了」。"""
        rows = self.recent(n)
        bits: list[str] = []
        for e in rows:
            label = e.title or re.sub(r"\s+", " ", e.text)[:20]
            if label:
                bits.append(label)
        return "；".join(bits)

    # -- 人读的那份 --
    def _rewrite_day_file(self, day: str) -> None:
        """重写当天 md。整天重写而不是追加：一天最多几条，成本可忽略，
        换来的是「同一天的多条永远按时间排好」，手工改过也不会错位。"""
        rows = [e for e in self.entries() if e.day == day]
        if not rows:
            return
        lines = [f"# {day}", ""]
        for e in rows:
            head = f"## {_clock_of(e.created_at)}"
            if e.title:
                head += f" {e.title}"
            if e.mood:
                head += f"（{e.mood}）"
            # 档位与编号写进 md 是为了**人工核对**：注入里给她的是 id，
            # 回收站里躺着的也是 id；md 上没有，就没法对上是哪一条被忘了。
            # 「普通」是默认档，不写出来，免得满屏 7 天档噪音。
            if e.tier and e.tier != TIER_NORMAL:
                head += f"　〔{e.tier}〕"
            head += f"　id={e.entry_id}"
            lines.append(head)
            if e.tags:
                lines.append("标签：" + " / ".join(e.tags))
            lines.append("")
            lines.append(e.text.strip())
            lines.append("")
        try:
            self.day_path(day).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except OSError as exc:
            # md 只是给人看的副本，写不进去不该让 JSONL 那条白写
            logger.warning(f"dl_senpai diary day file failed day={day}: {exc}")

    def pick_angle(self, *, window: int = 3) -> str:
        """挑一个近期没用过的角度；全用过了就随机。

        为什么不纯随机：九条角度随机撞车概率不低（三天里两次同一个角度
        就会显出「套模板」）。所以先排除最近 window 条用过的。
        """
        used = {e.angle for e in self.recent(window) if e.angle}
        fresh = [a for a in DIARY_ANGLES if a not in used]
        return random.choice(fresh or list(DIARY_ANGLES))


# ---------------------------------------------------------------- 生成


def parse_diary_reply(raw: str) -> dict[str, Any]:
    """从模型回复里抠出 JSON。带围栏、前后有废话、字段缺失都要能活。

    解析失败时**把整段当正文**而不是报错：她说的话本身是有价值的，
    格式错了顶多是少了标题。
    """
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```\s*$", "", text).strip()

    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            body = str(data.get("text") or data.get("body") or data.get("content") or "").strip()
            if body:
                tags_raw = data.get("tags") or []
                return {
                    "text": body,
                    "title": _clean_line(data.get("title"), max_len=12),
                    "mood": _clean_line(data.get("mood"), max_len=6),
                    # 档位由她自己标：不标就按「普通」7 天算。
                    # 为什么不自动判：什么算「刻骨铭心」是她的主观判断，
                    # 用关键词打分的做法最后只会把「哈哈」判成重要。
                    "tier": normalize_tier(data.get("tier")),
                    "tags": [
                        _clean_line(t, max_len=8)
                        for t in (tags_raw if isinstance(tags_raw, list) else [])
                        if _clean_line(t, max_len=8)
                    ][:4],
                }

    # 兜底：整段当正文，并且把 Markdown 结构去掉（人设里明确不要）
    return {
        "text": _MARKDOWN_RE.sub("", text).strip(),
        "title": "",
        "mood": "",
        "tags": [],
        "tier": TIER_NORMAL,
    }


def _stamp(now: datetime | None = None) -> str:
    d = now or datetime.now()
    return f"{d.year}-{d.month:02d}-{d.day:02d} {d.hour:02d}:{d.minute:02d}（{_WEEK[d.weekday()]}）"


async def compose_diary(
    llm: Any,
    *,
    role_text: str = "",
    memory_text: str = "",
    diary_text: str = "",
    avoid_text: str = "",
    hint: str = "",
    angle: str = "",
    max_chars: int = _DEFAULT_MAX_CHARS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """让她写一条日记（不落盘，交给调用方 add）。

    走 llm._complete_once 而不是 llm.chat：chat() 会带联网预取、few-shot、
    记忆召回、工具说明一整套对话设施，而写日记只需要「发消息、收文本」
    这一步。用它还能顺带绕开 chat() 里对 user 文本的各种预处理。
    """
    picked = angle or random.choice(DIARY_ANGLES)

    system = "\n".join(
        [
            "【你的角色】" + (role_text.strip()[:6000] if role_text.strip() else "你是一个住在别人电脑里的女孩子，有自己的脾气和小日子。"),
            "",
            "【现在要做的事】写一篇你自己的日记。",
            "硬规矩：",
            "1. 第一人称，像自己跟自己说话，口语，短句。",
            "2. 1~4 句就够，别写作文，别写总结句（「今天真是充实的一天」这种一律不要）。",
            "3. 不带 Markdown、不带标题符号、不列点。",
            "4. 写具体的事：谁说了什么、你干了什么、你当时什么心情。",
            "5. 提到人就用名字或叫法，别写「用户」「对方」「群友」。",
            "6. 时间就是现在，别自己编一个钟点（现在是白天就别写成半夜）。",
            "7. 给你的日记是**已经写过的**：不许重复那些事，也不许重复同样的标题。",
            "8. 给你一个今天的开口角度，就往这个方向写（别把「角度」两个字写进正文）：" + picked,
            "9. 不许写成汇报（「他问了我三个问题我都答了」这种不要）。"
            "宁可写一件小事、一个念头，也别写流水账式的工作总结。",
            "10. 还要标一个档位 tier，只许是这三个词之一：",
            "    - 普通：日常小事、随口聊的、过几天就淡了的。默认就是它。",
            "    - 重要：过几个月还想得起来的。他说过的心事、你们之间的约定、"
            "你自己难受过或高兴过的一天。",
            "    - 刻骨铭心：这辈子不想忘的。极少数，一年也不会有一条两条。",
            "    写的时候顺手标，别为了显得重情就往上抬——抬上去就收不回来了。",
            "11. 只输出一个 JSON，不要任何解释：",
            '{"title":"不超过12字的小标题","mood":"两三个字","tags":["标签1","标签2"],'
            '"tier":"普通","text":"日记正文"}',
        ]
    )

    parts = [f"现在是 {_stamp(now)}。"]
    if diary_text.strip():
        parts.append(diary_text.strip())
    if avoid_text.strip():
        parts.append("【这几件已经写过了，别重复】\n" + avoid_text.strip())
    if memory_text.strip():
        parts.append("【你记得的人和事】\n" + memory_text.strip())
    if hint.strip():
        parts.append("【这次想写的是】\n" + hint.strip())
    parts.append("有的写就写今天真发生的；没得写就写你自己的小事。现在写这一篇，只输出 JSON。")

    completion = await llm._complete_once(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(parts)},
        ],
        max_tokens=800,
        timeout=60.0,
    )
    # _complete_once 返 _Completion（正文在 .text）；单测替身可能直接给 str
    raw = getattr(completion, "text", None)
    if not isinstance(raw, str):
        raw = completion if isinstance(completion, str) else ""
    if not raw.strip():
        raise RuntimeError("模型没说话（可能被截断）")

    result = parse_diary_reply(raw)
    if not result.get("text"):
        raise RuntimeError("解析出来的日记正文是空的")
    if max_chars > 0 and len(result["text"]) > max_chars:
        result["text"] = result["text"][:max_chars].rstrip()
    result["angle"] = picked
    return result


async def write_diary(
    llm: Any,
    *,
    store: DiaryStore,
    role_text: str = "",
    memory_text: str = "",
    hint: str = "",
    context_n: int = 3,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> DiaryEntry | None:
    """生成 + 落盘一步到位。任何失败都只记日志、返 None。

    为什么静默吞异常：调用它的是后台循环 / 命令，写不出日记不该让整轮
    挂掉，更不该把异常抛回给正在聊天的用户。
    """
    try:
        data = await compose_diary(
            llm,
            role_text=role_text,
            memory_text=memory_text,
            diary_text=store.context(context_n),
            avoid_text=store.avoid_text(context_n),
            hint=hint,
            angle=store.pick_angle(),
            max_chars=max_chars,
        )
        return store.add(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai diary write failed: {type(exc).__name__}: {exc}")
        return None


def build_diary_brief(store: DiaryStore, n: int = 3, mood_word: str = "") -> str:
    """给提示词用的一段。n<=0 或没有日记时返回空串（调用方据此判断注不注入）。

    顺手做两件结算：到期的一条条划进回收站；快到期的列出来让她自己决定留不留。
    为什么挂在「读」上而不是定时任务：定时任务要靠别人记得开，而读是每轮都会
    发生的事；惰性结算漏不掉，也不需要额外进程。
    """
    # 日记关掉也照样让她知道自己在刷什么：影响渠道不该被日记开关连带关死
    def _influence() -> str:
        try:
            from .browse_influence import build_browse_brief
            from .config import get_config

            return build_browse_brief(get_config())
        except Exception:  # noqa: BLE001 — 这条挂不上也不许挡聊天
            return ""

    if n <= 0:
        return _influence()
    try:
        # 到期的先清掉，再渲染——否则这一轮还会把已经该忘的东西塞进去
        store.expire()
    except Exception as exc:  # noqa: BLE001 — 结算失败不能挡聊天
        logger.warning(f"dl_senpai diary expire failed: {type(exc).__name__}: {exc}")
    body = store.context(n, mood_word=mood_word)
    try:
        due = store.due_soon()
    except Exception:  # noqa: BLE001
        due = []
    if due:
        # 只报事实，不写「你该看看了」：留不留是她自己的决定
        extra = ["【日记快到期了】留还是不留，你自己定："]
        for e in due:
            extra.append(f"  {e.day} {e.tier} 还剩 {e.days_left()} 天　id={e.entry_id}")
        body = (body + "\n" + "\n".join(extra)).strip() if body else "\n".join(extra)
        try:
            store.mark_warned(due)
        except Exception:  # noqa: BLE001
            pass
    # 为什么挂在这里：diary_brief 是唯一一个「每轮都进 prompt、又不与并发改动
    # 冲突」的通道。刷到的东西跟着日记一起进去，她才真的会被看到的内容影响。
    influence = _influence()
    if influence:
        body = (body + chr(10) + influence).strip() if body else influence
    return body


_store: DiaryStore | None = None


def get_diary_store(directory: str | Path | None = None) -> DiaryStore:
    """取单例。传了目录就新建（测试/多实例用），不传走配置。"""
    global _store
    if directory is not None:
        return DiaryStore(directory)
    if _store is None:
        from .config import get_config

        cfg = get_config()
        _store = DiaryStore(getattr(cfg, "diary_dir", None) or _DEFAULT_DIR)
    return _store
