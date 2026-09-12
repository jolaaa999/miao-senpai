from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001
    TZ = timezone(timedelta(hours=8))

AffectionCmd = Literal["status", "leaderboard", "help", "none"]

# (门槛, 称号, 对学姐语气的提示)
AFFECTION_TIERS: list[tuple[int, str, str]] = [
    (0, "路人", "客气一点，像刚认识的群友。"),
    (15, "面熟", "稍微放松，偶尔损一句。"),
    (40, "常来", "自然亲近，记得对方常来群里。"),
    (80, "亲近", "可以更损更宠，像熟人互怼。"),
    (150, "信赖", "明显偏袒对方，嘴硬心软。"),
    (300, "心头好", "特别亲近，偶尔撒娇式傲娇。"),
    (450, "热恋", "像热恋期恋人：黏、宠、会吃醋也会哄，甜里带损但明显偏爱。"),
    (700, "眷侣", "像举案齐眉的夫妻：默契、安稳、彼此撑腰，嘴上拌嘴心里全是你。"),
]

# 各档位可选群专属头衔（升档或首次记录时按用户稳定抽取一个）
AFFECTION_TIER_TITLES: dict[str, tuple[str, ...]] = {
    "路人": ("过路人", "新面孔", "潜水员", "围观群众"),
    "面熟": ("面熟客", "常露面", "混脸熟", "老住户"),
    "常来": ("群常客", "老朋友", "熟面孔", "常来坐"),
    "亲近": ("自己人", "损友位", "熟络了", "嘴硬友"),
    "信赖": ("信赖方", "偏袒位", "嘴硬宠", "学姐向"),
    "心头好": ("心头好", "小骄傲", "偏爱位", "心尖宠"),
    "热恋": ("热恋中", "小甜心", "吃醋鬼", "黏人精"),
    "眷侣": ("眷侣档", "举案齐眉", "如胶似漆", "半边天"),
}


def all_affection_tier_titles() -> frozenset[str]:
    return frozenset(title for titles in AFFECTION_TIER_TITLES.values() for title in titles)


PRIVATE_SCOPE_PREFIX = "private_"


def affection_scope_private(user_id: int | str) -> str:
    return f"{PRIVATE_SCOPE_PREFIX}{user_id}"


def is_private_affection_scope(scope: str | int) -> bool:
    return str(scope).startswith(PRIVATE_SCOPE_PREFIX)


@dataclass
class UserAffection:
    user_id: str
    display_name: str = ""
    value: int = 0
    tier_title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, user_id: str, data: dict[str, Any]) -> UserAffection:
        return cls(
            user_id=str(user_id),
            display_name=str(data.get("display_name") or ""),
            value=max(0, int(data.get("value") or 0)),
            tier_title=str(data.get("tier_title") or ""),
        )


@dataclass
class AffectionGainResult:
    gained: int = 0
    total: int = 0
    tier_name: str = ""
    tier_upgraded: bool = False
    title_changed: bool = False
    user: UserAffection | None = None


def tier_for_value(value: int) -> tuple[int, str, str]:
    current = AFFECTION_TIERS[0]
    for threshold, name, hint in AFFECTION_TIERS:
        if value >= threshold:
            current = (threshold, name, hint)
        else:
            break
    return current


def tier_index_for_value(value: int) -> int:
    idx = 0
    for i, (threshold, _name, _hint) in enumerate(AFFECTION_TIERS):
        if value >= threshold:
            idx = i
        else:
            break
    return idx


def next_tier_for_value(value: int) -> tuple[int, str] | None:
    for threshold, name, _hint in AFFECTION_TIERS:
        if value < threshold:
            return threshold, name
    return None


def pick_tier_title(
    tier_name: str,
    *,
    user_id: str | int,
    group_id: str | int,
) -> str:
    options = AFFECTION_TIER_TITLES.get(tier_name, ())
    if not options:
        return ""
    seed = f"{group_id}:{user_id}:{tier_name}".encode()
    idx = int(hashlib.md5(seed).hexdigest(), 16) % len(options)
    return options[idx]


def _refresh_user_tier_title(
    user: UserAffection,
    *,
    group_id: int | str,
    old_tier: str,
    new_tier: str,
) -> bool:
    if new_tier != old_tier or not user.tier_title:
        title = pick_tier_title(
            new_tier,
            user_id=user.user_id,
            group_id=group_id,
        )
        if title and title != user.tier_title:
            user.tier_title = title
            return True
    return False


def format_affection_line(value: int) -> str:
    _threshold, name, _hint = tier_for_value(value)
    nxt = next_tier_for_value(value)
    if nxt is None:
        return f"好感 {value} · 「{name}」（已满级）"
    need = nxt[0] - value
    return f"好感 {value} · 「{name}」· 距「{nxt[1]}」还差 {need}"


def build_affection_brief(*, name: str, value: int) -> str:
    _threshold, tier_name, hint = tier_for_value(value)
    nxt = next_tier_for_value(value)
    progress = format_affection_line(value)
    lines = [
        f"【与当前说话者的好感度】{name}：{progress}",
        f"关系档位「{tier_name}」——{hint}",
    ]
    if nxt is not None:
        lines.append(f"再涨 {nxt[0] - value} 点升到「{nxt[1]}」。")
    lines.append("语气随档位调整，但仍保持学姐傲娇，别突然变客服。")
    return "\n".join(lines)


class AffectionStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _path(self, group_id: int | str) -> Path:
        return self.data_dir / f"group_{group_id}.json"

    def load_group(self, group_id: int | str) -> dict[str, UserAffection]:
        path = self._path(group_id)
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        users_raw = raw.get("users") if isinstance(raw, dict) else None
        if not isinstance(users_raw, dict):
            return {}
        out: dict[str, UserAffection] = {}
        for uid, row in users_raw.items():
            if isinstance(row, dict):
                out[str(uid)] = UserAffection.from_dict(str(uid), row)
        return out

    def save_group(self, group_id: int | str, users: dict[str, UserAffection]) -> None:
        path = self._path(group_id)
        payload = {
            "users": {uid: u.to_dict() for uid, u in users.items()},
            "updated_at": datetime.now(TZ).isoformat(),
        }
        with self._lock:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def get_user(self, group_id: int | str, user_id: int | str) -> UserAffection:
        users = self.load_group(group_id)
        uid = str(user_id)
        return users.get(uid) or UserAffection(user_id=uid)

    def add(
        self,
        group_id: int | str,
        user_id: int | str,
        amount: int,
        *,
        display_name: str = "",
        now: datetime | None = None,
    ) -> AffectionGainResult:
        if amount <= 0:
            user = self.get_user(group_id, user_id)
            _t, tier_name, _ = tier_for_value(user.value)
            return AffectionGainResult(gained=0, total=user.value, tier_name=tier_name, user=user)

        now = now or datetime.now(TZ)
        users = self.load_group(group_id)
        uid = str(user_id)
        user = users.get(uid) or UserAffection(user_id=uid)
        if display_name:
            user.display_name = display_name

        old_tier = tier_for_value(user.value)[1]
        user.value += amount
        new_tier = tier_for_value(user.value)[1]
        tier_upgraded = new_tier != old_tier
        title_changed = _refresh_user_tier_title(
            user,
            group_id=group_id,
            old_tier=old_tier,
            new_tier=new_tier,
        )
        users[uid] = user
        self.save_group(group_id, users)
        return AffectionGainResult(
            gained=amount,
            total=user.value,
            tier_name=new_tier,
            tier_upgraded=tier_upgraded,
            title_changed=title_changed,
            user=user,
        )

    def add_chat(
        self,
        group_id: int | str,
        user_id: int | str,
        amount: int,
        *,
        display_name: str = "",
    ) -> AffectionGainResult:
        return self.add(
            group_id,
            user_id,
            amount,
            display_name=display_name,
        )

    def leaderboard(self, group_id: int | str, *, limit: int = 10) -> list[UserAffection]:
        users = list(self.load_group(group_id).values())
        users.sort(key=lambda u: (u.value, u.display_name), reverse=True)
        return users[: max(1, limit)]


_CMD_PATTERNS: list[tuple[AffectionCmd, re.Pattern[str]]] = [
    ("help", re.compile(r"^(好感帮助|好感说明)$")),
    ("status", re.compile(r"^(好感度|我的好感|好感查询)$")),
    ("leaderboard", re.compile(r"^(好感排行|好感榜)$")),
]


def parse_affection_command(text: str) -> AffectionCmd:
    t = re.sub(r"\s+", "", (text or "").strip())
    if not t:
        return "none"
    for cmd, pat in _CMD_PATTERNS:
        if pat.match(t):
            return cmd
    return "none"


_RULE = "────────"


def format_status(user: UserAffection, *, name: str, private_mode: bool = False) -> str:
    _t, tier_name, _ = tier_for_value(user.value)
    lines = [
        "💕 好感档案",
        name,
        _RULE,
        format_affection_line(user.value),
    ]
    if user.tier_title:
        if private_mode:
            lines.append(f"专属称谓：「{user.tier_title}」（{tier_name}档）")
        else:
            lines.append(f"群头衔：「{user.tier_title}」（{tier_name}档，群成员资料可见）")
    return "\n".join(lines)


def format_leaderboard(users: list[UserAffection]) -> str:
    if not users:
        return "💕 好感排行榜还是空的\n多 @学姐 聊聊天、签到、买衣服都能涨哦～"
    lines = ["💕 好感排行榜", _RULE]
    medals = ("🥇", "🥈", "🥉")
    for i, u in enumerate(users, start=1):
        nick = u.display_name or u.user_id
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        _t, tier_name, _ = tier_for_value(u.value)
        lines.append(f"{prefix} {nick}")
        lines.append(f"   {u.value} 点 · 「{tier_name}」")
    return "\n".join(lines)


def format_help(*, private_mode: bool = False) -> str:
    if private_mode:
        return (
            "💕 学姐好感度（私聊）\n"
            f"{_RULE}\n"
            "怎么涨\n"
            "· 和学姐私聊 —— 每次聊天都会涨\n"
            "\n"
            "查看\n"
            "· 好感度 —— 我的档位与进度\n"
            "\n"
            "档位从「路人」一路升到「眷侣」，学姐对你语气会越来越亲近～\n"
            "私聊好感是你和学姐专属的，和群里分开算哦。"
        )
    return (
        "💕 学姐好感度\n"
        f"{_RULE}\n"
        "怎么涨\n"
        "· 签到 / 完成任务 —— 稳定加分\n"
        "· @学姐 聊天 —— 每次找学姐都会涨\n"
        "· 商店购买 / 换上衣服 —— 给学姐换装也会涨\n"
        "· 私聊学姐 —— 也会涨（私聊好感单独计算）\n"
        "\n"
        "查看\n"
        "· 好感度 —— 我的档位与进度\n"
        "· 好感排行 —— 看看谁最讨学姐喜欢\n"
        "\n"
        "档位从「路人」一路升到「眷侣」，学姐对你语气会越来越亲近～\n"
        "升档时会从该档头衔池里抽一个群专属头衔帮你挂上（需机器人是群管）。"
    )


def format_gain_footnote(result: AffectionGainResult, *, always_show: bool = True) -> str:
    if result.gained <= 0:
        return ""
    if not always_show and not result.tier_upgraded:
        return ""
    lines = [f"💕 好感 +{result.gained}（当前 {result.total} · 「{result.tier_name}」）"]
    if result.tier_upgraded:
        lines.append(f"🎀 关系升级！现在是「{result.tier_name}」啦～")
    return "\n".join(lines)


_store: AffectionStore | None = None


def get_affection_store(data_dir: str | Path | None = None) -> AffectionStore:
    global _store
    if data_dir is not None:
        return AffectionStore(data_dir)
    if _store is None:
        from .config import get_config

        _store = AffectionStore(get_config().affection_dir)
    return _store


def reset_affection_store() -> None:
    global _store
    _store = None
